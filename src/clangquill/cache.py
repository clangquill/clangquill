"""The incremental-build cache: parse-skip and per-output render tracking.

clangquill caches two things between runs, both under the configured
``clangquill_cache_dir``:

* the SQLite IR (``clangquill.sqlite``), so an unchanged build can reuse the
  previous parse instead of invoking libclang again; and
* a small bookkeeping database (:data:`BuildCache.FILENAME`) recording the
  fingerprint of the inputs that produced that IR and the content hash of every
  output page that was written.

The bookkeeping database is deliberately separate from the IR: the IR is
rebuilt wholesale on every parse, so anything that must survive a re-parse
(the previous run's output hashes) cannot live inside it.

This module is pure Python on top of :mod:`sqlite3` and :mod:`hashlib`; the
heavy lifting (parsing, content hashing of symbols) happens elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator, Mapping

# Bump when the cache schema/semantics below change incompatibly; a mismatch
# transparently discards the old cache and forces a full rebuild.
#
# v2 added the ``(mtime_ns, size_bytes)`` fast-path columns on ``inputs``; v3
# adds the ``tu_inputs`` table so the cache can attribute each tracked file to
# the input(s) that #include it, enabling per-TU incremental re-parses; v4 adds
# the ``page_cache`` table that memoises rendered page text by a dependency key,
# so an incremental build re-renders only the pages whose symbols changed; v5
# adds the ``(mtime_ns, size_bytes)`` fast-path columns on ``outputs`` so a
# build can cheaply verify the pages it wrote are still intact on disk (and
# restore any that were deleted or hand-edited).
CACHE_VERSION = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Every file the last parse touched (the inputs plus their transitive
-- #include dependencies), with the content hash it had at parse time and the
-- (mtime_ns, size_bytes) metadata used as a fast-path to skip re-hashing a file
-- whose stat() is unchanged. The metadata columns are nullable: a file that
-- could not be stat'd at record time stores NULL and always takes the hash path.
CREATE TABLE IF NOT EXISTS inputs (
  path       TEXT PRIMARY KEY,
  sha256     TEXT NOT NULL,
  mtime_ns   INTEGER,
  size_bytes INTEGER
);

-- Which input translation unit each tracked dependency belongs to: one row per
-- (input, dependency) edge. A dependency #included by several inputs has several
-- rows. When a dependency changes, the inputs joined to it here are the exact
-- translation units that must be re-parsed.
CREATE TABLE IF NOT EXISTS tu_inputs (
  input_path TEXT NOT NULL,
  dep_path   TEXT NOT NULL,
  PRIMARY KEY (input_path, dep_path)
);
CREATE INDEX IF NOT EXISTS idx_tu_inputs_dep ON tu_inputs(dep_path);

-- Every page the last render wrote, with the hash of its rendered content and
-- the (mtime_ns, size_bytes) the file had after writing. The metadata is the
-- fast-path used to verify an output is still intact on disk without re-reading
-- it (mirroring the ``inputs`` fast-path); NULL metadata always takes the hash
-- path.
CREATE TABLE IF NOT EXISTS outputs (
  output_path  TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL,
  mtime_ns     INTEGER,
  size_bytes   INTEGER
);

-- Memoised render output: the rendered text of every page from the last render,
-- keyed by a hash of everything that page reads (its symbols' content hashes,
-- their cross-references, plus the render fingerprint). On an incremental build
-- a page whose key is unchanged replays its text from here instead of running
-- the Jinja pass again, so render work scales with the change, not the project.
CREATE TABLE IF NOT EXISTS page_cache (
  page_stem TEXT PRIMARY KEY,
  key_hash  TEXT NOT NULL,
  text      TEXT NOT NULL
);
"""

# Meta key holding the fingerprint of the parse-affecting configuration (compile
# args, std, defines, the resolved input set, toolchain version). When this
# changes the cached IR is stale regardless of file contents.
_PARSE_FINGERPRINT = "parse_fingerprint"

# Meta keys backing the noop-render skip: the fingerprint of everything that
# shapes the rendered output *given an unchanged IR* (render config plus any
# override template contents), and a small JSON summary of the last render so a
# fully unchanged build can return its counts/pages without rendering at all.
_RENDER_FINGERPRINT = "render_fingerprint"
_RENDER_SUMMARY = "render_summary"

# Slack subtracted from the restat threshold in :meth:`BuildCache._fast_path_mtime`
# to absorb coarse filesystem timestamp granularity. A write that lands after the
# parse began can be *recorded* with an mtime truncated down to before it — 2s on
# FAT/exFAT, 1s on ext3 and many NFS/SMB mounts — which would make the guard trust
# a stat describing content the stored hash never saw. make and ninja carry the
# same epsilon for the same reason. 2s covers the coarsest common case; erring
# large only costs a redundant hash of files touched just before the parse.
_MTIME_GRANULARITY_NS = 2_000_000_000


def file_sha256(path: str | Path) -> str:
    """Return the hex SHA-256 of ``path``'s bytes, matching the C++ digest.

    Raises :class:`OSError` if the file cannot be read; callers treat that as a
    changed/absent dependency.
    """
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_text(text: str) -> str:
    """Return the hex SHA-256 of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_bytes(data: bytes) -> str:
    """Return the hex SHA-256 of ``data``.

    What :func:`file_sha256` computes, for a caller that has already read the
    file for another reason and should not read it a second time.
    """
    return hashlib.sha256(data).hexdigest()


def fingerprint(payload: Mapping[str, object]) -> str:
    """Hash a JSON-able mapping into a stable fingerprint string.

    Keys are sorted so the digest is independent of insertion order.
    """
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hash_text(blob)


@dataclass(frozen=True)
class OutputRecord:
    """One row of the ``outputs`` table: what a written page looked like.

    ``content_hash`` is the SHA-256 of the rendered text; ``mtime_ns`` and
    ``size_bytes`` are the file's stat after writing, used as a fast-path to
    verify the page is still intact on disk without re-reading it. ``None``
    metadata (a file that could not be stat'd) always takes the hash path.
    """

    content_hash: str
    mtime_ns: int | None = None
    size_bytes: int | None = None


@dataclass(frozen=True)
class ParseStatus:
    """How much of the cached parse a rebuild can reuse.

    Exactly one of three shapes:

    * ``current=True`` — every tracked file is unchanged; reuse the IR as-is.
    * ``current=False, stale_inputs=None`` — the parse configuration changed (or
      no usable per-TU map exists); the whole module must be re-parsed.
    * ``current=False, stale_inputs={...}`` — only those inputs' dependency sets
      changed; re-parse just those translation units into the existing IR.
    """

    current: bool
    #: Inputs whose dependencies changed (``None`` means "re-parse everything").
    stale_inputs: frozenset[str] | None = None


class BuildCache:
    """Read/write bookkeeping for incremental builds under a cache directory."""

    #: Filename of the bookkeeping database within the cache directory.
    FILENAME = "clangquill-cache.sqlite"

    def __init__(self, connection: sqlite3.Connection) -> None:
        """Wrap an open connection (prefer :meth:`open`)."""
        self._con = connection
        self._con.row_factory = sqlite3.Row

    @classmethod
    @contextmanager
    def open(cls, cache_dir: str | Path) -> Iterator[BuildCache]:
        """Open (creating if needed) the cache under ``cache_dir`` and yield it.

        A cache written by an incompatible :data:`CACHE_VERSION` is reset so a
        stale layout never produces wrong incremental decisions.
        """
        directory = Path(cache_dir)
        directory.mkdir(parents=True, exist_ok=True)
        cache_file = directory / cls.FILENAME
        try:
            con = sqlite3.connect(cache_file)
            con.executescript(_SCHEMA)
        except sqlite3.DatabaseError:
            # A corrupted cache (interrupted write, bad disk) must not wedge every
            # future build: discard it and start clean rather than propagate.
            with suppress(sqlite3.Error):
                con.close()
            cache_file.unlink(missing_ok=True)
            con = sqlite3.connect(cache_file)
            con.executescript(_SCHEMA)
        try:
            cache = cls(con)
            if cache._meta("cache_version") != str(CACHE_VERSION):
                cache.reset()
                cache._set_meta("cache_version", str(CACHE_VERSION))
                con.commit()
            yield cache
        finally:
            con.close()

    # -- meta -----------------------------------------------------------------

    def _meta(self, key: str) -> str | None:
        row = self._con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._con.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def reset(self) -> None:
        """Forget everything and rebuild the schema (used on a version mismatch).

        Tables are dropped and recreated rather than merely emptied: a cache
        written by an older :data:`CACHE_VERSION` may have a different column
        layout (``CREATE TABLE IF NOT EXISTS`` never adds new columns), so only
        deleting rows would leave the stale layout in place and make the first
        upgraded write fail. Dropping forces the current :data:`_SCHEMA`.
        """
        self._con.executescript(
            "DROP TABLE IF EXISTS meta;DROP TABLE IF EXISTS inputs;"
            "DROP TABLE IF EXISTS tu_inputs;DROP TABLE IF EXISTS outputs;"
            "DROP TABLE IF EXISTS page_cache;",
        )
        self._con.executescript(_SCHEMA)

    # -- parse bookkeeping ----------------------------------------------------

    @property
    def parse_fingerprint(self) -> str | None:
        """The configuration fingerprint of the cached parse, or ``None``."""
        return self._meta(_PARSE_FINGERPRINT)

    def tracked_files(self) -> dict[str, str]:
        """Return ``{path: sha256}`` for every file the cached parse touched."""
        return {row["path"]: row["sha256"] for row in self._con.execute("SELECT path, sha256 FROM inputs")}

    def _scan_inputs(self) -> set[str]:
        """Return tracked files whose content no longer matches the cache.

        Each file is checked with the ``(st_mtime_ns, st_size)`` fast-path the
        same way ``make`` does: a file whose metadata is unchanged from parse
        time is assumed identical and its SHA-256 read is skipped. The hash is
        still computed for any file whose metadata moved, so a touched-but-
        identical file is correctly recognised as unchanged. When the hash
        confirms such a file is unchanged, its stored metadata is healed to the
        current ``stat`` so a mere ``touch`` is paid for once rather than
        re-hashed on every later build.
        """
        changed: set[str] = set()
        refreshed: list[tuple[int, int, str]] = []
        rows = self._con.execute("SELECT path, sha256, mtime_ns, size_bytes FROM inputs").fetchall()
        for row in rows:
            path = row["path"]
            try:
                stat = Path(path).stat()
            except OSError:
                changed.add(path)
                continue
            if (
                row["mtime_ns"] is not None
                and row["size_bytes"] is not None
                and stat.st_mtime_ns == row["mtime_ns"]
                and stat.st_size == row["size_bytes"]
            ):
                continue  # metadata unchanged — skip the hash read
            try:
                if file_sha256(path) != row["sha256"]:
                    changed.add(path)
                    continue
            except OSError:
                changed.add(path)
                continue
            # Same content, moved metadata: heal the fast-path for next time.
            refreshed.append((stat.st_mtime_ns, stat.st_size, path))
        if refreshed:
            self._con.executemany(
                "UPDATE inputs SET mtime_ns = ?, size_bytes = ? WHERE path = ?",
                refreshed,
            )
            self._con.commit()
        return changed

    def parse_is_current(self, parse_fingerprint: str) -> bool:
        """Whether the cached parse still matches the inputs.

        ``True`` only when the configuration fingerprint is unchanged *and*
        every tracked file still exists with the same content. Any added,
        removed, or edited dependency makes the cached IR stale. Change detection
        uses the ``(mtime_ns, size_bytes)`` fast-path described on
        :meth:`_scan_inputs`.
        """
        if self.parse_fingerprint != parse_fingerprint:
            return False
        if not self.tracked_files():
            return False
        return not self._scan_inputs()

    def tu_inputs(self) -> dict[str, set[str]]:
        """Return ``{dep_path: {input_path, ...}}`` from the cached parse."""
        mapping: dict[str, set[str]] = {}
        for row in self._con.execute("SELECT input_path, dep_path FROM tu_inputs"):
            mapping.setdefault(row["dep_path"], set()).add(row["input_path"])
        return mapping

    def deps_only_from(self, inputs: Iterable[str]) -> list[str]:
        """Return tracked files the cached parse attributed *only* to ``inputs``.

        Re-parsing those inputs is the only chance to notice that one of these
        files has left the build: no other translation unit contributes it, so
        if the fresh parse no longer reaches it, nothing ever will and its IR
        rows can go. A dependency any surviving unit also pulls in is never
        returned, so the caller can delete the whole list unconditionally.
        """
        owners = set(inputs)
        return sorted(dep for dep, tus in self.tu_inputs().items() if tus and tus <= owners)

    def parse_status(self, parse_fingerprint: str) -> ParseStatus:
        """Classify how much of the cached parse a rebuild can reuse.

        See :class:`ParseStatus`. The configuration fingerprint already covers
        the resolved input set and compile args, so when it matches we know the
        inputs are the same and only file *contents* can differ — which lets us
        narrow the rebuild to the translation units that actually changed.
        """
        if self.parse_fingerprint != parse_fingerprint:
            return ParseStatus(current=False, stale_inputs=None)
        if not self.tracked_files():
            return ParseStatus(current=False, stale_inputs=None)
        changed = self._scan_inputs()
        if not changed:
            return ParseStatus(current=True)
        tu_map = self.tu_inputs()
        if not tu_map:
            # A parse recorded without per-TU attribution: fall back to a full
            # rebuild rather than guess which inputs are affected.
            return ParseStatus(current=False, stale_inputs=None)
        stale: set[str] = set()
        for dep in changed:
            inputs_for_dep = tu_map.get(dep)
            if inputs_for_dep is None:
                # A changed file we cannot attribute to any input (should not
                # happen while the table is consistent): be safe, rebuild all.
                return ParseStatus(current=False, stale_inputs=None)
            stale |= inputs_for_dep
        return ParseStatus(current=False, stale_inputs=frozenset(stale))

    def _write_tu_inputs(self, tu_deps: Mapping[str, Iterable[str]]) -> None:
        """Replace the ``tu_inputs`` rows for each input in ``tu_deps``."""
        for input_path, deps in tu_deps.items():
            self._con.execute("DELETE FROM tu_inputs WHERE input_path = ?", (input_path,))
            self._con.executemany(
                "INSERT OR REPLACE INTO tu_inputs(input_path, dep_path) VALUES(?, ?)",
                [(input_path, dep) for dep in deps],
            )

    def record_parse(
        self,
        parse_fingerprint: str,
        files: Mapping[str, tuple[str, int]],
        tu_deps: Mapping[str, Iterable[str]] | None = None,
        *,
        parse_started_ns: int | None = None,
    ) -> None:
        """Persist the fingerprint, per-file snapshot and per-TU map of a parse.

        ``files`` maps each path to its ``(sha256, size_bytes)`` parse snapshot
        (the same observation the hash was computed from, so size cannot drift
        from the stored hash). Each path is additionally ``stat``'d here for its
        ``mtime_ns`` to complete the ``(mtime_ns, size_bytes)`` fast-path in
        :meth:`parse_is_current`; a path that cannot be stat'd records ``NULL``
        mtime and always falls back to the hash comparison.

        ``parse_started_ns`` is when the parse that produced ``files`` began;
        see :meth:`_fast_path_mtime` for why it is needed.

        ``tu_deps`` maps each input to the files its translation unit pulled in;
        without it the per-TU map is left empty and future rebuilds fall back to
        a full re-parse on any change.
        """
        self._set_meta(_PARSE_FINGERPRINT, parse_fingerprint)
        self._con.execute("DELETE FROM inputs")
        self._con.executemany(
            "INSERT OR REPLACE INTO inputs(path, sha256, mtime_ns, size_bytes) VALUES(?, ?, ?, ?)",
            [(path, sha, self._fast_path_mtime(path, parse_started_ns), size) for path, (sha, size) in files.items()],
        )
        self._con.execute("DELETE FROM tu_inputs")
        if tu_deps:
            self._write_tu_inputs(tu_deps)
        # Advancing the parse invalidates the previous render in the same
        # transaction, so the noop shortcut can never trust a stale render.
        self._clear_render_meta()
        self._con.commit()

    def record_partial_parse(
        self,
        tu_deps: Mapping[str, Iterable[str]],
        files: Mapping[str, tuple[str, int]],
        *,
        parse_started_ns: int | None = None,
    ) -> None:
        """Update the cache after re-parsing only some translation units.

        ``tu_deps`` is the fresh dependency set of each re-parsed input and
        ``files`` carries the up-to-date ``(sha256, size_bytes)`` snapshot for
        every file in the IR. The ``inputs`` table is rebuilt from the files
        still referenced by any translation unit, so a dependency no input
        includes anymore is dropped. The parse fingerprint is unchanged (the
        input set/config did not move — only file contents did).

        ``parse_started_ns`` is when the re-parse began; see
        :meth:`_fast_path_mtime`.
        """
        self._write_tu_inputs(tu_deps)
        referenced = {row["dep_path"] for row in self._con.execute("SELECT dep_path FROM tu_inputs")}
        self._con.execute("DELETE FROM inputs")
        self._con.executemany(
            "INSERT OR REPLACE INTO inputs(path, sha256, mtime_ns, size_bytes) VALUES(?, ?, ?, ?)",
            [
                (path, sha, self._fast_path_mtime(path, parse_started_ns), size)
                for path, (sha, size) in files.items()
                if path in referenced
            ],
        )
        # Advancing the parse invalidates the previous render in the same
        # transaction, so the noop shortcut can never trust a stale render.
        self._clear_render_meta()
        self._con.commit()

    # -- render bookkeeping ---------------------------------------------------

    @property
    def render_fingerprint(self) -> str | None:
        """The fingerprint of the render config/templates that last rendered."""
        return self._meta(_RENDER_FINGERPRINT)

    def render_summary(self) -> dict[str, object] | None:
        """Return the cached summary of the last render, or ``None``.

        The summary carries the symbol/reference/file counts and the ordered
        page stems so a noop build can reproduce its :class:`BuildResult` without
        opening the store or running Jinja.
        """
        raw = self._meta(_RENDER_SUMMARY)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def render_is_current(self, render_fingerprint: str) -> bool:
        """Whether the last render still applies to ``render_fingerprint``.

        ``True`` only when the fingerprint matches *and* a summary was stored, so
        a cache predating this optimisation never short-circuits a render.
        Callers must separately confirm the IR itself is unchanged (a cache-hit
        parse) before trusting this — the fingerprint deliberately omits the IR.
        """
        return self.render_fingerprint == render_fingerprint and self.render_summary() is not None

    def record_render(self, render_fingerprint: str, summary: Mapping[str, object]) -> None:
        """Persist the render fingerprint and summary of a fresh render."""
        self._set_meta(_RENDER_FINGERPRINT, render_fingerprint)
        self._set_meta(_RENDER_SUMMARY, json.dumps(dict(summary)))
        self._con.commit()

    def _clear_render_meta(self) -> None:
        """Drop the noop-render bookkeeping (the caller's transaction commits it).

        Folded into the parse-recording transaction so the parse pointer and the
        render invalidation advance atomically: a crash can never leave a current
        parse paired with a stale render fingerprint/summary that the noop
        shortcut would wrongly trust against the new IR. A successful render
        re-records the bookkeeping via :meth:`record_render`.
        """
        self._con.execute("DELETE FROM meta WHERE key IN (?, ?)", (_RENDER_FINGERPRINT, _RENDER_SUMMARY))

    @staticmethod
    def _mtime_ns(path: str) -> int | None:
        """Return ``st_mtime_ns`` for ``path``, or ``None`` if it cannot be stat'd."""
        try:
            return Path(path).stat().st_mtime_ns
        except OSError:
            return None

    @classmethod
    def _fast_path_mtime(cls, path: str, parse_started_ns: int | None) -> int | None:
        """Return the mtime to store for ``path``, or ``None`` to force re-hashing.

        The hash paired with this stat was computed by the parser, which may
        have read the file minutes ago on a large project. If the file has been
        written since the parse began, this ``stat`` describes content the
        stored hash never saw — and the next build's fast path would see
        matching metadata, skip the hash, and miss the edit forever (a same-size
        edit, common in C++ headers, defeats the size check too).

        So the ``restat`` guard make and ninja use: anything touched at or after
        the parse started records ``NULL`` metadata and is re-hashed next build.

        The threshold is relaxed by :data:`_MTIME_GRANULARITY_NS` because a raw
        comparison trusts the stat in the unsafe direction. On a filesystem with
        coarse timestamps (FAT/exFAT, ext3, some NFS/SMB mounts) a write landing
        *after* the parse began is recorded with an mtime truncated *down* to
        before it, so the guard would pair a post-edit stat with a pre-edit hash
        and every later build's fast path would skip the file forever. Clock skew
        in the other direction (a network mount running ahead) errs safely: the
        cost is a redundant hash, never a missed change.
        """
        mtime = cls._mtime_ns(path)
        if mtime is None or parse_started_ns is None:
            return mtime
        return None if mtime >= parse_started_ns - _MTIME_GRANULARITY_NS else mtime

    # -- output bookkeeping ---------------------------------------------------

    def outputs(self) -> dict[str, OutputRecord]:
        """Return ``{output_path: OutputRecord}`` from the last render."""
        return {
            row["output_path"]: OutputRecord(row["content_hash"], row["mtime_ns"], row["size_bytes"])
            for row in self._con.execute("SELECT output_path, content_hash, mtime_ns, size_bytes FROM outputs")
        }

    def record_outputs(self, outputs: Mapping[str, OutputRecord]) -> None:
        """Replace the stored output index with ``{output_path: OutputRecord}``."""
        self._con.execute("DELETE FROM outputs")
        self._con.executemany(
            "INSERT OR REPLACE INTO outputs(output_path, content_hash, mtime_ns, size_bytes) VALUES(?, ?, ?, ?)",
            [(name, rec.content_hash, rec.mtime_ns, rec.size_bytes) for name, rec in outputs.items()],
        )
        self._con.commit()

    def refresh_output_stats(self, stats: Mapping[str, tuple[int, int]]) -> None:
        """Heal the ``(mtime_ns, size_bytes)`` fast-path of touched-but-identical outputs.

        Mirrors the healing :meth:`_scan_inputs` does for inputs: a page whose
        stat moved but whose content still matches pays the hash read once, not
        on every later build.
        """
        self._con.executemany(
            "UPDATE outputs SET mtime_ns = ?, size_bytes = ? WHERE output_path = ?",
            [(mtime_ns, size, name) for name, (mtime_ns, size) in stats.items()],
        )
        self._con.commit()

    # -- per-page render memoisation ------------------------------------------

    def cached_page(self, page_stem: str, key_hash: str) -> str | None:
        """Return the memoised text for ``page_stem`` if its key still matches.

        ``None`` means a miss — the page is new, was rendered under a different
        key (its symbols or the render fingerprint changed), or was never cached —
        and the caller must render it afresh.
        """
        row = self._con.execute(
            "SELECT text FROM page_cache WHERE page_stem = ? AND key_hash = ?",
            (page_stem, key_hash),
        ).fetchone()
        return row["text"] if row is not None else None

    def record_pages(self, pages: Mapping[str, tuple[str, str]], *, stems: Collection[str] | None = None) -> None:
        """Upsert ``{page_stem: (key_hash, text)}``, pruning pages ``stems`` omits.

        Only the pages handed in are written. An incremental build re-renders a
        handful of pages and replays the rest from this table, and a replayed
        page's row already holds the key and text it was replayed under — so
        rewriting the whole corpus would spend an O(project) write (every page's
        full text) on an O(change) render.

        ``stems`` names every page the render produced, including the ones it
        replayed. Rows outside it are deleted, so a page whose symbol vanished
        still drops out. Omit it and ``pages`` is taken to be the whole page
        set, which is what a build that rendered everything hands over.
        """
        keep = set(pages) if stems is None else set(stems)
        # Read the stems alone rather than deleting by a NOT IN list: the row
        # count is the page count, the text never leaves SQLite, and the delete
        # that follows is normally empty.
        stale = [
            (row["page_stem"],)
            for row in self._con.execute("SELECT page_stem FROM page_cache")
            if row["page_stem"] not in keep
        ]
        self._con.executemany("DELETE FROM page_cache WHERE page_stem = ?", stale)
        self._con.executemany(
            "INSERT OR REPLACE INTO page_cache(page_stem, key_hash, text) VALUES(?, ?, ?)",
            [(stem, key_hash, text) for stem, (key_hash, text) in pages.items()],
        )
        self._con.commit()


__all__ = [
    "CACHE_VERSION",
    "BuildCache",
    "OutputRecord",
    "ParseStatus",
    "file_sha256",
    "fingerprint",
    "hash_bytes",
    "hash_text",
]

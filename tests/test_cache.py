"""Unit tests for the incremental-build cache bookkeeping (pure Python)."""

from __future__ import annotations

import os
import sqlite3
from typing import TYPE_CHECKING

import clangquill.cache as cache_module
from clangquill.cache import (
    CACHE_VERSION,
    BuildCache,
    OutputRecord,
    ParseStatus,
    file_sha256,
    fingerprint,
    hash_text,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _entry(path: Path) -> tuple[str, int]:
    """Build the ``(sha256, size_bytes)`` parse-snapshot value for ``path``."""
    return (file_sha256(path), path.stat().st_size)


def test_fingerprint_is_order_independent_but_value_sensitive() -> None:
    a = fingerprint({"x": [1, 2], "y": "z"})
    b = fingerprint({"y": "z", "x": [1, 2]})
    assert a == b
    assert a != fingerprint({"x": [2, 1], "y": "z"})


def test_file_sha256_matches_hash_text_for_text_bytes(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_text("hello", encoding="utf-8")
    assert file_sha256(path) == hash_text("hello")


def test_parse_is_current_tracks_configuration_and_contents(tmp_path: Path) -> None:
    header = tmp_path / "a.hpp"
    header.write_text("one", encoding="utf-8")
    with BuildCache.open(tmp_path / "cache") as cache:
        cache.record_parse("fp-1", {str(header): _entry(header)})

        # Same fingerprint and unchanged file -> reuse the cached parse.
        assert cache.parse_is_current("fp-1")
        # A different configuration fingerprint invalidates regardless of files.
        assert not cache.parse_is_current("fp-2")

        # Editing a tracked file invalidates the cached parse.
        header.write_text("two", encoding="utf-8")
        assert not cache.parse_is_current("fp-1")

        # A vanished dependency also invalidates it.
        header.unlink()
        assert not cache.parse_is_current("fp-1")


def test_parse_is_current_skips_hash_when_metadata_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    header = tmp_path / "a.hpp"
    header.write_text("one", encoding="utf-8")
    with BuildCache.open(tmp_path / "cache") as cache:
        cache.record_parse("fp-1", {str(header): _entry(header)})

        # With (mtime_ns, size_bytes) unchanged the fast-path must avoid reading
        # the file at all: a hash that explodes proves it is never called.
        def explode(_path: object) -> str:
            msg = "file_sha256 must not run when metadata is unchanged"
            raise AssertionError(msg)

        monkeypatch.setattr(cache_module, "file_sha256", explode)
        assert cache.parse_is_current("fp-1")


def test_parse_is_current_falls_back_to_hash_when_only_mtime_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    header = tmp_path / "a.hpp"
    header.write_text("one", encoding="utf-8")
    with BuildCache.open(tmp_path / "cache") as cache:
        cache.record_parse("fp-1", {str(header): _entry(header)})

        # A touched-but-identical file (new mtime, same bytes) defeats the
        # fast-path, but the hash comparison still recognises it as unchanged.
        stat = header.stat()
        touched_ns = stat.st_mtime_ns + 1_000_000_000
        os.utime(header, ns=(stat.st_atime_ns, touched_ns))
        assert cache.parse_is_current("fp-1")

        # The hash fallback heals the stored metadata, so a second noop now hits
        # the fast-path without re-reading the file. The stub is scoped to just
        # this call (via monkeypatch.context) because the same-size edit below
        # relies on the real hash to detect the change.
        def explode(_path: object) -> str:
            msg = "metadata should have been refreshed after the hash fallback"
            raise AssertionError(msg)

        with monkeypatch.context() as m:
            m.setattr(cache_module, "file_sha256", explode)
            assert cache.parse_is_current("fp-1")

        # Same-size edit (3 bytes -> 3 bytes) still invalidates via the hash.
        header.write_text("two", encoding="utf-8")
        assert not cache.parse_is_current("fp-1")


def test_edit_during_the_parse_is_not_hidden_by_the_stat_fast_path(tmp_path: Path) -> None:
    header = tmp_path / "h.hpp"
    header.write_text("aaaa", encoding="utf-8")
    parsed = _entry(header)  # what the parser saw

    # The file is rewritten while the parse is still running: same size, so the
    # size check cannot catch it either. Recording the post-parse stat next to
    # the pre-edit hash would make every later build trust metadata that
    # describes content the hash never saw.
    started = header.stat().st_mtime_ns
    header.write_text("bbbb", encoding="utf-8")
    os.utime(header, ns=(started + 1000, started + 1000))

    with BuildCache.open(tmp_path / "cache") as cache:
        cache.record_parse("fp", {str(header): parsed}, parse_started_ns=started)

        row = cache._con.execute("SELECT mtime_ns FROM inputs").fetchone()  # noqa: SLF001
        assert row["mtime_ns"] is None  # no fast path: the next build must hash
        assert not cache.parse_is_current("fp")


def test_a_truncated_mtime_just_before_the_parse_still_forces_a_re_hash(tmp_path: Path) -> None:
    header = tmp_path / "h.hpp"
    header.write_text("aaaa", encoding="utf-8")
    parsed = _entry(header)  # what the parser saw

    # Same edit-during-the-parse race, but on a coarse-granularity filesystem
    # (exFAT's 2s, ext3's 1s): the write lands after the parse began yet is
    # *recorded* with an mtime truncated down to before it. A raw ``>=`` would
    # trust the stat and pair it with the pre-edit hash forever.
    started = header.stat().st_mtime_ns + 900_000_000
    header.write_text("bbbb", encoding="utf-8")
    os.utime(header, ns=(started - 900_000_000, started - 900_000_000))

    with BuildCache.open(tmp_path / "cache") as cache:
        cache.record_parse("fp", {str(header): parsed}, parse_started_ns=started)

        row = cache._con.execute("SELECT mtime_ns FROM inputs").fetchone()  # noqa: SLF001
        assert row["mtime_ns"] is None  # no fast path: the next build must hash
        assert not cache.parse_is_current("fp")


def test_untouched_file_keeps_its_fast_path_across_a_parse(tmp_path: Path) -> None:
    header = tmp_path / "h.hpp"
    header.write_text("aaaa", encoding="utf-8")
    # Nothing writes the file during the parse, so its stat is trustworthy and
    # the fast path is kept: the guard must not cost every build a re-hash. The
    # gap clears the granularity epsilon the guard subtracts from the threshold.
    started = header.stat().st_mtime_ns + 5_000_000_000

    with BuildCache.open(tmp_path / "cache") as cache:
        cache.record_parse("fp", {str(header): _entry(header)}, parse_started_ns=started)

        row = cache._con.execute("SELECT mtime_ns FROM inputs").fetchone()  # noqa: SLF001
        assert row["mtime_ns"] == header.stat().st_mtime_ns
        assert cache.parse_is_current("fp")


def test_parse_is_current_false_without_tracked_files(tmp_path: Path) -> None:
    with BuildCache.open(tmp_path / "cache") as cache:
        cache.record_parse("fp", {})
        assert not cache.parse_is_current("fp")


def test_parse_status_narrows_to_the_changed_translation_unit(tmp_path: Path) -> None:
    a = tmp_path / "a.hpp"
    a.write_text("a", encoding="utf-8")
    b = tmp_path / "b.hpp"
    b.write_text("b", encoding="utf-8")
    shared = tmp_path / "shared.hpp"
    shared.write_text("s", encoding="utf-8")
    files = {str(a): _entry(a), str(b): _entry(b), str(shared): _entry(shared)}
    # Both a and b include shared.hpp; a additionally has its own leaf content.
    tu_deps = {str(a): [str(a), str(shared)], str(b): [str(b), str(shared)]}

    with BuildCache.open(tmp_path / "cache") as cache:
        cache.record_parse("fp", files, tu_deps)

        # Nothing changed -> fully current.
        assert cache.parse_status("fp") == ParseStatus(current=True)
        # A different configuration fingerprint forces a full rebuild.
        assert cache.parse_status("fp2") == ParseStatus(current=False, stale_inputs=None)

        # Editing a leaf header marks only that input stale.
        a.write_text("a-edited", encoding="utf-8")
        assert cache.parse_status("fp") == ParseStatus(current=False, stale_inputs=frozenset({str(a)}))

        # Editing the shared header marks *every* input that includes it stale.
        a.write_text("a", encoding="utf-8")
        shared.write_text("s-edited", encoding="utf-8")
        assert cache.parse_status("fp") == ParseStatus(current=False, stale_inputs=frozenset({str(a), str(b)}))


def test_parse_status_without_tu_map_falls_back_to_full(tmp_path: Path) -> None:
    header = tmp_path / "a.hpp"
    header.write_text("one", encoding="utf-8")
    with BuildCache.open(tmp_path / "cache") as cache:
        # Recorded without per-TU attribution (tu_deps omitted).
        cache.record_parse("fp", {str(header): _entry(header)})
        header.write_text("two", encoding="utf-8")
        # No map to narrow with -> rebuild everything.
        assert cache.parse_status("fp") == ParseStatus(current=False, stale_inputs=None)


def test_record_partial_parse_updates_map_and_prunes_orphans(tmp_path: Path) -> None:
    a = tmp_path / "a.hpp"
    shared = tmp_path / "shared.hpp"
    for p in (a, shared):
        p.write_text(p.name, encoding="utf-8")
    files = {str(a): _entry(a), str(shared): _entry(shared)}

    with BuildCache.open(tmp_path / "cache") as cache:
        cache.record_parse("fp", files, {str(a): [str(a), str(shared)]})

        # a no longer includes shared.hpp; its fresh dep set drops it.
        cache.record_partial_parse({str(a): [str(a)]}, files)

        # shared.hpp is orphaned (no input references it) and pruned from inputs.
        assert set(cache.tracked_files()) == {str(a)}
        assert cache.tu_inputs() == {str(a): {str(a)}}
        # The fingerprint is unchanged across a partial update.
        assert cache.parse_fingerprint == "fp"


def test_deps_only_from_names_files_no_surviving_unit_reaches(tmp_path: Path) -> None:
    a = tmp_path / "a.hpp"
    b = tmp_path / "b.hpp"
    private = tmp_path / "private.hpp"
    shared = tmp_path / "shared.hpp"
    for p in (a, b, private, shared):
        p.write_text(p.name, encoding="utf-8")
    files = {str(p): _entry(p) for p in (a, b, private, shared)}

    with BuildCache.open(tmp_path / "cache") as cache:
        cache.record_parse(
            "fp",
            files,
            {
                str(a): [str(a), str(private), str(shared)],
                str(b): [str(b), str(shared)],
            },
        )

        # Re-parsing a.hpp alone: only a.hpp itself and its private header are
        # candidates. shared.hpp is still reached by b.hpp and must not be
        # offered for deletion; b.hpp is another unit's input entirely.
        assert cache.deps_only_from([str(a)]) == sorted([str(a), str(private)])
        # Re-parsing both units puts every tracked file in reach.
        assert cache.deps_only_from([str(a), str(b)]) == sorted(str(p) for p in (a, b, private, shared))
        assert cache.deps_only_from([]) == []


def test_outputs_round_trip_and_replacement(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    with BuildCache.open(cache_dir) as cache:
        cache.record_outputs({"a.md": OutputRecord("h1", 1, 10), "b.md": OutputRecord("h2", 2, 20)})
    # State persists across open()s.
    with BuildCache.open(cache_dir) as cache:
        assert cache.outputs() == {"a.md": OutputRecord("h1", 1, 10), "b.md": OutputRecord("h2", 2, 20)}
        # record_outputs replaces the whole index rather than merging.
        cache.record_outputs({"a.md": OutputRecord("h1b")})
        assert cache.outputs() == {"a.md": OutputRecord("h1b", None, None)}
        # refresh_output_stats heals just the stat fast-path, not the hash.
        cache.refresh_output_stats({"a.md": (3, 30)})
        assert cache.outputs() == {"a.md": OutputRecord("h1b", 3, 30)}


def test_render_round_trip_and_currency(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    with BuildCache.open(cache_dir) as cache:
        # Nothing recorded yet: never current, no summary.
        assert not cache.render_is_current("rfp-1")
        assert cache.render_summary() is None

        summary = {"symbol_count": 3, "reference_count": 1, "file_count": 2, "pages": ["a", "b"]}
        cache.record_render("rfp-1", summary)

    with BuildCache.open(cache_dir) as cache:
        # State persists and currency is fingerprint-sensitive.
        assert cache.render_is_current("rfp-1")
        assert not cache.render_is_current("rfp-2")
        assert cache.render_summary() == summary


def test_page_cache_round_trip_key_sensitivity_and_pruning(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    with BuildCache.open(cache_dir) as cache:
        # Nothing memoised yet -> a miss.
        assert cache.cached_page("a", "k1") is None
        cache.record_pages({"a": ("k1", "TEXT A"), "b": ("k2", "TEXT B")})

    with BuildCache.open(cache_dir) as cache:
        # State persists across open()s and the text replays on a key match.
        assert cache.cached_page("a", "k1") == "TEXT A"
        assert cache.cached_page("b", "k2") == "TEXT B"
        # A stale key (the page's dependencies moved) is a miss, not the old text.
        assert cache.cached_page("a", "different-key") is None

        # Rewriting the table updates 'a' and prunes 'b' (its page vanished).
        cache.record_pages({"a": ("k3", "TEXT A2")})
        assert cache.cached_page("a", "k3") == "TEXT A2"
        assert cache.cached_page("a", "k1") is None
        assert cache.cached_page("b", "k2") is None


def test_record_pages_writes_only_the_pages_the_render_rendered(tmp_path: Path) -> None:
    """An O(change) render costs an O(change) write, not a rewrite of the corpus."""
    cache_dir = tmp_path / "cache"
    with BuildCache.open(cache_dir) as cache:
        cache.record_pages({"a": ("k1", "TEXT A"), "b": ("k2", "TEXT B"), "gone": ("k3", "TEXT G")})

        statements: list[str] = []
        cache._con.set_trace_callback(statements.append)  # noqa: SLF001
        try:
            # 'a' was re-rendered, 'b' replayed from the cache under its stored
            # key, and 'gone' left the render because its symbol vanished.
            cache.record_pages({"a": ("k4", "TEXT A2")}, stems=["a", "b"])
        finally:
            cache._con.set_trace_callback(None)  # noqa: SLF001

        assert cache.cached_page("a", "k4") == "TEXT A2"
        # Replayed, never rewritten -- and still replayable next build.
        assert cache.cached_page("b", "k2") == "TEXT B"
        assert cache.cached_page("gone", "k3") is None

    written = [sql for sql in statements if "INSERT" in sql]
    assert len(written) == 1
    assert "TEXT B" not in written[0]


def test_render_summary_survives_version_reset_as_absent(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    with BuildCache.open(cache_dir) as cache:
        cache.record_render("rfp", {"symbol_count": 1, "pages": []})
        cache._set_meta("cache_version", str(CACHE_VERSION + 1))  # noqa: SLF001
        cache._con.commit()  # noqa: SLF001
    # An incompatible version wipes the render bookkeeping along with the rest.
    with BuildCache.open(cache_dir) as cache:
        assert cache.render_summary() is None
        assert not cache.render_is_current("rfp")


def test_corrupted_cache_is_discarded_and_rebuilt(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    # A garbage file where the SQLite database is expected must not wedge builds.
    (cache_dir / BuildCache.FILENAME).write_bytes(b"not a sqlite database at all")
    with BuildCache.open(cache_dir) as cache:
        assert cache.outputs() == {}
        cache.record_outputs({"a.md": OutputRecord("h1")})
    with BuildCache.open(cache_dir) as cache:
        assert cache.outputs() == {"a.md": OutputRecord("h1")}


def _write_v1_cache(cache_dir: Path) -> Path:
    """Create a pre-mtime ``CACHE_VERSION = 1`` cache file with the old layout.

    The v1 ``inputs`` table had only ``(path, sha256)`` — no ``mtime_ns`` /
    ``size_bytes`` — so this exercises the column-adding migration, which a row
    delete alone cannot perform.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(cache_dir / BuildCache.FILENAME)
    try:
        con.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE inputs (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
            CREATE TABLE outputs (output_path TEXT PRIMARY KEY, content_hash TEXT NOT NULL);
            """,
        )
        con.execute("INSERT INTO meta(key, value) VALUES('cache_version', '1')")
        con.execute("INSERT INTO inputs(path, sha256) VALUES('/old/a.hpp', 'deadbeef')")
        con.commit()
    finally:
        con.close()
    return cache_dir


def test_v1_cache_is_migrated_to_current_schema(tmp_path: Path) -> None:
    cache_dir = _write_v1_cache(tmp_path / "cache")
    header = tmp_path / "a.hpp"
    header.write_text("one", encoding="utf-8")

    # Opening a v1 cache must rebuild the schema (not just delete rows), so the
    # new mtime_ns/size_bytes columns exist and the 4-column write path works.
    with BuildCache.open(cache_dir) as cache:
        assert cache.parse_fingerprint is None  # old inputs were discarded
        cache.record_parse("fp-1", {str(header): _entry(header)})
        assert cache.parse_is_current("fp-1")


def test_version_mismatch_resets_cache(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    with BuildCache.open(cache_dir) as cache:
        cache.record_outputs({"a.md": OutputRecord("h1")})
        cache._set_meta("cache_version", str(CACHE_VERSION + 1))  # noqa: SLF001
        cache._con.commit()  # noqa: SLF001
    # Re-opening sees an incompatible version and starts from scratch.
    with BuildCache.open(cache_dir) as cache:
        assert cache.outputs() == {}
        assert cache.parse_fingerprint is None

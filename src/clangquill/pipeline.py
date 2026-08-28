"""The end-to-end build: parse C++ → SQLite IR → rendered MyST pages.

Both front ends (the Sphinx extension and the ``clangquill build`` CLI) drive
the same pipeline here so they behave identically. The steps are:

1. Resolve the configured inputs against a base directory.
2. Parse them with the libclang-backed core into a SQLite database.
3. Render the IR into MyST pages with the :class:`~clangquill.generator.Generator`.
4. Prune pages left over from a previous run.

When ``clangquill_cache_dir`` is configured the build becomes *incremental*
(milestone M6): the SQLite IR and a small bookkeeping cache persist between
runs, so an unchanged build skips both the libclang parse and every output
write, and symbols that disappear have their pages deleted. A *fully* unchanged
build (cache-hit parse and unchanged render config/templates) goes one step
further and skips the Jinja render entirely, returning the previous run's counts
straight from the cache rather than re-rendering only to discover nothing
changed. When *some* symbols did change, the render is still incremental: each
page is keyed by the content hashes of the symbols it reads (plus the render
fingerprint) and replayed from a per-page cache unless that key moved, so only
the pages whose symbols actually changed are re-rendered. (Per-page memoisation
is used only with the bundled templates; a custom template falls back to a full
render of every page, since it may read IR data the key does not track.) The
parse side is *per translation unit*: when the input set and
compile configuration are unchanged, only the translation units whose files
actually changed are re-parsed into the existing IR (the rest are reused), so
touching one header out of many costs roughly one TU parse instead of the whole
module. A change to the input set or compile configuration still forces a full
re-parse. Without a cache directory the build is stateless: it always re-parses
into a throwaway IR, rewrites every page, and prunes stale pages via a
manifest.

``warnings_as_errors`` is the one setting that opts out of the parse cache
entirely: a verdict on the whole input set can only come from a parse of the
whole input set (see :func:`_incremental_build`).
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from clangquill import _core
from clangquill.cache import BuildCache, OutputRecord, ParseStatus, file_sha256, fingerprint, hash_bytes, hash_text
from clangquill.comments import OVERRIDE_ENV
from clangquill.config import CONFIG_PREFIX
from clangquill.generator import Generator, write_if_changed
from clangquill.store import Store, StoreVersionError

if TYPE_CHECKING:
    from clangquill.config import Config

# Name of the manifest tracking generated pages, written into ``output_dir`` so
# stale pages from a previous build can be pruned on the next one.
MANIFEST_NAME = ".clangquill-manifest.json"

# The file libclang looks for inside a compilation-database directory.
COMPILE_COMMANDS_NAME = "compile_commands.json"

# Filename of the persisted SQLite IR within a configured cache directory.
IR_NAME = "clangquill.sqlite"

# Umbrella batch size used for the *incremental* re-parse path when
# ``tu_batch`` is auto (0). Stale sets are usually small — a leaf-header edit
# invalidates a handful of translation units — and the cold-build default of 64
# would lump them into a single umbrella parsed on one thread, serially
# re-paying the whole shared ``#include`` prelude. A smaller batch spreads a
# small stale set across the thread pool. Cold builds keep the larger default:
# batch composition only affects the TUs being re-parsed here, so the
# determinism rationale for the fixed cold batch (see ``kDefaultTuBatch`` in
# ``parser.cpp``) does not bind on this path.
#
# What this path cannot make identical to a cold build is a header that is not
# self-contained: an incremental run parses a *subset*, so its batches are
# composed differently whatever the batch size. That is a property of umbrella
# batching, not of input order, and it is what ``verify.py``'s isolation check
# measures.
_INCREMENTAL_TU_BATCH = 8


#: Severity levels as libclang reports them (mirrors ``CXDiagnosticSeverity``).
SEVERITY_NAMES = {0: "ignored", 1: "note", 2: "warning", 3: "error", 4: "fatal"}

#: Lowest severity ``warnings_as_errors`` fails a build on. ``note`` (1) is
#: excluded deliberately: notes are the explanatory chain hanging off a
#: diagnostic, never a problem in their own right.
WARNING_SEVERITY = 2

#: Severity ``BuildResult.diagnostics`` (the console/Sphinx-warning stream) is
#: filtered to, mirroring what the core itself reports there regardless of
#: ``capture_all_diagnostics``.
ERROR_SEVERITY = 3


@dataclass(frozen=True)
class Diagnostic:
    """One libclang diagnostic captured during a parse.

    libclang nests explanatory ``note:`` diagnostics under the diagnostic they
    belong to; they arrive here flattened, each following its parent with a
    :attr:`depth` one greater.
    """

    #: One of the :data:`SEVERITY_NAMES` keys.
    severity: int
    #: 0 for a top-level diagnostic, ``n`` for a note under the nearest
    #: preceding record of depth ``n - 1``.
    depth: int
    #: Message as libclang formats it, already carrying ``file:line:col``, the
    #: severity word and any ``[-Wflag]`` suffix.
    text: str
    #: Presumed location of the diagnostic; empty/zero when it has none.
    file: str = ""
    line: int = 0
    column: int = 0


class CompileCommandsError(FileNotFoundError):
    """Raised when the configured compilation database cannot be used.

    Subclasses :class:`FileNotFoundError` so both front ends keep reporting it
    the way they already report a bad input pattern: the CLI prints it and exits
    non-zero, the Sphinx extension turns it into a clean ``ExtensionError``.
    """


@dataclass
class BuildResult:
    """Outcome of a :func:`build` run."""

    #: Resolved output directory holding the generated pages.
    output_dir: Path
    #: Page stems written (excluding the index), in toctree order.
    pages: list[str]
    #: Path of the SQLite IR (a temp file unless ``cache_dir`` was configured).
    db_path: Path
    #: Whether ``db_path`` is a throwaway temp file the caller should remove.
    db_is_temporary: bool = False
    #: Number of symbols written to the IR.
    symbol_count: int = 0
    #: Number of cross-reference edges written to the IR.
    reference_count: int = 0
    #: Number of source files parsed.
    file_count: int = 0
    #: Error-severity diagnostics, without their notes: what the front ends
    #: print. Unaffected by ``diagnostics_log``, so enabling the log never
    #: changes what a build reports on the console.
    diagnostics: list[str] = field(default_factory=list)
    #: Every diagnostic captured this run, in parse order, notes flattened
    #: behind their parent. Empty unless ``clangquill_diagnostics_log`` asked
    #: for full capture.
    diagnostic_records: list[Diagnostic] = field(default_factory=list)
    #: How many diagnostics of each severity this run captured, keyed by the
    #: :data:`SEVERITY_NAMES` word and holding only non-zero entries. Derived
    #: from :attr:`diagnostic_records`, so it is empty for the same reason that
    #: list is: nothing asked for full capture, or nothing was parsed.
    diagnostic_counts: dict[str, int] = field(default_factory=dict)
    #: Path of the diagnostics log this run wrote, or ``None`` — either because
    #: none was configured, or because a fully cached build deliberately left
    #: the previous run's log in place.
    diagnostics_log: Path | None = None
    #: Whether libclang re-parsed this run (``False`` = served from the cache).
    parsed: bool = True
    #: Output filenames actually (re)written this run (incremental builds only
    #: write changed pages; a full build lists every page it wrote).
    pages_written: list[str] = field(default_factory=list)
    #: Output filenames deleted this run because their source vanished.
    pages_deleted: list[str] = field(default_factory=list)


def _resolve_inputs(patterns: list[str], base_dir: Path) -> list[str]:
    """Expand ``patterns`` (paths or globs) relative to ``base_dir``.

    Duplicates are removed so no file is parsed twice; the order patterns are
    listed in is preserved for readable diagnostics only, since the parse itself
    is order-independent (``parse_files`` in ``parser.cpp`` canonicalises).
    Raises :class:`FileNotFoundError` if a pattern matches nothing.
    """
    resolved: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        candidate = Path(pattern)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        # A literal path that exists on disk always wins over glob expansion,
        # so a name containing glob metacharacters (e.g. ``foo[1].h``) resolves
        # to itself rather than silently matching an unrelated file the glob
        # happens to expand to (e.g. ``foo1.h``).
        if candidate.exists():
            matches = [str(candidate)]
        else:
            matches = sorted(glob.glob(str(candidate), recursive=True))  # noqa: PTH207
            if not matches:
                msg = f"clangquill input matched no files: {pattern!r} (under {base_dir})"
                raise FileNotFoundError(msg)
        # A glob can match directories (e.g. ``include/*``); only files can be
        # parsed, so skip the rest rather than handing them to libclang.
        for match in matches:
            match_path = Path(match)
            if not match_path.is_file():
                continue
            full = str(match_path.resolve())
            if full not in seen:
                seen.add(full)
                resolved.append(full)
    return resolved


def _compile_commands_candidates(value: str, base_dir: Path) -> list[Path]:
    """Paths searched for the compilation database configured as ``value``.

    ``compile_commands`` names the *directory* holding a ``compile_commands.json``
    (that is what libclang's ``clang_CompilationDatabase_fromDirectory`` takes),
    but pointing it straight at the JSON file is the obvious slip, so that
    spelling is accepted too. Relative values resolve against ``base_dir``.
    """
    configured = Path(value).expanduser()
    if not configured.is_absolute():
        configured = base_dir / configured
    if configured.name == COMPILE_COMMANDS_NAME:
        return [configured]
    return [configured / COMPILE_COMMANDS_NAME]


@dataclass(frozen=True)
class _CompileDatabase:
    """A resolved, validated ``compile_commands.json``: where it is and its hash.

    A build needs the database's directory (libclang takes the directory, not
    the file), proof that it loads, and its content hash for the parse
    fingerprint. Each of those used to re-read and re-parse the whole file —
    three reads plus a fourth for the hash, noticeable at monorepo sizes — so
    :func:`build` produces one of these up front and passes it around.
    """

    path: Path
    sha256: str


def resolve_compile_commands(value: str, base_dir: Path) -> Path:
    """Return the usable ``compile_commands.json`` configured as ``value``.

    libclang reports a database it cannot open only as "no flags for this file",
    which then degrades silently into the ``std``/``include_dirs``/``defines``
    fallback. Checking it here instead means a database that is missing,
    unreadable, malformed or empty fails loudly — and the message lists every
    path that was searched, so a misconfigured directory is obvious.

    Raises :class:`CompileCommandsError` if no candidate path holds a loadable
    database.
    """
    return _load_compile_commands(value, base_dir).path


def _load_compile_commands(value: str, base_dir: Path) -> _CompileDatabase:
    """Resolve, validate and hash the configured database in a single read."""
    candidates = _compile_commands_candidates(value, base_dir)
    for candidate in candidates:
        if candidate.is_file():
            return _check_compile_commands(candidate)
    looked = "\n".join(f"  {candidate}" for candidate in candidates)
    msg = (
        f"{CONFIG_PREFIX}compile_commands={value!r} does not point at a "
        f"{COMPILE_COMMANDS_NAME} (relative paths resolve against {base_dir}); looked for:\n{looked}"
    )
    raise CompileCommandsError(msg)


def _check_compile_commands(path: Path) -> _CompileDatabase:
    """Return ``path`` described, or raise unless it is a loadable database.

    Read as bytes: the same buffer answers "does it load?" and "what is its
    hash?", and the hash then matches :func:`file_sha256` exactly, so nothing
    about this shortcut moves an existing parse fingerprint. ``json.loads``
    takes bytes directly and reports a bad encoding as the ValueError below.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        msg = f"compilation database {path} could not be read: {exc}"
        raise CompileCommandsError(msg) from exc
    try:
        entries = json.loads(raw)
    except ValueError as exc:
        msg = f"compilation database {path} is not valid JSON: {exc}"
        raise CompileCommandsError(msg) from exc
    if not isinstance(entries, list):
        msg = f"compilation database {path} must hold a JSON array of compile commands"
        raise CompileCommandsError(msg)
    if not entries:
        msg = (
            f"compilation database {path} is empty, so it supplies flags for no file at all — "
            "regenerate it (e.g. cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON)"
        )
        raise CompileCommandsError(msg)
    return _CompileDatabase(path, hash_bytes(raw))


def _parse_options(
    config: Config,
    base_dir: Path,
    compile_db: _CompileDatabase | None = None,
) -> _core.ParseOptions:
    """Translate a :class:`Config` into core :class:`ParseOptions`.

    ``compile_db`` is the database :func:`build` already resolved; omitting it
    resolves one, which is what a caller outside a build wants.
    """
    opt = _core.ParseOptions()
    opt.std_flag = config.std
    opt.include_dirs = [str((base_dir / d).resolve()) for d in config.include_dirs]
    opt.defines = list(config.defines)
    extra = list(config.compile_args)
    if config.clang_resource_dir:
        extra.append(f"-resource-dir={Path(config.clang_resource_dir).expanduser()}")
    opt.extra_args = extra
    opt.jobs = config.jobs
    opt.tu_batch = config.tu_batch
    opt.extract_anonymous_namespaces = config.extract_anonymous_namespaces
    # Two knobs ask the core to capture more than errors: writing them all to a
    # log, and judging the build on them. Nothing else consumes the extra
    # records, so capturing them for a run that does neither would be pure cost.
    opt.capture_all_diagnostics = bool(config.diagnostics_log or config.warnings_as_errors)
    if config.compile_commands:
        # libclang takes the *directory*; the resolver has already proven a
        # loadable compile_commands.json sits inside it.
        if compile_db is None:
            compile_db = _load_compile_commands(config.compile_commands, base_dir)
        opt.compile_commands_dir = str(compile_db.path.parent)
    return opt


def _parse_fingerprint(
    config: Config,
    base_dir: Path,
    inputs: list[str],
    compile_db: _CompileDatabase | None = None,
) -> str:
    """Fingerprint everything that, if changed, invalidates the cached parse.

    Covers the resolved input set, the normalized compile arguments, the
    libclang toolchain version and (when used) the ``compile_commands.json``
    contents. File *contents* are tracked separately via per-file hashes, so
    this captures only the parse *configuration*.
    """
    compile_commands_hash = ""
    if config.compile_commands:
        # ``compile_commands`` names the *directory* holding compile_commands.json
        # (it is handed to clang_CompilationDatabase_fromDirectory), so this is
        # the hash of the JSON file inside it, not of the directory. It comes
        # from the one read ``build`` already did, which also closes the window
        # where the file vanished between that read and this one.
        if compile_db is None:
            compile_db = _load_compile_commands(config.compile_commands, base_dir)
        compile_commands_hash = compile_db.sha256
    return fingerprint(
        {
            # Sorted because the parse is a function of the input *set*: two
            # runs that name the same files in a different order genuinely do
            # produce the same IR, so they must share a cache entry.
            "inputs": sorted(inputs),
            "std": config.std,
            "include_dirs": [str((base_dir / d).resolve()) for d in config.include_dirs],
            "defines": list(config.defines),
            "compile_args": list(config.compile_args),
            "tu_batch": config.tu_batch,
            # Changes which symbols the parse extracts at all, so a cached IR
            # built with the other setting must not be reused.
            "extract_anonymous_namespaces": config.extract_anonymous_namespaces,
            # The IR is identical either way, but the *diagnostics* are not:
            # without this, switching the log on for an already-cached project
            # would give a no-op build and an empty log. The derived boolean,
            # never the path, so relocating the log costs no re-parse.
            "capture_all_diagnostics": bool(config.diagnostics_log or config.warnings_as_errors),
            "clang_resource_dir": config.clang_resource_dir or "",
            "compile_commands": compile_commands_hash,
            "core_version": getattr(_core, "__core_version__", ""),
            "libclang_version": _core.libclang_version(),
        },
    )


def _records(result: _core.ParseResult) -> list[Diagnostic]:
    """Convert the core's diagnostic records into :class:`Diagnostic` values."""
    return [
        Diagnostic(
            severity=record.severity,
            depth=record.depth,
            text=record.text,
            file=record.file,
            line=record.line,
            column=record.column,
        )
        for record in result.diagnostic_records
    ]


def severity_counts(records: list[Diagnostic]) -> dict[str, int]:
    """Count ``records`` by severity word, omitting severities that never occur."""
    counts = Counter(record.severity for record in records)
    return {name: counts[severity] for severity, name in sorted(SEVERITY_NAMES.items()) if counts[severity]}


def warnings_or_worse(records: list[Diagnostic]) -> list[Diagnostic]:
    """Return the top-level records ``warnings_as_errors`` fails a build on.

    Notes are dropped along with their severity: they are only meaningful
    hanging off the diagnostic they explain, and that diagnostic is already in
    the list when it is one worth failing on.
    """
    return [record for record in records if record.severity >= WARNING_SEVERITY]


def _diagnostic_texts(records: list[Diagnostic]) -> list[str]:
    """Error-severity-or-worse message texts, matching the core's ``diagnostics`` field.

    Used to rebuild :attr:`BuildResult.diagnostics` from cached records on a
    cache-hit build, so it stays exactly what a live parse would have reported.
    """
    return [record.text for record in records if record.severity >= ERROR_SEVERITY and record.depth == 0]


def _diagnostics_to_json(records: list[Diagnostic]) -> list[dict[str, object]]:
    """Serialize ``records`` for storage in the render cache summary."""
    return [
        {
            "severity": record.severity,
            "depth": record.depth,
            "text": record.text,
            "file": record.file,
            "line": record.line,
            "column": record.column,
        }
        for record in records
    ]


def _diagnostics_from_json(raw: object) -> list[Diagnostic]:
    """Deserialize the diagnostics stored by :func:`_diagnostics_to_json`.

    Tolerant of anything malformed or missing — an older cache predating this
    field, or a hand-edited summary — by dropping the offending entry rather
    than raising, since a cached diagnostic is only ever a best-effort replay.
    """
    if not isinstance(raw, list):
        return []
    records: list[Diagnostic] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            records.append(
                Diagnostic(
                    severity=int(item["severity"]),
                    depth=int(item["depth"]),
                    text=str(item["text"]),
                    file=str(item.get("file", "")),
                    line=int(item.get("line", 0)),
                    column=int(item.get("column", 0)),
                ),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return records


def _carry_forward_diagnostics(
    previous: list[Diagnostic],
    partial_deps: dict[str, list[str]],
    dropped: list[str],
    records: list[Diagnostic],
) -> list[Diagnostic]:
    """Combine a partial reparse's diagnostics with the untouched rest of the project.

    Every file the reparsed translation units now reach (each of their own
    paths included, per the core's inclusion walk) is freshly known, so a
    previously carried diagnostic located in one of them is superseded by
    ``records`` — dropped if it cleared up, replaced if it did not — rather
    than kept alongside a stale duplicate. ``dropped`` (the reparse's candidates
    for removal from the IR, see :meth:`BuildCache.deps_only_from`) covers the
    other direction: a file the stale translation units no longer reach at all
    is not in ``partial_deps`` either, so without it a diagnostic that used to
    live there would never be superseded and would linger forever.
    """
    reparsed_files = {dep for deps in partial_deps.values() for dep in deps}
    reparsed_files.update(dropped)
    fresh = warnings_or_worse(records)
    # A location-less diagnostic (``file == ""`` — a command-line warning, say,
    # which every translation unit re-emits) belongs to no file, so the path
    # check below can never supersede it: the carried copy would be kept
    # alongside the freshly re-emitted one and the list would grow by one on
    # every partial reparse (issue #302). Fall back for those to the identity
    # the parser itself dedups diagnostics on across batches — severity plus
    # the formatted text, which already carries ``file:line:col`` when there is
    # one. Carried diagnostics are keyed against each other too, so a list that
    # an older cache already grew collapses back on the next incremental build.
    seen = {(diagnostic.severity, diagnostic.text) for diagnostic in fresh}
    carried: list[Diagnostic] = []
    for diagnostic in previous:
        if diagnostic.file in reparsed_files:
            continue
        identity = (diagnostic.severity, diagnostic.text)
        if identity in seen:
            continue
        seen.add(identity)
        carried.append(diagnostic)
    return carried + fresh


def _diagnostics_log_path(config: Config, base: Path) -> Path | None:
    """Resolve ``config.diagnostics_log`` against ``base``, or ``None``."""
    if not config.diagnostics_log:
        return None
    return (base / config.diagnostics_log).resolve()


def write_diagnostics_log(
    path: Path,
    records: list[Diagnostic],
    *,
    inputs: int,
    partial: int | None = None,
) -> None:
    """Write ``records`` to ``path`` as plain text, replacing any previous run's.

    The file is a snapshot of one build, not a rolling history: appending would
    grow without bound across a ``make html`` loop and leave no way to tell
    which entries are current. The ``generated`` header line is the staleness
    signal instead.

    ``partial`` is the number of translation units re-parsed on an incremental
    build (``None`` for a full parse). It goes into the header, because on an
    incremental build ``records`` covers only those units and a log that did not
    say so would read as a complete picture of the project.
    """
    counts = Counter(record.severity for record in records)
    totals = ", ".join(
        f"{counts[severity]} {name}(s)" for severity, name in sorted(SEVERITY_NAMES.items()) if counts[severity]
    )
    scope = "full" if partial is None else f"incremental — {partial} of {inputs} translation unit(s) re-parsed"
    lines = [
        "# clangquill diagnostics",
        f"# generated: {datetime.now(tz=UTC).isoformat(timespec='seconds')}",
        f"# inputs: {inputs} file(s)",
        f"# parse: {scope}",
        f"# totals: {totals or 'none'}",
        "",
    ]
    for index, record in enumerate(records):
        # A note is indented under the diagnostic it explains, and each
        # top-level group is separated by a blank line. The text is emitted
        # verbatim — libclang already prefixed it with file:line:col, the
        # severity word and any [-Wflag], so re-stating those would only
        # duplicate them. Records stay in parse order, which is deterministic
        # (batches merge in the parser's canonical order) and meaningful.
        if record.depth == 0 and index:
            lines.append("")
        indent = "  " * record.depth
        lines.extend(f"{indent}{line}" for line in record.text.splitlines() or [""])

    path.parent.mkdir(parents=True, exist_ok=True)
    # Staged then renamed, like the IR writes above, so a crashed build never
    # leaves a half-written log. The staging name is unique per write — not just
    # per process — so concurrent builds sharing a srcdir race to a whole file
    # (last one wins) instead of interleaving into one or unlinking each other's
    # staging file mid-flight.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    handle.close()
    staged = Path(handle.name)
    try:
        staged.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def _template_files_hash(template_dirs: list[str]) -> dict[str, str]:
    """Hash every file under each override template directory.

    Editing an override template changes the rendered output even though the IR
    is untouched, so the noop-render skip must notice it. Builtin templates are
    package data versioned with ``core_version``/the install, so only the
    user-provided dirs are walked here.
    """
    digests: dict[str, str] = {}
    for directory in template_dirs:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                try:
                    digests[str(path)] = file_sha256(path)
                except OSError:
                    digests[str(path)] = "missing"
    return digests


def _render_fingerprint(config: Config, base_dir: Path) -> str:
    """Fingerprint everything that shapes the output *given an unchanged IR*.

    Covers the render-affecting configuration (template selection, grouping,
    toctree shape, output location, …) and the contents of any override template
    directories. The IR itself is excluded on purpose: the caller only consults
    this when the parse was served from cache, which already guarantees the IR is
    byte-identical to the run that produced the cached render.

    Also covers the ``CLANGQUILL_COMMENT_PARSER`` environment variable: it can
    override the effective comment parser (see :func:`clangquill.comments.resolve_override`),
    and a page's ``content_hash`` covers only the raw comment text, not its
    parse — so without this, changing the env var would silently replay
    stale rendered comments from cache.
    """
    template_dirs = [str((base_dir / d).resolve()) for d in config.template_dirs]
    return fingerprint(
        {
            "template_dirs": template_dirs,
            "template_files": _template_files_hash(template_dirs),
            "templates": dict(sorted(config.templates.items())),
            "include_undocumented": config.include_undocumented,
            "comment_parser": config.comment_parser or "",
            "comment_parser_env_override": os.environ.get(OVERRIDE_ENV, ""),
            "group_by": config.group_by,
            "toctree_maxdepth": config.toctree_maxdepth,
            "root_document": config.root_document,
            "path_base": str((base_dir / config.path_base).resolve()) if config.path_base else "",
            "output_dir": str((base_dir / config.output_dir).resolve()),
            "core_version": getattr(_core, "__core_version__", ""),
        },
    )


def _make_generator(config: Config, base_dir: Path, store: Store) -> Generator:
    """Build a :class:`Generator` wired from ``config`` against ``store``."""
    return Generator(
        store,
        template_dirs=[str((base_dir / d).resolve()) for d in config.template_dirs],
        templates=config.templates,
        include_undocumented=config.include_undocumented,
        comment_parser=config.comment_parser,
        path_base=str((base_dir / config.path_base).resolve()) if config.path_base else None,
    )


#: Declaration a custom template puts in a comment to opt back into per-page
#: render memoisation (see :func:`_page_cache_mode`).
PAGE_CACHE_MARKER = "clangquill:page-cache"

#: Template files an override directory can contribute. Anything else in there
#: (a README, an editor backup) is not a template and does not gate the cache.
_TEMPLATE_GLOB = "*.jinja"


def _page_cache_mode(config: Config, base_dir: Path) -> tuple[bool, bool]:
    """Return ``(memoise, wide)``: whether — and how — pages may be memoised.

    The page cache replays a page's text whenever the IR data that page reads is
    unchanged, which only works if the dependency fingerprint tracks everything
    the templates read.

    * Bundled templates only (no ``template_dirs``; a ``templates`` mapping may
      still point kinds at other *bundled* stems): memoise on the default
      fingerprint, which is exactly what those templates read.
    * Override template files that *all* declare ``clangquill:page-cache`` in a
      comment: memoise on the wide fingerprint
      (:meth:`~clangquill.generator.Generator.page_fingerprint`), which covers
      every per-symbol field the documented template context exposes.
    * Anything else: no memoisation. An undeclared template may read IR the
      fingerprint does not track (``gen.roots()`` and friends reach outside the
      page entirely), so those builds keep the full-render path and stay
      correct — the render fingerprint still busts the whole cache when a
      template changes, but within a build every page is rendered.

    A directory that contributes no template file at all is treated as bundled
    only: there is nothing custom to read anything.
    """
    templates = [
        path
        for directory in config.template_dirs
        for path in sorted((base_dir / directory).rglob(_TEMPLATE_GLOB))
        if path.is_file()
    ]
    if not templates:
        return True, False
    if all(_declares_page_cache(path) for path in templates):
        return True, True
    return False, False


def _declares_page_cache(path: Path) -> bool:
    """Whether the template at ``path`` opts into page memoisation.

    The declaration is a plain substring match on the file's text: it is meant
    to sit in a Jinja comment (``{# clangquill:page-cache #}``), but a template
    is free to put it wherever a comment is legal for its own syntax. An
    unreadable file does not declare anything.
    """
    try:
        return PAGE_CACHE_MARKER in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def _rendered_files(
    generator: Generator,
    config: Config,
    base_dir: Path,
    *,
    cache: BuildCache | None = None,
    render_fingerprint: str = "",
) -> list[tuple[str, str]]:
    """Render every output into ``(filename, text)`` pairs, index last.

    When ``cache`` is supplied and the build's templates allow it
    (:func:`_page_cache_mode`), each page is keyed by its dependency fingerprint
    combined with ``render_fingerprint`` and replayed from the page cache when
    unchanged, so an incremental build re-runs Jinja only for the pages whose
    symbols actually moved. Without a cache it renders everything.
    """
    # The index stem is reserved so no symbol page (e.g. a function named
    # ``index``) can collide with the root document appended below.
    plans = generator.plan_pages(group_by=config.group_by, reserved_stems=(config.root_document,))
    eligible, wide = _page_cache_mode(config, base_dir)
    memoize = cache is not None and eligible
    rendered: list[tuple[str, str]] = []
    # Only the pages this build actually rendered need writing back: a page
    # replayed from the cache is already stored under the very key it was
    # replayed by. ``stems`` carries the rest so the cache can still prune the
    # pages that left the render.
    records: dict[str, tuple[str, str]] = {}
    stems: list[str] = []
    for plan in plans:
        key = ""
        text: str | None = None
        if memoize:
            key = hash_text(render_fingerprint + generator.page_fingerprint(plan, wide=wide))
            text = cache.cached_page(plan.stem, key)
            stems.append(plan.stem)
        if text is None:
            text = plan.render()
            if memoize:
                records[plan.stem] = (key, text)
        rendered.append((f"{plan.stem}.md", text))

    index_stem = config.root_document
    index_key = ""
    index_text: str | None = None
    if memoize:
        # The index links the page *set*; its toctree depth/root ride in the
        # render fingerprint, so the stem/label/top_level list is all that
        # varies here. top_level must be included: render_index filters on it,
        # so a page set identical in stems/labels but differing in top_level
        # would otherwise replay a stale index.
        index_key = hash_text(
            render_fingerprint
            + fingerprint(
                {"index": [[plan.stem, plan.label, getattr(plan, "top_level", True)] for plan in plans]},
            ),
        )
        index_text = cache.cached_page(index_stem, index_key)
        stems.append(index_stem)
    if index_text is None:
        index_text = generator.render_index(plans, toctree_maxdepth=config.toctree_maxdepth)
        if memoize:
            records[index_stem] = (index_key, index_text)
    rendered.append((f"{index_stem}.md", index_text))

    if memoize:
        # ``stems`` is the render's whole page set, so pages whose symbol
        # vanished are still pruned; ``records`` holds only the re-rendered ones.
        cache.record_pages(records, stems=stems)
    return rendered


def build(config: Config, *, base_dir: str | Path) -> BuildResult:
    """Run the pipeline for ``config`` rooted at ``base_dir``.

    ``base_dir`` is the Sphinx srcdir (or the CWD for the CLI); every relative
    path in ``config`` is resolved against it. A configured ``cache_dir`` makes
    the build incremental (see the module docstring); otherwise it is stateless.
    """
    config.validate()
    base = Path(base_dir).resolve()
    # Read, validated and hashed exactly once here: a missing or unloadable
    # database fails before any parsing, cache bookkeeping or page writing
    # happens, and the parse options and the parse fingerprint below are both
    # served from this one read rather than re-parsing the JSON each.
    compile_db = _load_compile_commands(config.compile_commands, base) if config.compile_commands else None
    inputs = _resolve_inputs(config.input, base)
    output_dir = (base / config.output_dir).resolve()
    if config.cache_dir:
        cache_dir = (base / config.cache_dir).resolve()
        return _incremental_build(config, base, inputs, output_dir, cache_dir, compile_db)
    return _full_build(config, base, inputs, output_dir, compile_db)


def _new_temp_db(directory: Path | None = None) -> Path:
    """Create an empty temp file for a throwaway IR and return its path."""
    handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False, dir=directory)  # noqa: SIM115
    handle.close()
    return Path(handle.name)


def _full_build(
    config: Config,
    base: Path,
    inputs: list[str],
    output_dir: Path,
    compile_db: _CompileDatabase | None = None,
) -> BuildResult:
    """Stateless build: parse into a throwaway IR and rewrite every page."""
    db_path = _new_temp_db()

    log_path = _diagnostics_log_path(config, base)
    records: list[Diagnostic] = []
    succeeded = False
    try:
        result = _core.parse_to_sqlite(inputs, str(db_path), _parse_options(config, base, compile_db))
        records = _records(result)
        if log_path is not None:
            # Written before the render, not after: a log of the parse that
            # preceded a render crash is exactly what you want to read when
            # working out why the render crashed.
            write_diagnostics_log(log_path, records, inputs=len(inputs))
        with Store.open(db_path) as store:
            pages = _make_generator(config, base, store).generate(
                output_dir,
                group_by=config.group_by,
                toctree_maxdepth=config.toctree_maxdepth,
                root_document=config.root_document,
            )
        succeeded = True
    finally:
        if not succeeded:
            db_path.unlink(missing_ok=True)

    written = [f"{config.root_document}.md", *(f"{stem}.md" for stem in pages)]
    deleted = prune_stale(output_dir, written)

    return BuildResult(
        output_dir=output_dir,
        pages=pages,
        db_path=db_path,
        db_is_temporary=True,
        symbol_count=result.symbol_count,
        reference_count=result.reference_count,
        file_count=result.file_count,
        diagnostics=result.diagnostics,
        diagnostic_records=records,
        diagnostic_counts=severity_counts(records),
        diagnostics_log=log_path,
        parsed=True,
        pages_written=sorted(written),
        pages_deleted=deleted,
    )


def _parse_status(
    cache: BuildCache,
    config: Config,
    parse_fp: str,
    ir_path: Path,
    log_path: Path | None,
) -> ParseStatus:
    """Decide how much of the cached parse this build may reuse.

    On top of the cache's own verdict this rejects reuse when something outside
    the input contents demands a fresh parse, and discards an IR that cannot be
    read at all. Called before any parsing, so an unusable IR is already gone by
    the time the build picks a path.
    """
    # No IR on disk yet means a full parse regardless of bookkeeping.
    status = cache.parse_status(parse_fp) if ir_path.is_file() else ParseStatus(current=False)
    if log_path is not None and not log_path.is_file():
        # A configured log that is not on disk — relocated to a new path,
        # deleted, or never written — has to be materialised, and only a
        # parse produces its contents (diagnostics live in neither the IR
        # nor the cache). Force one rather than nooping past it and leaving
        # the configured path empty. Costs one re-parse per relocation.
        status = ParseStatus(current=False)
    if config.warnings_as_errors:
        # A strict verdict has to cover every input, and only a full parse
        # produces diagnostics for every input. A cached build has none at
        # all, and an incremental one reports only the translation units it
        # re-parsed — so a warning in an untouched header would go unseen
        # and the build would pass while the tree is dirty. Force the full
        # parse rather than quietly narrowing what "clean" means. This is
        # why strict mode costs a full parse every run; leave it off for
        # the edit-rebuild loop and turn it on in CI.
        status = ParseStatus(current=False)
    if (status.current or status.stale_inputs is not None) and not _ir_is_readable(ir_path):
        # Both remaining paths read the cached IR: the noop shortcut renders
        # from it, the partial re-parse rewrites the stale TUs' rows inside it.
        # A truncated file (killed build, full disk) or one left by an
        # incompatible schema version would fail every later build with a raw
        # sqlite3 traceback until the user deleted the cache directory by hand.
        # Discard it and pay for one full parse instead — the same recovery the
        # bookkeeping database already performs. The rest of the cache is kept:
        # it describes the pages on disk, which are still good, so only the
        # pages the fresh parse actually changes are rewritten.
        ir_path.unlink(missing_ok=True)
        status = ParseStatus(current=False)
    return status


def _incremental_build(  # noqa: PLR0913
    config: Config,
    base: Path,
    inputs: list[str],
    output_dir: Path,
    cache_dir: Path,
    compile_db: _CompileDatabase | None = None,
) -> BuildResult:
    """Reuse the cached parse where possible and write only changed pages."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    ir_path = cache_dir / IR_NAME
    parse_fp = _parse_fingerprint(config, base, inputs, compile_db)
    render_fp = _render_fingerprint(config, base)
    options = _parse_options(config, base, compile_db)
    log_path = _diagnostics_log_path(config, base)

    with BuildCache.open(cache_dir) as cache:
        status = _parse_status(cache, config, parse_fp, ir_path, log_path)
        parsed = not status.current
        # Fully unchanged build: the parse came from cache (IR identical) and the
        # render config/templates are unchanged, so the output the last run wrote
        # is already on disk. Skip the store open and every Jinja render — the
        # dominant cost of a noop build — and replay the cached summary. The
        # outputs are verified first: a page deleted or edited since the last run
        # (e.g. a `git clean` of the output dir) falls through to the render,
        # which rewrites exactly the pages that no longer match.
        if not parsed and cache.render_is_current(render_fp) and _outputs_intact(output_dir, cache):
            return _noop_result(output_dir, ir_path, cache.render_summary())

        # The complete warning-severity-or-worse view as of the *previous* run,
        # read before anything below advances the parse (which invalidates this
        # same summary). A build that does not re-parse everything below must
        # carry these forward, or a project's diagnostics would only ever exist
        # for one run — see the module-level note on why the cache cannot just
        # noop past a dirty tree.
        previous_diagnostics = _diagnostics_from_json((cache.render_summary() or {}).get("diagnostics"))

        counts: _core.ParseResult | None = None
        records: list[Diagnostic] = []
        # Translation units actually re-parsed, or None for a full parse. Only
        # those units' diagnostics exist this run, so the log has to say so.
        reparsed: int | None = None
        partial_deps: dict[str, list[str]] | None = None
        # The diagnostics this ``BuildResult`` reports: unlike ``records`` (this
        # run's parse only, for the log), this always covers the whole project,
        # replaying whatever a full parse would show even when this run only
        # touched part of it — see issue #207.
        full_diagnostics: list[Diagnostic]
        # Read before libclang touches anything: the hashes recorded below come
        # from the parser's read of each file, so any file written at or after
        # this instant must not have its stat trusted as describing them.
        parse_started_ns = time.time_ns()
        if not status.current and status.stale_inputs is None:
            # Configuration changed or no per-TU map: rebuild the whole IR.
            counts = _parse_into(inputs, ir_path, options)
            records = _records(counts)
            full_diagnostics = warnings_or_worse(records)
        elif not status.current:
            # Only some inputs are stale: re-parse just those translation units
            # into the existing IR, leaving every other TU's rows in place.
            stale = [inp for inp in inputs if inp in status.stale_inputs]
            # Files only these units reached last time. Whichever of them the
            # re-parse no longer pulls in has left the build for good, and the
            # writer drops its IR rows — otherwise a header removed from an
            # include closure keeps rendering until the next full rebuild.
            dropped = cache.deps_only_from(stale)
            partial_deps, _diagnostics, records = _parse_tus_into(stale, ir_path, options, dropped)
            reparsed = len(stale)
            full_diagnostics = _carry_forward_diagnostics(previous_diagnostics, partial_deps, dropped, records)
        else:
            # Render-only rebuild: the parse is unchanged, so its diagnostics
            # are exactly what the previous run already established.
            full_diagnostics = previous_diagnostics
        # Only when libclang actually ran: a render-only rebuild (parse cached,
        # templates or output changed) has no diagnostics of its own, and
        # overwriting a good log with an empty one would report silence where
        # there were problems. Same reasoning as ``_noop_result``.
        written_log = log_path if parsed and log_path is not None else None
        if written_log is not None:
            write_diagnostics_log(written_log, records, inputs=len(inputs), partial=reparsed)

        with Store.open(ir_path) as store:
            snapshot = {f.path: (f.sha256, f.size_bytes) for f in store.files()}
            # Both record_* calls invalidate the previous render bookkeeping in
            # the same transaction that advances the parse, so a failure before
            # record_render below can never let the next run noop-skip rendering
            # against this new IR; a clean render re-establishes it at the end.
            if partial_deps is not None:
                cache.record_partial_parse(partial_deps, snapshot, parse_started_ns=parse_started_ns)
            elif parsed:
                cache.record_parse(parse_fp, snapshot, _tu_deps(counts), parse_started_ns=parse_started_ns)
            generator = _make_generator(config, base, store)
            rendered = _rendered_files(generator, config, base, cache=cache, render_fingerprint=render_fp)
            symbol_count = store.symbol_count()
            reference_count = store.reference_count()
            file_count = store.file_count()

        page_stems = [name[: -len(".md")] for name, _ in rendered[:-1]]
        written, deleted = _apply_outputs(output_dir, rendered, cache)

        symbol_count = counts.symbol_count if counts else symbol_count
        reference_count = counts.reference_count if counts else reference_count
        file_count = counts.file_count if counts else file_count
        cache.record_render(
            render_fp,
            {
                "symbol_count": symbol_count,
                "reference_count": reference_count,
                "file_count": file_count,
                "pages": page_stems,
                "diagnostics": _diagnostics_to_json(full_diagnostics),
            },
        )

    return BuildResult(
        output_dir=output_dir,
        pages=page_stems,
        db_path=ir_path,
        db_is_temporary=False,
        symbol_count=symbol_count,
        reference_count=reference_count,
        file_count=file_count,
        diagnostics=_diagnostic_texts(full_diagnostics),
        diagnostic_records=full_diagnostics,
        diagnostic_counts=severity_counts(full_diagnostics),
        diagnostics_log=written_log,
        parsed=parsed,
        pages_written=written,
        pages_deleted=deleted,
    )


def _noop_result(output_dir: Path, ir_path: Path, summary: dict[str, object] | None) -> BuildResult:
    """Build the :class:`BuildResult` for a fully cached (unrendered) build.

    No diagnostics log is written here — deliberately. Nothing was re-parsed, so
    truncating an existing log would delete accurate information and replace it
    with silence. Leaving the previous run's file (with its own ``generated``
    timestamp) in place and reporting ``diagnostics_log=None`` lets the caller
    tell "wrote it" from "left the old one alone".

    The in-process diagnostics are a different matter: ``result.diagnostics`` is
    what a Sphinx build re-emits as warnings, so replaying the last real parse's
    warning-severity-or-worse records here is what keeps a ``-W`` build failing
    on every cache-hit rebuild instead of just the first one (issue #207).
    """
    summary = summary or {}

    def count(key: str) -> int:
        value = summary.get(key)
        return value if isinstance(value, int) else 0

    pages = summary.get("pages")
    records = _diagnostics_from_json(summary.get("diagnostics"))
    return BuildResult(
        output_dir=output_dir,
        pages=[str(p) for p in pages] if isinstance(pages, list) else [],
        db_path=ir_path,
        db_is_temporary=False,
        symbol_count=count("symbol_count"),
        reference_count=count("reference_count"),
        file_count=count("file_count"),
        diagnostics=_diagnostic_texts(records),
        diagnostic_records=records,
        diagnostic_counts=severity_counts(records),
        parsed=False,
        pages_written=[],
        pages_deleted=[],
    )


def _ir_is_readable(ir_path: Path) -> bool:
    """Whether the cached IR can still be opened and queried by this core.

    ``False`` for a file that is not a database, is truncated, or carries a
    schema version this build cannot read — the cases a rebuild has to recover
    from rather than propagate. The row count is read on purpose: opening a
    SQLite file and checking its ``meta`` table touches only the first pages, so
    a file damaged past them would still look fine.
    """
    try:
        with Store.open(ir_path) as store:
            store.symbol_count()
    except (sqlite3.DatabaseError, StoreVersionError, OSError):
        return False
    return True


def _parse_into(inputs: list[str], ir_path: Path, options: _core.ParseOptions) -> _core.ParseResult:
    """Parse into a sibling temp DB, then atomically replace ``ir_path``.

    Used for a *full* rebuild. A fresh database is built next to the target and
    moved into place only on success; a failed parse leaves any previously cached
    IR untouched.
    """
    tmp = _new_temp_db(ir_path.parent)
    try:
        result = _core.parse_to_sqlite(inputs, str(tmp), options)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(ir_path)
    return result


def _tu_deps(result: _core.ParseResult | None) -> dict[str, list[str]]:
    """Extract the ``{input: [dependency, ...]}`` map from a parse result.

    The core hands the map over interned — a list of distinct dependency paths
    plus per-input index lists — because inputs share most of their include
    closure. Rebuilding it here keeps that sharing: every input's list holds
    references to the same path strings instead of a private copy each.

    Each ``result`` attribute is read exactly once: every access converts the
    whole underlying C++ vector into a fresh Python list.
    """
    if result is None:
        return {}
    paths = result.tu_dep_paths
    return {
        input_path: [paths[i] for i in ids] for input_path, ids in zip(result.tu_inputs, result.tu_dep_ids, strict=True)
    }


def _parse_tus_into(
    stale: list[str],
    ir_path: Path,
    options: _core.ParseOptions,
    dropped_candidates: list[str] | None = None,
) -> tuple[dict[str, list[str]], list[str], list[Diagnostic]]:
    """Re-parse the stale inputs, replacing only their rows, atomically.

    One batched writer call re-parses every stale translation unit (in parallel,
    like a full parse) and replaces just those units' rows, reusing every other
    TU's. Returns the fresh dependency map, the error-severity diagnostics and
    the full diagnostic records — the latter two covering the re-parsed units
    only, since nothing else was parsed.

    The stale set lands all-or-nothing, and the writer already guarantees that
    without a staging copy: every input is parsed before the IR is opened at
    all, a hard parse failure raises before the first write, and the row
    replacement itself runs in one ``BEGIN IMMEDIATE`` transaction that rolls
    back on any error or on a killed process. Copying the whole database first
    only added an O(project) read+write to every O(change) rebuild. A failure
    therefore leaves the IR (and the cache, which is only updated afterwards) as
    it was, forcing a clean rebuild next run.

    ``dropped_candidates`` lists the files the previous parse attributed only to
    ``stale`` (see :meth:`BuildCache.deps_only_from`); those the fresh parse no
    longer reaches are deleted from the IR by the same transaction.
    """
    if options.tu_batch == 0:
        # Auto batching: stale sets are usually far smaller than a cold build's
        # input list, so re-batch them at _INCREMENTAL_TU_BATCH to recover
        # thread-pool parallelism (a single cold-sized umbrella would re-parse
        # the whole set on one thread). An explicit user tu_batch is respected.
        # The caller never reuses ``options`` after this call, so mutating the
        # incremental path's copy cannot leak into a full rebuild.
        options.tu_batch = _INCREMENTAL_TU_BATCH
    result = _core.parse_tus_to_sqlite(stale, str(ir_path), options, dropped_candidates or [])
    return _tu_deps(result), result.diagnostics, _records(result)


def _stat_pair(path: Path) -> tuple[int | None, int | None]:
    """Return ``(st_mtime_ns, st_size)`` for ``path``, or ``(None, None)``."""
    try:
        stat = path.stat()
    except OSError:
        return None, None
    return stat.st_mtime_ns, stat.st_size


def _output_intact(target: Path, record: OutputRecord) -> bool:
    """Whether ``target`` still holds the content ``record`` describes.

    Checked with the same ``(mtime_ns, size_bytes)`` fast-path the input scan
    uses: an unchanged stat is trusted without reading the file; a moved stat
    falls back to hashing the content, so a touched-but-identical page is still
    recognised as intact. A missing or unreadable file is not intact.
    """
    try:
        stat = target.stat()
    except OSError:
        return False
    if (
        record.mtime_ns is not None
        and record.size_bytes is not None
        and stat.st_mtime_ns == record.mtime_ns
        and stat.st_size == record.size_bytes
    ):
        return True
    try:
        return file_sha256(target) == record.content_hash
    except OSError:
        return False


def _outputs_intact(output_dir: Path, cache: BuildCache) -> bool:
    """Whether every page of the last render is still intact on disk.

    Gates the noop shortcut: replaying the cached summary is only sound while
    the pages it describes actually exist with the content that was written.
    Touched-but-identical pages have their stat fast-path healed so the hash
    read is paid once, not on every later noop build. An empty output index is
    never intact — there is always at least the root document.
    """
    records = cache.outputs()
    if not records:
        return False
    healed: dict[str, tuple[int, int]] = {}
    for name, record in records.items():
        target = output_dir / name
        if not _output_intact(target, record):
            return False
        mtime_ns, size = _stat_pair(target)
        if mtime_ns is not None and size is not None and (mtime_ns, size) != (record.mtime_ns, record.size_bytes):
            healed[name] = (mtime_ns, size)
    if healed:
        cache.refresh_output_stats(healed)
    return True


def _apply_outputs(
    output_dir: Path,
    rendered: list[tuple[str, str]],
    cache: BuildCache,
) -> tuple[list[str], list[str]]:
    """Write changed pages, delete vanished ones, and refresh the cache index.

    Returns ``(written, deleted)`` filenames. A page is rewritten when its
    content hash differs from the cached one *or* the file on disk no longer
    matches what was written (deleted or hand-edited), so an unchanged build
    leaves every page untouched while a damaged output dir is repaired.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    previous = cache.outputs()
    new_index: dict[str, OutputRecord] = {}
    written: list[str] = []
    for name, text in rendered:
        content_hash = hash_text(text)
        target = output_dir / name
        prev = previous.get(name)
        if prev is None or prev.content_hash != content_hash or not _output_intact(target, prev):
            # newline="\n" is load-bearing, not cosmetic: content_hash is the
            # SHA-256 of `text`, while _output_intact re-checks a page by
            # hashing its *bytes*. Left to translate, Windows would write CRLF
            # and no page would ever hash back to its own record, so every
            # touched page would read as damaged and be rewritten.
            target.write_text(text, encoding="utf-8", newline="\n")
            written.append(name)
        new_index[name] = OutputRecord(content_hash, *_stat_pair(target))

    deleted: list[str] = []
    for name in previous:
        if name not in new_index:
            (output_dir / name).unlink(missing_ok=True)
            deleted.append(name)

    cache.record_outputs(new_index)
    # Keep the manifest in sync so a later switch to a stateless build prunes
    # these pages correctly. Written only when it changes: it lives inside the
    # Sphinx source directory, so touching it on an otherwise unchanged build is
    # enough to keep a srcdir watcher (sphinx-autobuild) rebuilding forever.
    write_if_changed(output_dir / MANIFEST_NAME, json.dumps(sorted(new_index), indent=2))
    return sorted(written), sorted(deleted)


def prune_stale(output_dir: Path, kept: list[str]) -> list[str]:
    """Delete pages this run did not write, then record the new manifest.

    Only files listed in the *previous* manifest are removed, so hand-written
    files that happen to share ``output_dir`` are never touched. Returns the
    filenames that were deleted.
    """
    deleted: list[str] = []
    manifest = output_dir / MANIFEST_NAME
    if manifest.exists():
        try:
            previous = json.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            previous = []
        for name in previous:
            if name not in kept:
                (output_dir / name).unlink(missing_ok=True)
                deleted.append(name)
    write_if_changed(manifest, json.dumps(sorted(kept), indent=2))
    return sorted(deleted)


__all__ = [
    "COMPILE_COMMANDS_NAME",
    "IR_NAME",
    "MANIFEST_NAME",
    "BuildResult",
    "CompileCommandsError",
    "build",
    "prune_stale",
    "resolve_compile_commands",
]

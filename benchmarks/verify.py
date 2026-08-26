#!/usr/bin/env python3
"""Verify ClangQuill's parse and Doxygen extraction across real C++ codebases.

The sibling :mod:`benchmark` driver asks how *fast* the two tools are, and
treats a non-zero exit as a data point. This one asks whether the extraction is
*correct*, and treats anything short of silence from ClangQuill as a failure. It
runs over the same projects — the TOML files under ``configs/``, shared through
:mod:`harness` — so "the fast path and the correct path see the same code" is a
fact rather than a claim.

Four checks per project, all of which must pass:

    parse       ``clangquill build --warnings-as-errors`` exits 0, i.e. libclang
                produced no diagnostic of warning severity or worse over the
                whole input set. The full diagnostic list is written to a log
                next to the report whatever the outcome.
    doxygen     ``doxygen`` ran over the identical file set and wrote XML. This
                is a precondition, not a verdict on Doxygen: its warnings are
                logged and counted in the report, and do not fail the run.
    extraction  every input file Doxygen extracted a documented entity from
                yielded a documented symbol in ClangQuill's IR too.
    isolation   re-parsing at ``--tu-batch 1`` yields the same symbols as the
                default umbrella batching, i.e. batching really is only an
                optimisation on this project's headers.

The extraction check is the reason both tools are run, and it is what this
driver is for: a parse can be clean and the output still be wrong, and a regression that
silently stops attaching doc comments to symbols shows up here as files Doxygen
documented and ClangQuill did not, and nowhere else. Comparing *per file*
rather than by raw symbol count is deliberate — the two tools model symbols
differently enough that an exact count comparison would be noise. The overall
documented-entity ratio is reported alongside it, and gates the run only for
configs that set ``min_documented_ratio``.

Doxygen's own warnings are not gated because they are not evidence about
ClangQuill. Real projects make Doxygen warn for reasons no generated Doxyfile
can fix — abseil's ``friend Type;`` and ``extern template`` declarations are
valid C++11 that Doxygen mis-parses as members to match, and Eigen's comments
reference an ``EXAMPLE_PATH`` and ``ALIASES`` that live in the project's own
Doxyfile. Gating on that would make the run red for facts about Doxygen.

Why the two remaining checks are hard failures with no baseline: a project that
cannot be parsed cleanly is telling you its config lacks include directories or
defines, which is fixable in ``configs/<name>.toml``. Recording the noise as
"expected" instead would make the whole run decorative.

The driver depends only on the standard library plus ``clangquill`` itself
(whose SQLite IR it reads); ``doxygen`` is required and the run fails without
it, since half the verification would otherwise be silently skipped.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from harness import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_WORK_DIR,
    HERE,
    RepoConfig,
    RepoContext,
    clangquill_build_argv,
    clangquill_version,
    clangquill_work,
    doxygen_argv,
    libclang_version,
    load_configs,
    machine_info,
    prepare_repo,
    tool_version,
    wipe,
    write_doxyfile,
)

DEFAULT_RESULTS_DIR = HERE / "verify-results"

# One doxygen diagnostic, as written to WARN_LOGFILE: a location, then the
# severity. Counting lines instead would inflate the number, since doxygen wraps
# a single message over its "Possible candidates:" list.
DOXYGEN_MESSAGE_RE = re.compile(r"^.+:\d+: (?:warning|error):", re.MULTILINE)

# What a Doxygen group identifier can look like. Deliberately permissive — the
# point is to catch prose that leaked into an `\ingroup` value, not to police
# naming.
GROUP_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:+-]*$")

# Commands whose argument is a verbatim block that clangquill parks in `custom`
# and no bundled template renders yet. Multi-word by nature, so they are not
# evidence of prose that went astray.
VERBATIM_COMMANDS = frozenset({"code", "verbatim", "dot", "msc"})

# Compound kinds whose *own* description is about a file rather than an API
# symbol. ClangQuill models no symbol for a file, so a `\file` comment must not
# count as something it failed to extract. Their members are still counted:
# free functions in a header are members of its `file` compound.
NON_SYMBOL_COMPOUND_KINDS = frozenset({"file", "dir", "page"})

# How many diagnostic lines to inline per failing project in the Markdown
# report. The full list is in the per-project logs the workflow uploads.
MAX_REPORTED_LINES = 20


@dataclass
class Check:
    """One named pass/fail check, with the detail lines explaining a failure."""

    name: str
    passed: bool
    summary: str
    detail: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Serialise the check to a JSON-friendly dict."""
        return {
            "name": self.name,
            "passed": self.passed,
            "summary": self.summary,
            "detail": self.detail,
        }


@dataclass
class Run:
    """The outcome of one external command."""

    exit_code: int
    log: Path

    @property
    def output(self) -> str:
        """The captured stdout+stderr, or "" if the log vanished."""
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def run_logged(argv: list[str], cwd: Path, log_path: Path) -> Run:
    """Run ``argv`` in ``cwd``, capturing stdout+stderr into ``log_path``.

    Output goes to a file rather than a pipe so a project that produces tens of
    thousands of diagnostics cannot fill a pipe buffer, and so the log survives
    as an artifact regardless of how the run ended. A non-zero exit is the
    signal this driver is looking for, so it is returned, never raised.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        proc = subprocess.run(argv, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT, check=False)
    return Run(exit_code=proc.returncode, log=log_path)


# --------------------------------------------------------------------------- #
# Reading what each tool extracted
# --------------------------------------------------------------------------- #
def _has_text(element: ET.Element | None) -> bool:
    """Whether a doxygen description element holds any non-whitespace text.

    ``briefdescription``/``detaileddescription`` are always present in the XML
    and simply empty when the entity is undocumented, so their existence proves
    nothing — only their content does. ``itertext`` covers descriptions whose
    text sits inside nested ``<para>``/markup elements, which is all of them.
    """
    return element is not None and any(text.strip() for text in element.itertext())


def _documented(element: ET.Element) -> bool:
    """Whether a doxygen ``compounddef``/``memberdef`` carries documentation."""
    return _has_text(element.find("briefdescription")) or _has_text(element.find("detaileddescription"))


def _location(element: ET.Element, root: Path) -> str | None:
    """Return an entity's source file, relative to ``root``, or ``None``.

    Doxygen writes ``location/@file`` with ``STRIP_FROM_PATH`` already applied,
    which the strict Doxyfile pins to the project root — so the attribute is
    normally *relative* to ``root`` and must be joined onto it rather than
    resolved against this process's working directory. (Resolving it against
    the CWD happens to work when the project under verification is this
    repository itself and silently yields nothing for every cloned one, which
    is exactly the kind of vacuous green this driver exists to prevent.)

    Entities outside ``root`` — a system header pulled in by an include — are
    dropped: they are not part of the project under verification, and the two
    tools resolve them to different paths anyway.
    """
    location = element.find("location")
    if location is None:
        return None
    raw = location.get("file")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return None


def _qualified_name(element: ET.Element) -> str:
    """Doxygen's fully qualified name for a compound or member, or ``""``."""
    # `<qualifiedname>` when Doxygen emits one, which is already exactly the
    # form ClangQuill stores. Not `<definition>`: that is the whole declaration
    # and Doxygen spaces out template brackets inside it, so the last token of
    # "... Eigen::MatrixBase< Derived >::diagonal" is ">::diagonal".
    qualified = element.findtext("qualifiedname")
    if qualified:
        return qualified.strip()
    return (element.findtext("compoundname") or "").strip()


def doxygen_extraction(xml_dir: Path, source_root: Path) -> tuple[dict[str, int], int, dict[str, set[str]]]:
    """Count Doxygen's documented entities per source file.

    Returns ``({relative path: documented entity count}, total, {path: names})``.
    Files with no documented entity are omitted, because the cross-check only
    asks about files Doxygen *did* find documentation in. The names ride along
    so a file can be cleared when what it documents lives, in ClangQuill's IR,
    under the header that *declares* it — see :func:`check_extraction`.
    """
    per_file: dict[str, int] = {}
    names: dict[str, set[str]] = {}
    total = 0
    for path in sorted(xml_dir.glob("*.xml")):
        if path.name in {"index.xml", "Doxyfile.xml"}:
            continue
        try:
            root = ET.parse(path).getroot()  # noqa: S314 - doxygen's own output
        except ET.ParseError:
            # A truncated compound file (doxygen killed mid-write) must not take
            # the whole comparison down with it; the parse/doxygen checks
            # already report the run that produced it.
            continue
        for compound in root.iter("compounddef"):
            if compound.get("kind") not in NON_SYMBOL_COMPOUND_KINDS and _documented(compound):
                rel = _location(compound, source_root)
                if rel is not None:
                    per_file[rel] = per_file.get(rel, 0) + 1
                    names.setdefault(rel, set()).add(_qualified_name(compound))
                    total += 1
        for member in root.iter("memberdef"):
            if _documented(member):
                rel = _location(member, source_root)
                if rel is not None:
                    per_file[rel] = per_file.get(rel, 0) + 1
                    names.setdefault(rel, set()).add(_qualified_name(member))
                    total += 1
    return per_file, total, names


def clangquill_extraction(ir_path: Path, source_root: Path) -> tuple[dict[str, int], int, set[str]]:
    """Count ClangQuill's documented symbols per source file, from the IR.

    Reads the SQLite IR the build left in its ``--cache-dir``, which already
    records ``is_documented`` and the owning file per symbol — no re-parse and
    no scraping of rendered Markdown. The third value is every documented
    symbol's qualified name regardless of which file it was attributed to.
    """
    from clangquill.store import Store  # noqa: PLC0415 - optional at import time, required here

    per_file: dict[str, int] = {}
    documented_names: set[str] = set()
    total = 0
    with Store.open(ir_path) as store:
        paths = {f.id: f.path for f in store.files()}
        for symbol in store.symbols():
            if not symbol.is_documented:
                continue
            documented_names.add(symbol.qualified_name)
            if symbol.file_id is None:
                continue
            raw = paths.get(symbol.file_id)
            if raw is None:
                continue
            try:
                rel = str(Path(raw).resolve().relative_to(source_root))
            except ValueError:
                continue
            per_file[rel] = per_file.get(rel, 0) + 1
            total += 1
    return per_file, total, documented_names


def symbol_rows(ir_path: Path) -> set[tuple]:
    """Return the identity of every symbol in an IR, order-insensitively.

    ``is_documented`` rides along in the tuple, so a symbol whose doc comment
    stops being attached shows up as a pair — once on each side of the
    comparison — rather than silently matching.
    """
    from clangquill.store import Store  # noqa: PLC0415 - optional at import time, required here

    with Store.open(ir_path) as store:
        return {(s.usr, s.qualified_name, s.kind, s.is_documented) for s in store.symbols()}


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #
def _tail(text: str, limit: int = MAX_REPORTED_LINES) -> list[str]:
    """Return the last ``limit`` non-empty lines of ``text``."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def check_clangquill(ctx: RepoContext, clangquill_cmd: list[str], logs: Path) -> tuple[Check, Run]:
    """Run ``clangquill build`` in strict mode and judge the exit code."""
    diagnostics_log = logs / "clangquill-diagnostics.log"
    argv = clangquill_build_argv(
        ctx,
        clangquill_cmd,
        output_dir=ctx.myst_out,
        extra=["--diagnostics-log", str(diagnostics_log), "--warnings-as-errors"],
    )
    run = run_logged(argv, ctx.source_dir, logs / "clangquill.log")
    if run.exit_code == 0:
        work = clangquill_work(run.output)
        summary = f"clean parse of {work.get('files', 0)} file(s) → {work.get('symbols', 0)} symbol(s)"
        return Check("parse", passed=True, summary=summary), run
    return (
        Check(
            "parse",
            passed=False,
            summary=f"clangquill build exited {run.exit_code}",
            detail=_tail(run.output),
        ),
        run,
    )


def check_doxygen(ctx: RepoContext, doxygen_cmd: list[str], logs: Path) -> tuple[Check, Path]:
    """Run Doxygen over the same file set to produce the reference XML.

    Doxygen is the yardstick the extraction check measures ClangQuill against,
    so what this asserts is that the yardstick exists: that Doxygen ran and
    wrote XML. Its warnings are counted into the summary and written to
    ``doxygen-warnings.log``, but they do not fail the run — see the module
    docstring for why. The XML directory is returned whatever the verdict, so
    the extraction check can still compare against whatever was written.
    """
    warn_log = logs / "doxygen-warnings.log"
    doxyfile = write_doxyfile(ctx, "xml", strict=True, warn_log=warn_log)
    run = run_logged(doxygen_argv(doxygen_cmd, doxyfile), ctx.source_dir, logs / "doxygen.log")
    xml_dir = ctx.doxygen_out("xml") / "xml"
    # The warn log holds the diagnostics; the run log holds whatever doxygen
    # printed around them. Prefer the former and fall back to the latter.
    warnings = warn_log.read_text(encoding="utf-8", errors="replace") if warn_log.is_file() else ""
    detail = _tail(warnings) if warnings else []
    version = tool_version([*doxygen_cmd, "--version"])
    if run.exit_code != 0:
        return (
            Check(
                "doxygen",
                passed=False,
                summary=f"doxygen exited {run.exit_code}",
                detail=detail or _tail(run.output),
            ),
            xml_dir,
        )
    count = len(DOXYGEN_MESSAGE_RE.findall(warnings))
    noted = "no warnings" if not count else f"{count} warning(s), not gating ({warn_log})"
    return Check("doxygen", passed=True, summary=f"{noted} (doxygen {version})"), xml_dir


def check_isolation(ctx: RepoContext, clangquill_cmd: list[str], logs: Path) -> Check:
    """Re-parse with ``--tu-batch 1`` and demand the same symbols.

    Umbrella batching is meant to be an optimisation: a batch shares one
    translation unit so the common ``#include`` closure is parsed once instead
    of once per input, and the extracted IR is supposed to be what fully
    isolated per-file parsing would have produced. That holds for self-contained
    headers and quietly fails for the rest, which see whatever preprocessor state
    their batch-mates left behind. Canonical input ordering makes the outcome
    reproducible; only this check says whether it is also *right*.

    Compared against the IR the parse check already built, so a project costs one
    extra parse rather than two. Diagnostics are counted into the summary but do
    not decide it: libclang reports a batched diagnostic through the synthetic
    umbrella main file, and the parser dedups per run, so the two logs are not
    comparable line for line even when the IR agrees.
    """
    batched_ir = ctx.cache_dir / "clangquill.sqlite"
    isolated_cache = ctx.bench_dir / "cache-isolated"
    wipe(isolated_cache)
    wipe(ctx.bench_dir / "myst-isolated")
    diagnostics_log = logs / "clangquill-isolated-diagnostics.log"
    argv = clangquill_build_argv(
        ctx,
        clangquill_cmd,
        output_dir=ctx.bench_dir / "myst-isolated",
        cache_dir=isolated_cache,
        extra=["--tu-batch", "1", "--diagnostics-log", str(diagnostics_log)],
    )
    run = run_logged(argv, ctx.source_dir, logs / "clangquill-isolated.log")
    isolated_ir = isolated_cache / "clangquill.sqlite"
    if not batched_ir.is_file() or not isolated_ir.is_file():
        missing = batched_ir if not batched_ir.is_file() else isolated_ir
        return Check(
            "isolation",
            passed=False,
            summary=f"no IR to compare at {missing} (isolated build exited {run.exit_code})",
            detail=_tail(run.output),
        )

    batched = symbol_rows(batched_ir)
    isolated = symbol_rows(isolated_ir)
    # A symbol that agrees on name, kind and documented but not on USR is not a
    # difference this project can act on: libclang renders a dependent template
    # argument differently depending on how much of the translation unit it has
    # seen, so abseil's FormatConvertImpl overloads come out with
    # ArgConvertResult<absl::FormatConversionCharSet::v> one way and
    # ArgConvertResult<524288> the other, and `<! X` versus `<!X`. Counted into
    # the summary, never gated on — the same treatment doxygen's warnings get.
    # The multiset intersection is what pairs those up; whatever is left over is
    # a symbol that really is present on one side only.
    batched_only = Counter(row[1:] for row in batched - isolated)
    isolated_only = Counter(row[1:] for row in isolated - batched)
    drift = sum((batched_only & isolated_only).values())
    missing_when_batched = isolated_only - batched_only
    missing_when_isolated = batched_only - isolated_only

    drift_note = "" if not drift else f"; {drift} symbol(s) differ only in libclang's USR spelling"
    if not missing_when_batched and not missing_when_isolated:
        return Check(
            "isolation",
            passed=True,
            summary=f"{len(batched)} symbol(s) identical under --tu-batch 1{drift_note}",
        )
    detail = [
        f"only when batched: {name} ({kind}, documented={doc})" for name, kind, doc in sorted(missing_when_isolated)
    ]
    detail += [
        f"only when isolated: {name} ({kind}, documented={doc})" for name, kind, doc in sorted(missing_when_batched)
    ]
    return Check(
        "isolation",
        passed=False,
        summary=(
            f"batching changes the IR: {sum(missing_when_isolated.values())} symbol(s) only when batched, "
            f"{sum(missing_when_batched.values())} only when isolated, "
            f"of {len(batched)} vs {len(isolated)}{drift_note}"
        ),
        detail=detail[:MAX_REPORTED_LINES],
    )


def check_comments(ctx: RepoContext) -> Check:
    r"""Assert that what ClangQuill marked documented actually says something.

    The extraction check counts *whether* a symbol is documented, never whether
    anything came out of its comment, so a parser bug that swallows a whole
    description into one command's argument leaves every check green. That is
    not hypothetical: it is how ``Eigen::operator<<`` came to carry a 400
    character ``relates`` field and no brief and no detail at all.

    Two invariants, both already held by abseil, clangquill and dune-gdt:

    * every documented symbol yields at least one renderable field. "Renderable"
      is read off :class:`CommentModel` rather than hardcoded, and excludes
      ``custom`` precisely because that is where a swallowed argument lands.
    * every group id looks like one. This is a tripwire rather than a proof: a
      swallowed *paragraph* always contains punctuation somewhere, so it fires,
      but individual swallowed words that happen to be valid identifiers slip
      through. It is here because ``\ingroup`` values are split on whitespace
      into ids, so one swallowed paragraph became 400 groups — and a page each.
    """
    from clangquill.comments import CommentModel  # noqa: PLC0415 - optional at import time
    from clangquill.store import Store  # noqa: PLC0415 - optional at import time

    ir_path = ctx.cache_dir / "clangquill.sqlite"
    if not ir_path.is_file():
        return Check("comments", passed=False, summary=f"no clangquill IR at {ir_path}")

    renderable = tuple(f.name for f in dataclasses.fields(CommentModel) if f.name != "custom")

    def swallowed(model: CommentModel) -> list[str]:
        """Return the commands holding prose when nothing renders, else ``[]``."""
        if any(getattr(model, name) for name in renderable):
            return []
        # `/** \ingroup Geometry_Module */` is a real thing to write: the symbol
        # is documented (it carries a comment) and has nothing to render, and
        # that is not a defect. A command argument holding a *sentence* is —
        # one token is a name or an id, several are prose that went astray.
        return sorted(
            name
            for name, values in model.custom.items()
            if name not in VERBATIM_COMMANDS and any(len(v.split()) > 1 for v in values)
        )

    gaps: list[str] = []
    documented = 0
    with Store.open(ir_path) as store:
        for symbol in store.symbols():
            if not symbol.is_documented:
                continue
            documented += 1
            model = store.comment(symbol.usr)
            if model is None:
                continue
            lost = swallowed(model)
            if lost:
                gaps.append(f"{symbol.qualified_name} — prose sits in {lost}")
        groups = store.groups()
        malformed = [g.id for g in groups if not GROUP_ID_RE.match(g.id)]

    if not gaps and not malformed:
        return Check(
            "comments",
            passed=True,
            summary=(f"no prose lost by {documented} documented symbol(s), {len(groups)} group id(s) well formed"),
        )
    detail = [f"renders nothing: {gap}" for gap in sorted(gaps)]
    detail += [f"group id from prose: {gid!r}" for gid in sorted(malformed)]
    return Check(
        "comments",
        passed=False,
        summary=(
            f"{len(gaps)} of {documented} documented symbol(s) hold prose nothing renders; "
            f"{len(malformed)} of {len(groups)} group id(s) malformed"
        ),
        detail=detail[:MAX_REPORTED_LINES],
    )


def check_extraction(ctx: RepoContext, xml_dir: Path) -> tuple[Check, dict]:
    """Compare what each tool documented, per source file.

    The gate is one-directional on purpose: a file Doxygen documented and
    ClangQuill did not is a gap in the tool under test, while the reverse
    (ClangQuill documenting something Doxygen's lexer skipped) is the outcome
    the project exists to produce. That asymmetry is also why the reported
    ratio can exceed 100 % — ClangQuill's IR carries documented private members,
    which Doxygen omits without ``EXTRACT_PRIVATE`` — so treat the ratio as a
    drift signal between runs, not as a score out of 100.
    """
    ir_path = ctx.cache_dir / "clangquill.sqlite"
    source_root = ctx.source_dir.resolve()
    stats: dict = {}
    if not xml_dir.is_dir():
        return Check("extraction", passed=False, summary=f"no doxygen XML at {xml_dir}"), stats
    if not ir_path.is_file():
        return Check("extraction", passed=False, summary=f"no clangquill IR at {ir_path}"), stats

    doxygen_files, doxygen_total, doxygen_names = doxygen_extraction(xml_dir, source_root)
    quill_files, quill_total, quill_names = clangquill_extraction(ir_path, source_root)
    # A file Doxygen documented and ClangQuill did not is only a gap when the
    # documentation went missing, not when it was filed elsewhere. C++ puts an
    # out-of-line definition in a different header from its declaration, and
    # Doxygen's `\fn` blocks document an entity from a third file again;
    # ClangQuill attributes both to the declaration. Eigen's Dot.h is the case
    # in point — it carries the prose for MatrixBase::dot, which ClangQuill
    # records, documented, against MatrixBase.h.
    missed = sorted(
        path
        for path in doxygen_files
        if path not in quill_files and not (doxygen_names.get(path, set()) <= quill_names)
    )
    ratio = (quill_total / doxygen_total) if doxygen_total else None
    stats = {
        "doxygen_documented": doxygen_total,
        "clangquill_documented": quill_total,
        "doxygen_files": len(doxygen_files),
        "clangquill_files": len(quill_files),
        "missed_files": missed,
        "ratio": ratio,
    }

    # Two different ways to end up with nothing to compare, and they are not the
    # same verdict. Doxygen dying before it writes any XML leaves its output
    # directory in place, so every test below is vacuously satisfied: no missed
    # files, no ratio, "ok over 0 file(s)" on top of a broken run. That is a
    # failure. A project whose sources carry no Doxygen-style comment at all —
    # abseil documents its 60 headers in plain ``//``, which Doxygen does not
    # read as documentation — produces a full set of XML with nothing documented
    # in it. There is no reference to measure against, but nothing went wrong,
    # and this driver's own gate is the parse check.
    if not doxygen_files:
        wrote_xml = any(path.name not in {"index.xml", "Doxyfile.xml"} for path in xml_dir.glob("*.xml"))
        return (
            Check(
                "extraction",
                passed=wrote_xml,
                summary=(
                    f"no doxygen-documented entity in this project, nothing to cross-check "
                    f"(clangquill documented {quill_total})"
                    if wrote_xml
                    else f"doxygen wrote no XML under {xml_dir} — nothing to compare against"
                ),
            ),
            stats,
        )

    floor = ctx.config.min_documented_ratio
    summary = (
        f"{quill_total} vs {doxygen_total} documented entities "
        f"({'n/a' if ratio is None else f'{ratio:.0%}'}) over {len(doxygen_files)} file(s)"
    )
    if missed:
        detail = [f"{path}: doxygen documented {doxygen_files[path]}, clangquill documented 0" for path in missed]
        return (
            Check(
                "extraction",
                passed=False,
                summary=f"{len(missed)} file(s) documented by doxygen and not by clangquill — {summary}",
                detail=detail[:MAX_REPORTED_LINES],
            ),
            stats,
        )
    if floor is not None and ratio is not None and ratio < floor:
        return (
            Check(
                "extraction",
                passed=False,
                summary=f"documented ratio {ratio:.0%} is below the configured floor of {floor:.0%} — {summary}",
            ),
            stats,
        )
    return Check("extraction", passed=True, summary=summary), stats


def verify_repo(cfg: RepoConfig, args: argparse.Namespace, tools: Tools) -> dict:
    """Run every check for one project and return its result record."""
    ctx = prepare_repo(cfg, args.work_dir, fresh_clone=args.fresh_clone)
    # A verification run always starts from nothing: strict mode re-parses the
    # whole input set anyway, and a stale IR or a half-written XML tree from a
    # previous run would silently change what the cross-check compares.
    for path in (ctx.myst_out, ctx.cache_dir, ctx.doxygen_out("xml"), ctx.logs):
        wipe(path)
    logs = ctx.logs
    logs.mkdir(parents=True, exist_ok=True)

    parse_check, _ = check_clangquill(ctx, tools.clangquill, logs)
    print(f"  parse: {'ok' if parse_check.passed else 'FAILED'} — {parse_check.summary}")
    doxygen_check, xml_dir = check_doxygen(ctx, tools.doxygen, logs)
    print(f"  doxygen: {'ok' if doxygen_check.passed else 'FAILED'} — {doxygen_check.summary}")
    extraction_check, stats = check_extraction(ctx, xml_dir)
    print(f"  extraction: {'ok' if extraction_check.passed else 'FAILED'} — {extraction_check.summary}")
    isolation_check = check_isolation(ctx, tools.clangquill, logs)
    print(f"  isolation: {'ok' if isolation_check.passed else 'FAILED'} — {isolation_check.summary}")
    comments_check = check_comments(ctx)
    print(f"  comments: {'ok' if comments_check.passed else 'FAILED'} — {comments_check.summary}")

    checks = [parse_check, doxygen_check, extraction_check, isolation_check, comments_check]
    return {
        "resolved_ref": ctx.resolved_ref,
        "commit": ctx.commit,
        "repo": cfg.repo,
        "passed": all(check.passed for check in checks),
        "checks": [check.as_dict() for check in checks],
        "extraction": stats,
        "logs": str(logs),
    }


# --------------------------------------------------------------------------- #
# Tooling
# --------------------------------------------------------------------------- #
@dataclass
class Tools:
    """Resolved command (argv prefix) for each external tool this driver runs."""

    clangquill: list[str]
    doxygen: list[str]


def environment_info(tools: Tools) -> dict:
    """Collect machine, Python and tool versions for the report."""
    doxygen = tool_version([*tools.doxygen, "--version"])
    return {
        **machine_info(),
        "tools": {
            "clangquill": clangquill_version(tools.clangquill),
            "doxygen": doxygen,
            "libclang": libclang_version(),
        },
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def render_markdown(payload: dict) -> str:
    """Render the run as a Markdown report (also used as the CI job summary)."""
    env = payload["environment"]
    tools = env.get("tools", {})
    results = payload["results"]
    passed = sum(1 for r in results.values() if r["passed"])
    lines = [
        "# ClangQuill extraction verification",
        "",
        f"**{passed} of {len(results)} project(s) passed.**",
        "",
        f"- Generated: `{env['timestamp']}`",
        f"- Machine: {env['platform']} · {env['cpu_count']} CPU · {env['ram_gb']} GB RAM",
        f"- clangquill: `{tools.get('clangquill') or 'n/a'}` · libclang `{tools.get('libclang') or 'n/a'}`",
        f"- doxygen: `{tools.get('doxygen') or 'n/a'}`",
        "",
    ]
    lines += [
        "| project | parse | doxygen | extraction | isolation | comments | documented (clangquill / doxygen) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, result in results.items():
        marks = {check["name"]: "✅" if check["passed"] else "❌" for check in result["checks"]}
        stats = result.get("extraction") or {}
        ratio = stats.get("ratio")
        counts = (
            f"{stats.get('clangquill_documented', 0)} / {stats.get('doxygen_documented', 0)}"
            f"{'' if ratio is None else f' ({ratio:.0%})'}"
        )
        lines.append(
            f"| {name} | {marks.get('parse', '—')} | {marks.get('doxygen', '—')} "
            f"| {marks.get('extraction', '—')} | {marks.get('isolation', '—')} "
            f"| {marks.get('comments', '—')} | {counts} |",
        )
    lines.append("")

    for name, result in results.items():
        failures = [check for check in result["checks"] if not check["passed"]]
        if not failures:
            continue
        lines += [f"## {name}", "", f"_ref: {result['resolved_ref']} · commit: `{result['commit'][:12]}`_", ""]
        for check in failures:
            lines += [f"**{check['name']}** — {check['summary']}", ""]
            if check["detail"]:
                lines += ["```text", *check["detail"], "```", ""]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the verification CLI arguments."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    p.add_argument("--repos", default="", help="Comma-separated repo names to verify (default: all configs).")
    p.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    p.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    p.add_argument("--clangquill", default="clangquill")
    p.add_argument("--doxygen", default="doxygen")
    p.add_argument("--fresh-clone", action="store_true", help="Re-clone even if a clone already exists.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Verify every selected project; return 0 only if all of them passed."""
    args = parse_args(argv)
    tools = Tools(clangquill=shlex.split(args.clangquill), doxygen=shlex.split(args.doxygen))

    for label, cmd in (("clangquill", tools.clangquill), ("doxygen", tools.doxygen)):
        if shutil.which(cmd[0]) is None:
            # Both halves are load-bearing: skipping one would report a green
            # run that verified half of what it claims to.
            print(f"Required tool {label!r} not found on PATH ({cmd[0]!r}).", file=sys.stderr)
            return 1

    configs = load_configs(args.config_dir, args.repos)
    if not configs:
        print(f"No configs found in {args.config_dir}", file=sys.stderr)
        return 1

    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = args.results_dir / f"{stamp}.json"
    md_path = args.results_dir / f"{stamp}.md"

    payload: dict = {"environment": environment_info(tools), "results": {}}

    def checkpoint() -> None:
        """Persist the results gathered so far, atomically.

        A full run parses four large projects; if it is killed partway, what
        already finished should survive as both data and a marker of how far it
        got. Writing through a temp file keeps each snapshot all-or-nothing.
        """
        for path, content in ((json_path, json.dumps(payload, indent=2)), (md_path, render_markdown(payload))):
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(path)

    for cfg in configs:
        print(f"\n=== {cfg.name} ===")
        try:
            payload["results"][cfg.name] = verify_repo(cfg, args, tools)
        except Exception as exc:
            # A project that could not be cloned, checked out or read is a
            # failure of the verification, not a project to quietly drop.
            print(f"  ERROR: {exc}", file=sys.stderr)
            payload["results"][cfg.name] = {
                "resolved_ref": "",
                "commit": "",
                "repo": cfg.repo,
                "passed": False,
                "checks": [Check("harness", passed=False, summary=str(exc)).as_dict()],
                "extraction": {},
                "logs": "",
            }
        checkpoint()

    markdown = render_markdown(payload)
    print("\n" + markdown)
    print(f"\nWrote {json_path}\n      {md_path}")
    failed = sorted(name for name, result in payload["results"].items() if not result["passed"])
    if failed:
        print(f"\nFAILED: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

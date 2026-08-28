#!/usr/bin/env python3
"""Benchmark ClangQuill against Doxygen across real C++ codebases.

The harness times the two stages of each tool's pipeline separately so the
numbers line up apples-to-apples:

    stage              ClangQuill                       Doxygen
    -----------------  -------------------------------  ----------------------
    parse -> structured  ``clangquill build`` (-> MyST)   ``doxygen`` GENERATE_XML
    render -> HTML       ``sphinx-build`` (MyST -> HTML)   ``doxygen`` GENERATE_HTML

For every ``(repo, stage)`` pair four *scenarios* are measured, each repeated
``--repeat`` times after ``--warmup`` un-recorded passes:

    cold              build from a clean state (fresh clangquill ``--cache-dir``)
    noop              immediately rebuild with no source change
    incremental       apply a small fixed patch to a widely-included header,
                      then rebuild
    incremental-leaf  apply the same patch to a leaf header instead, then
                      rebuild (skipped for configs without ``patch.leaf_files``)

ClangQuill's incremental cache (only active with ``--cache-dir``) makes the
``noop`` scenario cheap (the parse is skipped entirely); ``incremental``
re-parses only the translation units whose include closure contains the patched
file and rewrites only the changed pages. The two incremental scenarios bracket
the cache's behaviour: the configured ``incremental`` patch targets are
widely-included headers, so the stale set legitimately approaches the whole
module (a worst case), while ``incremental-leaf`` patches a header almost
nothing includes, showing the cost of the everyday local edit. Doxygen has no
parse cache and re-parses every run, which is exactly the contrast the
benchmark surfaces.

Design notes / benchmarking practices baked in:

* per-process CPU time and peak RSS are captured via :func:`os.wait4` (not just
  wall clock), so a noisy scheduler is visible in the data;
* git refs are pinned per repo for reproducibility (with a recorded fallback to
  the default branch if a pinned ref is missing);
* a warmup pass plus several repetitions are aggregated to min / median / mean /
  stddev, with the median reported as the headline figure;
* both tools are pointed at the same inputs, output to isolated directories that
  are reset between repetitions, and run quietly with logs captured to files;
* tool/toolchain versions, the resolved git commit, machine info and a
  timestamp are recorded alongside the numbers;
* a non-zero exit is recorded, not fatal: libclang emits diagnostics on heavy,
  dependency-rich repos (abseil/eigen) without their full include trees, and
  that is a real-world data point rather than a benchmark failure. The one
  exception is the ``clangquill-sphinx`` stage: a non-zero ``sphinx-build``
  exit means the build *died partway* (e.g. an exception mid-read), so its
  wall clock is not a render time — such samples are recorded but excluded
  from the statistics, and the report shows ``failed`` instead of a number.

The driver depends only on the standard library; the tools it *drives*
(``clangquill``, ``sphinx-build`` + ``myst-parser``, ``doxygen``) are detected
at runtime and skipped with a warning when absent.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from harness import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_WORK_DIR,
    HERE,
    RepoContext,
    clangquill_build_argv,
    clangquill_version,
    clangquill_work,
    dir_stats,
    doxygen_argv,
    human_bytes,
    libclang_version,
    load_configs,
    machine_info,
    prepare_repo,
    run_git,
    tool_version,
    wipe,
    write_doxyfile,
)

DEFAULT_RESULTS_DIR = HERE / "results"

# The four pipeline stages we know how to drive. ``clangquill-myst`` and
# ``doxygen-xml`` are the parse stages (structured intermediate); the other two
# are the human-facing HTML render stages.
ALL_STAGES = ("clangquill-myst", "clangquill-sphinx", "doxygen-xml", "doxygen-html")
ALL_SCENARIOS = ("cold", "noop", "incremental", "incremental-leaf")

# Stages whose timed command must complete to have produced its artifact: a
# non-zero exit there means the build aborted partway (Sphinx raising mid-read,
# say), so the recorded wall clock measures a *fraction* of the work and would
# both understate the true cost and hide follow-on effects (a crashed Sphinx
# never writes its environment pickle, so noop/incremental can never benefit
# from its incrementality). Samples from these stages with a non-zero exit are
# kept in the raw data but excluded from the statistics. The parse stages stay
# exempt: clangquill/doxygen exit non-zero on diagnostics while still doing all
# their work, which is a coverage data point, not an invalid time.
EXIT_INVALIDATES_SAMPLE = ("clangquill-sphinx",)

# A deterministic, identical-everywhere "fixed patch": a fully documented C++
# snippet appended to each configured target header. Appending (rather than
# shipping brittle unified diffs) is robust across pinned refs while still
# forcing a re-parse of exactly the touched file. The guard macro keeps repeated
# applications and odd include orders harmless.
#: The include guard the snippet below defines, and so the string that says a
#: patched file was never reverted.
PATCH_MARKER = "CLANGQUILL_BENCHMARK_PATCH_MARKER"

PATCH_SNIPPET = """

#ifndef CLANGQUILL_BENCHMARK_PATCH_MARKER
#define CLANGQUILL_BENCHMARK_PATCH_MARKER
namespace clangquill_benchmark_patch {
/// A synthetic symbol injected by the ClangQuill benchmark harness to measure
/// incremental rebuild cost. It is reverted after every measured run.
///
/// \\param value an arbitrary input.
/// \\returns \\p value unchanged.
inline int benchmark_marker(int value) { return value; }
}  // namespace clangquill_benchmark_patch
#endif
"""


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #
@dataclass
class Measurement:
    """The resource cost of a single subprocess run."""

    wall_s: float
    user_s: float
    sys_s: float
    maxrss_kb: int
    exit_code: int
    stdout: str

    def as_dict(self) -> dict:
        """Serialise the measurement to a JSON-friendly dict (RSS in MB)."""
        return {
            "wall_s": self.wall_s,
            "user_s": self.user_s,
            "sys_s": self.sys_s,
            "cpu_s": self.user_s + self.sys_s,
            "maxrss_mb": round(self.maxrss_kb / 1024, 1),
            "exit_code": self.exit_code,
        }


def measure(argv: list[str], cwd: Path, log_path: Path, env: dict | None = None) -> Measurement:
    """Run ``argv`` under ``cwd`` and capture wall time + per-process rusage.

    stdout/stderr are redirected to ``log_path`` (not a pipe) so there is no risk
    of a pipe-buffer deadlock and :func:`os.wait4` — not :meth:`Popen.communicate`
    — reaps the child, yielding the exact ``ru_utime``/``ru_stime``/``ru_maxrss``
    for *this* process. A non-zero exit is reported in the result, never raised.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    run_env = {**os.environ, **(env or {})}
    with log_path.open("wb") as log:
        start = time.perf_counter()
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=run_env,
        )
        _pid, status, rusage = os.wait4(proc.pid, 0)
        wall = time.perf_counter() - start
    # Mark the Popen as reaped so its destructor does not warn or wait again.
    exit_code = os.waitstatus_to_exitcode(status)
    proc.returncode = exit_code
    return Measurement(
        wall_s=wall,
        user_s=rusage.ru_utime,
        sys_s=rusage.ru_stime,
        maxrss_kb=_maxrss_kb(rusage.ru_maxrss),
        exit_code=exit_code,
        stdout=log_path.read_text(encoding="utf-8", errors="replace"),
    )


def _maxrss_kb(ru_maxrss: int) -> int:
    """Normalize ``rusage.ru_maxrss`` to kilobytes.

    Linux reports the peak RSS in kilobytes, but macOS/BSD report it in bytes;
    divide by 1024 there so the recorded figure is always KB.
    """
    if sys.platform == "darwin":
        return int(ru_maxrss) // 1024
    return int(ru_maxrss)


# --------------------------------------------------------------------------- #
# Repo preparation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Patched:
    """One patched file and the bytes it held before :func:`apply_patch`."""

    path: Path
    original: bytes


def apply_patch(ctx: RepoContext, files: list[str] | None = None) -> list[Patched]:
    """Append the fixed snippet to each target file (default: ``patch_files``).

    ``files`` selects the patch-target list — the widely-included
    ``patch_files`` for the ``incremental`` scenario or the ``leaf_patch_files``
    for ``incremental-leaf``. The pre-patch bytes ride along in the returned
    records so :func:`revert_patch` can put them back exactly.
    """
    # Resolved, and checked before the first write: two spellings of one file
    # (``header.h`` and ``dir/../header.h``) would patch it twice, and the second
    # record's "original" would already carry the snippet -- so restoring it last
    # would leave the patch in the tree, exactly the leak :func:`revert_patch`
    # exists to prevent. Raising mid-loop would leave the targets before it
    # patched and unrecorded, so nothing could put them back either.
    targets: list[Path] = []
    seen: set[Path] = set()
    for rel in ctx.config.patch_files if files is None else files:
        target = ctx.source_dir / rel
        if not target.is_file():
            print(f"  WARNING: patch target {rel!r} missing in {ctx.config.name}", file=sys.stderr)
            continue
        canonical = target.resolve()
        if canonical in seen:
            message = f"duplicate benchmark patch target in {ctx.config.name}: {rel}"
            raise ValueError(message)
        seen.add(canonical)
        targets.append(target)

    patched: list[Patched] = []
    for target in targets:
        original = target.read_bytes()
        with target.open("a", encoding="utf-8") as fh:
            fh.write(PATCH_SNIPPET)
        patched.append(Patched(target, original))
    return patched


def revert_patch(patched: list[Patched]) -> None:
    """Undo :func:`apply_patch` by restoring the bytes it recorded.

    Not ``git checkout``: that reverts to *HEAD*, so on a ``local`` run it would
    also discard whatever the operator had uncommitted in the patch target — and,
    because its exit code was never inspected, a checkout that failed (no git, a
    detached path, a locked index) left the patch in place. Every later scenario
    would then measure a tree that already carried the change, and the harness
    would report incremental timings for a rebuild with nothing to do.

    A failure to restore is raised rather than warned: from here on, every number
    the run would produce describes a tree nobody configured.
    """
    for entry in patched:
        entry.path.write_bytes(entry.original)
        if PATCH_MARKER in entry.original.decode("utf-8", errors="replace"):
            continue  # the file legitimately carried the marker before we ran
        if PATCH_MARKER in entry.path.read_text(encoding="utf-8", errors="replace"):
            message = f"benchmark patch survived its revert in {entry.path}"
            raise RuntimeError(message)


def reset_state(ctx: RepoContext) -> None:
    """Return the target to a clean pre-build state between repetitions.

    Generated artifacts (MyST, Sphinx, Doxygen, cache, logs) are removed, and a
    patch left behind by an earlier run that died mid-scenario is reverted with
    ``git checkout``. That last-resort cleanup fires only for a file that
    actually still carries :data:`PATCH_MARKER`, so a ``local`` run cannot
    discard the operator's own uncommitted work on a file that merely happens to
    be a patch target -- and a checkout that does not take is fatal, since every
    scenario after it would measure an already-patched tree.
    """
    for path in (ctx.sphinx_src, ctx.sphinx_out, ctx.cache_dir, ctx.doxygen_out("xml"), ctx.doxygen_out("html")):
        wipe(path)
    for rel in ctx.config.patch_files + ctx.config.leaf_patch_files:
        target = ctx.source_dir / rel
        if not target.is_file():
            continue
        if PATCH_MARKER not in target.read_text(encoding="utf-8", errors="replace"):
            continue
        print(f"  reverting a benchmark patch left behind in {rel}", file=sys.stderr)
        run_git(["checkout", "--", rel], ctx.source_dir, check=False)
        if PATCH_MARKER in target.read_text(encoding="utf-8", errors="replace"):
            message = f"a benchmark patch left behind in {rel} could not be reverted"
            raise RuntimeError(message)


# --------------------------------------------------------------------------- #
# Stage command builders
# --------------------------------------------------------------------------- #
def write_sphinx_scaffold(ctx: RepoContext) -> None:
    """Create a minimal MyST-only Sphinx project around the generated api/."""
    ctx.sphinx_src.mkdir(parents=True, exist_ok=True)
    (ctx.sphinx_src / "conf.py").write_text(
        # A deliberately minimal project: myst_parser only, no clangquill.sphinx_ext
        # (that would re-run the parse inside Sphinx and defeat stage isolation).
        'project = "clangquill benchmark"\n'
        'extensions = ["myst_parser"]\n'
        'html_theme = "alabaster"\n'
        'exclude_patterns = ["_build"]\n'
        'master_doc = "index"\n',
        encoding="utf-8",
    )
    (ctx.sphinx_src / "index.md").write_text(
        "# Benchmark API\n\n```{toctree}\n:maxdepth: 2\n\napi/index\n```\n",
        encoding="utf-8",
    )


def sphinx_argv(ctx: RepoContext, sphinx_cmd: list[str]) -> list[str]:
    """Build the ``sphinx-build`` argv for the render stage (quiet, serial).

    Serial on purpose: an incremental Sphinx build loads the full pickled
    environment into the parent (~5.4 GB resident for eigen's 1,700+
    cpp-domain-heavy pages), and every ``-j`` fork then dirties — hence
    copies — essentially the whole environment through Python refcounting,
    so each worker costs ~a full environment regardless of the worker count.
    Measured on a 4-core/16 GB CI runner: ``-j auto`` peaked at 15.8 GB and
    ``-j 2`` still peaked at 15.9 GB, both getting the VM resource-killed
    mid-benchmark; serial peaks at the parent alone and completes.
    """
    return [*sphinx_cmd, "-b", "html", "-q", str(ctx.sphinx_src), str(ctx.sphinx_out)]


# --------------------------------------------------------------------------- #
# Scenario execution per stage
# --------------------------------------------------------------------------- #
@dataclass
class Tools:
    """Resolved command (argv prefix) for each external tool the harness drives."""

    clangquill: list[str]
    sphinx: list[str]
    doxygen: list[str]


def _stage_log(ctx: RepoContext, stage: str, scenario: str, rep: int) -> Path:
    """Path of the captured log for one ``(stage, scenario, rep)`` run."""
    return ctx.logs / f"{stage}.{scenario}.{rep}.log"


def run_stage(
    ctx: RepoContext,
    stage: str,
    scenarios: list[str],
    tools: Tools,
    repeat: int,
    warmup: int,
) -> dict:
    """Measure ``stage`` for ``ctx`` across the requested scenarios.

    Returns a nested dict ``{scenario: {"samples": [...], "stats": {...}, ...}}``.
    Each repetition resets to a clean state, then runs the cold / noop /
    incremental / incremental-leaf sequence so the scenarios share one warmed
    clone but independent build state. ``incremental-leaf`` runs only for
    configs that name ``patch.leaf_files``; elsewhere it records no samples.
    """

    # ``cold_prep`` produces the timed command's *preconditions* from a clean
    # state; ``incr_prep`` refreshes them after the patch is applied. Both return
    # the timed (argv, cwd) plus a work-metric extractor and the output dir to
    # size up afterwards.
    def myst_cmd() -> tuple[list[str], Path]:
        """Return the timed command + cwd for the clangquill parse stage."""
        return clangquill_build_argv(ctx, tools.clangquill), ctx.source_dir

    def sphinx_cmd() -> tuple[list[str], Path]:
        """Return the timed command + cwd for the Sphinx render stage."""
        return sphinx_argv(ctx, tools.sphinx), ctx.sphinx_src

    def doxy_cmd(mode: str) -> tuple[list[str], Path]:
        """Return the timed command + cwd for the Doxygen ``mode`` stage."""
        return doxygen_argv(tools.doxygen, write_doxyfile(ctx, mode)), ctx.source_dir

    def untimed(argv: list[str], cwd: Path, tag: str) -> None:
        """Run ``argv`` for its side effects (e.g. produce MyST), discarding timing."""
        measure(argv, cwd, ctx.logs / f"_prep.{tag}.log")

    results: dict = {sc: {"samples": []} for sc in scenarios}
    total_passes = warmup + repeat

    for pass_idx in range(total_passes):
        recording = pass_idx >= warmup
        rep = pass_idx - warmup
        print(f"    pass {pass_idx + 1}/{total_passes}", flush=True)
        reset_state(ctx)

        # -- cold ----------------------------------------------------------- #
        if stage == "clangquill-sphinx":
            write_sphinx_scaffold(ctx)
            untimed(*myst_cmd(), tag=f"sphinx-cold-myst-{pass_idx}")  # produce MyST first
            argv, cwd = sphinx_cmd()
            out_dir = ctx.sphinx_out
        elif stage == "clangquill-myst":
            write_sphinx_scaffold(ctx)
            argv, cwd = myst_cmd()
            out_dir = ctx.myst_out
        else:  # doxygen-xml / doxygen-html
            mode = stage.split("-", 1)[1]
            argv, cwd = doxy_cmd(mode)
            out_dir = ctx.doxygen_out(mode)
        print("      cold", flush=True)
        cold = measure(argv, cwd, _stage_log(ctx, stage, "cold", rep))
        cold_output = dir_stats(out_dir)

        # -- noop ----------------------------------------------------------- #
        if stage == "clangquill-sphinx":
            untimed(*myst_cmd(), tag=f"sphinx-noop-myst-{pass_idx}")
        print("      noop", flush=True)
        noop = measure(argv, cwd, _stage_log(ctx, stage, "noop", rep)) if "noop" in scenarios else None
        noop_output = dir_stats(out_dir) if noop is not None else None

        # -- incremental ---------------------------------------------------- #
        incr = None
        incr_output = None
        if "incremental" in scenarios:
            print("      incremental", flush=True)
            patched = apply_patch(ctx)
            try:
                if stage == "clangquill-sphinx":
                    # Regenerate MyST so the render sees the change, then time render.
                    untimed(*myst_cmd(), tag=f"sphinx-incr-myst-{pass_idx}")
                incr = measure(argv, cwd, _stage_log(ctx, stage, "incremental", rep))
                incr_output = dir_stats(out_dir)
            finally:
                revert_patch(patched)

        # -- incremental-leaf ------------------------------------------------ #
        leaf = None
        leaf_output = None
        if "incremental-leaf" in scenarios and ctx.config.leaf_patch_files:
            print("      incremental-leaf", flush=True)
            # Reverting the wide patch above re-staled its targets, so re-sync
            # the cached state with an untimed rebuild first: this scenario must
            # measure the cost of the leaf edit alone. When the incremental
            # scenario didn't run this pass, nothing was patched or reverted and
            # the cache is still in sync from cold/noop, so no resync is needed.
            if "incremental" in scenarios:
                if stage.startswith("clangquill"):
                    untimed(*myst_cmd(), tag=f"leaf-resync-myst-{pass_idx}")
                if stage == "clangquill-sphinx":
                    untimed(argv, cwd, tag=f"leaf-resync-sphinx-{pass_idx}")
            patched = apply_patch(ctx, ctx.config.leaf_patch_files)
            try:
                if stage == "clangquill-sphinx":
                    untimed(*myst_cmd(), tag=f"sphinx-leaf-myst-{pass_idx}")
                leaf = measure(argv, cwd, _stage_log(ctx, stage, "incremental-leaf", rep))
                leaf_output = dir_stats(out_dir)
            finally:
                revert_patch(patched)

        if not recording:
            continue
        # Each scenario records the output snapshot taken right after it ran;
        # a single shared snapshot would mislabel cold/noop with the patched
        # incremental tree (the scenarios rebuild the same out_dir in turn).
        for scenario, m, output in (
            ("cold", cold, cold_output),
            ("noop", noop, noop_output),
            ("incremental", incr, incr_output),
            ("incremental-leaf", leaf, leaf_output),
        ):
            if scenario not in scenarios or m is None:
                continue
            sample = m.as_dict()
            if stage == "clangquill-myst":
                sample["work"] = clangquill_work(m.stdout)
            sample["output"] = output
            results[scenario]["samples"].append(sample)

    for scenario in scenarios:
        samples = results[scenario]["samples"]
        valid = [s for s in samples if stage not in EXIT_INVALIDATES_SAMPLE or not s.get("exit_code")]
        # Crashed-build samples stay in the raw data (their exit codes are
        # reported) but must not masquerade as timings; see EXIT_INVALIDATES_SAMPLE.
        results[scenario]["invalid_samples"] = len(samples) - len(valid)
        results[scenario]["stats"] = summarize([s["wall_s"] for s in valid])
    return results


def summarize(values: list[float]) -> dict:
    """Min / median / mean / stddev for a list of measurements."""
    if not values:
        return {}
    return {
        "n": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "stddev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


# --------------------------------------------------------------------------- #
# Environment / tool metadata
# --------------------------------------------------------------------------- #
def environment_info(tools: Tools) -> dict:
    """Collect machine, Python and tool/toolchain versions for the report."""
    return {
        **machine_info(),
        "tools": {
            "clangquill": clangquill_version(tools.clangquill),
            "sphinx": tool_version([*tools.sphinx, "--version"]),
            "doxygen": tool_version([*tools.doxygen, "--version"]),
            "libclang": libclang_version(),
        },
    }


def available_stages(requested: list[str], tools: Tools) -> list[str]:
    """Filter ``requested`` (already-validated) stages to those whose tool is installed.

    A stage whose backing tool is missing is skipped with a warning; unknown
    stage names are rejected earlier in :func:`main` so they never reach here.
    """
    have_clangquill = shutil.which(tools.clangquill[0]) is not None
    have_sphinx = shutil.which(tools.sphinx[0]) is not None and _have_myst()
    have_doxygen = shutil.which(tools.doxygen[0]) is not None
    keep: list[str] = []
    for stage in requested:
        if stage == "clangquill-myst" and not have_clangquill:
            print(f"  skipping {stage}: '{tools.clangquill[0]}' not found", file=sys.stderr)
            continue
        if stage == "clangquill-sphinx" and not (have_clangquill and have_sphinx):
            print(f"  skipping {stage}: needs clangquill + sphinx-build + myst-parser", file=sys.stderr)
            continue
        if stage.startswith("doxygen") and not have_doxygen:
            print(f"  skipping {stage}: '{tools.doxygen[0]}' not found", file=sys.stderr)
            continue
        keep.append(stage)
    return keep


def _have_myst() -> bool:
    """Return whether the ``myst_parser`` Sphinx extension is importable."""
    import importlib.util  # noqa: PLC0415

    return importlib.util.find_spec("myst_parser") is not None


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _median(results: dict, repo: str, stage: str, scenario: str) -> float | None:
    """Return the median wall time for one result cell, or None if absent."""
    try:
        return results[repo][stage][scenario]["stats"]["median"]
    except (KeyError, TypeError):
        return None


def _fmt(value: float | None) -> str:
    """Format a seconds value to 3 decimals, or an em dash when None."""
    return f"{value:.3f}" if value is not None else "—"


def _cell(results: dict, repo: str, stage: str, scenario: str) -> str:
    """Format one timing table cell.

    A median where one exists; ``failed`` when the scenario ran but every
    sample was an aborted build (see :data:`EXIT_INVALIDATES_SAMPLE`) — a
    number there would be the wall clock of a partial build; an em dash when
    the scenario was not measured at all.
    """
    median = _median(results, repo, stage, scenario)
    if median is not None:
        return f"{median:.3f}"
    try:
        data = results[repo][stage][scenario]
    except (KeyError, TypeError):
        return "—"
    if isinstance(data, dict) and data.get("invalid_samples"):
        return "failed"
    return "—"


def _cold_sample(results: dict, repo: str, stage: str) -> dict | None:
    """Return the first recorded cold sample for one ``(repo, stage)``, or None."""
    try:
        samples = results[repo][stage]["cold"]["samples"]
    except (KeyError, TypeError):
        return None
    return samples[0] if samples else None


def _work_lines(results: dict, repo: str) -> list[str]:
    """Build the per-repo "how much did each tool actually do" lines.

    Wall time alone can flatter a tool that extracted very little (e.g. when
    libclang lacks a repo's dependency tree), so the report pairs every timing
    table with the cold-run symbol/file/page counts and output sizes — and
    calls out non-zero exit codes — to make coverage gaps visible.
    """
    out: list[str] = []
    bits: list[str] = []
    myst = _cold_sample(results, repo, "clangquill-myst")
    if myst is not None:
        work = myst.get("work") or {}
        output = myst.get("output") or {}
        seg = "clangquill-myst: "
        if work:
            seg += (
                f"{work.get('symbols', '?')} symbols from {work.get('files', '?')} files "
                f"→ {work.get('pages_written', '?')} pages, "
            )
        seg += f"output {output.get('files', 0)} files · {human_bytes(output.get('bytes', 0))}"
        bits.append(seg)
    for stage in ("doxygen-xml", "doxygen-html"):
        sample = _cold_sample(results, repo, stage)
        if sample is not None:
            output = sample.get("output") or {}
            bits.append(f"{stage}: output {output.get('files', 0)} files · {human_bytes(output.get('bytes', 0))}")
    if bits:
        out.append("- **work (cold)** — " + "; ".join(bits))

    failures: list[str] = []
    for stage, stage_data in results.get(repo, {}).items():
        if not isinstance(stage_data, dict):
            continue
        for scenario, scenario_data in stage_data.items():
            if not isinstance(scenario_data, dict):
                continue
            codes = sorted({s["exit_code"] for s in scenario_data.get("samples", []) if s.get("exit_code")})
            if codes:
                failures.append(f"{stage}/{scenario}={','.join(map(str, codes))}")
    if failures:
        out.append(
            "- **non-zero exits** — "
            + "; ".join(failures)
            + " (diagnostics in logs; the work figures above show the achieved coverage)",
        )
    return out


def render_markdown(payload: dict) -> str:
    """Render the results ``payload`` as a Markdown report."""
    env = payload["environment"]
    lines: list[str] = ["# ClangQuill vs Doxygen benchmark", ""]
    lines.append(f"- Generated: `{env['timestamp']}`")
    lines.append(f"- Machine: {env['platform']} · {env['cpu_count']} CPU · {env['ram_gb']} GB RAM")
    lines.append(
        f"- clangquill: `{env['tools']['clangquill'] or 'n/a'}` · libclang `{env['tools']['libclang'] or 'n/a'}`",
    )
    lines.append(f"- doxygen: `{env['tools']['doxygen'] or 'n/a'}` · sphinx: `{env['tools']['sphinx'] or 'n/a'}`")
    lines.append(f"- repeat={payload['repeat']} warmup={payload['warmup']} (median wall-clock seconds)")
    lines.append("")

    scenarios = payload["scenarios"]
    for repo, repo_data in payload["results"].items():
        meta = payload["repos"][repo]
        lines.append(f"## {repo}")
        lines.append("")
        lines.append(f"_ref: {meta['resolved_ref']} · commit: `{meta['commit'][:12] or 'n/a'}`_")
        lines.append("")
        header = "| stage | " + " | ".join(scenarios) + " |"
        sep = "| --- | " + " | ".join("---" for _ in scenarios) + " |"
        lines += [header, sep]
        for stage in ALL_STAGES:
            if stage not in repo_data:
                continue
            cells = [_cell(payload["results"], repo, stage, sc) for sc in scenarios]
            lines.append(f"| {stage} | " + " | ".join(cells) + " |")
        lines.append("")

        # Derived comparisons, then the work/coverage context for the timings.
        lines += _derived_lines(payload["results"], repo, scenarios)
        lines += _work_lines(payload["results"], repo)
        lines.append("")
    return "\n".join(lines)


def _derived_lines(results: dict, repo: str, scenarios: list[str]) -> list[str]:
    """Build the per-repo derived parse/full-HTML comparison and cache-speedup lines."""
    out: list[str] = []
    for scenario in scenarios:
        myst = _median(results, repo, "clangquill-myst", scenario)
        sphinx = _median(results, repo, "clangquill-sphinx", scenario)
        dox_xml = _median(results, repo, "doxygen-xml", scenario)
        dox_html = _median(results, repo, "doxygen-html", scenario)
        bits: list[str] = []
        if myst is not None and myst > 0 and dox_xml is not None and dox_xml > 0:
            bits.append(f"parse: clangquill-myst {myst:.3f}s vs doxygen-xml {dox_xml:.3f}s ({dox_xml / myst:.2f}× )")
        if myst is not None and sphinx is not None and (myst + sphinx) > 0:
            full = myst + sphinx
            tail = ""
            if dox_html is not None and dox_html > 0:
                tail = f" vs doxygen-html {dox_html:.3f}s ({dox_html / full:.2f}× )"
            bits.append(f"full HTML: clangquill {full:.3f}s{tail}")
        if bits:
            out.append(f"- **{scenario}** — " + "; ".join(bits))
    # The headline incremental story.
    cold = _median(results, repo, "clangquill-myst", "cold")
    noop = _median(results, repo, "clangquill-myst", "noop")
    incr = _median(results, repo, "clangquill-myst", "incremental")
    leaf = _median(results, repo, "clangquill-myst", "incremental-leaf")
    if cold and noop:
        out.append(
            f"- **clangquill cache** — cold→noop {cold / noop:.1f}× faster"
            + (f", cold→incremental {cold / incr:.1f}× faster" if incr else "")
            + (f", cold→incremental-leaf {cold / leaf:.1f}× faster" if leaf else ""),
        )
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the benchmark CLI arguments."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    p.add_argument("--repos", default="", help="Comma-separated repo names to run (default: all configs).")
    p.add_argument("--scenarios", default=",".join(ALL_SCENARIOS))
    p.add_argument("--tools", default=",".join(ALL_STAGES), help="Comma-separated stages to run.")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    p.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    p.add_argument("--clangquill", default="clangquill")
    p.add_argument("--sphinx", default="sphinx-build")
    p.add_argument("--doxygen", default="doxygen")
    p.add_argument("--fresh-clone", action="store_true", help="Re-clone even if a clone already exists.")
    p.add_argument("--keep-clones", action="store_true", help="(default) keep clones for reuse; here for clarity.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark CLI: drive every repo/stage and write the reports."""
    args = parse_args(argv)
    tools = Tools(
        clangquill=shlex.split(args.clangquill),
        sphinx=shlex.split(args.sphinx),
        doxygen=shlex.split(args.doxygen),
    )
    requested_stages = [s.strip() for s in args.tools.split(",") if s.strip()]
    unknown = [s for s in requested_stages if s not in ALL_STAGES]
    if unknown:
        # A typo'd --tools value (easy to miss in a workflow_dispatch input) is a
        # CLI error, not something to silently skip into a partial report.
        print(f"Unknown stage(s): {', '.join(unknown)}. Valid stages: {', '.join(ALL_STAGES)}", file=sys.stderr)
        return 2
    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    stages = available_stages(requested_stages, tools)
    if not stages:
        print("No runnable stages — install clangquill / sphinx-build+myst-parser / doxygen.", file=sys.stderr)
        return 1

    configs = load_configs(args.config_dir, args.repos)
    if not configs:
        print(f"No configs found in {args.config_dir}", file=sys.stderr)
        return 1

    args.work_dir.mkdir(parents=True, exist_ok=True)
    env_info = environment_info(tools)

    payload: dict = {
        "environment": env_info,
        "repeat": args.repeat,
        "warmup": args.warmup,
        "scenarios": scenarios,
        "stages": stages,
        "repos": {},
        "results": {},
    }

    args.results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = args.results_dir / f"{stamp}.json"
    md_path = args.results_dir / f"{stamp}.md"

    def atomic_write(path: Path, content: str) -> None:
        """Write ``content`` to ``path`` via a sibling temp file and rename.

        A checkpoint exists to survive the process being killed; an in-place
        ``write_text`` truncates first, so a kill mid-write would corrupt the
        very file meant to outlive it. The rename makes each write all-or-nothing.
        """
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    def checkpoint(markdown: str | None = None) -> None:
        """Persist the (possibly partial) results gathered so far.

        A full run takes long enough that an interrupted or killed process is a
        real possibility; rewriting the report after every completed stage means
        whatever finished survives, both as data and as a marker of how far the
        run got before dying. ``markdown`` lets the final call reuse an
        already-rendered report instead of rendering twice.
        """
        atomic_write(json_path, json.dumps(payload, indent=2))
        atomic_write(md_path, markdown if markdown is not None else render_markdown(payload))

    for cfg in configs:
        print(f"\n=== {cfg.name} ===")
        ctx = prepare_repo(cfg, args.work_dir, fresh_clone=args.fresh_clone)
        payload["repos"][cfg.name] = {"resolved_ref": ctx.resolved_ref, "commit": ctx.commit, "repo": cfg.repo}
        payload["results"][cfg.name] = {}
        for stage in stages:
            print(f"  [{stage}] {scenarios} x{args.repeat} (+{args.warmup} warmup)")
            try:
                payload["results"][cfg.name][stage] = run_stage(
                    ctx,
                    stage,
                    scenarios,
                    tools,
                    args.repeat,
                    args.warmup,
                )
            except Exception as exc:
                print(f"    ERROR in {stage}: {exc}", file=sys.stderr)
                payload["results"][cfg.name][stage] = {"error": str(exc)}
            checkpoint()

    markdown = render_markdown(payload)
    checkpoint(markdown)

    print("\n" + markdown)
    print(f"\nWrote {json_path}\n      {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

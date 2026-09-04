# Benchmark methodology

**Status:** Living document · **Scope:** `benchmarks/` harness and the
published [Benchmarks](../benchmarks.md) page

This page explains *how* ClangQuill is benchmarked against Doxygen and, more
importantly, *why* the harness is built the way it is. The numbers themselves
live on the auto-generated [Benchmarks](../benchmarks.md) page; instructions for
running the harness locally live in
[`benchmarks/README.md`](https://github.com/renefritze/clangquill/tree/main/benchmarks).
This document is the rationale that ties the two together — read it to
understand what a given figure does and does not claim.

## Why benchmark against Doxygen at all

ClangQuill's reason to exist is a specific bet: that a libclang-backed parse
into a persistent IR, with an incremental cache, produces a better
documentation-build experience than re-parsing the whole source tree on every
run. Doxygen is the incumbent that bet is made against, so the benchmark exists
to keep that claim **honest and falsifiable**. A benchmark that only ever
flatters ClangQuill would be worthless; the harness is therefore designed to
surface the cases where ClangQuill *loses* (cold builds on small projects,
Sphinx render cost on a single huge page) just as clearly as the cases where it
wins (warm rebuilds, large dependency-light trees).

## What is measured

Each tool runs a two-stage pipeline, and the harness times the stages
**separately** so the comparison lines up parse-to-parse and render-to-render
rather than tool-to-tool:

| Stage | ClangQuill | Doxygen |
| --- | --- | --- |
| parse → structured intermediate | `clangquill build` (C++ → MyST Markdown) | `doxygen` with `GENERATE_XML` |
| render → human-facing HTML | `sphinx-build` (MyST → HTML) | `doxygen` with `GENERATE_HTML` |

Timing the stages apart matters because the two tools split the work
differently. Doxygen parses and renders in one process; ClangQuill parses in
its core and defers rendering to Sphinx. Reporting a single end-to-end number
would hide *where* the time goes — and the parse stage is exactly where the
incremental cache changes the picture. The published page therefore reports
each stage on its own line plus a derived **"full HTML"** figure
(`clangquill-myst + clangquill-sphinx` versus `doxygen-html`) so the
all-in cost is still visible.

## Scenarios: cold, noop, incremental

For every `(repo, stage)` pair the harness times three scenarios:

- **cold** — build from a clean state (a fresh ClangQuill `--cache-dir`). This
  is the worst case for ClangQuill: nothing is cached, so libclang parses
  everything and every page is written.
- **noop** — immediately rebuild with no source change. This is where the cache
  earns its keep: ClangQuill skips the parse entirely, while Doxygen, having no
  parse cache, re-parses the whole tree exactly as it did for the cold run.
- **incremental** — apply a small, fixed source edit, then rebuild. This models
  the everyday edit-rebuild loop.

The three scenarios exist precisely to *separate* the steady-state cost (which a
caching tool should win) from the first-run cost (which it may lose). Quoting
only one scenario would tell a misleadingly one-sided story in either direction.

## Keeping the comparison fair

A performance number is only meaningful if both tools did the same work under
the same conditions. The harness bakes in several deliberate fairness
constraints:

### Equal file sets

This is the subtlest trap and the one most likely to produce a flattering lie.
ClangQuill is pointed at explicit input globs; Doxygen is pointed at input
directories it walks itself. If the globs are single-level while Doxygen
recurses (its default), Doxygen silently processes many more files and looks
slower for reasons that have nothing to do with its parser.

Each benchmark config therefore keeps the two tools' file sets in lockstep:

- ClangQuill's `inputs` globs and Doxygen's `RECURSIVE` setting agree on whether
  the walk descends (a recursive `**/` glob pairs with `RECURSIVE = YES`; a
  single-level glob pairs with `doxygen_recursive = false`).
- `doxygen_file_patterns` pins Doxygen's `FILE_PATTERNS` to the same extensions
  the globs match, so neither tool lexes files the other never sees (e.g.
  Doxygen is kept off `.cpp` implementation files when ClangQuill only documents
  headers).

The published report also prints each tool's file and symbol counts (see
*Reporting work, not just time* below) so a coverage mismatch is visible at a
glance rather than hidden inside a wall-clock figure.

### All-CPU parallelism and graphviz-free

All three tools run with full hardware concurrency so no tool is artificially
handicapped by a serial constraint:

- **ClangQuill** defaults to `jobs = 0`, which resolves to
  `std::thread::hardware_concurrency()` at parse time.
- **Sphinx** is invoked with `-j auto`, which uses `multiprocessing.cpu_count()`
  worker processes for the render stage (requires Sphinx ≥ 1.7; not supported
  on Windows).
- **Doxygen** has `NUM_PROC_THREADS = 0` in the generated Doxyfile, which
  instructs Doxygen ≥ 1.9 to use all available CPU cores.

`HAVE_DOT = NO` is still set to disable call-graph rendering, keeping Doxygen
off the Graphviz dependency and ensuring neither tool spends time on diagrams.

Because all tools use the same all-CPU strategy, the published wall-clock ratios
reflect the tools' actual throughput on the available hardware rather than an
artificial serialisation. On a shared CI runner the numbers are still
*indicative* rather than absolute — see *Caveats* — but the parallelism playing
field is level.

### Same inputs, isolated outputs

Both tools read the same files; each writes to its own output directory, and
those directories are reset between repetitions so no run benefits from a
previous run's artifacts (except ClangQuill's `--cache-dir`, which *is* the
thing under test in the noop/incremental scenarios). Logs are captured to files
rather than pipes to avoid buffer-deadlock perturbation of the timing.

### Warmup, repetition, and a median headline

Each scenario runs one or more un-recorded **warmup** passes (to prime the OS
page cache, the filesystem, and any first-touch cost) followed by several
recorded **repetitions**. The harness aggregates min / median / mean / stddev
and reports the **median** as the headline, so a single noisy outlier does not
move the published number.

### Per-process resource metrics

Timing uses `os.wait4`, capturing per-process user+system CPU time and peak RSS
alongside wall-clock time. CPU time that diverges from wall time is a direct
signal of scheduler noise on the runner, which keeps a busy CI machine from
quietly corrupting the data.

### Pinned refs

Every external repo is pinned to a tag or commit, so the same code is measured
every run. A pinned ref also guarantees the incremental-edit target file exists,
which lets the "fixed patch" be a deterministic appended snippet rather than a
brittle unified diff.

## Reporting work, not just time

Wall-clock time alone can flatter a tool that did *less*. The clearest example:
on a dependency-heavy repo benchmarked without its full include tree, libclang
hits unresolved `#include`s and may extract far fewer symbols than Doxygen's
tolerant, non-compiling lexer — finishing faster precisely because it gave up
earlier.

To make that visible, each repo section of the report carries a **work (cold)**
line with the symbol, file, and page counts plus the on-disk output size for
both tools, and a **non-zero exits** line whenever a run returned a non-zero
exit code. A reader comparing two timings can therefore check whether the two
tools produced comparable output before trusting the ratio between them. The
underlying per-run JSON additionally records CPU time, peak RSS, and exit codes
for deeper analysis.

## Measuring the code under test

The benchmark is only useful if it measures the ClangQuill that is actually
being shipped. The publishing workflow therefore **builds ClangQuill from the
tagged checkout** rather than installing a previously released wheel — an
earlier setup that measured the last PyPI release would attribute stale numbers
to a new tag and report the wrong version string entirely.

The build links against LLVM 23 from `apt.llvm.org`, the same libclang major the
release wheels bundle, so parse coverage and timing are representative of what
users get; a fail-fast assertion rejects the run if the core silently linked an
older distro libclang. The harness records the resolved tool versions, libclang
version, git commit, machine info, and a timestamp next to every result, and
falls back to package metadata for the version string when the CLI predates the
`--version` flag.

## What the incremental cache does — and does not — do today

The benchmark is also a check on the project's own claims, so it is worth being
precise about the cache's current behaviour. With a `--cache-dir` configured:

- An **unchanged** rebuild (noop) skips the libclang parse entirely and rewrites
  no pages — this is the win the noop scenario exists to demonstrate.
- A **changed** rebuild (incremental) re-parses only the translation units whose
  transitive include closure contains a changed file, merging them into the
  existing IR (in parallel, like a full parse). Only the page *writes* for
  changed content happen; unchanged pages are not rewritten.

One caveat keeps the incremental scenario from tracking the noop cost on every
repo: the configured patch targets are deliberately *widely included* headers
(`string_view.h`, `Matrix.h`, …), so their stale set can legitimately approach
the whole module. That is correct invalidation, not a missing optimisation —
repos where the edit touches a leaf header see a stale set of one. The
methodology deliberately documents this so the published numbers are read
against what the implementation actually promises.

## Caveats

A headless CI harness cannot control everything, and a few honest limitations
are worth stating with the numbers:

- **Dependency-heavy repos** (Abseil, Eigen, dune-gdt) are template- and
  include-heavy. Without their full dependency trees installed, libclang emits
  diagnostics and may extract fewer symbols than Doxygen does. The harness
  records this (exit codes and symbol counts) and does **not** treat it as a
  failure — it is a real-world data point about parsing third-party code without
  its build environment. Extend a config's `include_dirs` to recover more
  symbols when the dependencies are available.
- **Environment noise** is left to the operator. For the most stable medians,
  pin the CPU governor to `performance`, run on an otherwise-idle and
  thermally-stable machine, and prefer more repetitions. The published CI
  numbers are run on shared GitHub-hosted runners and should be read as
  *indicative*, with the per-process CPU metrics available to spot a noisy run.

## Where the numbers live

- **Published results:** the [Benchmarks](../benchmarks.md) page, regenerated on
  each tagged release by the `benchmark` GitHub Actions workflow, which opens a
  pull request against `main` with the refreshed figures.
- **Running it yourself:**
  [`benchmarks/README.md`](https://github.com/renefritze/clangquill/tree/main/benchmarks)
  documents the `uv`-driven setup, the CLI flags, and the per-repo config
  schema.

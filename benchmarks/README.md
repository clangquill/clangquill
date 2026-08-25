# ClangQuill vs Doxygen harnesses

Two drivers run **ClangQuill** and **Doxygen** over the same real C++ codebases
and answer different questions about them:

| Driver | Question | Failure policy |
|--------|----------|----------------|
| `benchmark.py` | How *fast* is each tool? | A non-zero exit is recorded as data. |
| `verify.py` | Is the extraction *correct*? | Any ClangQuill diagnostic fails the run. |

Both read the same project list — the TOML files in `configs/` — and share
their configuration schema, cloning, and Doxyfile generation through
`harness.py`, so "the fast path and the correct path see the same code" is a
fact rather than a claim. The rest of this page covers the benchmark; the
verification driver has [its own section](#verifying-extraction).

## Benchmark

A standard-library-only harness that times **ClangQuill** against **Doxygen** on
real C++ codebases, with each tool's two pipeline stages measured separately.

| Stage | ClangQuill | Doxygen |
|-------|------------|---------|
| parse → structured intermediate | `clangquill build` (C++ → MyST Markdown) | `doxygen` with `GENERATE_XML` |
| render → human-facing HTML | `sphinx-build` (MyST → HTML) | `doxygen` with `GENERATE_HTML` |

The "full HTML" cost is `clangquill-myst + clangquill-sphinx` versus
`doxygen-html`, reported next to the per-stage numbers and the parse-only
comparison (`clangquill-myst` vs `doxygen-xml`).

## Scenarios

For every `(repo, stage)` pair, four scenarios are timed:

- **cold** — build from a clean state (a fresh clangquill `--cache-dir`).
- **noop** — immediately rebuild with no source change.
- **incremental** — apply a small fixed patch to a widely-included header,
  then rebuild (the invalidation worst case: the stale set can legitimately
  approach the whole module).
- **incremental-leaf** — apply the same patch to a *leaf* header (one almost
  nothing else includes), then rebuild — the cost of the everyday local edit.
  Skipped for configs without `patch.leaf_files`.

ClangQuill's incremental cache (only active with `--cache-dir`) makes *noop*
cheap — the parse is skipped entirely. Both incremental scenarios re-parse only
the translation units whose include closure contains the patched file and
rewrite only the changed pages; together they bracket the cache's behaviour
between the worst case and the everyday edit. Doxygen has no parse cache and
re-parses on every run, which is exactly the contrast the benchmark surfaces.

## Prerequisites

`benchmarks/` is a self-contained [uv](https://docs.astral.sh/uv/) project
(`pyproject.toml` + `uv.lock`). Its dependencies are the tools the harness
*drives* — the **published `clangquill` binary wheel** (which bundles libclang,
so there is no C++ build), plus `sphinx` and `myst-parser` for the render stage:

```bash
# From the repo root: install the locked toolchain into benchmarks/.venv
uv sync --frozen --project benchmarks
# doxygen is a system package, not a Python dependency:
sudo apt-get install doxygen   # or your platform's equivalent
```

The harness itself (`benchmark.py`) is standard-library only. Any tool that is
missing is **skipped with a warning** rather than failing the run, so you can
benchmark a subset (e.g. only the ClangQuill stages).

## Usage

Run through `uv` so the locked `clangquill`/`sphinx-build` are on `PATH`:

```bash
# Full comparison across every config in configs/
uv run --project benchmarks python benchmarks/benchmark.py

# Fast smoke test: this repo, parse stage only, one repetition
uv run --project benchmarks python benchmarks/benchmark.py \
    --repos clangquill --tools clangquill-myst --repeat 1 --warmup 0

# All four stages on one repo
uv run --project benchmarks python benchmarks/benchmark.py --repos clangquill \
    --tools clangquill-myst,clangquill-sphinx,doxygen-xml,doxygen-html
```

(If you already have `clangquill`/`sphinx-build`/`doxygen` on `PATH`, you can
drop the `uv run --project benchmarks` prefix and just run
`python benchmarks/benchmark.py`.)

Key flags (see `--help` for all): `--repos`, `--tools`, `--scenarios`,
`--repeat`, `--warmup`, `--work-dir`, `--results-dir`, `--fresh-clone`, and
`--clangquill/--sphinx/--doxygen` to override the tool commands.

## Output

Each run writes a timestamped pair into `benchmarks/results/` (gitignored):

- `<ts>.json` — full structured data: environment + tool/libclang versions,
  resolved git commit per repo, and per repo/stage/scenario samples (wall, CPU,
  peak RSS, exit code) plus work metrics (clangquill symbol/file/page counts and
  on-disk output file count + bytes for both tools).
- `<ts>.md` — a readable table per repo (median wall-clock seconds) with the
  derived full-HTML / parse comparisons and cache speedups; also echoed to stdout.

## Continuous benchmarking

The `benchmark` GitHub Actions workflow (`.github/workflows/benchmark.yml`) runs
on version tags (`v*`) and on manual dispatch. It installs the locked
sphinx/myst toolchain, **builds clangquill from the checked-out source** (against
LLVM 22 from apt.llvm.org, the same libclang major the release wheels bundle) so
the published numbers measure the commit they are labeled with, runs the
harness, appends the report to the job summary, uploads the raw results
as an artifact, and — on a tagged run, or a manual dispatch with the
`publish_docs` input enabled — regenerates
[`docs/benchmarks.md`](../docs/benchmarks.md) and opens a pull request against
`main` with the refreshed numbers (so the published docs track tagged releases,
and can be refreshed from `main` between releases when the numbers have moved).
By default it benchmarks every config under `configs/` (the external repos are
cloned blobless); a manual dispatch can narrow `--repos` or change `--tools` via
the workflow inputs.

## Verifying extraction

`verify.py` runs the same projects to ask whether the extraction is *correct*.
Nothing is timed. Three checks per project, all of which must pass:

- **parse** — `clangquill build --warnings-as-errors` exits 0: libclang
  produced no diagnostic of warning severity or worse over the whole input set.
  The complete diagnostic list is written to a log whatever the outcome.
- **doxygen** — `doxygen` ran over the identical file set and wrote XML. This
  is a precondition for the next check, not a verdict on Doxygen: its warnings
  are logged and counted into the report, and do not fail the run.
- **extraction** — every input file Doxygen extracted a documented entity from
  yielded a documented symbol in ClangQuill's IR too. A project Doxygen finds
  no documentation in at all — abseil comments its headers in plain `//`, which
  Doxygen does not read as documentation — has no reference to measure against;
  the check says so and passes, leaving **parse** as the project's real gate.

Doxygen's own warnings are not gated because they are not evidence about
ClangQuill. Real projects make Doxygen warn for reasons no generated Doxyfile
can fix: abseil's `friend Type;` and `extern template` declarations are valid
C++11 that Doxygen mis-parses as members to match, and Eigen's comments
reference an `EXAMPLE_PATH` and `ALIASES` that live in the project's own
Doxyfile, which this harness replaces. Gating on that would paint the run red
for facts about Doxygen.

The third check is why both tools are run at all. A parse can be perfectly clean
and the output still be wrong: a regression that stops attaching doc comments to
symbols shows up here as files Doxygen documented and ClangQuill did not, and
nowhere else. The comparison is *per file* rather than by raw symbol count,
because the two tools model symbols differently enough that an exact count
comparison would be noise — the overall documented-entity ratio is reported
next to it, and gates the run only for configs that set `min_documented_ratio`.
That ratio can exceed 100 %: ClangQuill's IR carries documented private members
which Doxygen omits without `EXTRACT_PRIVATE`, so read it as drift between runs
rather than as a score out of 100.

```bash
# Every config (needs doxygen on PATH; the IR is read through clangquill itself)
uv run --project benchmarks python benchmarks/verify.py

# One project
uv run --project benchmarks python benchmarks/verify.py --repos clangquill
```

Flags: `--repos`, `--config-dir`, `--work-dir`, `--results-dir`,
`--fresh-clone`, `--clangquill`, `--doxygen`. The exit status is 0 only when
every selected project passed every check. Results land in
`benchmarks/verify-results/` (gitignored) as a `<ts>.json` / `<ts>.md` pair,
with the per-project diagnostics and Doxygen warning logs under
`.work/_bench/<repo>/logs/`.

Unlike the benchmark, **there are no baselines and no ClangQuill diagnostic is
tolerated**. A dependency-heavy project fails until its config grows the
`include_dirs`, `defines` or `cmake_preset` its headers need, or narrows
`inputs` to a subset that parses — a recorded "expected noise" list would make
the whole run decorative. `dune-gdt` is the worked example: it only parses against a
configured build tree, so its config names a CMake preset and the harness runs
it, which on a cold vcpkg cache takes about an hour.
Strict mode also re-parses everything every run: a verdict on the whole input
set can only come from a parse of the whole input set, so the harness starts
each project from a wiped cache.

### Continuous verification

The `verify-extraction` workflow
(`.github/workflows/verify_extraction.yml`) runs weekly (Mondays, 05:00 UTC) and
on manual dispatch, with the same checkout-build steps as the benchmark so the
verdict is about the current commit rather than a published wheel. It appends
the report to the job summary and uploads the results plus every project's logs
as an artifact. Weekly rather than per-push because it clones and fully
re-parses four large upstream projects: the per-push correctness signal is the
test suite, and this catches the slower drift — an upstream project moving, a
libclang upgrade changing what parses, or a comment-extraction regression a
green parse would hide.

## Configs

One TOML file per target in `configs/` (`clangquill`, `dune-gdt`, `abseil`,
`eigen`), shared by both drivers. Schema:

```toml
name = "eigen"
repo = "https://gitlab.com/libeigen/eigen.git"   # omit + local=true to use this repo's tree
ref  = "3.4.0"                                    # pinned tag/commit (fallback to default branch)
local = false
std = "c++17"
include_dirs = ["."]            # -I dirs for clangquill, relative to repo root
defines = []                    # -D defines
compile_args = []               # extra clang args; "{llvm_includedir}" expands to the
                                # dir holding clang-c/Index.h (see clangquill.toml)
cmake_preset = ""               # when set, `cmake --preset <it>` runs before either driver
cmake_args = []                 # extra -D flags for that configure (see dune-gdt.toml)
inputs = ["Eigen/src/Core/**/*.h"]  # clangquill globs (relative to repo root)
exclude = []                        # repo-relative fnmatch patterns dropped from *both*
                                    # tools' input sets (see abseil.toml)
doxygen_input = ["Eigen/src/Core"]  # Doxygen INPUT dirs, same tree as the globs
doxygen_recursive = true            # Doxygen RECURSIVE; false when the glob is single-level
doxygen_file_patterns = ["*.h"]     # Doxygen FILE_PATTERNS; pin to the glob's extension
doxygen_extra = []                  # verbatim Doxyfile lines appended last, for a project
                                    # Doxygen cannot read on the shared settings
group_by = "namespace"              # clangquill --group-by (empty = tool default "symbol";
                                    # set "namespace" for namespace-rooted libraries so one
                                    # root namespace doesn't collapse onto a single huge page)
min_documented_ratio = 0.5          # verify.py only: fail when clangquill documents less than
                                    # this share of doxygen's documented entities (omit to
                                    # report the ratio without gating on it)
[patch]
files = ["Eigen/src/Core/Matrix.h"]      # widely-included incremental-edit targets
leaf_files = ["Eigen/src/Core/Stride.h"] # leaf targets for incremental-leaf (optional)
```

The "fixed patch" is a constant, documented C++ snippet appended to each
`patch.files` (or `patch.leaf_files`) target (identical across repos) and
reverted with `git checkout` after each measured run. Pinning `ref` guarantees
the file exists, making the edit deterministic without shipping brittle diffs.

## Benchmarking practices baked in

- **Pinned refs** per repo for reproducibility (with a recorded fallback to the
  default branch if a pinned ref is missing).
- **Warmup + repetitions**: `--warmup` un-recorded passes prime the OS page
  cache / git / disk; `--repeat` recorded passes are aggregated to
  min/median/mean/stddev, with the **median** as the headline.
- **Per-process metrics** via `os.wait4`: CPU user+sys time and peak RSS, not
  wall clock alone, so scheduler noise is visible.
- **Fair inputs**: both tools are pointed at the same files — the clangquill
  globs and Doxygen's `RECURSIVE`/`FILE_PATTERNS` are kept in lockstep per
  config so neither tool processes files the other does not; outputs go to
  isolated directories reset between repetitions; tools run quietly with logs
  captured under `.work/_bench/<repo>/logs/`.
- **Work metrics in the report**: each repo section pairs the timings with the
  cold-run symbol/file/page counts and output sizes for both tools (plus any
  non-zero exit codes), so a fast run that extracted little is visible as such.
- **Crashed renders are not timings**: a non-zero `sphinx-build` exit means the
  build died partway, so its wall clock measures a fraction of the work. Those
  samples are kept in the raw JSON but excluded from the statistics, and the
  report cell shows `failed` instead of a number. (Parse-stage non-zero exits
  still record normally — there they signal diagnostics, not an aborted build.)
- **All cores for both tools & graphviz-free**: Doxygen runs with
  `NUM_PROC_THREADS = 0` (all available CPUs) and `HAVE_DOT = NO`, matching
  ClangQuill's default `jobs = 0` (auto-detected CPU count) parallel parse.
  `sphinx-build` runs *serial*: during incremental builds each `-j` fork
  copies essentially the parent's whole loaded environment (~5 GB for eigen's
  cpp-domain-heavy pages) through refcount dirtying, so any worker count >= 2
  exhausted a 4-core/16 GB runner's memory and got the VM killed (measured
  15.8 GB at `-j auto`, 15.9 GB at `-j 2`). Serial peaks at the parent alone.
  The parse-side comparison is unaffected; the render column measures the
  memory-safe configuration for this hardware class.
- **Recorded provenance**: tool + libclang versions, resolved commit, machine
  info and timestamp are stored with the numbers.

### Caveats

- **abseil / eigen** are template- and dependency-heavy. Without their full
  include trees, libclang emits diagnostics and may extract fewer symbols than
  Doxygen's tolerant, non-compiling lexer. The *benchmark* records this (exit
  codes, symbol counts) and does **not** treat it as a failure; `verify.py`
  does, deliberately. Both are red today: abseil's glob pulls in gtest-dependent
  test helpers, and eigen's `Eigen/src/Core/*.h` are not standalone translation
  units — they expect to be reached through the `Eigen/Core` umbrella. Extend
  each config's `include_dirs` if you have the dependencies available.
- **dune-gdt** used to be in that list and no longer is: its config names a
  CMake preset, so the harness configures the project first and parses against
  the real build tree. That is the pattern to copy for a project whose headers
  cannot resolve from a bare checkout.
- Things this headless harness cannot control are left to the operator: pin the
  CPU governor to `performance`, run on an otherwise-idle, thermally-stable
  machine, and prefer more `--repeat` passes for stable medians.

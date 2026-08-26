#!/usr/bin/env python3
"""Shared machinery for the drivers under ``benchmarks/``.

Two drivers sit on top of this module and answer different questions about the
same set of projects (the TOML files under ``configs/``):

* ``benchmark.py`` — *how fast* are ClangQuill and Doxygen?
* ``verify.py``    — *is the extraction correct*, with warnings as errors?

Everything they have in common lives here: the per-project configuration
schema, cloning and pinning the sources, generating a Doxyfile that keeps both
tools on the identical file set, and the version/machine metadata both reports
record. Keeping it in one place is what makes "the same projects" a fact rather
than a claim — neither driver has its own copy of the project list, the clone
logic, or the Doxygen input rules.

Standard library only, like the drivers themselves.
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DEFAULT_CONFIG_DIR = HERE / "configs"
DEFAULT_WORK_DIR = HERE / ".work"


# --------------------------------------------------------------------------- #
# Per-project configuration
# --------------------------------------------------------------------------- #
@dataclass
class RepoConfig:
    """One target project, loaded from a TOML file in ``configs/``."""

    name: str
    repo: str = ""
    ref: str = ""
    local: bool = False
    std: str = "c++20"
    include_dirs: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    compile_args: list[str] = field(default_factory=list)
    # A project whose headers only resolve against a configured build tree names
    # its CMake preset here; :func:`cmake_configure` runs it before either driver
    # parses, and ``include_dirs`` may then point into the preset's binary dir.
    # ``cmake_args`` carries the project-specific ``-D`` flags that configure
    # needs, so this module stays free of per-project knowledge.
    cmake_preset: str = ""
    cmake_args: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    # Repo-relative fnmatch patterns dropped from the input set on *both* sides:
    # the harness expands ``inputs`` itself and passes explicit paths to
    # clangquill, and writes the same patterns into the Doxyfile's
    # EXCLUDE_PATTERNS. One knob rather than two is what keeps the file sets
    # identical. For a header the project itself does not ship as API — a
    # test helper needing gtest, say — not for hiding a file that fails to
    # parse for a reason worth fixing.
    exclude: list[str] = field(default_factory=list)
    doxygen_input: list[str] = field(default_factory=list)
    # Workload parity with the clangquill ``inputs`` globs: ``doxygen_recursive``
    # mirrors whether the glob descends (``**``), and ``doxygen_file_patterns``
    # pins Doxygen's FILE_PATTERNS to the same extensions, so both tools always
    # process the identical file set.
    doxygen_recursive: bool = True
    doxygen_file_patterns: list[str] = field(default_factory=list)
    # Verbatim Doxyfile lines appended last, so they win over the generated
    # ones. The escape hatch for a project Doxygen cannot read on the shared
    # settings — not a place to re-tune the input set, which both tools share.
    doxygen_extra: list[str] = field(default_factory=list)
    # Page partitioning passed to ``clangquill build --group-by``. Empty keeps
    # the tool default (``symbol``). Namespace-rooted libraries should set
    # ``namespace`` (or ``class``): with the default, a single root namespace
    # collapses the whole subtree onto one page — on eigen, one page held 84 %
    # of the output bytes, dominating the render, serialising Sphinx's read
    # phase, and being re-rendered on every symbol change.
    group_by: str = ""
    patch_files: list[str] = field(default_factory=list)
    # Leaf-header counterpart of ``patch_files`` for the ``incremental-leaf``
    # scenario: headers (almost) nothing else includes, so the stale set is a
    # handful of translation units instead of most of the module. Empty list =
    # the scenario is skipped for this config.
    leaf_patch_files: list[str] = field(default_factory=list)
    # verify.py only: the smallest share of Doxygen's documented entities
    # ClangQuill may extract before the coarse ratio check fails. ``None``
    # reports the ratio without gating on it — the per-file check below is the
    # hard gate, and this exists for the slower drift a per-file check cannot
    # see (many symbols lost from files that still yield one).
    min_documented_ratio: float | None = None

    @classmethod
    def from_toml(cls, path: Path) -> RepoConfig:
        """Load a :class:`RepoConfig` from the TOML file at ``path``."""
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        patch = data.get("patch", {}) or {}
        ratio = data.get("min_documented_ratio")
        return cls(
            name=data.get("name", path.stem),
            repo=data.get("repo", ""),
            ref=data.get("ref", ""),
            local=bool(data.get("local", False)),
            std=data.get("std", "c++20"),
            include_dirs=list(data.get("include_dirs", [])),
            defines=list(data.get("defines", [])),
            compile_args=list(data.get("compile_args", [])),
            cmake_preset=data.get("cmake_preset", ""),
            cmake_args=list(data.get("cmake_args", [])),
            inputs=list(data.get("inputs", [])),
            exclude=list(data.get("exclude", [])),
            doxygen_input=list(data.get("doxygen_input", [])),
            doxygen_recursive=bool(data.get("doxygen_recursive", True)),
            doxygen_file_patterns=list(data.get("doxygen_file_patterns", [])),
            doxygen_extra=list(data.get("doxygen_extra", [])),
            group_by=data.get("group_by", ""),
            patch_files=list(patch.get("files", [])),
            leaf_patch_files=list(patch.get("leaf_files", [])),
            min_documented_ratio=float(ratio) if ratio is not None else None,
        )


def load_configs(config_dir: Path, repos: str) -> list[RepoConfig]:
    """Load TOML configs from ``config_dir``, filtered to ``repos`` when given.

    The directory *is* the project list: adding a TOML file adds the project to
    both drivers at once.
    """
    wanted = {r.strip() for r in repos.split(",") if r.strip()}
    configs: list[RepoConfig] = []
    for path in sorted(config_dir.glob("*.toml")):
        cfg = RepoConfig.from_toml(path)
        if wanted and cfg.name not in wanted:
            continue
        configs.append(cfg)
    return configs


# --------------------------------------------------------------------------- #
# Small filesystem / git helpers
# --------------------------------------------------------------------------- #
def run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run ``git args`` in ``cwd``, capturing output as text."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def wipe(path: Path) -> None:
    """Remove ``path`` (file or directory tree) if it exists."""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()


def dir_stats(path: Path) -> dict:
    """Return ``{files, bytes}`` for everything under ``path`` (recursively)."""
    files = 0
    total = 0
    if path.is_dir():
        for p in path.rglob("*"):
            if p.is_file():
                files += 1
                total += p.stat().st_size
    return {"files": files, "bytes": total}


# --------------------------------------------------------------------------- #
# Repo preparation
# --------------------------------------------------------------------------- #
@dataclass
class RepoContext:
    """Resolved on-disk locations and git metadata for one target."""

    config: RepoConfig
    source_dir: Path  # where the C++ sources live (clone or local working tree)
    bench_dir: Path  # scratch space for outputs/caches/logs (never the source)
    resolved_ref: str
    commit: str

    @property
    def sphinx_src(self) -> Path:
        """Sphinx source dir for the render stage (holds conf.py + index.md)."""
        return self.bench_dir / "sphinx_src"

    @property
    def myst_out(self) -> Path:
        """MyST output dir, placed inside the Sphinx srcdir under api/."""
        return self.sphinx_src / "api"

    @property
    def sphinx_out(self) -> Path:
        """Sphinx HTML output dir (also holds the ``.doctrees`` cache)."""
        return self.bench_dir / "sphinx_out"

    @property
    def cache_dir(self) -> Path:
        """Incremental clangquill ``--cache-dir`` for this target."""
        return self.bench_dir / "cache"

    def doxygen_out(self, mode: str) -> Path:
        """Doxygen output dir for ``mode`` ("xml" or "html")."""
        return self.bench_dir / f"doxygen-{mode}"

    @property
    def logs(self) -> Path:
        """Directory holding captured per-run stdout/stderr logs."""
        return self.bench_dir / "logs"


def prepare_repo(cfg: RepoConfig, work_dir: Path, *, fresh_clone: bool) -> RepoContext:
    """Clone (or locate) ``cfg`` and resolve its pinned ref to a commit."""
    bench_dir = work_dir / "_bench" / cfg.name
    bench_dir.mkdir(parents=True, exist_ok=True)

    if cfg.local:
        source = REPO_ROOT
        commit = run_git(["rev-parse", "HEAD"], source, check=False).stdout.strip()
        ctx = RepoContext(cfg, source, bench_dir, resolved_ref="(local working tree)", commit=commit)
        cmake_configure(ctx)
        return ctx

    source = work_dir / cfg.name
    if fresh_clone:
        wipe(source)
    if not source.exists():
        print(f"  cloning {cfg.repo} -> {source}")
        run_git(["clone", "--filter=blob:none", cfg.repo, str(source)], work_dir)

    resolved_ref = cfg.ref
    if cfg.ref:
        checkout = run_git(["checkout", "--force", cfg.ref], source, check=False)
        if checkout.returncode != 0:
            print(f"  WARNING: ref {cfg.ref!r} not found for {cfg.name}; using default branch", file=sys.stderr)
            # Actually move HEAD to the remote default; a failed checkout leaves
            # the worktree where it was, which on a reused clone could be a
            # previously benchmarked ref and diverge from the recorded label.
            run_git(["checkout", "--force", "origin/HEAD"], source, check=False)
            resolved_ref = "(default branch; pinned ref missing)"
    else:
        resolved_ref = "(default branch)"
    commit = run_git(["rev-parse", "HEAD"], source, check=False).stdout.strip()
    ctx = RepoContext(cfg, source, bench_dir, resolved_ref=resolved_ref, commit=commit)
    cmake_configure(ctx)
    return ctx


def cmake_configure(ctx: RepoContext) -> None:
    """Run ``cmake --preset`` for a project that declares one.

    A DUNE-style project's headers do not resolve against the checkout alone:
    the dependency stack arrives through the preset's package manager, and the
    generated ``config.h`` only exists once configure has completed. Running it
    here means both drivers see the same tree, and that ``include_dirs``
    pointing into the binary dir are not a promise the harness leaves unkept.

    Configure is re-run on every invocation rather than skipped when the binary
    dir looks populated: CMake already knows what is stale, and a cached "looks
    configured" guess is how a run ends up parsing against a half-built tree.
    """
    cfg = ctx.config
    if not cfg.cmake_preset:
        return
    argv = ["cmake", "--preset", cfg.cmake_preset, *cfg.cmake_args]
    print(f"  cmake --preset {cfg.cmake_preset} (this can take a long time on a cold dependency cache)")
    run = subprocess.run(argv, cwd=str(ctx.source_dir), capture_output=True, text=True, check=False)
    if run.returncode != 0:
        log = ctx.logs
        log.mkdir(parents=True, exist_ok=True)
        (log / "cmake-configure.log").write_text(run.stdout + run.stderr, encoding="utf-8")
        message = f"cmake --preset {cfg.cmake_preset} failed for {cfg.name} ({log / 'cmake-configure.log'})"
        raise RuntimeError(message)


# --------------------------------------------------------------------------- #
# Command builders
# --------------------------------------------------------------------------- #
def resolved_inputs(ctx: RepoContext) -> list[str]:
    """Return the ``inputs`` globs, or the explicit file list when excluding.

    clangquill expands globs itself, so the globs are passed through untouched
    whenever nothing is excluded — that is the common case and it keeps the
    command line short. With ``exclude`` set the harness has to expand them
    here instead: there is no exclude flag on ``clangquill build``, and the
    file set has to end up identical to the one the Doxyfile's EXCLUDE_PATTERNS
    produces.
    """
    cfg = ctx.config
    if not cfg.exclude:
        return list(cfg.inputs)
    root = ctx.source_dir
    # Sorted and deduplicated, with no attempt to preserve the order the config
    # listed its patterns in: clangquill parses its inputs in a canonical order
    # of its own, so the sequence handed over here cannot change what it
    # extracts.
    found = sorted({p.relative_to(root).as_posix() for glob in cfg.inputs for p in root.glob(glob) if p.is_file()})
    return [path for path in found if not any(fnmatch.fnmatch(path, pattern) for pattern in cfg.exclude)]


def clangquill_build_argv(
    ctx: RepoContext,
    clangquill_cmd: list[str],
    *,
    output_dir: Path | None = None,
    cache_dir: Path | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """Build the ``clangquill build`` argv for ``ctx``.

    ``output_dir``/``cache_dir`` default to the context's own locations, and
    ``extra`` appends driver-specific flags (verify.py adds the strict-mode
    pair). Every compile-affecting option comes from the shared
    :class:`RepoConfig`, so both drivers parse each project the same way — a
    verification pass that used different flags from the benchmark would be
    verifying something else.
    """
    cfg = ctx.config
    out = ctx.myst_out if output_dir is None else output_dir
    argv = [*clangquill_cmd, "build", *resolved_inputs(ctx), "-o", str(out), "--std", cfg.std]
    for inc in cfg.include_dirs:
        argv += ["-I", inc]
    for define in cfg.defines:
        argv += ["-D", define]
    for arg in cfg.compile_args:
        # str.replace, not str.format: a compile arg may legitimately contain a
        # literal brace. Resolved lazily so configs without the placeholder work
        # on a machine that has no llvm-config at all.
        # `{source_dir}` matters for a forced-include prologue: clang resolves a
        # relative `-include` against the working directory and then spells
        # every file that prologue pulls in the same relative way, so the IR
        # ends up carrying `./x/y.h` where the umbrella carries the absolute
        # path. Same file, two spellings, and anything that renders a path — an
        # anonymous entity's name — then differs with the batching.
        resolved = arg.replace("{source_dir}", str(ctx.source_dir))
        if "{llvm_includedir}" in resolved:
            resolved = resolved.replace("{llvm_includedir}", llvm_includedir())
        argv += ["--compile-arg", resolved]
    if cfg.group_by:
        argv += ["--group-by", cfg.group_by]
    argv += ["--cache-dir", str(ctx.cache_dir if cache_dir is None else cache_dir)]
    return [*argv, *(extra or [])]


def write_doxyfile(ctx: RepoContext, mode: str, *, strict: bool = False, warn_log: Path | None = None) -> Path:
    """Generate a minimal Doxyfile for ``mode`` ("xml" or "html").

    ``strict`` flips the diagnostics half of the configuration: the benchmark
    silences Doxygen entirely (warnings are not what it measures, and printing
    them would only add I/O to a timed run), while verify.py turns them on and
    makes them fatal. The *input* half — INPUT, RECURSIVE, FILE_PATTERNS,
    EXTRACT_ALL — is identical either way, which is what keeps the two tools on
    the same file set in both drivers.

    ``WARN_IF_UNDOCUMENTED`` stays off even under ``strict``: with
    ``EXTRACT_ALL = YES`` it fires for every undocumented entity in the project,
    which says nothing about whether extraction *worked*.
    """
    cfg = ctx.config
    out_dir = ctx.doxygen_out(mode)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Doxygen quotes with `"`, not shell-style: shlex.quote would only kick in
    # for a path containing a space, and would then produce single quotes it
    # takes literally.
    inputs = " ".join(f'"{ctx.source_dir / d}"' for d in cfg.doxygen_input)
    common = [
        f'PROJECT_NAME = "{cfg.name}"',
        f"OUTPUT_DIRECTORY = {out_dir}",
        f"INPUT = {inputs}",
        # Both knobs exist to keep Doxygen's file set identical to clangquill's
        # ``inputs`` globs; see RepoConfig.
        f"RECURSIVE = {'YES' if cfg.doxygen_recursive else 'NO'}",
        *([f"FILE_PATTERNS = {' '.join(cfg.doxygen_file_patterns)}"] if cfg.doxygen_file_patterns else []),
        # Same file set as :func:`resolved_inputs` drops for clangquill. The
        # patterns are repo-relative and Doxygen matches them against absolute
        # paths, hence the leading ``*/``.
        *(
            # Double quotes, not shlex: Doxygen's config parser understands `"`
            # for a value containing spaces and takes `'` literally, so
            # shell-style quoting produced patterns that matched nothing and
            # silently left the excluded files in Doxygen's input.
            [f"""EXCLUDE_PATTERNS = {" ".join(f'"*/{pattern}"' for pattern in cfg.exclude)}"""] if cfg.exclude else []
        ),
        # Third knob of the same rule: Doxygen's default is to run its
        # preprocessor across ``#include``s reaching outside INPUT, which is the
        # opposite of an identical file set. It is not a complete fence —
        # a quoted include still resolves against the including file's own
        # directory — so a project whose deep tree Doxygen mis-preprocesses may
        # still need ``doxygen_extra``; see dune-gdt.
        "SEARCH_INCLUDES = NO",
        "QUIET = YES",
        "WARN_IF_UNDOCUMENTED = NO",
        "GENERATE_LATEX = NO",
        "EXTRACT_ALL = YES",
        # NUM_PROC_THREADS = 0 lets Doxygen use all available CPUs (mirrors
        # clangquill's default jobs=0 / hardware_concurrency behaviour).
        "NUM_PROC_THREADS = 0",
        "HAVE_DOT = NO",
    ]
    if strict:
        common += [
            # Pin what doxygen strips off the paths it records. Left unset it
            # strips the directory it happens to be run from, which makes the
            # recorded locations depend on the caller's working directory —
            # unusable for verify.py, which matches them against ClangQuill's.
            f"STRIP_FROM_PATH = {ctx.source_dir}",
            "WARNINGS = YES",
            "WARN_IF_DOC_ERROR = YES",
            # Deliberately no WARN_AS_ERROR: Doxygen is the reference the
            # extraction check compares against, not a tool under test, and
            # FAIL_ON_WARNINGS made it exit before writing the XML that
            # comparison needs. The warnings are still logged, and verify.py
            # reports the count without gating on it.
            *([f"WARN_LOGFILE = {warn_log}"] if warn_log is not None else []),
        ]
    else:
        common += ["WARNINGS = NO"]
    if mode == "xml":
        common += ["GENERATE_XML = YES", "GENERATE_HTML = NO", "XML_OUTPUT = xml"]
    else:
        common += [
            "GENERATE_XML = NO",
            "GENERATE_HTML = YES",
            "HTML_OUTPUT = html",
            "SEARCHENGINE = NO",
        ]
    # Last, so a config's override beats the generated value for the same tag.
    common += cfg.doxygen_extra
    doxyfile = out_dir / "Doxyfile"
    doxyfile.write_text("\n".join(common) + "\n", encoding="utf-8")
    return doxyfile


def doxygen_argv(doxygen_cmd: list[str], doxyfile: Path) -> list[str]:
    """Build the ``doxygen`` argv that runs ``doxyfile``."""
    return [*doxygen_cmd, str(doxyfile)]


def clangquill_work(stdout: str) -> dict:
    """Extract symbol/file/page counts from ``clangquill build`` output."""
    work: dict = {}
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith("Parsed "):
            parts = line.replace("(s)", "").split()
            # "Parsed N symbol from M file."
            with contextlib.suppress(ValueError, IndexError):
                work["symbols"] = int(parts[1])
                work["files"] = int(parts[4])
        elif line.startswith("Wrote "):
            parts = line.split()
            with contextlib.suppress(ValueError, IndexError):
                work["pages_written"] = int(parts[1])
    return work


# --------------------------------------------------------------------------- #
# Environment / tool metadata
# --------------------------------------------------------------------------- #
def tool_version(argv: list[str]) -> str:
    """Return the first line of ``argv`` output (e.g. ``--version``), or ""."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        if out.returncode != 0:
            return ""
        text = (out.stdout or out.stderr).strip()
        return text.splitlines()[0] if text else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def clangquill_version(cmd: list[str]) -> str:
    """Return the clangquill version, robust to a CLI without ``--version``.

    Older published wheels predate the ``--version`` flag (which made earlier
    reports record ``n/a``), so fall back to the installed package metadata —
    the harness runs in the same environment as the tool it drives.
    """
    version = tool_version([*cmd, "--version"])
    if version:
        return version
    try:
        import importlib.metadata  # noqa: PLC0415

        return f"clangquill {importlib.metadata.version('clangquill')}"
    except Exception:
        return ""


def libclang_version() -> str:
    """Return the linked libclang version string, or "" if unavailable."""
    try:
        from clangquill import _core  # noqa: PLC0415

        return str(_core.libclang_version())
    except Exception:
        return ""


def llvm_includedir() -> str:
    """Return the LLVM include dir holding ``clang-c/Index.h``.

    A config whose headers include ``<clang-c/Index.h>`` (clangquill's own, via
    ``compile_args = ["-I{llvm_includedir}"]``) needs a path that is not on the
    default search path and differs per machine. ``docs/conf.py`` resolves the
    same thing for the self-documentation build; the two cannot share code
    because ``benchmarks/`` is a separate uv project.

    Only a directory that actually contains the header is returned, so a version
    mismatch yields a clear error rather than a bogus ``-I``.
    """

    def holds_clang_c(directory: str) -> bool:
        return bool(directory) and (Path(directory) / "clang-c" / "Index.h").is_file()

    # The CI build pins the prefix this way; prefer it over whatever is on PATH.
    # Spelled the way CMake spells it, hence the not-upper-case name.
    root = os.environ.get("LibClang_ROOT", "")  # noqa: SIM112
    if holds_clang_c(str(Path(root) / "include") if root else ""):
        return str(Path(root) / "include")

    major = ""
    with contextlib.suppress(Exception):
        from clangquill._libclang import libclang_major  # noqa: PLC0415

        major = str(libclang_major() or "")
    # Prefer the llvm-config matching the linked libclang: a different major's
    # headers would describe a backend the parse never actually uses.
    for exe in ([f"llvm-config-{major}"] if major else []) + ["llvm-config"]:
        path = shutil.which(exe)
        if not path:
            continue
        with contextlib.suppress(OSError, subprocess.CalledProcessError):
            out = subprocess.run([path, "--includedir"], capture_output=True, text=True, check=True)
            if holds_clang_c(out.stdout.strip()):
                return out.stdout.strip()

    message = (
        f"no LLVM include dir with clang-c/Index.h found (linked libclang major {major or '?'}); "
        f"install libclang-{major or 'N'}-dev, or set LibClang_ROOT to the LLVM prefix"
    )
    raise RuntimeError(message)


def total_ram_gb() -> float:
    """Return total physical RAM in GB (0.0 if it cannot be determined)."""
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9, 1)
    except (ValueError, OSError):
        return 0.0


def machine_info() -> dict:
    """Collect machine and Python metadata for a report header.

    Tool versions are the caller's business: the two drivers run different tool
    sets and each records exactly the ones it drove.
    """
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "ram_gb": total_ram_gb(),
        "python": platform.python_version(),
    }


def human_bytes(count: float) -> str:
    """Format a byte count as B / KB / MB / GB with one decimal."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB"):
        if count < step:
            return f"{count:.1f} {unit}" if unit != "B" else f"{int(count)} B"
        count /= step
    return f"{count:.1f} TB"

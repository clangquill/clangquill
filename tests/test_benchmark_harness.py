"""Adversarial tests for ``benchmarks/harness.py`` and ``benchmarks/benchmark.py``.

Every published benchmark number rests on two claims the harness makes and
nothing checked: that both tools are pointed at the *identical* file set, and
that the incremental scenarios measure a tree the harness put back the way it
found it. A bug in either does not fail a run — it produces a plausible number
for a comparison nobody made, which is the worst failure mode a benchmark has.

So these read the harness as code under test rather than as ground truth.
"""

from __future__ import annotations

import fnmatch
import importlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
CONFIG_DIR = BENCHMARKS / "configs"


def _load(name: str, monkeypatch) -> object:
    monkeypatch.syspath_prepend(str(BENCHMARKS))
    return importlib.import_module(name)


def _glob_root(pattern: str) -> str:
    """Return the fixed directory prefix of a glob: everything before the first wildcard."""
    parts: list[str] = []
    for part in Path(pattern).parts:
        if any(ch in part for ch in "*?["):
            break
        parts.append(part)
    return "/".join(parts)


# --------------------------------------------------------------------------- #
# File-set lockstep
# --------------------------------------------------------------------------- #
def test_there_are_configs_to_check() -> None:
    # The parametrization below globs the config directory; an empty glob would
    # make every one of its assertions vacuous rather than failing.
    assert sorted(CONFIG_DIR.glob("*.toml"))


@pytest.mark.parametrize("config_path", sorted(CONFIG_DIR.glob("*.toml")), ids=lambda p: p.stem)
def test_doxygen_input_rules_mirror_the_clangquill_globs(config_path: Path, monkeypatch) -> None:
    # `inputs` drives clangquill and INPUT/RECURSIVE/FILE_PATTERNS drive Doxygen;
    # nothing but this test ties them together. A config that adds a `**` to its
    # glob without flipping `doxygen_recursive`, or a new extension without
    # extending FILE_PATTERNS, silently benchmarks two different workloads.
    harness = _load("harness", monkeypatch)
    cfg = harness.RepoConfig.from_toml(config_path)

    assert cfg.inputs, "a config with no inputs benchmarks nothing"
    recursive_globs = {"**" in pattern for pattern in cfg.inputs}
    assert len(recursive_globs) == 1, f"{cfg.name} mixes recursive and non-recursive globs"
    assert cfg.doxygen_recursive == recursive_globs.pop(), (
        f"{cfg.name}: doxygen_recursive={cfg.doxygen_recursive} does not match its inputs globs"
    )

    suffixes = {Path(pattern).suffix for pattern in cfg.inputs}
    assert cfg.doxygen_file_patterns, f"{cfg.name} leaves FILE_PATTERNS at Doxygen's default"
    covered = {Path(pattern).suffix for pattern in cfg.doxygen_file_patterns}
    assert suffixes == covered, f"{cfg.name}: clangquill reads {suffixes}, doxygen reads {covered}"

    roots = {_glob_root(pattern) for pattern in cfg.inputs}
    for root in roots:
        assert any(root == d or root.startswith(f"{d}/") for d in cfg.doxygen_input), (
            f"{cfg.name}: clangquill reads {root!r}, which no doxygen_input covers"
        )


def test_exclude_drops_the_same_files_from_both_tools(tmp_path: Path, monkeypatch) -> None:
    # `exclude` is one knob feeding two mechanisms: the harness expands the globs
    # and filters them for clangquill, and writes EXCLUDE_PATTERNS for Doxygen.
    # They are matched against *different* strings — a repo-relative path one
    # side, an absolute path the other — so agreeing is a property, not a given.
    harness = _load("harness", monkeypatch)
    source = tmp_path / "src"
    for rel in ("lib/keep.h", "lib/internal/keep.h", "lib/skip_me.h", "lib/internal/skip_me.h"):
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).touch()

    cfg = harness.RepoConfig(
        name="fixture",
        inputs=["lib/**/*.h"],
        exclude=["lib/skip_me.h", "lib/internal/*.h"],
        doxygen_input=["lib"],
        doxygen_file_patterns=["*.h"],
    )
    ctx = harness.RepoContext(cfg, source, tmp_path / "bench", resolved_ref="", commit="")

    kept = harness.resolved_inputs(ctx)
    assert kept == ["lib/keep.h"]

    # Re-run Doxygen's side of the same decision: the Doxyfile's EXCLUDE_PATTERNS
    # as written, matched the way Doxygen matches them (against absolute paths).
    doxyfile = harness.write_doxyfile(ctx, "xml")
    line = next(ln for ln in doxyfile.read_text(encoding="utf-8").splitlines() if ln.startswith("EXCLUDE_PATTERNS"))
    patterns = [token.strip('"') for token in line.split("=", 1)[1].split()]
    everything = {"lib/keep.h", "lib/internal/keep.h", "lib/skip_me.h", "lib/internal/skip_me.h"}
    excluded_by_doxygen = {
        rel for rel in everything if any(fnmatch.fnmatch(str(source / rel), pattern) for pattern in patterns)
    }
    # The property: what one tool drops is exactly what the other does not read.
    assert set(kept) == everything - excluded_by_doxygen


def test_excluded_files_never_reach_clangquill(tmp_path: Path, monkeypatch) -> None:
    # The same decision one level up: what actually lands on the command line.
    harness = _load("harness", monkeypatch)
    source = tmp_path / "src"
    (source / "lib").mkdir(parents=True)
    (source / "lib" / "keep.h").touch()
    (source / "lib" / "skip_me.h").touch()
    cfg = harness.RepoConfig(name="fixture", inputs=["lib/*.h"], exclude=["lib/skip_me.h"])
    ctx = harness.RepoContext(cfg, source, tmp_path / "bench", resolved_ref="", commit="")

    argv = harness.clangquill_build_argv(ctx, ["clangquill"])
    assert "lib/keep.h" in argv
    assert "lib/skip_me.h" not in argv


# --------------------------------------------------------------------------- #
# Patch / revert
# --------------------------------------------------------------------------- #
def _patch_ctx(tmp_path: Path, harness, *, files: list[str]) -> object:
    source = tmp_path / "src"
    source.mkdir(exist_ok=True)
    cfg = harness.RepoConfig(name="fixture", inputs=["*.h"], patch_files=files)
    return harness.RepoContext(cfg, source, tmp_path / "bench", resolved_ref="", commit="")


def test_revert_restores_the_exact_bytes_the_patch_found(tmp_path: Path, monkeypatch) -> None:
    # A leaked patch corrupts every later scenario: the "incremental" rebuild
    # after it has nothing to do, so the harness reports a cache hit as the cost
    # of an edit. Reverting to the recorded bytes -- rather than to git HEAD --
    # also means an operator's uncommitted work on a patch target survives a
    # `local = true` run.
    harness = _load("harness", monkeypatch)
    benchmark = _load("benchmark", monkeypatch)
    ctx = _patch_ctx(tmp_path, harness, files=["header.h"])
    target = ctx.source_dir / "header.h"
    uncommitted = "#pragma once\n/// work in progress\nint mine();\n"
    target.write_text(uncommitted, encoding="utf-8")

    patched = benchmark.apply_patch(ctx)
    assert [p.path for p in patched] == [target]
    assert benchmark.PATCH_MARKER in target.read_text(encoding="utf-8")

    benchmark.revert_patch(patched)
    assert target.read_text(encoding="utf-8") == uncommitted


def test_revert_raises_when_the_patch_survives(tmp_path: Path, monkeypatch) -> None:
    # The canary for the revert itself. Restoring silently is how a leak used to
    # happen -- the old revert shelled out to git and never looked at the exit
    # code -- so a revert that does not take must stop the run, not warn into a
    # log nobody reads.
    harness = _load("harness", monkeypatch)
    benchmark = _load("benchmark", monkeypatch)
    ctx = _patch_ctx(tmp_path, harness, files=["header.h"])
    target = ctx.source_dir / "header.h"
    target.write_text("#pragma once\n", encoding="utf-8")
    patched = benchmark.apply_patch(ctx)

    # Simulate a restore that does not stick (a read-only file, a racing editor)
    # by making the write a no-op.
    def refuse(self: Path, data: bytes) -> int:  # noqa: ARG001
        return len(data)

    monkeypatch.setattr(Path, "write_bytes", refuse)
    with pytest.raises(RuntimeError, match="survived its revert"):
        benchmark.revert_patch(patched)


def test_reset_state_leaves_an_unpatched_target_alone(tmp_path: Path, monkeypatch) -> None:
    # `reset_state` reverts a patch an earlier crashed run left behind, with
    # `git checkout` -- which discards uncommitted work. On a `local = true`
    # config the patch targets are this repository's own headers, so it must
    # only fire for a file that actually still carries the marker.
    harness = _load("harness", monkeypatch)
    benchmark = _load("benchmark", monkeypatch)
    ctx = _patch_ctx(tmp_path, harness, files=["header.h"])
    target = ctx.source_dir / "header.h"
    target.write_text("#pragma once\n/// my own uncommitted edit\n", encoding="utf-8")

    pristine = target.read_text(encoding="utf-8")
    checkouts: list[list[str]] = []

    def fake_git(args: list[str], cwd: Path, check: bool = True) -> SimpleNamespace:  # noqa: ARG001, FBT001, FBT002
        """Stand in for ``git checkout --``: record the call, restore the file."""
        checkouts.append(args)
        target.write_text(pristine, encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(benchmark, "run_git", fake_git)
    benchmark.reset_state(ctx)
    assert checkouts == []
    assert target.read_text(encoding="utf-8") == pristine

    # ... and does fire once the marker really is there.
    benchmark.apply_patch(ctx)
    benchmark.reset_state(ctx)
    assert checkouts == [["checkout", "--", "header.h"]]
    assert target.read_text(encoding="utf-8") == pristine


def test_reset_state_raises_when_a_leaked_patch_cannot_be_reverted(tmp_path: Path, monkeypatch) -> None:
    # The recovery checkout used to be `check=False` with its result ignored, so
    # a locked index left the marker in place and every scenario after it
    # measured an already-patched tree. It has to stop the run instead.
    harness = _load("harness", monkeypatch)
    benchmark = _load("benchmark", monkeypatch)
    ctx = _patch_ctx(tmp_path, harness, files=["header.h"])
    (ctx.source_dir / "header.h").write_text("#pragma once\n", encoding="utf-8")
    benchmark.apply_patch(ctx)

    monkeypatch.setattr(
        benchmark,
        "run_git",
        lambda args, cwd, check=True: SimpleNamespace(returncode=1),  # noqa: ARG005
    )
    with pytest.raises(RuntimeError, match="could not be reverted"):
        benchmark.reset_state(ctx)


@pytest.mark.parametrize("spelling", ["header.h", "sub/../header.h"], ids=["exact", "alias"])
def test_a_duplicate_patch_target_is_rejected_before_anything_is_written(
    spelling: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    # The second record's "original" would already carry the snippet, so
    # restoring it last would leave the patch in the tree -- the leak the whole
    # byte-restoring revert exists to prevent. Two spellings of one file are the
    # same hazard as a literal repeat, and the check has to come before the first
    # write: a raise mid-loop would leave the targets before it patched with no
    # record to restore them from.
    harness = _load("harness", monkeypatch)
    benchmark = _load("benchmark", monkeypatch)
    ctx = _patch_ctx(tmp_path, harness, files=["other.h", "header.h", spelling])
    (ctx.source_dir / "sub").mkdir()
    pristine = "#pragma once\n"
    for name in ("header.h", "other.h"):
        (ctx.source_dir / name).write_text(pristine, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate benchmark patch target"):
        benchmark.apply_patch(ctx)

    for name in ("header.h", "other.h"):
        assert (ctx.source_dir / name).read_text(encoding="utf-8") == pristine


# --------------------------------------------------------------------------- #
# Pinning a ref
#
# These drive the harness's real git calls against a local bare "origin" rather
# than a mock: the bug below is in what `git checkout` selects, which a mock
# would have to already model correctly to catch.
# --------------------------------------------------------------------------- #
def _git(args: list[str], cwd: Path) -> str:
    """Run one git command for test setup, failing loudly; return its stdout."""
    return subprocess.run(  # noqa: S603 - a fixed argv of test-authored arguments
        ["git", *args],  # noqa: S607 - git off PATH, as `harness.run_git` itself invokes it
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def moved_branch(tmp_path: Path) -> tuple[Path, str, str]:
    """Build a reused clone of a repo whose pinned branch has since moved.

    Returns ``(work_dir, stale_commit, current_commit)``.
    """
    origin, seed, work = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "work"
    work.mkdir()
    _git(["init", "--bare", "-b", "pinned", str(origin)], tmp_path)
    _git(["init", "-b", "pinned", str(seed)], tmp_path)
    _git(["config", "user.email", "harness@example.invalid"], seed)
    _git(["config", "user.name", "harness"], seed)
    (seed / "value").write_text("old\n", encoding="utf-8")
    _git(["add", "value"], seed)
    _git(["commit", "-m", "initial"], seed)
    _git(["remote", "add", "origin", str(origin)], seed)
    _git(["push", "-u", "origin", "pinned"], seed)

    _git(["clone", str(origin), "fixture"], work)  # the clone an earlier run left
    stale = _git(["rev-parse", "HEAD"], work / "fixture")

    (seed / "value").write_text("new\n", encoding="utf-8")
    _git(["add", "value"], seed)
    _git(["commit", "-m", "moved"], seed)
    _git(["push", "--force", "origin", "pinned"], seed)
    current = _git(["rev-parse", "HEAD"], seed)

    assert stale != current
    return work, stale, current


def test_a_reused_clone_benchmarks_the_commit_its_config_pins(
    moved_branch: tuple[Path, str, str],
    tmp_path: Path,
    monkeypatch,
) -> None:
    # `git fetch` refreshes `origin/<branch>` but leaves an existing local
    # `<branch>` where it was, so fetching and then checking out the bare name
    # still lands on the stale commit -- and labels it with the configured ref.
    # Only asserting the resulting commit catches that; an assertion on the order
    # of the git calls passes either way.
    harness = _load("harness", monkeypatch)
    work, stale, current = moved_branch
    cfg = harness.RepoConfig(name="fixture", repo=str(tmp_path / "origin.git"), ref="pinned")

    ctx = harness.prepare_repo(cfg, work, fresh_clone=False)

    assert ctx.commit == current, f"benchmarked the stale commit {stale}"
    assert ctx.resolved_ref == "pinned"


def test_a_reused_clone_says_so_when_origin_cannot_be_reached(
    moved_branch: tuple[Path, str, str],
    tmp_path: Path,
    monkeypatch,
) -> None:
    # An unreachable origin is not fatal -- the clone may already hold the pinned
    # commit, and an offline re-run is worth allowing -- but the recorded label
    # has to stop claiming a ref nothing verified.
    harness = _load("harness", monkeypatch)
    work, stale, _ = moved_branch
    _git(["remote", "set-url", "origin", str(tmp_path / "gone.git")], work / "fixture")
    cfg = harness.RepoConfig(name="fixture", repo=str(tmp_path / "origin.git"), ref="pinned")

    ctx = harness.prepare_repo(cfg, work, fresh_clone=False)

    assert ctx.commit == stale  # all the clone has
    assert "may be stale" in ctx.resolved_ref


def test_a_pinned_tag_resolves_without_a_remote_tracking_ref(
    moved_branch: tuple[Path, str, str],
    tmp_path: Path,
    monkeypatch,
) -> None:
    # A tag (like a commit sha) has no `origin/<ref>` form, so preferring the
    # remote-tracking ref has to fall through to the bare name rather than give
    # up and label the run as having missed its pin.
    harness = _load("harness", monkeypatch)
    work, stale, _ = moved_branch
    _git(["tag", "v1.0", stale], tmp_path / "seed")
    _git(["push", "origin", "v1.0"], tmp_path / "seed")
    cfg = harness.RepoConfig(name="fixture", repo=str(tmp_path / "origin.git"), ref="v1.0")

    ctx = harness.prepare_repo(cfg, work, fresh_clone=False)

    assert ctx.commit == stale
    assert ctx.resolved_ref == "v1.0"


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def test_summarize_reports_nothing_rather_than_zero_for_no_samples(monkeypatch) -> None:
    # An empty stats dict is what makes the report print "failed"/"—" instead of
    # a number; a zero would read as an instantaneous build.
    benchmark = _load("benchmark", monkeypatch)
    assert benchmark.summarize([]) == {}
    assert benchmark.summarize([2.0])["stddev"] == 0.0
    assert benchmark.summarize([1.0, 3.0, 2.0])["median"] == 2.0


def test_a_crashed_render_reports_failed_rather_than_its_partial_wall_clock(monkeypatch) -> None:
    # A sphinx-build that died mid-read produces a wall clock for a fraction of
    # the work. It is kept as raw data and must never reach the table as a time.
    benchmark = _load("benchmark", monkeypatch)
    cell = benchmark._cell  # noqa: SLF001 - the report formatter is what is under test here
    results = {
        "eigen": {
            "clangquill-sphinx": {
                "cold": {"samples": [{"wall_s": 3.0, "exit_code": 2}], "invalid_samples": 1, "stats": {}},
                "noop": {"samples": [{"wall_s": 1.0, "exit_code": 0}], "invalid_samples": 0, "stats": {"median": 1.0}},
            },
        },
    }
    assert cell(results, "eigen", "clangquill-sphinx", "cold") == "failed"
    assert cell(results, "eigen", "clangquill-sphinx", "noop") == "1.000"
    assert cell(results, "eigen", "clangquill-sphinx", "incremental") == "—"

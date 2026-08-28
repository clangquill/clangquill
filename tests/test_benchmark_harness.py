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


def test_a_duplicate_patch_target_is_rejected(tmp_path: Path, monkeypatch) -> None:
    # The second record's "original" would already carry the snippet, so
    # restoring it last would leave the patch in the tree -- the leak the whole
    # byte-restoring revert exists to prevent.
    harness = _load("harness", monkeypatch)
    benchmark = _load("benchmark", monkeypatch)
    ctx = _patch_ctx(tmp_path, harness, files=["header.h", "header.h"])
    (ctx.source_dir / "header.h").write_text("#pragma once\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate benchmark patch target"):
        benchmark.apply_patch(ctx)


def test_a_reused_clone_is_fetched_before_its_pinned_ref_is_checked_out(tmp_path: Path, monkeypatch) -> None:
    # A stale local branch or tag checks out fine, so fetching only after a
    # failed checkout never fires for a ref that *moved*: the run would
    # benchmark the old commit under the configured ref's label.
    harness = _load("harness", monkeypatch)
    work = tmp_path / "work"
    (work / "fixture").mkdir(parents=True)  # a clone from an earlier run
    cfg = harness.RepoConfig(name="fixture", repo="https://example.invalid/fixture.git", ref="v1.2.3")

    calls: list[list[str]] = []
    monkeypatch.setattr(
        harness,
        "run_git",
        lambda args, cwd, check=True: calls.append(args) or SimpleNamespace(returncode=0, stdout="abc123\n"),  # noqa: ARG005
    )
    harness.prepare_repo(cfg, work, fresh_clone=False)

    assert calls[0][0] == "fetch", calls
    assert calls[1][:2] == ["checkout", "--force"], calls
    assert "clone" not in [args[0] for args in calls]


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

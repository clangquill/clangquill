"""Tests for the parse -> SQLite -> MyST pipeline and the CLI that drives it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from clangquill import _core, cli, pipeline
from clangquill.cache import BuildCache
from clangquill.config import Config
from clangquill.pipeline import MANIFEST_NAME, build
from clangquill.store import Store


def _store_symbols(db_path: Path) -> list[object]:
    with Store.open(db_path) as store:
        return store.symbols()


requires_libclang = pytest.mark.skipif(
    not _core.have_libclang(),
    reason="core built without libclang",
)

FIXTURE = """
/// A documented namespace.
namespace demo {
/// A documented widget.
struct Widget {
  /// the width
  int width;
};
/// A documented free function.
int twice(int x);
}
"""


M7_FIXTURE = Path(__file__).parent / "cpp" / "fixtures" / "m7.hpp"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "demo.hpp").write_text(FIXTURE)
    return tmp_path


@requires_libclang
def test_build_renders_all_m7_kinds(tmp_path: Path) -> None:
    (tmp_path / "m7.hpp").write_text(M7_FIXTURE.read_text())
    result = build(Config(input=["m7.hpp"], output_dir="api"), base_dir=tmp_path)
    assert "group_math" in result.pages

    api = tmp_path / "api"
    ns = (api / "m7.md").read_text()
    # Concept and class template render with their `template<...>` heads.
    assert "{cpp:concept} template<typename T> m7::Addable" in ns
    assert "{cpp:class} template<typename T, int N = 4> m7::Buffer" in ns
    assert "{cpp:function} template <typename T> T m7::max_value" in ns
    # Friends and operators.
    assert "**Friends**" in ns
    assert "{cpp:function} int m7::Vec::operator[]" in ns
    # `\ingroup` bookkeeping never leaks into the rendered prose.
    assert "\nmath\n" not in ns

    # Macros become C-domain objects on their own pages, with attached docs.
    macro = (api / "CQ_MAX.md").read_text()
    assert "{c:macro} CQ_MAX(a, b)" in macro
    assert "function-like macro" in macro

    # The group page lists its `\ingroup` members.
    grp = (api / "group_math.md").read_text()
    assert grp.startswith("# Math utilities")
    assert "{cpp:any}`m7::add`" in grp


@requires_libclang
def test_build_generates_pages_and_index(project: Path) -> None:
    config = Config(input=["demo.hpp"], output_dir="api")
    result = build(config, base_dir=project)

    assert result.symbol_count > 0
    assert result.pages == ["demo"]
    api = project / "api"
    assert (api / "demo.md").is_file()
    assert (api / "index.md").read_text().startswith("# API Reference")
    assert (api / MANIFEST_NAME).is_file()
    # The throwaway IR is reported as temporary so the caller can clean it up.
    assert result.db_is_temporary


@requires_libclang
def test_build_path_base_reroots_file_headings(project: Path) -> None:
    # With group_by="file" the heading renders the source path; path_base="."
    # re-roots it against the project so the absolute build-machine path the IR
    # stores never leaks into the output.
    config = Config(input=["demo.hpp"], output_dir="api", group_by="file", path_base=".")
    build(config, base_dir=project)

    page = (project / "api" / "demo_hpp.md").read_text()
    assert page.startswith("# File `demo.hpp`")
    assert str(project.resolve()) not in page


@requires_libclang
def test_build_caches_db_when_cache_dir_set(project: Path) -> None:
    config = Config(input=["demo.hpp"], cache_dir=".cache")
    result = build(config, base_dir=project)
    assert not result.db_is_temporary
    assert result.db_path.is_file()
    assert result.db_path.parent == (project / ".cache").resolve()


def test_index_cache_key_includes_top_level(fixture_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # render_index filters entries on `top_level` (a page set identical in
    # stems/labels but differing only in top_level must produce a different
    # toctree), so the memoised index key has to fold top_level in too —
    # otherwise the second render would replay the first's stale index text.
    from clangquill.generator import Generator, PagePlan  # noqa: PLC0415

    config = Config(input=[], output_dir="api")

    with Store.open(fixture_db) as store, BuildCache.open(tmp_path / ".cache") as cache:
        generator = Generator(store)

        top_level_plan = PagePlan("geo", "geo", lambda: "geo text", top_level=True)
        monkeypatch.setattr(generator, "plan_pages", lambda **_kw: [top_level_plan])
        rendered = pipeline._rendered_files(generator, config, cache=cache, render_fingerprint="rf")  # noqa: SLF001
        index_top_level = dict(rendered)["index.md"]

        nested_plan = PagePlan("geo", "geo", lambda: "geo text", top_level=False)
        monkeypatch.setattr(generator, "plan_pages", lambda **_kw: [nested_plan])
        rendered = pipeline._rendered_files(generator, config, cache=cache, render_fingerprint="rf")  # noqa: SLF001
        index_nested = dict(rendered)["index.md"]

    assert "geo" in index_top_level
    assert "geo" not in index_nested


def _mtimes(api: Path) -> dict[str, float]:
    return {p.name: p.stat().st_mtime_ns for p in api.glob("*.md")}


@requires_libclang
def test_incremental_unchanged_build_regenerates_nothing(project: Path) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    first = build(config, base_dir=project)
    assert first.parsed
    assert first.pages_written  # the first run writes every page

    api = project / "api"
    before = _mtimes(api)
    second = build(config, base_dir=project)

    # Nothing changed: the parse is served from cache and no page is rewritten.
    assert not second.parsed
    assert second.pages_written == []
    assert second.pages_deleted == []
    assert _mtimes(api) == before


@requires_libclang
def test_incremental_noop_skips_rendering(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    first = build(config, base_dir=project)
    assert first.parsed

    # A noop rebuild must not render at all: the second run short-circuits before
    # _rendered_files (the Jinja pass) is ever reached.
    def boom(*_args: object, **_kwargs: object) -> list[tuple[str, str]]:
        msg = "rendering should be skipped on a noop build"
        raise AssertionError(msg)

    monkeypatch.setattr(pipeline, "_rendered_files", boom)

    second = build(config, base_dir=project)
    assert not second.parsed
    assert second.pages_written == []
    assert second.pages_deleted == []
    # Counts and page list are replayed faithfully from the cache.
    assert second.pages == first.pages
    assert second.symbol_count == first.symbol_count
    assert second.reference_count == first.reference_count
    assert second.file_count == first.file_count


@requires_libclang
def test_incremental_restores_deleted_output(project: Path) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    first = build(config, base_dir=project)
    page = project / "api" / "demo.md"
    original = page.read_text()

    # A deleted page (e.g. `git clean` of the output dir) must not be trusted by
    # the noop shortcut: the next build rewrites exactly the missing page.
    page.unlink()
    second = build(config, base_dir=project)
    assert not second.parsed
    assert second.pages_written == ["demo.md"]
    assert page.read_text() == original
    assert second.pages == first.pages

    # And once repaired, the build noops again.
    third = build(config, base_dir=project)
    assert third.pages_written == []


@requires_libclang
def test_incremental_restores_hand_edited_output(project: Path) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)
    page = project / "api" / "demo.md"
    original = page.read_text()

    # A hand-edited generated page is restored, not silently left stale.
    page.write_text("# tampered\n", encoding="utf-8")
    result = build(config, base_dir=project)
    assert result.pages_written == ["demo.md"]
    assert page.read_text() == original


@requires_libclang
def test_incremental_touched_but_identical_output_still_noops(project: Path) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)
    page = project / "api" / "demo.md"

    # Rewriting identical bytes moves the stat but not the content: the build
    # must recognise the page as intact (via the hash fallback) and still noop.
    page.write_text(page.read_text(), encoding="utf-8")
    result = build(config, base_dir=project)
    assert not result.parsed
    assert result.pages_written == []


@requires_libclang
def test_incremental_render_config_change_rerenders_without_reparse(project: Path) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)

    # Changing a render-only option leaves the parse cached but must still
    # re-render: the noop skip is keyed on the render fingerprint too.
    deeper = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache", toctree_maxdepth=4)
    result = build(deeper, base_dir=project)
    assert not result.parsed
    assert "index.md" in result.pages_written
    assert ":maxdepth: 4" in (project / "api" / "index.md").read_text()


@requires_libclang
def test_incremental_template_edit_busts_noop_skip(project: Path) -> None:
    templates = project / "templates"
    templates.mkdir()
    override = templates / "namespace.md.jinja"
    override.write_text("# OVERRIDE {{ symbol.qualified_name }}\n")
    config = Config(
        input=["demo.hpp"],
        output_dir="api",
        cache_dir=".cache",
        template_dirs=["templates"],
    )
    first = build(config, base_dir=project)
    assert "OVERRIDE demo" in (project / "api" / "demo.md").read_text()
    assert first.parsed

    # Editing the override template (IR untouched) must re-render rather than
    # serve the stale page from the noop cache.
    override.write_text("# CHANGED {{ symbol.qualified_name }}\n")
    result = build(config, base_dir=project)
    assert not result.parsed
    assert "demo.md" in result.pages_written
    assert "CHANGED demo" in (project / "api" / "demo.md").read_text()


@requires_libclang
def test_incremental_touch_header_regenerates_only_affected(project: Path) -> None:
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text("/// beta ns\nnamespace beta { /// g\nint g(); }\n")
    config = Config(input=["alpha.hpp", "beta.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)

    api = project / "api"
    before = _mtimes(api)

    # Edit only alpha.hpp's documentation; beta and the toctree are untouched.
    (project / "alpha.hpp").write_text("/// alpha ns edited\nnamespace alpha { /// f\nint f(); }\n")
    result = build(config, base_dir=project)

    assert result.parsed
    assert result.pages_written == ["alpha.md"]
    assert result.pages_deleted == []
    after = _mtimes(api)
    assert after["alpha.md"] != before["alpha.md"]
    assert after["beta.md"] == before["beta.md"]
    assert after["index.md"] == before["index.md"]


@requires_libclang
def test_incremental_page_cache_replays_unchanged_pages(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Editing one header must re-render only its page; every other page replays
    # its text from the page cache instead of running Jinja again.
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text("/// beta ns\nnamespace beta { /// g\nint g(); }\n")
    config = Config(input=["alpha.hpp", "beta.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)

    # Spy on the memoisation decision: which page stems hit the cache vs miss.
    hits: list[str] = []
    misses: list[str] = []
    real = BuildCache.cached_page

    def spy(self: BuildCache, stem: str, key: str) -> str | None:
        text = real(self, stem, key)
        (hits if text is not None else misses).append(stem)
        return text

    monkeypatch.setattr(BuildCache, "cached_page", spy)

    (project / "alpha.hpp").write_text("/// alpha ns edited\nnamespace alpha { /// f\nint f(); }\n")
    result = build(config, base_dir=project)

    assert result.parsed
    # Only alpha's page is re-rendered; beta and the toctree replay from cache.
    assert misses == ["alpha"]
    assert "beta" in hits
    assert "index" in hits
    assert result.pages_written == ["alpha.md"]
    assert "alpha ns edited" in (project / "api" / "alpha.md").read_text()


@requires_libclang
def test_incremental_page_cache_busts_dependent_on_base_rename(project: Path) -> None:
    # A page's key must include the symbols it *references*, not just the ones it
    # renders: renaming a base class leaves the derived class's own row (hence its
    # content hash) untouched, so a content-hash-only key would replay a stale
    # "Inherits from Base". The reference tokens guard against exactly that.
    (project / "base.hpp").write_text("#pragma once\n/// base\nstruct Base {};\n")
    (project / "derived.hpp").write_text('#include "base.hpp"\n/// derived\nstruct Derived : public Base {};\n')
    config = Config(input=["base.hpp", "derived.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)
    derived_page = project / "api" / "Derived.md"
    assert "Base" in derived_page.read_text()

    # Rename the base class (in both files so the reference stays resolved).
    # Derived's hashed row — name, comment, signature — is unchanged; only its
    # base-class reference moves, so the page replays stale without ref tokens.
    (project / "base.hpp").write_text("#pragma once\n/// base\nstruct Renamed {};\n")
    (project / "derived.hpp").write_text('#include "base.hpp"\n/// derived\nstruct Derived : public Renamed {};\n')
    result = build(config, base_dir=project)

    assert result.parsed
    text = derived_page.read_text()
    assert "Renamed" in text
    assert "Base" not in text
    assert "Derived.md" in result.pages_written


@requires_libclang
def test_incremental_reparses_only_the_changed_translation_unit(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text("/// beta ns\nnamespace beta { /// g\nint g(); }\n")
    config = Config(input=["alpha.hpp", "beta.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)

    # Spy on the two parse entry points to prove the incremental rebuild takes
    # the per-TU path for exactly the touched input and never re-parses the world.
    full_calls = 0
    tu_calls: list[list[str]] = []
    real_full = _core.parse_to_sqlite
    real_tus = _core.parse_tus_to_sqlite

    def spy_full(inputs: list[str], db: str, opt: object) -> object:
        nonlocal full_calls
        full_calls += 1
        return real_full(inputs, db, opt)

    def spy_tus(inputs: list[str], db: str, opt: object, dropped: list[str]) -> object:
        tu_calls.append([Path(inp).name for inp in inputs])
        return real_tus(inputs, db, opt, dropped)

    monkeypatch.setattr(_core, "parse_to_sqlite", spy_full)
    monkeypatch.setattr(_core, "parse_tus_to_sqlite", spy_tus)

    (project / "alpha.hpp").write_text("/// alpha ns edited\nnamespace alpha { /// f\nint f(); }\n")
    result = build(config, base_dir=project)

    assert result.parsed
    assert full_calls == 0  # no whole-module rebuild
    assert tu_calls == [["alpha.hpp"]]  # only the touched TU re-parsed
    assert result.pages_written == ["alpha.md"]


@requires_libclang
def test_incremental_shared_header_change_reparses_every_dependent(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A header shared by two inputs: editing it must re-parse *both* translation
    # units that include it (not just one), and leave the IR consistent.
    (project / "shared.hpp").write_text("#pragma once\nusing Id = int;\n")
    (project / "alpha.hpp").write_text('#include "shared.hpp"\n/// a\nnamespace a { /// f\nId f(); }\n')
    (project / "beta.hpp").write_text('#include "shared.hpp"\n/// b\nnamespace b { /// g\nId g(); }\n')
    config = Config(input=["alpha.hpp", "beta.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)

    full_calls = 0
    tu_calls: list[list[str]] = []
    real_full = _core.parse_to_sqlite
    real_tus = _core.parse_tus_to_sqlite

    def spy_full(inputs: list[str], db: str, opt: object) -> object:
        nonlocal full_calls
        full_calls += 1
        return real_full(inputs, db, opt)

    def spy_tus(inputs: list[str], db: str, opt: object, dropped: list[str]) -> object:
        tu_calls.append([Path(inp).name for inp in inputs])
        return real_tus(inputs, db, opt, dropped)

    monkeypatch.setattr(_core, "parse_to_sqlite", spy_full)
    monkeypatch.setattr(_core, "parse_tus_to_sqlite", spy_tus)

    (project / "shared.hpp").write_text("#pragma once\nusing Id = unsigned long;\n")
    result = build(config, base_dir=project)

    assert result.parsed
    assert full_calls == 0  # still no whole-module rebuild
    # Both dependents re-parsed via one per-TU batch, in the resolved input order
    # (which the parser then canonicalises for itself).
    assert tu_calls == [["alpha.hpp", "beta.hpp"]]
    # The IR is consistent: no symbol was lost across the partial re-parse.
    assert {"a", "b"}.issubset({s.qualified_name for s in _store_symbols(result.db_path)})


@requires_libclang
@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        # Auto batching re-batches the (usually small) stale set at the
        # incremental size so it spreads across the thread pool instead of
        # re-parsing as one cold-sized umbrella on a single thread.
        (0, pipeline._INCREMENTAL_TU_BATCH),  # noqa: SLF001
        # An explicit user tu_batch is respected on the incremental path too.
        (1, 1),
    ],
)
def test_incremental_reparse_uses_smaller_auto_batch(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: int,
    expected: int,
) -> None:
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text("/// beta ns\nnamespace beta { /// g\nint g(); }\n")
    config = Config(input=["alpha.hpp", "beta.hpp"], output_dir="api", cache_dir=".cache", tu_batch=configured)
    build(config, base_dir=project)

    tu_batches: list[int] = []
    real_tus = _core.parse_tus_to_sqlite

    def spy_tus(inputs: list[str], db: str, opt: _core.ParseOptions, dropped: list[str]) -> object:
        tu_batches.append(opt.tu_batch)
        return real_tus(inputs, db, opt, dropped)

    monkeypatch.setattr(_core, "parse_tus_to_sqlite", spy_tus)

    (project / "alpha.hpp").write_text("/// alpha ns edited\nnamespace alpha { /// f\nint f(); }\n")
    result = build(config, base_dir=project)

    assert result.parsed
    assert tu_batches == [expected]


@requires_libclang
def test_incremental_partial_parse_failure_is_atomic(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two stale inputs where the batched re-parse fails: the IR must not be left
    # half-updated, and the cache must still describe the pre-build state so the
    # next run retries cleanly rather than trusting a torn IR.
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text("/// beta ns\nnamespace beta { /// g\nint g(); }\n")
    config = Config(input=["alpha.hpp", "beta.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)

    ir = project / ".cache" / pipeline.IR_NAME
    ir_before = ir.read_bytes()

    # Edit both inputs so both are stale, then make the batched re-parse blow up.
    (project / "alpha.hpp").write_text("/// alpha ns edit\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text("/// beta ns edit\nnamespace beta { /// g\nint g(); }\n")

    def flaky_tus(*_args: object, **_kwargs: object) -> object:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(_core, "parse_tus_to_sqlite", flaky_tus)

    with pytest.raises(RuntimeError, match="boom"):
        build(config, base_dir=project)

    # The staged copy was discarded: the on-disk IR is byte-for-byte unchanged.
    assert ir.read_bytes() == ir_before
    # Sanity: the staged temp DB was cleaned up, not left lingering.
    assert not list((project / ".cache").glob("tmp*.sqlite"))

    # With the patch removed, the next build recovers and re-parses both inputs.
    monkeypatch.undo()
    recovered = build(config, base_dir=project)
    assert recovered.parsed
    assert {"alpha.md", "beta.md"}.issubset(set(recovered.pages_written))


@requires_libclang
def test_render_failure_after_reparse_does_not_noop_next_run(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If rendering crashes after the parse pointer advanced, the next build must
    # still regenerate docs for the new IR rather than trust a stale render
    # summary and noop-skip it.
    config = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)

    # Edit the source so the next build re-parses, then make rendering blow up
    # *after* the parse has been recorded.
    (project / "demo.hpp").write_text(FIXTURE.replace("documented namespace", "documented namespace edited"))

    def boom(*_args: object, **_kwargs: object) -> list[tuple[str, str]]:
        msg = "render exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr(pipeline, "_rendered_files", boom)
    with pytest.raises(RuntimeError, match="render exploded"):
        build(config, base_dir=project)

    # Recover: rendering works again. The parse is now cache-current, but the
    # render bookkeeping was invalidated, so this must NOT noop — it must render.
    monkeypatch.undo()
    recovered = build(config, base_dir=project)
    assert not recovered.parsed  # parse served from cache (IR already updated)
    assert "demo.md" in recovered.pages_written  # but docs were regenerated
    assert "namespace edited" in (project / "api" / "demo.md").read_text()


@requires_libclang
def test_incremental_deletes_pages_for_removed_symbols(project: Path) -> None:
    header = project / "two.hpp"
    header.write_text("/// alpha\nnamespace alpha { /// f\nint f(); }\n/// beta\nnamespace beta { /// g\nint g(); }\n")
    config = Config(input=["two.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)
    api = project / "api"
    assert (api / "alpha.md").is_file()
    assert (api / "beta.md").is_file()

    # Drop the beta namespace entirely; its page must be deleted.
    header.write_text("/// alpha\nnamespace alpha { /// f\nint f(); }\n")
    result = build(config, base_dir=project)

    assert result.pages_deleted == ["beta.md"]
    assert not (api / "beta.md").exists()
    assert (api / "alpha.md").is_file()
    # The toctree shrank, so the index is rewritten; alpha's page did not change.
    assert "index.md" in result.pages_written
    assert "alpha.md" not in result.pages_written


def test_parse_fingerprint_tracks_compile_commands_file(tmp_path: Path) -> None:
    # ``compile_commands`` is a directory; the fingerprint must follow the JSON
    # file inside it so edits to the compile DB invalidate the cached parse.
    cc_dir = tmp_path / "build"
    cc_dir.mkdir()
    db = cc_dir / "compile_commands.json"
    db.write_text('[{"directory": ".", "command": "c++ a.cpp", "file": "a.cpp"}]', encoding="utf-8")
    config = Config(input=["a.hpp"], compile_commands="build")

    before = pipeline._parse_fingerprint(config, tmp_path, ["a.hpp"])  # noqa: SLF001
    db.write_text('[{"directory": ".", "command": "c++ -DX a.cpp", "file": "a.cpp"}]', encoding="utf-8")
    after = pipeline._parse_fingerprint(config, tmp_path, ["a.hpp"])  # noqa: SLF001
    assert before != after


def test_missing_compile_commands_names_every_path_searched(tmp_path: Path) -> None:
    """A database that isn't on disk fails with the full path that was searched."""
    (tmp_path / "a.hpp").write_text(FIXTURE, encoding="utf-8")
    config = Config(input=["a.hpp"], compile_commands="build")

    with pytest.raises(pipeline.CompileCommandsError) as excinfo:
        build(config, base_dir=tmp_path)

    message = str(excinfo.value)
    assert "looked for:" in message
    assert str(tmp_path / "build" / "compile_commands.json") in message
    # Relative values are ambiguous without the base they resolve against.
    assert str(tmp_path) in message


def test_compile_commands_may_name_the_json_file_itself(tmp_path: Path) -> None:
    """Pointing at the file rather than its directory is accepted, not an error."""
    cc_dir = tmp_path / "build"
    cc_dir.mkdir()
    db = cc_dir / "compile_commands.json"
    db.write_text('[{"directory": ".", "command": "c++ a.cpp", "file": "a.cpp"}]', encoding="utf-8")
    config = Config(input=["a.hpp"], compile_commands="build/compile_commands.json")

    assert pipeline.resolve_compile_commands(config.compile_commands, tmp_path) == db
    # libclang is handed the containing directory either way.
    assert pipeline._parse_options(config, tmp_path).compile_commands_dir == str(cc_dir)  # noqa: SLF001


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("not json at all", "is not valid JSON"),
        ('{"file": "a.cpp"}', "must hold a JSON array"),
        ("[]", "is empty"),
    ],
)
def test_unloadable_compile_commands_is_an_error(tmp_path: Path, contents: str, expected: str) -> None:
    """Libclang degrades silently on a bad database; the pipeline must not."""
    cc_dir = tmp_path / "build"
    cc_dir.mkdir()
    db = cc_dir / "compile_commands.json"
    db.write_text(contents, encoding="utf-8")
    config = Config(input=["a.hpp"], compile_commands="build")

    with pytest.raises(pipeline.CompileCommandsError) as excinfo:
        build(config, base_dir=tmp_path)

    message = str(excinfo.value)
    assert expected in message
    assert str(db) in message


@requires_libclang
def test_incremental_reparses_when_included_header_changes(project: Path) -> None:
    (project / "detail.hpp").write_text("#pragma once\nusing Width = int;\n")
    (project / "main.hpp").write_text('#include "detail.hpp"\n/// uses detail\nnamespace m { /// w\nWidth w(); }\n')
    config = Config(input=["main.hpp"], output_dir="api", cache_dir=".cache")

    first = build(config, base_dir=project)
    assert first.parsed
    # The transitive include is tracked, so it counts as a parsed source file.
    assert first.file_count >= 2

    # Rebuild with nothing touched -> cache hit, no parse.
    assert not build(config, base_dir=project).parsed

    # Touching the *included* header invalidates the cached parse.
    (project / "detail.hpp").write_text("#pragma once\nusing Width = unsigned;\n")
    assert build(config, base_dir=project).parsed


@requires_libclang
def test_incremental_prunes_a_dependency_that_left_the_closure(project: Path) -> None:
    # private.hpp is reached only through alpha.hpp; shared.hpp through both.
    (project / "private.hpp").write_text("#pragma once\nusing Priv = int;\n")
    (project / "shared.hpp").write_text("#pragma once\nusing Id = unsigned;\n")
    (project / "alpha.hpp").write_text(
        '#include "private.hpp"\n#include "shared.hpp"\n/// alpha ns\nnamespace alpha { /// f\nPriv f(); }\n',
    )
    (project / "beta.hpp").write_text('#include "shared.hpp"\n/// beta ns\nnamespace beta { /// g\nId g(); }\n')
    config = Config(input=["alpha.hpp", "beta.hpp"], output_dir="api", cache_dir=".cache")

    build(config, base_dir=project)
    ir = project / ".cache" / "clangquill.sqlite"
    with Store.open(ir) as store:
        tracked = {Path(f.path).name for f in store.files()}
    assert {"alpha.hpp", "beta.hpp", "private.hpp", "shared.hpp"} <= tracked

    # alpha.hpp drops both includes. shared.hpp survives because beta.hpp still
    # pulls it in; private.hpp is reached by nothing and must leave the IR.
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    result = build(config, base_dir=project)

    assert result.parsed
    with Store.open(ir) as store:
        tracked = {Path(f.path).name for f in store.files()}
    assert "private.hpp" not in tracked
    assert {"alpha.hpp", "beta.hpp", "shared.hpp"} <= tracked
    # The reported file count matches what is actually still in the build.
    assert result.file_count == len(tracked)

    # beta.hpp's page is untouched and still renders from the surviving rows.
    assert sorted(p.name for p in (project / "api").glob("*.md")) == ["alpha.md", "beta.md", "index.md"]


@requires_libclang
def test_build_prunes_stale_pages(project: Path) -> None:
    # First build with one input produces demo.md.
    build(Config(input=["demo.hpp"], output_dir="api"), base_dir=project)
    api = project / "api"
    assert (api / "demo.md").is_file()

    # Replace the input with a differently-named namespace and rebuild; the old
    # page must be pruned via the manifest while a hand-written file survives.
    (api / "handwritten.md").write_text("keep me\n")
    (project / "demo.hpp").write_text("/// other\nnamespace other { /// f\nint f(); }\n")
    build(Config(input=["demo.hpp"], output_dir="api"), base_dir=project)

    assert (api / "other.md").is_file()
    assert not (api / "demo.md").exists()
    assert (api / "handwritten.md").is_file()


@requires_libclang
def test_build_missing_input_raises(project: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build(Config(input=["nope.hpp"]), base_dir=project)


@requires_libclang
def test_build_skips_directories_matched_by_glob(project: Path) -> None:
    # A glob like ``*`` matches the subdirectory alongside the header; only the
    # header should be parsed, and the directory must not reach libclang.
    (project / "sub").mkdir()
    result = build(Config(input=["*"], output_dir="api"), base_dir=project)
    assert result.file_count == 1
    assert not result.diagnostics


def test_resolve_inputs_prefers_literal_path_over_glob_metacharacters(tmp_path: Path) -> None:
    # ``foo[1].h`` is a valid filename, but ``[1]`` is also a glob character
    # class matching the literal ``1``. If a same-named-minus-brackets file
    # happens to exist, a glob-first resolution would silently substitute it;
    # the literal on-disk file must win instead.
    literal = tmp_path / "foo[1].h"
    literal.write_text("// literal\n")
    (tmp_path / "foo1.h").write_text("// wrong match\n")

    resolved = pipeline._resolve_inputs(["foo[1].h"], tmp_path)  # noqa: SLF001

    assert resolved == [str(literal.resolve())]


def test_resolve_inputs_still_globs_when_no_literal_file_exists(tmp_path: Path) -> None:
    (tmp_path / "a.h").write_text("// a\n")
    (tmp_path / "b.h").write_text("// b\n")

    resolved = pipeline._resolve_inputs(["*.h"], tmp_path)  # noqa: SLF001

    assert resolved == sorted(str(p.resolve()) for p in [tmp_path / "a.h", tmp_path / "b.h"])


@requires_libclang
def test_temp_db_cleaned_up_when_generation_fails(
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pin the temp IR to a known path, then make generation fail; the finally
    # block must remove the throwaway database rather than leak it.
    db = tmp_path / "scratch.sqlite"
    monkeypatch.setattr(pipeline, "_new_temp_db", lambda *_, **__: db)

    # An override pointing at a missing template makes generate() raise.
    config = Config(input=["demo.hpp"], templates={"namespace": "missing_template"})
    with pytest.raises(Exception, match="missing_template"):
        build(config, base_dir=project)
    assert not db.exists()


@requires_libclang
def test_cli_build_from_cwd(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["build", "demo.hpp", "-o", "out"])
    assert result.exit_code == 0, result.output
    assert (project / "out" / "demo.md").is_file()
    assert "Wrote 1 page(s)" in result.output


@requires_libclang
def test_cli_build_root_document_path_base_and_template(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / "templates").mkdir()
    (project / "templates" / "my_ns.md.jinja").write_text("# OVERRIDE {{ symbol.qualified_name }}\n")
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "build",
            "demo.hpp",
            "-o",
            "out",
            "--root-document",
            "api_root",
            "--path-base",
            ".",
            "--template-dir",
            "templates",
            "--template",
            "namespace=my_ns",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (project / "out" / "api_root.md").is_file()
    assert not (project / "out" / "index.md").exists()
    assert "OVERRIDE demo" in (project / "out" / "demo.md").read_text()


def test_cli_build_rejects_malformed_template_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "demo.hpp").write_text("/// ns\nnamespace demo {}\n")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["build", "demo.hpp", "--template", "no-equals-sign"])
    assert result.exit_code != 0
    assert "KIND=STEM" in result.output


def test_cli_build_missing_input_exits_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A no-match input fails with a clean message, not a raw traceback.
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["build", "absent.hpp"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Error:" in result.output


# --- diagnostics log --------------------------------------------------------

# A warning clang always emits under the default flags, unlike the unused-*
# families, which need the function bodies the parser skips.
WARNING_FIXTURE = '#warning "demo is on its way out"\n' + FIXTURE

# A redefinition: an error carrying a "previous definition is here" note — the
# explanatory half that error-only capture drops.
ERROR_FIXTURE = "struct Widget { int a; };\nstruct Widget { int b; };\n"


def _log_header(text: str) -> dict[str, str]:
    """Parse the ``# key: value`` header block at the top of a log."""
    header = {}
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        key, _, value = line[1:].partition(":")
        header[key.strip()] = value.strip()
    return header


@requires_libclang
def test_no_diagnostics_log_written_by_default(project: Path) -> None:
    result = build(Config(input=["demo.hpp"], output_dir="api"), base_dir=project)

    assert result.diagnostics_log is None
    assert result.diagnostic_records == []
    assert list(project.glob("*.log")) == []


@requires_libclang
def test_diagnostics_log_written_with_header(project: Path) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", diagnostics_log="parse.log")
    result = build(config, base_dir=project)

    log = project / "parse.log"
    assert result.diagnostics_log == log.resolve()
    header = _log_header(log.read_text())
    assert header["parse"] == "full"
    assert header["inputs"] == "1 file(s)"
    assert "generated" in header
    assert "totals" in header


@requires_libclang
def test_diagnostics_log_creates_parent_directories(project: Path) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", diagnostics_log="_build/logs/parse.log")
    result = build(config, base_dir=project)

    assert (project / "_build" / "logs" / "parse.log").is_file()
    assert result.diagnostics_log == (project / "_build" / "logs" / "parse.log").resolve()


@requires_libclang
def test_diagnostics_log_absolute_path_used_verbatim(project: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "parse.log"
    config = Config(input=["demo.hpp"], output_dir="api", diagnostics_log=str(target))
    result = build(config, base_dir=project)

    assert result.diagnostics_log == target.resolve()
    assert target.is_file()


@requires_libclang
def test_warnings_reach_the_log_but_not_the_console_stream(project: Path) -> None:
    # The whole point of the feature: full detail in the file, an unchanged
    # (and here, empty) warning stream for the Sphinx build to fail -W on.
    (project / "demo.hpp").write_text(WARNING_FIXTURE)
    config = Config(input=["demo.hpp"], output_dir="api", diagnostics_log="parse.log")
    result = build(config, base_dir=project)

    assert result.diagnostics == []
    text = (project / "parse.log").read_text()
    assert "demo is on its way out" in text
    assert "warning" in _log_header(text)["totals"]
    assert any(record.severity == 2 for record in result.diagnostic_records)


@requires_libclang
def test_error_notes_are_logged_indented_under_their_parent(project: Path) -> None:
    (project / "demo.hpp").write_text(ERROR_FIXTURE)
    config = Config(input=["demo.hpp"], output_dir="api", diagnostics_log="parse.log")
    build(config, base_dir=project)

    lines = (project / "parse.log").read_text().splitlines()
    parent = next(i for i, line in enumerate(lines) if "redefinition" in line)
    note = lines[parent + 1]
    assert "previous definition" in note
    assert note.startswith("  ")


@requires_libclang
def test_a_failed_parse_explains_itself_in_the_log(project: Path) -> None:
    # libclang hands back no translation unit — and with it no diagnostics at
    # all — when it refuses a command, so without clangquill's own diagnosis a
    # failed input is a bare "failed to parse" line and nothing else. Here the
    # database entry carries a second input file, which is the way a
    # compile_commands.json breaks a header parse in practice.
    (project / "demo.hpp").write_text(ERROR_FIXTURE)
    (project / "other.cpp").write_text("int other() { return 1; }\n")
    entry = {
        "directory": str(project),
        "file": "demo.hpp",
        "arguments": ["clang++", "-std=c++20", "-c", "demo.hpp", str(project / "other.cpp")],
    }
    (project / "compile_commands.json").write_text(json.dumps([entry]))
    config = Config(
        input=["demo.hpp"],
        output_dir="api",
        compile_commands=".",
        diagnostics_log="parse.log",
    )
    result = build(config, base_dir=project)

    lines = (project / "parse.log").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("failed to parse:"))
    # The group is self-delimiting: the failure line is unindented and every
    # note under it is indented, so it ends at the next unindented line. A fixed
    # slice would break silently the day a note is added.
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i] and not lines[i].startswith(" ")),
        len(lines),
    )
    group = lines[start:end]
    assert "CXError" in group[0]
    assert any("names a second input file" in line for line in group)
    assert any("-std=c++20" in line and "compilation database" in line for line in group)
    # The recovered diagnostics sit one level deeper than the note introducing
    # them, so the log shows they came from a different command line.
    recovered = next(line for line in group if "redefinition" in line)
    assert recovered.startswith("    ")
    # Both halves of the contract: the failure reaches the console stream, the
    # notes explaining it do not.
    assert any("failed to parse:" in line for line in result.diagnostics)
    assert all("note:" not in line for line in result.diagnostics)


@requires_libclang
def test_diagnostics_log_untouched_by_a_noop_build(project: Path) -> None:
    config = Config(
        input=["demo.hpp"],
        output_dir="api",
        cache_dir=".cache",
        diagnostics_log="parse.log",
    )
    build(config, base_dir=project)
    log = project / "parse.log"
    before = log.read_bytes()

    result = build(config, base_dir=project)

    # Nothing was re-parsed, so the previous log is left alone rather than
    # truncated to silence.
    assert result.parsed is False
    assert result.diagnostics_log is None
    assert log.read_bytes() == before


@requires_libclang
def test_diagnostics_log_labels_a_partial_reparse(project: Path) -> None:
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text("/// beta ns\nnamespace beta { /// g\nint g(); }\n")
    config = Config(
        input=["alpha.hpp", "beta.hpp"],
        output_dir="api",
        cache_dir=".cache",
        diagnostics_log="parse.log",
    )
    build(config, base_dir=project)

    (project / "alpha.hpp").write_text("/// alpha ns edited\nnamespace alpha { /// f\nint f(); }\n")
    result = build(config, base_dir=project)

    assert result.parsed
    header = _log_header((project / "parse.log").read_text())
    assert header["parse"] == "incremental — 1 of 2 translation unit(s) re-parsed"


@requires_libclang
def test_enabling_the_log_invalidates_a_cached_parse(project: Path) -> None:
    # Otherwise turning the option on for an already-cached project would noop
    # straight past the parse and produce nothing to read.
    off = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    build(off, base_dir=project)

    on = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache", diagnostics_log="parse.log")
    result = build(on, base_dir=project)

    assert result.parsed
    assert (project / "parse.log").is_file()


@requires_libclang
def test_diagnostics_log_leaves_no_staging_file(project: Path) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", diagnostics_log="parse.log")
    build(config, base_dir=project)

    assert list(project.glob("*.tmp")) == []


# --- warnings as errors ------------------------------------------------------


@requires_libclang
def test_diagnostic_counts_are_empty_without_full_capture(project: Path) -> None:
    # Neither knob is on, so the core never collects warnings and there is
    # nothing to count — an empty dict, not a zeroed one.
    (project / "demo.hpp").write_text(WARNING_FIXTURE)
    result = build(Config(input=["demo.hpp"], output_dir="api"), base_dir=project)

    assert result.diagnostic_counts == {}


@requires_libclang
def test_header_only_project_builds_from_a_tests_only_database(tmp_path: Path) -> None:
    """A library whose only translation units are its tests still documents.

    A compile database lists translation units, never the headers they include,
    so a header-only library has no entry for anything it wants documented.
    libclang answers such a lookup with the closest listed file's command, which
    for this shape is the test that includes the header -- and that carries the
    include dirs and defines the header is meant to be read with.
    """
    (tmp_path / "include").mkdir()
    (tmp_path / "tests").mkdir()
    # Only visible with the -D the test's entry carries, so finding it proves
    # the flags really were borrowed rather than guessed.
    (tmp_path / "include" / "demo.hpp").write_text(
        "#pragma once\n#ifdef DEMO_FEATURE\n" + FIXTURE + "#endif\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_demo.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (tmp_path / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "file": str(tmp_path / "tests" / "test_demo.cpp"),
                    "arguments": [
                        "c++",
                        "-std=c++20",
                        f"-I{tmp_path / 'include'}",
                        "-DDEMO_FEATURE=1",
                        "-c",
                        str(tmp_path / "tests" / "test_demo.cpp"),
                    ],
                },
            ],
        ),
        encoding="utf-8",
    )

    config = Config(input=["include/demo.hpp"], output_dir="api", compile_commands=".")
    result = build(config, base_dir=tmp_path)

    assert (tmp_path / "api" / "index.md").is_file()
    assert result.symbol_count > 0
    # Borrowed flags are never silent, but they are never fatal either.
    assert result.diagnostics == []
    borrowed = [r for r in result.diagnostic_records if "no compilation database entry" in r.text]
    assert len(borrowed) == 1
    assert borrowed[0].severity == 2

    # Nothing was written next to the sources or into the build.
    assert not list(tmp_path.glob("*.d"))
    assert not list(tmp_path.glob("*.o"))


@requires_libclang
def test_warnings_as_errors_fails_on_borrowed_compile_flags(tmp_path: Path) -> None:
    """Strict mode treats borrowed flags as the not-quite-right parse they are."""
    (tmp_path / "demo.hpp").write_text(FIXTURE, encoding="utf-8")
    (tmp_path / "other.cpp").write_text("int other() { return 1; }\n", encoding="utf-8")
    (tmp_path / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "file": str(tmp_path / "other.cpp"),
                    "arguments": ["c++", "-std=c++20", "-c", str(tmp_path / "other.cpp")],
                },
            ],
        ),
        encoding="utf-8",
    )

    config = Config(
        input=["demo.hpp"],
        output_dir="api",
        compile_commands=".",
        warnings_as_errors=True,
    )
    result = build(config, base_dir=tmp_path)

    offenders = pipeline.warnings_or_worse(result.diagnostic_records)
    assert [r.text for r in offenders if "no compilation database entry" in r.text]
    # The pages are still written: the verdict belongs to the front end.
    assert (tmp_path / "api" / "index.md").is_file()


@requires_libclang
def test_warnings_as_errors_captures_and_counts_warnings(project: Path) -> None:
    # Turning strict mode on has to switch full capture on by itself: without a
    # diagnostics log configured there would otherwise be no warning to judge.
    (project / "demo.hpp").write_text(WARNING_FIXTURE)
    config = Config(input=["demo.hpp"], output_dir="api", warnings_as_errors=True)
    result = build(config, base_dir=project)

    assert result.diagnostic_counts.get("warning") == 1
    offenders = pipeline.warnings_or_worse(result.diagnostic_records)
    assert [record.severity for record in offenders] == [2]
    assert "demo is on its way out" in offenders[0].text
    # The pages are still written: the verdict belongs to the front end.
    assert (project / "api" / "index.md").is_file()


@requires_libclang
def test_warnings_as_errors_reports_a_clean_parse_as_clean(project: Path) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", warnings_as_errors=True)
    result = build(config, base_dir=project)

    assert pipeline.warnings_or_worse(result.diagnostic_records) == []


@requires_libclang
def test_warnings_as_errors_never_serves_a_cached_verdict(project: Path) -> None:
    # The trap this guards: a second run of an unchanged project normally noops
    # past the parse, which would leave no diagnostics and silently pass a
    # strict build over a tree that is still warning.
    (project / "demo.hpp").write_text(WARNING_FIXTURE)
    config = Config(
        input=["demo.hpp"],
        output_dir="api",
        cache_dir=".cache",
        warnings_as_errors=True,
    )
    first = build(config, base_dir=project)
    second = build(config, base_dir=project)

    assert first.parsed
    assert second.parsed
    assert second.diagnostic_counts == first.diagnostic_counts


@requires_libclang
def test_warnings_as_errors_sees_a_warning_in_an_untouched_input(project: Path) -> None:
    # An incremental re-parse reports only the translation units it touched, so
    # editing a clean header must not let a warning in its unedited neighbour
    # drop out of the verdict.
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text(
        '#warning "beta is on its way out"\n/// beta ns\nnamespace beta { /// g\nint g(); }\n',
    )
    config = Config(
        input=["alpha.hpp", "beta.hpp"],
        output_dir="api",
        cache_dir=".cache",
        warnings_as_errors=True,
    )
    build(config, base_dir=project)

    (project / "alpha.hpp").write_text("/// alpha ns edited\nnamespace alpha { /// f\nint f(); }\n")
    result = build(config, base_dir=project)

    assert any("beta is on its way out" in record.text for record in result.diagnostic_records)


def test_severity_counts_omits_severities_that_never_occurred() -> None:
    records = [
        pipeline.Diagnostic(severity=3, depth=0, text="a.hpp:1:1: error: bad"),
        pipeline.Diagnostic(severity=1, depth=1, text="a.hpp:1:1: note: here"),
        pipeline.Diagnostic(severity=3, depth=0, text="b.hpp:2:1: error: worse"),
    ]

    assert pipeline.severity_counts(records) == {"note": 1, "error": 2}
    assert pipeline.severity_counts([]) == {}


def test_warnings_or_worse_drops_notes() -> None:
    records = [
        pipeline.Diagnostic(severity=2, depth=0, text="warn"),
        pipeline.Diagnostic(severity=1, depth=1, text="note"),
        pipeline.Diagnostic(severity=4, depth=0, text="fatal"),
        pipeline.Diagnostic(severity=0, depth=0, text="ignored"),
    ]

    assert [record.text for record in pipeline.warnings_or_worse(records)] == ["warn", "fatal"]


def test_write_diagnostics_log_orders_and_indents_records(tmp_path: Path) -> None:
    records = [
        pipeline.Diagnostic(severity=3, depth=0, text="a.hpp:1:1: error: bad"),
        pipeline.Diagnostic(severity=1, depth=1, text="b.hpp:2:1: note: because"),
        pipeline.Diagnostic(severity=2, depth=0, text="c.hpp:3:1: warning: meh"),
    ]
    log = tmp_path / "parse.log"
    pipeline.write_diagnostics_log(log, records, inputs=2, partial=1)

    text = log.read_text()
    header = _log_header(text)
    assert header["parse"] == "incremental — 1 of 2 translation unit(s) re-parsed"
    assert header["totals"] == "1 note(s), 1 warning(s), 1 error(s)"
    body = text.split("\n\n", 1)[1]
    assert body == "a.hpp:1:1: error: bad\n  b.hpp:2:1: note: because\n\nc.hpp:3:1: warning: meh\n"


def test_write_diagnostics_log_with_no_records(tmp_path: Path) -> None:
    log = tmp_path / "parse.log"
    pipeline.write_diagnostics_log(log, [], inputs=3)

    header = _log_header(log.read_text())
    assert header["totals"] == "none"
    assert header["parse"] == "full"


@requires_libclang
def test_diagnostics_log_untouched_by_a_render_only_rebuild(project: Path) -> None:
    # Parse cached, render config changed: libclang never ran, so there are no
    # diagnostics to report and the previous log must survive intact.
    config = Config(
        input=["demo.hpp"],
        output_dir="api",
        cache_dir=".cache",
        diagnostics_log="parse.log",
    )
    build(config, base_dir=project)
    log = project / "parse.log"
    before = log.read_bytes()

    deeper = Config(
        input=["demo.hpp"],
        output_dir="api",
        cache_dir=".cache",
        diagnostics_log="parse.log",
        toctree_maxdepth=4,
    )
    result = build(deeper, base_dir=project)

    assert result.parsed is False
    assert result.pages_written  # the render did re-run
    assert result.diagnostics_log is None
    assert log.read_bytes() == before


@requires_libclang
def test_relocating_the_diagnostics_log_materialises_the_new_path(project: Path) -> None:
    # The parse fingerprint tracks only whether full capture is on, so moving
    # the log would otherwise noop straight past the parse and leave the new
    # path empty. The missing file itself is what forces the re-parse.
    first = Config(
        input=["demo.hpp"],
        output_dir="api",
        cache_dir=".cache",
        diagnostics_log="a.log",
    )
    build(first, base_dir=project)
    assert (project / "a.log").is_file()

    second = Config(
        input=["demo.hpp"],
        output_dir="api",
        cache_dir=".cache",
        diagnostics_log="b.log",
    )
    result = build(second, base_dir=project)

    assert result.parsed
    assert result.diagnostics_log == (project / "b.log").resolve()
    assert (project / "b.log").is_file()


@requires_libclang
def test_deleting_the_diagnostics_log_rewrites_it(project: Path) -> None:
    config = Config(
        input=["demo.hpp"],
        output_dir="api",
        cache_dir=".cache",
        diagnostics_log="parse.log",
    )
    build(config, base_dir=project)
    (project / "parse.log").unlink()

    result = build(config, base_dir=project)

    assert result.parsed
    assert (project / "parse.log").is_file()

"""Tests for the parse -> SQLite -> MyST pipeline and the CLI that drives it."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from clangquill import _core, cli, pipeline
from clangquill.cache import BuildCache, file_sha256, hash_text
from clangquill.config import Config
from clangquill.pipeline import COMPILE_COMMANDS_NAME, MANIFEST_NAME, build
from clangquill.store import Store

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping


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
def test_stateless_rebuild_touches_nothing_when_nothing_changed(project: Path) -> None:
    # Without a cache_dir every build re-renders from scratch, but an identical
    # render must still leave the output directory alone: Sphinx re-reads pages
    # by mtime, and sphinx-autobuild watches the source directory it writes into.
    config = Config(input=["demo.hpp"], output_dir="api")
    build(config, base_dir=project)
    api = project / "api"
    for path in api.iterdir():
        os.utime(path, ns=(0, 0))

    build(config, base_dir=project)

    assert {path.name for path in api.iterdir() if path.stat().st_mtime_ns != 0} == set()


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
        rendered = pipeline._rendered_files(generator, config, tmp_path, cache=cache, render_fingerprint="rf")  # noqa: SLF001
        index_top_level = dict(rendered)["index.md"]

        nested_plan = PagePlan("geo", "geo", lambda: "geo text", top_level=False)
        monkeypatch.setattr(generator, "plan_pages", lambda **_kw: [nested_plan])
        rendered = pipeline._rendered_files(generator, config, tmp_path, cache=cache, render_fingerprint="rf")  # noqa: SLF001
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
def test_incremental_detects_an_edit_made_during_the_parse(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    header = project / "demo.hpp"
    # Same length as the original, so the size half of the stat fast-path cannot
    # catch the edit either — the common case for a C++ header tweak.
    edited = FIXTURE.replace("A documented widget.", "A documented gadget.")
    assert len(edited) == len(FIXTURE)

    real_full = _core.parse_to_sqlite

    def racing_parse(inputs: list[str], db: str, opt: object) -> object:
        result = real_full(inputs, db, opt)
        # The editor saves while libclang is still working: the hash just
        # recorded describes the old text, the file on disk the new one.
        header.write_text(edited)
        return result

    monkeypatch.setattr(_core, "parse_to_sqlite", racing_parse)
    build(config, base_dir=project)
    monkeypatch.setattr(_core, "parse_to_sqlite", real_full)

    # Without the restat guard the cache would trust the post-edit stat against
    # the pre-edit hash and noop here, hiding the edit until the file moves
    # again. It has to re-parse and re-render instead.
    result = build(config, base_dir=project)

    assert result.parsed
    assert "gadget" in (project / "api" / "demo.md").read_text()


@requires_libclang
def test_incremental_recovers_from_a_truncated_ir(project: Path) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)
    ir = project / ".cache" / "clangquill.sqlite"

    # A build killed mid-write (or a full disk) leaves a truncated IR. Nothing
    # touches the inputs, so the cache still believes the parse is current and
    # would otherwise open this file and raise.
    ir.write_bytes(ir.read_bytes()[:64])
    (project / "api" / "demo.md").unlink()

    result = build(config, base_dir=project)

    assert result.parsed  # recovered by re-parsing from scratch
    assert result.symbol_count > 0
    assert (project / "api" / "demo.md").is_file()
    # The replacement IR is a real database again, so the next build noops.
    with Store.open(ir) as store:
        assert store.symbol_count() == result.symbol_count
    assert not build(config, base_dir=project).parsed


@requires_libclang
def test_incremental_recovers_from_an_ir_of_a_foreign_schema(project: Path) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)
    ir = project / ".cache" / "clangquill.sqlite"

    # An IR left behind by another clangquill version: readable SQLite, wrong
    # schema. Store.open rejects it, so the build has to discard and re-parse.
    con = sqlite3.connect(ir)
    con.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(_core.SCHEMA_VERSION + 1),))
    con.commit()
    con.close()

    result = build(config, base_dir=project)

    assert result.parsed
    with Store.open(ir) as store:
        assert store.meta("schema_version") == str(_core.SCHEMA_VERSION)


@requires_libclang
def test_incremental_recovers_from_a_truncated_ir_on_the_partial_path(project: Path) -> None:
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text("/// beta ns\nnamespace beta { /// g\nint g(); }\n")
    config = Config(input=["alpha.hpp", "beta.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)
    ir = project / ".cache" / "clangquill.sqlite"

    # One input changed, so the cache would take the per-TU path and re-parse
    # into a copy of this file — which the C++ writer cannot open either.
    ir.write_bytes(b"not a database at all")
    (project / "alpha.hpp").write_text("/// alpha ns edited\nnamespace alpha { /// f\nint f(); }\n")

    result = build(config, base_dir=project)

    assert result.parsed
    # A full re-parse, so both units are back in the IR, not just the stale one.
    assert sorted(p.name for p in (project / "api").glob("*.md")) == ["alpha.md", "beta.md", "index.md"]


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
    # Bytes, not text: a text round-trip would translate the newlines on
    # Windows and genuinely change the file, testing the opposite of this.
    page.write_bytes(page.read_bytes())
    result = build(config, base_dir=project)
    assert not result.parsed
    assert result.pages_written == []


@requires_libclang
def test_pages_are_written_as_the_bytes_their_hash_covers(project: Path) -> None:
    # The invariant the whole incremental path rests on: a page's record holds
    # the hash of the rendered *text*, while _output_intact re-checks the page
    # by hashing its *bytes*. Any newline translation on the way to disk breaks
    # that equality — on Windows it would make every touched page read as
    # damaged — and nothing else in the suite would notice on a POSIX runner.
    config = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)

    page = project / "api" / "demo.md"
    assert file_sha256(page) == hash_text(page.read_text(encoding="utf-8"))


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
def test_incremental_comment_parser_env_override_busts_noop_skip(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    first = build(config, base_dir=project)
    assert first.parsed
    assert "A documented namespace." in (project / "api" / "demo.md").read_text()

    # The env override changes every parsed comment (IR untouched), so the
    # render fingerprint must notice it rather than replaying the cached page.
    monkeypatch.setenv("CLANGQUILL_COMMENT_PARSER", "tests.test_comments.shouting_parser")
    result = build(config, base_dir=project)
    assert not result.parsed
    assert "demo.md" in result.pages_written
    assert "A DOCUMENTED NAMESPACE." in (project / "api" / "demo.md").read_text()


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

    # ... and on what the render writes back: the pages it rendered, plus the
    # full page set so vanished pages are still pruned.
    recorded: list[tuple[list[str], list[str]]] = []
    real_record = BuildCache.record_pages

    def record_spy(
        self: BuildCache,
        pages: Mapping[str, tuple[str, str]],
        *,
        stems: Collection[str] | None = None,
    ) -> None:
        recorded.append((sorted(pages), sorted(stems if stems is not None else pages)))
        real_record(self, pages, stems=stems)

    monkeypatch.setattr(BuildCache, "record_pages", record_spy)

    (project / "alpha.hpp").write_text("/// alpha ns edited\nnamespace alpha { /// f\nint f(); }\n")
    result = build(config, base_dir=project)

    assert result.parsed
    # Only alpha's page is re-rendered; beta and the toctree replay from cache.
    assert misses == ["alpha"]
    assert "beta" in hits
    assert "index" in hits
    assert result.pages_written == ["alpha.md"]
    assert "alpha ns edited" in (project / "api" / "alpha.md").read_text()
    # Written back: only alpha. Named as still present: every page.
    assert recorded == [(["alpha"], ["alpha", "beta", "index"])]


def test_page_cache_mode_follows_the_template_declaration(tmp_path: Path) -> None:
    templates = tmp_path / "my_templates"
    templates.mkdir()

    # Bundled templates only -- including a `templates` mapping that just points
    # kinds at other bundled stems -- memoise on the default fingerprint.
    assert pipeline._page_cache_mode(Config(input=[]), tmp_path) == (True, False)  # noqa: SLF001
    assert pipeline._page_cache_mode(Config(input=[], templates={"method": "function"}), tmp_path) == (  # noqa: SLF001
        True,
        False,
    )
    # A directory holding no template at all is still a bundled-only build.
    (templates / "README.md").write_text("not a template\n")
    config = Config(input=[], template_dirs=["my_templates"])
    assert pipeline._page_cache_mode(config, tmp_path) == (True, False)  # noqa: SLF001

    # A declared override memoises on the wide fingerprint.
    (templates / "class.md.jinja").write_text("{# clangquill:page-cache #}\n{{ symbol.spelling }}\n")
    assert pipeline._page_cache_mode(config, tmp_path) == (True, True)  # noqa: SLF001

    # One undeclared template is enough to disable memoisation for the build.
    (templates / "enum.md.jinja").write_text("{{ symbol.spelling }}\n")
    assert pipeline._page_cache_mode(config, tmp_path) == (False, False)  # noqa: SLF001


@requires_libclang
def test_declared_custom_template_keeps_page_memoisation(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A custom template used to disable per-page memoisation outright, so the
    # users who invest most in their output got the worst warm builds. One that
    # declares what it reads gets the same replay the bundled templates get.
    templates = project / "my_templates"
    templates.mkdir()
    (templates / "namespace.md.jinja").write_text(
        "{# clangquill:page-cache #}\n{{ '#' * level }} My `{{ symbol.qualified_name }}`\n",
        encoding="utf-8",
    )
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text("/// beta ns\nnamespace beta { /// g\nint g(); }\n")
    config = Config(
        input=["alpha.hpp", "beta.hpp"],
        output_dir="api",
        cache_dir=".cache",
        template_dirs=["my_templates"],
    )
    build(config, base_dir=project)
    assert (project / "api" / "alpha.md").read_text().startswith("# My `alpha`")

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
    assert misses == ["alpha"]
    assert "beta" in hits


@requires_libclang
def test_undeclared_custom_template_renders_every_page(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without the declaration the template may read anything at all, so the
    # build keeps the full-render path: the page cache is never consulted.
    templates = project / "my_templates"
    templates.mkdir()
    (templates / "namespace.md.jinja").write_text(
        "{{ '#' * level }} My `{{ symbol.qualified_name }}`\n",
        encoding="utf-8",
    )
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text("/// beta ns\nnamespace beta { /// g\nint g(); }\n")
    config = Config(
        input=["alpha.hpp", "beta.hpp"],
        output_dir="api",
        cache_dir=".cache",
        template_dirs=["my_templates"],
    )
    build(config, base_dir=project)

    consulted: list[str] = []
    monkeypatch.setattr(BuildCache, "cached_page", lambda _self, stem, _key: consulted.append(stem))

    (project / "alpha.hpp").write_text("/// alpha ns edited\nnamespace alpha { /// f\nint f(); }\n")
    result = build(config, base_dir=project)

    assert result.parsed
    assert consulted == []


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

    # The writer parses every input before it opens the IR and replaces the
    # stale rows in one transaction, so a failure leaves the on-disk IR
    # byte-for-byte unchanged.
    assert ir.read_bytes() == ir_before
    # Sanity: no temp DB was left lingering in the cache directory.
    assert not list((project / ".cache").glob("tmp*.sqlite"))

    # With the patch removed, the next build recovers and re-parses both inputs.
    monkeypatch.undo()
    recovered = build(config, base_dir=project)
    assert recovered.parsed
    assert {"alpha.md", "beta.md"}.issubset(set(recovered.pages_written))


@requires_libclang
def test_incremental_partial_parse_writes_the_ir_in_place(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The partial re-parse used to run against a full copy of the IR, so a
    # one-file edit paid an O(project) read+write before parsing anything. The
    # writer's own transaction gives the same all-or-nothing (see
    # test_incremental_partial_parse_failure_is_atomic), so nothing is staged.
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text("/// beta ns\nnamespace beta { /// g\nint g(); }\n")
    config = Config(input=["alpha.hpp", "beta.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=project)

    # Every staging path goes through _new_temp_db, so counting its calls covers
    # a copy reintroduced under any name.
    staged: list[object] = []
    real_temp_db = pipeline._new_temp_db  # noqa: SLF001

    def spy(directory: Path | None = None) -> Path:
        path = real_temp_db(directory)
        staged.append(path)
        return path

    monkeypatch.setattr(pipeline, "_new_temp_db", spy)

    (project / "alpha.hpp").write_text("/// alpha ns edit\nnamespace alpha { /// f\nint f(); }\n")
    result = build(config, base_dir=project)

    assert result.parsed
    assert result.pages_written == ["alpha.md"]
    assert staged == []
    # beta's rows came through the in-place write untouched.
    assert "beta ns" in (project / "api" / "beta.md").read_text()


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


HIERARCHICAL_FIXTURE = """
/// Geometry.
namespace geo {
/// A circle.
struct Circle {
  /// the area
  double area() const;
};
/// A square.
struct Square {
  /// the side length
  double side;
};
/// Scale a value.
int scale(int f);
}
"""

# The same header with Square removed, for the rebuild half of the tests below.
HIERARCHICAL_FIXTURE_WITHOUT_SQUARE = HIERARCHICAL_FIXTURE.replace(
    """/// A square.
struct Square {
  /// the side length
  double side;
};
""",
    "",
)


@requires_libclang
def test_class_mode_pages_records_and_rewrites_the_index_on_removal(project: Path) -> None:
    # group_by="class" had only ever been driven as a generator unit test over a
    # hand-built fixture database. Through the pipeline it also owns page-cache
    # keys, page deletion and index rewriting, all of which are mode-specific:
    # here the flat root index is the toctree that has to shrink.
    header = project / "geo.hpp"
    header.write_text(HIERARCHICAL_FIXTURE)
    config = Config(input=["geo.hpp"], output_dir="api", group_by="class", cache_dir=".cache")
    api = project / "api"

    first = build(config, base_dir=project)
    assert first.pages == ["geo", "geo_Circle", "geo_Square"]
    # Each record earns a page; the namespace page keeps only its leaf members.
    assert "{cpp:function} int geo::scale" in (api / "geo.md").read_text()
    assert "{cpp:struct} geo::Circle" not in (api / "geo.md").read_text()
    assert (api / "geo_Circle.md").read_text().startswith("# Struct `geo::Circle`")
    assert "geo_Square" in (api / "index.md").read_text()

    header.write_text(HIERARCHICAL_FIXTURE_WITHOUT_SQUARE)
    second = build(config, base_dir=project)

    assert second.parsed
    assert second.pages_deleted == ["geo_Square.md"]
    assert not (api / "geo_Square.md").exists()
    # Only the index changes: it lists every page in this mode, so dropping one
    # rewrites it, while the surviving pages replay from the page cache.
    assert second.pages_written == ["index.md"]
    index = (api / "index.md").read_text()
    assert "geo_Square" not in index
    assert "geo_Circle" in index


@requires_libclang
def test_namespace_mode_rewrites_the_hub_toctree_on_removal(project: Path) -> None:
    # The mode the issue calls out: removing a class must delete its page *and*
    # rewrite the namespace hub whose toctree links it. The root index links
    # only the hub here, so it is precisely the page that must not change.
    header = project / "geo.hpp"
    header.write_text(HIERARCHICAL_FIXTURE)
    config = Config(input=["geo.hpp"], output_dir="api", group_by="namespace", cache_dir=".cache")
    api = project / "api"

    first = build(config, base_dir=project)
    assert set(first.pages) == {"geo", "geo_Circle", "geo_Square", "geo_scale"}
    hub = (api / "geo.md").read_text()
    assert "```{toctree}" in hub
    assert "Square <geo_Square>" in hub
    # The hub links member bodies rather than inlining them.
    assert "{cpp:struct}" not in hub
    assert (api / "index.md").read_text().count("geo") == 1  # the hub, nothing deeper

    before = _mtimes(api)
    header.write_text(HIERARCHICAL_FIXTURE_WITHOUT_SQUARE)
    second = build(config, base_dir=project)

    assert second.parsed
    assert second.pages_deleted == ["geo_Square.md"]
    assert not (api / "geo_Square.md").exists()
    # The hub is the only page whose text changed; the root index still points
    # at the same single namespace, and the sibling pages are untouched.
    assert second.pages_written == ["geo.md"]
    hub = (api / "geo.md").read_text()
    assert "Square <geo_Square>" not in hub
    assert "Circle <geo_Circle>" in hub
    after = _mtimes(api)
    assert after["index.md"] == before["index.md"]
    assert after["geo_Circle.md"] == before["geo_Circle.md"]


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


ANONYMOUS_NS_FIXTURE = """
namespace demo {
/// Public API a caller can name.
inline int visible() { return 1; }
namespace {
/// An internal helper: internal linkage, one translation unit only.
inline int hidden_helper() { return 2; }
}
}
"""


@requires_libclang
def test_anonymous_namespace_contents_are_hidden_by_default(tmp_path: Path) -> None:
    # Internal linkage is not API: without the opt-in nothing from the
    # anonymous namespace is rendered — least of all under ``demo::``, where
    # eliding the unnamed scope used to put it.
    (tmp_path / "anon.hpp").write_text(ANONYMOUS_NS_FIXTURE)
    build(Config(input=["anon.hpp"], output_dir="api"), base_dir=tmp_path)

    page = (tmp_path / "api" / "demo.md").read_text()
    assert "demo::visible" in page
    assert "hidden_helper" not in page


@requires_libclang
def test_opting_in_renders_anonymous_contents_under_the_scope(tmp_path: Path) -> None:
    # With the opt-in they are documented, qualified by ``@anonymous`` — the
    # Sphinx C++ domain's spelling for an anonymous entity, so the emitted
    # declaration still parses — and never under ``demo::`` alone.
    (tmp_path / "anon.hpp").write_text(ANONYMOUS_NS_FIXTURE)
    build(
        Config(input=["anon.hpp"], output_dir="api", extract_anonymous_namespaces=True),
        base_dir=tmp_path,
    )

    page = (tmp_path / "api" / "demo.md").read_text()
    assert "demo::@anonymous::hidden_helper" in page
    assert "demo::hidden_helper" not in page


def test_parse_options_and_fingerprint_carry_anonymous_namespaces(tmp_path: Path) -> None:
    # The knob reaches the core parse options, and it changes which symbols the
    # parse extracts at all -- so a cached IR built with the other setting must
    # not be served for it.
    off = Config(input=["a.hpp"])
    on = Config(input=["a.hpp"], extract_anonymous_namespaces=True)

    assert pipeline._parse_options(off, tmp_path).extract_anonymous_namespaces is False  # noqa: SLF001
    assert pipeline._parse_options(on, tmp_path).extract_anonymous_namespaces is True  # noqa: SLF001
    assert pipeline._parse_fingerprint(off, tmp_path, ["a.hpp"]) != pipeline._parse_fingerprint(  # noqa: SLF001
        on,
        tmp_path,
        ["a.hpp"],
    )


@requires_libclang
def test_compile_commands_is_read_once_per_build(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The database was resolved (and re-parsed) once for the up-front check,
    # once for the parse options and once for the parse fingerprint, then read a
    # fourth time to hash it. A monorepo-sized database makes that noticeable,
    # and one read answers all four questions.
    (project / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(project),
                    "file": str(project / "demo.hpp"),
                    "arguments": ["c++", "-std=c++20", "-c", str(project / "demo.hpp")],
                },
            ],
        ),
        encoding="utf-8",
    )
    config = Config(input=["demo.hpp"], output_dir="api", compile_commands=".", cache_dir=".cache")

    # Every way the file was ever read: as bytes, as text, and through the
    # separate hashing read.
    reads: list[str] = []
    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text
    real_file_sha256 = pipeline.file_sha256

    def spy_read_bytes(self: Path) -> bytes:
        if self.name == COMPILE_COMMANDS_NAME:
            reads.append("read_bytes")
        return real_read_bytes(self)

    def spy_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == COMPILE_COMMANDS_NAME:
            reads.append("read_text")
        return real_read_text(self, *args, **kwargs)

    def spy_file_sha256(path: str | Path) -> str:
        if Path(path).name == COMPILE_COMMANDS_NAME:
            reads.append("file_sha256")
        return real_file_sha256(path)

    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    monkeypatch.setattr(pipeline, "file_sha256", spy_file_sha256)

    result = build(config, base_dir=project)

    assert result.parsed
    assert reads == ["read_bytes"]


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
def test_incremental_renamed_input_drops_stale_rows_and_pages(project: Path) -> None:
    # A glob-driven project: renaming the input on disk changes the resolved
    # input set, so the glob picks up renamed.hpp and drops demo.hpp. The old
    # file's IR rows and page must not survive the rebuild (see #199, whose
    # store-side stale-row bug could otherwise leak into this exact scenario).
    config = Config(input=["*.hpp"], output_dir="api", cache_dir=".cache", group_by="file")
    build(config, base_dir=project)
    api = project / "api"
    assert (api / "demo_hpp.md").is_file()

    (project / "demo.hpp").rename(project / "renamed.hpp")
    result = build(config, base_dir=project)

    assert result.parsed
    assert (api / "renamed_hpp.md").is_file()
    assert not (api / "demo_hpp.md").exists()

    ir = project / ".cache" / "clangquill.sqlite"
    with Store.open(ir) as store:
        tracked = {Path(f.path).name for f in store.files()}
    assert tracked == {"renamed.hpp"}
    assert result.file_count == len(tracked)

    # No stale bookkeeping keeps flagging work: the next build noops cleanly.
    assert not build(config, base_dir=project).parsed


@requires_libclang
def test_incremental_removed_input_prunes_pages_and_ir_together(tmp_path: Path) -> None:
    # test_build_prunes_stale_pages exercises page pruning without a cache_dir;
    # this pins down the same removal with the cache warm, so pruning and the
    # IR/cache bookkeeping for a dropped input are exercised together.
    (tmp_path / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (tmp_path / "beta.hpp").write_text("/// beta ns\nnamespace beta { /// g\nint g(); }\n")
    config = Config(input=["*.hpp"], output_dir="api", cache_dir=".cache")
    build(config, base_dir=tmp_path)
    api = tmp_path / "api"
    assert (api / "alpha.md").is_file()
    assert (api / "beta.md").is_file()

    # Remove one input entirely while the cache is warm.
    (tmp_path / "beta.hpp").unlink()
    result = build(config, base_dir=tmp_path)

    assert result.parsed
    assert (api / "alpha.md").is_file()
    assert not (api / "beta.md").exists()

    ir = tmp_path / ".cache" / "clangquill.sqlite"
    with Store.open(ir) as store:
        tracked = {Path(f.path).name for f in store.files()}
    assert tracked == {"alpha.hpp"}
    assert result.file_count == len(tracked)
    symbols = {s.qualified_name for s in _store_symbols(result.db_path)}
    assert "alpha" in symbols
    assert not any(name.startswith("beta") for name in symbols)

    # A follow-up build with the surviving input noops cleanly.
    assert not build(config, base_dir=tmp_path).parsed


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


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive-filesystem semantics only apply on Windows")
def test_resolve_inputs_normalizes_case_on_windows(tmp_path: Path) -> None:
    # NTFS is case-insensitive but case-preserving. ``BuildCache.deps_only_from``
    # and ``tu_inputs`` (cache.py) key straight off these resolved strings, so if
    # two spellings of the same input resolved to two different strings here, the
    # same physical translation unit would look like two distinct inputs to the
    # cache's per-TU dependency map -- silently breaking stale-row pruning across
    # a rebuild that happened to spell an input differently than the one before it
    # (issue 313). ``Path.resolve()`` is what keeps this safe: on Windows it asks
    # the OS for the file's one true on-disk spelling, whichever case the caller
    # used to name it.
    header = tmp_path / "Foo.hpp"
    header.write_text("// foo\n")

    resolved_matching_case = pipeline._resolve_inputs(["Foo.hpp"], tmp_path)  # noqa: SLF001
    resolved_other_case = pipeline._resolve_inputs(["FOO.HPP"], tmp_path)  # noqa: SLF001

    assert resolved_matching_case == resolved_other_case == [str(header.resolve())]


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
    header = _log_header(log.read_text(encoding="utf-8"))
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
    text = (project / "parse.log").read_text(encoding="utf-8")
    assert "demo is on its way out" in text
    assert "warning" in _log_header(text)["totals"]
    assert any(record.severity == 2 for record in result.diagnostic_records)


@requires_libclang
def test_error_notes_are_logged_indented_under_their_parent(project: Path) -> None:
    (project / "demo.hpp").write_text(ERROR_FIXTURE)
    config = Config(input=["demo.hpp"], output_dir="api", diagnostics_log="parse.log")
    build(config, base_dir=project)

    lines = (project / "parse.log").read_text(encoding="utf-8").splitlines()
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

    lines = (project / "parse.log").read_text(encoding="utf-8").splitlines()
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
    header = _log_header((project / "parse.log").read_text(encoding="utf-8"))
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


@requires_libclang
def test_a_fully_cached_build_replays_the_previous_diagnostics(project: Path) -> None:
    # Issue #207: without warnings_as_errors, a noop build must not silently
    # drop the diagnostics a Sphinx -W build relies on to keep failing.
    (project / "demo.hpp").write_text(ERROR_FIXTURE)
    config = Config(input=["demo.hpp"], output_dir="api", cache_dir=".cache")
    first = build(config, base_dir=project)
    second = build(config, base_dir=project)

    assert first.parsed
    assert second.parsed is False
    assert first.diagnostics
    assert second.diagnostics == first.diagnostics
    assert second.diagnostic_counts == first.diagnostic_counts
    assert [r.text for r in second.diagnostic_records] == [r.text for r in first.diagnostic_records]


@requires_libclang
def test_a_partial_reparse_keeps_diagnostics_from_an_untouched_input(project: Path) -> None:
    # The non-strict counterpart of test_warnings_as_errors_sees_a_warning_in_
    # an_untouched_input: even without warnings_as_errors forcing a full parse,
    # an incremental re-parse of one input must not drop another input's
    # still-standing error just because this run never touched it.
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text(ERROR_FIXTURE)
    config = Config(input=["alpha.hpp", "beta.hpp"], output_dir="api", cache_dir=".cache")
    first = build(config, base_dir=project)
    assert first.diagnostics

    (project / "alpha.hpp").write_text("/// alpha ns edited\nnamespace alpha { /// f\nint f(); }\n")
    second = build(config, base_dir=project)

    assert second.parsed
    assert second.diagnostics == first.diagnostics


@requires_libclang
def test_a_partial_reparse_clears_a_diagnostic_from_a_dropped_include(project: Path) -> None:
    # The other direction from the previous test: a diagnostic must not survive
    # forever once the file it lived in has actually left the build, or a fixed
    # project would keep failing a -W build on an error that no longer exists.
    (project / "leaf.hpp").write_text(ERROR_FIXTURE)
    (project / "beta.hpp").write_text('#include "leaf.hpp"\n/// beta ns\nnamespace beta { /// g\nint g(); }\n')
    config = Config(input=["beta.hpp"], output_dir="api", cache_dir=".cache")
    first = build(config, base_dir=project)
    assert first.diagnostics

    (project / "beta.hpp").write_text("/// beta ns\nnamespace beta { /// g\nint g(); }\n")
    second = build(config, base_dir=project)

    assert second.parsed
    assert second.diagnostics == []


@requires_libclang
def test_diagnostics_log_count_matches_the_log_not_the_project_wide_replay(project: Path) -> None:
    # Issue #319: diagnostic_records is the whole project's picture (including
    # diagnostics carried forward from untouched inputs on an incremental
    # reparse), but the log only ever holds this run's own parse. The count
    # reported alongside "wrote N diagnostic(s) to <log>" has to come from what
    # was actually written to the log, not from diagnostic_records.
    (project / "alpha.hpp").write_text("/// alpha ns\nnamespace alpha { /// f\nint f(); }\n")
    (project / "beta.hpp").write_text(
        '#warning "beta is on its way out"\n/// beta ns\nnamespace beta { /// g\nint g(); }\n',
    )
    config = Config(
        input=["alpha.hpp", "beta.hpp"],
        output_dir="api",
        cache_dir=".cache",
        diagnostics_log="parse.log",
    )
    build(config, base_dir=project)

    (project / "alpha.hpp").write_text("/// alpha ns edited\nnamespace alpha { /// f\nint f(); }\n")
    result = build(config, base_dir=project)

    # This run only reparsed the (clean) edited alpha.hpp, so nothing new to log.
    assert result.diagnostics_log_count == 0
    log_text = (project / "parse.log").read_text(encoding="utf-8")
    assert "beta is on its way out" not in log_text

    # But the project-wide picture still carries beta's still-standing warning.
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


def test_diagnostic_texts_drops_nested_records_even_at_error_severity() -> None:
    records = [
        pipeline.Diagnostic(severity=3, depth=0, text="a.hpp:1:1: error: bad"),
        pipeline.Diagnostic(severity=3, depth=2, text="a.hpp:1:1: error: recovered nested"),
        pipeline.Diagnostic(severity=1, depth=1, text="a.hpp:1:1: note: here"),
        pipeline.Diagnostic(severity=2, depth=0, text="a.hpp:2:1: warning: meh"),
        pipeline.Diagnostic(severity=4, depth=0, text="b.hpp:1:1: fatal: worse"),
    ]

    assert pipeline._diagnostic_texts(records) == [  # noqa: SLF001
        "a.hpp:1:1: error: bad",
        "b.hpp:1:1: fatal: worse",
    ]


def _carry_forward(
    previous: list[pipeline.Diagnostic],
    records: list[pipeline.Diagnostic],
    *,
    partial_deps: dict[str, list[str]] | None = None,
    dropped: list[str] | None = None,
) -> list[pipeline.Diagnostic]:
    return pipeline._carry_forward_diagnostics(  # noqa: SLF001
        previous,
        partial_deps if partial_deps is not None else {},
        dropped if dropped is not None else [],
        records,
    )


def test_carry_forward_keeps_a_diagnostic_from_an_untouched_file() -> None:
    previous = [pipeline.Diagnostic(severity=3, depth=0, text="b.hpp:1:1: error: bad", file="b.hpp")]
    fresh = [pipeline.Diagnostic(severity=2, depth=0, text="a.hpp:1:1: warning: meh", file="a.hpp")]

    carried = _carry_forward(previous, fresh, partial_deps={"a.hpp": ["a.hpp"]})

    assert [record.text for record in carried] == [
        "b.hpp:1:1: error: bad",
        "a.hpp:1:1: warning: meh",
    ]


def test_carry_forward_supersedes_a_diagnostic_from_a_reparsed_file() -> None:
    previous = [pipeline.Diagnostic(severity=3, depth=0, text="a.hpp:1:1: error: bad", file="a.hpp")]

    assert _carry_forward(previous, [], partial_deps={"a.hpp": ["a.hpp"]}) == []


def test_carry_forward_does_not_duplicate_a_location_less_diagnostic() -> None:
    # Issue #302: a command-line warning has no file, so the path check can
    # never supersede it — without the identity check every partial reparse
    # would keep the carried copy *and* append the re-emitted one, growing the
    # stored list (and the replayed warnings) by one per incremental build.
    command_line = pipeline.Diagnostic(severity=2, depth=0, text="warning: argument unused: '-lfoo'")
    previous = [command_line]

    carried = _carry_forward(previous, [command_line], partial_deps={"a.hpp": ["a.hpp"]})

    assert carried == [command_line]


def test_carry_forward_collapses_location_less_duplicates_from_an_older_cache() -> None:
    # A cache written before the fix above already carries the repeats; the
    # next incremental build has to fold them back into one rather than
    # replaying the accumulated list forever.
    command_line = pipeline.Diagnostic(severity=2, depth=0, text="warning: argument unused: '-lfoo'")
    previous = [command_line, command_line, command_line]

    assert _carry_forward(previous, [command_line], partial_deps={"a.hpp": ["a.hpp"]}) == [command_line]
    assert _carry_forward(previous, [], partial_deps={"a.hpp": ["a.hpp"]}) == [command_line]


def test_carry_forward_keeps_distinct_location_less_diagnostics() -> None:
    unused = pipeline.Diagnostic(severity=2, depth=0, text="warning: argument unused: '-lfoo'")
    deprecated = pipeline.Diagnostic(severity=2, depth=0, text="warning: '-std=c++11' is deprecated")

    carried = _carry_forward([unused, deprecated], [unused], partial_deps={"a.hpp": ["a.hpp"]})

    assert carried == [deprecated, unused]


def test_write_diagnostics_log_orders_and_indents_records(tmp_path: Path) -> None:
    records = [
        pipeline.Diagnostic(severity=3, depth=0, text="a.hpp:1:1: error: bad"),
        pipeline.Diagnostic(severity=1, depth=1, text="b.hpp:2:1: note: because"),
        pipeline.Diagnostic(severity=2, depth=0, text="c.hpp:3:1: warning: meh"),
    ]
    log = tmp_path / "parse.log"
    pipeline.write_diagnostics_log(log, records, inputs=2, partial=1)

    text = log.read_text(encoding="utf-8")
    header = _log_header(text)
    assert header["parse"] == "incremental — 1 of 2 translation unit(s) re-parsed"
    assert header["totals"] == "1 note(s), 1 warning(s), 1 error(s)"
    body = text.split("\n\n", 1)[1]
    assert body == "a.hpp:1:1: error: bad\n  b.hpp:2:1: note: because\n\nc.hpp:3:1: warning: meh\n"


def test_write_diagnostics_log_with_no_records(tmp_path: Path) -> None:
    log = tmp_path / "parse.log"
    pipeline.write_diagnostics_log(log, [], inputs=3)

    header = _log_header(log.read_text(encoding="utf-8"))
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

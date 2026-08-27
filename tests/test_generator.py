"""Tests for the MyST generator: golden output, overrides, xrefs, Sphinx build."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from clangquill.generator import Generator
from clangquill.store import Reference, RefKind, SourceFile, Store, Symbol

if TYPE_CHECKING:
    from collections.abc import Iterator

GOLDEN_DIR = Path(__file__).parent / "golden"
REGEN_ENV = "CLANGQUILL_REGEN_GOLDENS"


def _assert_golden(name: str, text: str) -> None:
    """Compare ``text`` to ``golden/<name>``; regenerate it when REGEN is set."""
    path = GOLDEN_DIR / name
    if os.environ.get(REGEN_ENV):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    expected = path.read_text(encoding="utf-8")
    assert text == expected


@pytest.fixture
def store(fixture_db: Path) -> Iterator[Store]:
    with Store.open(fixture_db) as opened:
        yield opened


@pytest.fixture
def gen(store: Store) -> Generator:
    return Generator(store)


def _symbol(store: Store, qualified_name: str) -> Symbol:
    sym = next((s for s in store.symbols() if s.qualified_name == qualified_name), None)
    assert sym is not None, f"missing fixture symbol {qualified_name!r}"
    return sym


def test_namespace_golden(gen: Generator, store: Store) -> None:
    rendered = gen.render_symbol(_symbol(store, "geo"), level=1)
    _assert_golden("geo.md", rendered)


def test_generate_writes_pages_and_index(gen: Generator, tmp_path: Path) -> None:
    out = tmp_path / "api"
    pages = gen.generate(out)

    assert pages == ["geo"]
    assert (out / "geo.md").is_file()
    _assert_golden("index.md", (out / "index.md").read_text())
    # The generated page is the same content the per-symbol render produces.
    assert (out / "geo.md").read_text().startswith("# Namespace `geo`")


def test_unique_stem_dedupes_case_insensitively() -> None:
    # `Foo` and `foo` are the same file on macOS/Windows, so they must not
    # share a stem.
    seen: set[str] = set()
    assert Generator._unique_stem("Foo", seen) == "Foo"  # noqa: SLF001
    assert Generator._unique_stem("foo", seen) == "foo_"  # noqa: SLF001
    assert Generator._unique_stem("FOO", seen) == "FOO__"  # noqa: SLF001


def test_conflicting_same_name_declarations_degrade_to_code_blocks(degraded_db: Path, tmp_path: Path) -> None:
    # Degraded extraction can emit the same name several times with conflicting
    # kinds (the eigen benchmark mis-extracts a template function as colliding
    # `cpp:var` declarations); Sphinx's C++ domain crashes on such duplicates
    # instead of warning, so only the first declaration of a name may emit a
    # domain directive — later conflicts render as plain code blocks.
    out = tmp_path / "api"
    with Store.open(degraded_db) as store:
        Generator(store).generate(out)

    page = (out / "Eigen.md").read_text()
    # First declaration keeps its directive; the two conflicting repeats do not.
    assert page.count("{cpp:var} int Eigen::plogical_shift_right") == 1
    assert page.count("{cpp:type}") == 0
    # The degraded repeats stay visible as plain C++ code blocks.
    assert page.count("```cpp") == 2
    # Legitimate overloads of one function name still render as directives.
    assert page.count("{cpp:function}") == 2


def test_declaration_registry_resets_between_pages(degraded_db: Path) -> None:
    # The registry is per page: rendering the same store twice (or the same
    # name on two different pages) must not degrade a later page's first
    # declaration. Rendering all pages twice yields identical output.
    with Store.open(degraded_db) as store:
        gen = Generator(store)
        first = [(p.stem, p.text) for p in gen.render_pages()]
        second = [(p.stem, p.text) for p in gen.render_pages()]
    assert first == second


def test_generate_avoids_root_document_and_case_collisions(collision_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "api"
    with Store.open(collision_db) as store:
        pages = Generator(store).generate(out)

    # A symbol named `index` must not collide with the toctree root document,
    # and `Foo`/`foo` must not collide with each other on a case-insensitive
    # filesystem.
    assert sorted(pages) == ["Foo", "foo_", "index_"]
    index = (out / "index.md").read_text()
    assert index.startswith("# API Reference")
    assert "index_" in index
    index_page = (out / "index_.md").read_text()
    assert index_page.startswith("# Function `index`")
    assert "{cpp:function} void index()" in index_page
    assert "{cpp:function} void foo()" in (out / "foo_.md").read_text()


def test_group_stem_matches_planned_stem_after_dedup(m7_db: Path) -> None:
    # Force the group's natural slug to collide so its planned stem is suffixed;
    # group_stem() (which templates use for subgroup links) must follow suit.
    with Store.open(m7_db) as store:
        gen = Generator(store)
        plans = gen.plan_pages(reserved_stems=("group_grp",))
        group_plan = next(p for p in plans if p.group is not None)
        assert group_plan.stem == "group_grp_"
        assert gen.group_stem(group_plan.group) == "group_grp_"


def test_emitted_directives_cover_each_kind(gen: Generator, store: Store) -> None:
    rendered = gen.render_symbol(_symbol(store, "geo"), level=1)
    for directive in ("{cpp:class}", "{cpp:function}", "{cpp:member}", "{cpp:enum}", "{cpp:enumerator}"):
        assert directive in rendered


def test_undocumented_symbol_is_present_but_marked(gen: Generator, store: Store) -> None:
    rendered = gen.render_symbol(_symbol(store, "geo::mystery"), level=2)
    assert "{cpp:function} void geo::mystery()" in rendered
    assert "*No documentation provided.*" in rendered


def test_signature_carries_qualified_name(gen: Generator, store: Store) -> None:
    # Out-of-line member declarations must be qualified so the C++ domain can
    # attach them to the right parent without nesting directives.
    assert gen.signature(_symbol(store, "geo::Circle::area")) == "double geo::Circle::area() const"
    assert gen.signature(_symbol(store, "geo::Circle::radius")) == "double geo::Circle::radius"


def test_base_clause_from_references(gen: Generator, store: Store) -> None:
    assert gen.signature(_symbol(store, "geo::Circle")) == "geo::Circle : public Shape"


def test_typedef_signature_uses_underlying_reference(gen: Generator, store: Store) -> None:
    assert gen.signature(_symbol(store, "geo::Distance")) == "geo::Distance = double"


def test_xref_resolves_usr_symbol_and_name(gen: Generator, store: Store) -> None:
    shape = _symbol(store, "geo::Shape")
    assert gen.xref(shape.usr) == "{cpp:any}`geo::Shape`"
    assert gen.xref(shape) == "{cpp:any}`geo::Shape`"
    assert gen.xref("geo::Circle", role="class") == "{cpp:class}`geo::Circle`"


def test_xref_unresolved_reference_degrades_to_code(gen: Generator) -> None:
    ref = Reference(
        from_usr="x",
        ref_kind=RefKind.PARAM_TYPE,
        to_usr="",
        to_spelling="std::size_t",
        is_resolved=False,
        access=gen.store.symbol("c:@N@geo").access,  # AccessKind.NONE
        ordinal=0,
    )
    assert gen.xref(ref) == "`std::size_t`"


def test_xref_string_url_degrades_to_autolink(gen: Generator) -> None:
    # Free-form @see/@sa URLs must not become C++ cross-references.
    url = "http://en.cppreference.com/w/cpp/utility/hash"
    result = gen.xref(url)
    assert result == f"<{url}>"
    assert "{cpp:" not in result


def test_xref_string_prose_degrades_to_plain_text(gen: Generator) -> None:
    # Multi-word prose is rendered verbatim, never as a cross-reference.
    prose = "IntersectionFunctor to a Walker."
    result = gen.xref(prose)
    assert result == prose
    assert "{cpp:" not in result


def test_xref_string_name_keeps_role(gen: Generator) -> None:
    # A bare C++ name is parseable, so it stays a role for the domain to resolve
    # (or silently ignore) -- never an "Unparseable" warning.
    assert gen.xref("some::Unknown") == "{cpp:any}`some::Unknown`"


def test_xref_string_strips_trailing_call_syntax(gen: Generator) -> None:
    # "geo::scale()." -> the cleaned name stays a parseable cross-reference.
    assert gen.xref("geo::scale().") == "{cpp:any}`geo::scale`"


def test_user_template_overrides_default_by_name(store: Store, tmp_path: Path) -> None:
    user_dir = tmp_path / "templates"
    user_dir.mkdir()
    (user_dir / "function.md.jinja").write_text("CUSTOM {{ symbol.qualified_name }}\n")

    overridden = Generator(store, template_dirs=[user_dir])
    rendered = overridden.render_symbol(_symbol(store, "geo::scale"))
    assert rendered.strip() == "CUSTOM geo::scale"

    # Other kinds still fall through to the bundled defaults.
    klass = overridden.render_symbol(_symbol(store, "geo::Shape"))
    assert "{cpp:class} geo::Shape" in klass


def test_include_undocumented_false_drops_leaf_but_keeps_scope(store: Store, tmp_path: Path) -> None:
    # The geo namespace itself is documented and holds documented members, so it
    # stays; the undocumented free function geo::mystery is suppressed.
    gen = Generator(store, include_undocumented=False)
    rendered = gen.render_symbol(_symbol(store, "geo"), level=1)
    assert "geo::Circle" in rendered
    assert "geo::mystery" not in rendered

    pages = gen.generate(tmp_path / "api")
    assert pages == ["geo"]


def test_group_by_file_writes_one_page_per_file(gen: Generator, tmp_path: Path) -> None:
    out = tmp_path / "api"
    pages = gen.generate(out, group_by="file")
    assert pages == ["geo_hpp"]
    page = (out / "geo_hpp.md").read_text()
    assert page.startswith("# File `geo.hpp`")
    assert "geo::Circle" in page


def test_group_by_class_splits_namespace_into_per_class_pages(gen: Generator, tmp_path: Path) -> None:
    # Where group_by="symbol" collapses the whole geo namespace onto one page,
    # group_by="class" descends through the namespace: each documented record
    # earns its own page, and the namespace keeps only its leaf members.
    out = tmp_path / "api"
    pages = gen.generate(out, group_by="class")
    assert pages == ["geo", "geo_Shape", "geo_Circle"]

    namespace = (out / "geo.md").read_text()
    assert namespace.startswith("# Namespace `geo`")
    # Leaf members of the namespace live on the namespace page ...
    assert "{cpp:function} Circle geo::scale" in namespace
    assert "{cpp:enum} geo::Color" in namespace
    assert "{cpp:type} geo::Distance" in namespace
    # ... but its record members are split out, not duplicated here.
    assert "{cpp:class} geo::Circle" not in namespace
    assert "{cpp:class} geo::Shape" not in namespace

    circle = (out / "geo_Circle.md").read_text()
    assert circle.startswith("# Class `geo::Circle`")
    # A record renders in full, so its own members stay on its page.
    assert "{cpp:function} double geo::Circle::area() const" in circle
    assert "{cpp:member} double geo::Circle::radius" in circle


def test_group_by_class_drops_leafless_namespace_but_keeps_records(store: Store, tmp_path: Path) -> None:
    # With undocumented symbols suppressed the geo namespace still holds
    # documented records and leaves, so it pages as usual; the undocumented free
    # function geo::mystery is gone from the namespace page.
    gen = Generator(store, include_undocumented=False)
    out = tmp_path / "api"
    pages = gen.generate(out, group_by="class")
    assert pages == ["geo", "geo_Shape", "geo_Circle"]
    assert "geo::mystery" not in (out / "geo.md").read_text()


def test_group_by_class_pages_global_macros_and_nested_records(m7_db: Path, tmp_path: Path) -> None:
    # Global macros are non-container roots: each gets its own page. The nn
    # namespace splits its record members (a struct and a class template) onto
    # their own pages while its concept/function leaves stay on the namespace
    # page; the \defgroup page is still appended last.
    with Store.open(m7_db) as store:
        out = tmp_path / "api"
        pages = Generator(store).generate(out, group_by="class")

    assert pages == ["nn", "nn_Pt", "nn_Box", "MAXM", "PI", "group_grp"]
    nn = (out / "nn.md").read_text()
    assert "nn::Addable" in nn  # a concept leaf
    assert "nn::helper" in nn  # a free-function leaf
    assert "nn::Box" not in nn  # the class template is its own page
    assert "nn::Pt" not in nn  # the struct is its own page
    assert (out / "nn_Box.md").read_text().startswith("# Class template `nn::Box`")


def test_group_by_namespace_index_lists_only_top_namespaces(gen: Generator, tmp_path: Path) -> None:
    # The root index of the hierarchical grouping is the entry point to the tree:
    # it links the top namespace(s) only, not every class/function, so the
    # colossal flat index becomes a short, browsable list.
    out = tmp_path / "api"
    gen.generate(out, group_by="namespace")
    index = (out / "index.md").read_text()
    assert index.startswith("# API Reference")
    assert "\ngeo\n" in index
    # The deep pages are reached through the namespace hub, not the root index.
    assert "geo_Circle" not in index
    assert "geo_scale" not in index


def test_group_by_namespace_hub_links_members_without_inlining(gen: Generator, tmp_path: Path) -> None:
    # A namespace becomes a navigational hub: heading, its own docs, and a
    # toctree linking each member page. No member body is inlined, so the hub
    # stays compact however large the namespace.
    out = tmp_path / "api"
    pages = gen.generate(out, group_by="namespace")
    assert set(pages) == {"geo", "geo_Circle", "geo_Shape", "geo_scale", "geo_mystery", "geo_types", "geo_constants"}

    hub = (out / "geo.md").read_text()
    assert hub.startswith("# Namespace `geo`")
    assert "```{toctree}" in hub
    # Classes, the free function, and the grouped pages are linked with short labels.
    assert "Circle <geo_Circle>" in hub
    assert "scale <geo_scale>" in hub
    assert "Types <geo_types>" in hub
    assert "Constants <geo_constants>" in hub
    # The hub links bodies; it never inlines a member directive.
    assert "{cpp:class}" not in hub
    assert "{cpp:function}" not in hub


def test_group_by_namespace_pages_split_per_symbol(gen: Generator, tmp_path: Path) -> None:
    # Each class and each free-function name earns its own page; the namespace's
    # type-like and value-like leaves collect onto a Types and a Constants page.
    out = tmp_path / "api"
    gen.generate(out, group_by="namespace")

    assert (out / "geo_Circle.md").read_text().startswith("# Class `geo::Circle`")
    scale = (out / "geo_scale.md").read_text()
    assert scale.startswith("# Function `geo::scale`")
    assert "{cpp:function} Circle geo::scale" in scale

    types = (out / "geo_types.md").read_text()
    assert types.startswith("# Types in `geo`")
    assert "{cpp:enum} geo::Color" in types
    assert "{cpp:type} geo::Distance" in types

    constants = (out / "geo_constants.md").read_text()
    assert constants.startswith("# Constants in `geo`")
    assert "{cpp:var} const double geo::pi" in constants


def test_group_by_namespace_nests_subnamespaces_and_lumps_operators(ns_db: Path, tmp_path: Path) -> None:
    # A sub-namespace is a child hub (reached through its parent, not the root
    # index); overloads of one name share a page; and every free operator lumps
    # onto a single operators page rather than spawning a page each.
    with Store.open(ns_db) as store:
        out = tmp_path / "api"
        pages = Generator(store).generate(out, group_by="namespace")

    # Only the top namespace is at the index root; app::sub is nested under it.
    index = (out / "index.md").read_text()
    assert "\napp\n" in index
    assert "app_sub" not in index

    hub = (out / "app.md").read_text()
    assert "sub <app_sub>" in hub
    assert "make <app_make>" in hub
    assert "Operators <app_operators>" in hub

    # The sub-namespace is its own hub page listing its class.
    sub = (out / "app_sub.md").read_text()
    assert sub.startswith("# Namespace `app::sub`")
    assert "Gadget <app_sub_Gadget>" in sub

    # Both make() overloads render on the single name page.
    make = (out / "app_make.md").read_text()
    assert "Widget app::make()" in make
    assert "Widget app::make(int n)" in make

    # Both free operators lump onto one operators page; no per-operator pages.
    operators = (out / "app_operators.md").read_text()
    assert operators.startswith("# Operators in `app`")
    assert "operator==" in operators
    assert "operator<<" in operators
    assert not (out / "app_operatoreq.md").exists()
    assert "app_operators" in pages


def test_repair_split_operators_rejoins_eqeq() -> None:
    from clangquill.generator import _repair_split_operators  # noqa: PLC0415

    # libclang prints the first `==` of a SFINAE expression as `= =`; rejoin it.
    assert (
        _repair_split_operators("std::enable_if<G::dimension = = 2 || G::dimension == 3, void>")
        == "std::enable_if<G::dimension == 2 || G::dimension == 3, void>"
    )
    assert _repair_split_operators("a =   = b") == "a == b"
    # A single `=` (a default argument / alias) must be left untouched.
    assert _repair_split_operators("template<int N = 4>") == "template<int N = 4>"
    assert _repair_split_operators("using T = int") == "using T = int"


def test_signature_repairs_split_eqeq_for_function(gen: Generator, store: Store) -> None:
    import dataclasses  # noqa: PLC0415

    # A function whose pretty-printed signature carries libclang's `= =` artifact
    # must emit a parseable `==` so the Sphinx C++ domain does not choke on it.
    broken = dataclasses.replace(
        _symbol(store, "geo::scale"),
        signature="enable_if_t<D = = 2, Circle> geo::scale(const Circle &c)",
    )
    out = gen.signature(broken)
    assert "= =" not in out
    assert "D == 2" in out


def test_signature_repairs_split_eqeq_for_concept_and_class_template(m7_db: Path) -> None:
    import dataclasses  # noqa: PLC0415

    # The repair also covers the template-head branches: a concept and a class
    # template whose head carries libclang's `= =` must emit a parseable `==`.
    with Store.open(m7_db) as store:
        gen = Generator(store)

        concept = dataclasses.replace(
            _symbol(store, "nn::Addable"),
            signature="template<class T> requires (sizeof(T) = = 4)",
        )
        concept_out = gen.signature(concept)
        assert "= =" not in concept_out
        assert "sizeof(T) == 4" in concept_out

        template = dataclasses.replace(
            _symbol(store, "nn::Box"),
            signature="template<class T, bool B = (sizeof(T) = = 4)>",
        )
        template_out = gen.signature(template)
        assert "= =" not in template_out
        assert "sizeof(T) == 4" in template_out


def test_relpath_filter_reroots_under_base(store: Store) -> None:
    gen = Generator(store, path_base="/work/repo")
    # A file under the base is shown relative, with forward slashes.
    assert gen._relpath("/work/repo/src/foo.hpp") == "src/foo.hpp"  # noqa: SLF001
    # The base directory itself collapses to ".".
    assert gen._relpath("/work/repo") == "."  # noqa: SLF001
    # A path outside the base keeps its absolute spelling (no ".." escape).
    assert gen._relpath("/work/other/bar.hpp") == "/work/other/bar.hpp"  # noqa: SLF001


def test_relpath_filter_is_identity_without_base(store: Store) -> None:
    gen = Generator(store)
    assert gen._relpath("/work/repo/src/foo.hpp") == "/work/repo/src/foo.hpp"  # noqa: SLF001


def test_file_heading_reroots_with_path_base(store: Store) -> None:
    # The IR stores absolute, build-machine paths; the bundled file.md.jinja runs
    # them through the `relpath` filter, so a path_base re-roots the "File"
    # heading to a stable, relative path with forward slashes.
    absolute = SourceFile(id=99, path="/work/repo/include/geo.hpp", sha256="x", size_bytes=0)
    gen = Generator(store, path_base="/work/repo")
    rendered = gen.render_file(absolute)
    assert rendered.startswith("# File `include/geo.hpp`")
    assert "/work/repo" not in rendered


def test_store_file_roots_skips_same_file_parents(multifile_db: Path) -> None:
    with Store.open(multifile_db) as store:
        # alpha.hpp owns the namespace record, so the namespace is its file-root;
        # the class and its method nest under it and are not file-roots.
        alpha_roots = {s.qualified_name for s in store.file_roots(1)}
        assert alpha_roots == {"app"}
        # beta.hpp declares only a class whose parent namespace lives elsewhere,
        # so the class is the file-root despite not being a global root.
        beta_roots = {s.qualified_name for s in store.file_roots(2)}
        assert beta_roots == {"app::Beta"}


def test_group_by_file_pages_every_file_of_a_spanning_namespace(multifile_db: Path, tmp_path: Path) -> None:
    # Regression: a namespace spanning two files used to leave every file but
    # the namespace's recorded home without a page (only global roots counted),
    # so whole subtrees vanished from the index. Each file must now get a page.
    with Store.open(multifile_db) as store:
        out = tmp_path / "api"
        pages = Generator(store).generate(out, group_by="file")

    assert sorted(pages) == ["alpha_hpp", "beta_hpp"]
    alpha = (out / "alpha_hpp.md").read_text()
    beta = (out / "beta_hpp.md").read_text()
    # Each file lists only the class it declares, even though both share the
    # ``app`` namespace whose single record lives in alpha.hpp.
    assert "app::Alpha" in alpha
    assert "app::Beta" not in alpha
    assert "app::Beta" in beta
    assert "app::Alpha" not in beta
    # A method shares its class's file, so it renders under the class rather
    # than as a second top-of-file entry.
    assert "app::Alpha::run" in alpha


def test_templates_override_by_kind(store: Store, tmp_path: Path) -> None:
    user_dir = tmp_path / "templates"
    user_dir.mkdir()
    (user_dir / "tweaked.md.jinja").write_text("TWEAKED {{ symbol.qualified_name }}\n")

    gen = Generator(store, template_dirs=[user_dir], templates={"function": "tweaked"})
    # Free functions and methods both resolve to the "function" stem, so both
    # pick up the override keyed by kind name.
    assert gen.render_symbol(_symbol(store, "geo::scale")).strip() == "TWEAKED geo::scale"
    # Classes are unaffected.
    assert "{cpp:class} geo::Shape" in gen.render_symbol(_symbol(store, "geo::Shape"))


def test_toctree_maxdepth_is_honoured(gen: Generator, tmp_path: Path) -> None:
    out = tmp_path / "api"
    gen.generate(out, toctree_maxdepth=4)
    assert ":maxdepth: 4" in (out / "index.md").read_text()


def test_root_document_renames_index(gen: Generator, tmp_path: Path) -> None:
    out = tmp_path / "api"
    gen.generate(out, root_document="contents")
    assert (out / "contents.md").is_file()
    assert not (out / "index.md").exists()


@pytest.fixture
def m7_store(m7_db: Path) -> Iterator[Store]:
    with Store.open(m7_db) as opened:
        yield opened


@pytest.fixture
def m7_gen(m7_store: Store) -> Generator:
    return Generator(m7_store)


def test_class_template_signature_carries_head(m7_gen: Generator, m7_store: Store) -> None:
    sig = m7_gen.signature(_symbol(m7_store, "nn::Box"))
    assert sig == "template<typename T, int N = 4> nn::Box"


def test_concept_signature_carries_head(m7_gen: Generator, m7_store: Store) -> None:
    assert m7_gen.signature(_symbol(m7_store, "nn::Addable")) == "template<typename T> nn::Addable"


def test_macro_signature_is_name_or_call(m7_gen: Generator, m7_store: Store) -> None:
    assert m7_gen.signature(_symbol(m7_store, "PI")) == "PI"
    assert m7_gen.signature(_symbol(m7_store, "MAXM")) == "MAXM(a, b)"


def test_concept_and_macro_emit_domain_directives(m7_gen: Generator, m7_store: Store) -> None:
    assert "{cpp:concept} template<typename T> nn::Addable" in m7_gen.render_symbol(_symbol(m7_store, "nn::Addable"))
    assert "{c:macro} MAXM(a, b)" in m7_gen.render_symbol(_symbol(m7_store, "MAXM"))


def test_friends_block_links_documented_and_inlines_unknown(m7_gen: Generator, m7_store: Store) -> None:
    rendered = m7_gen.render_symbol(_symbol(m7_store, "nn::Pt"))
    assert "**Friends**" in rendered
    # A documented friend links via the domain; an out-of-TU friend degrades to code.
    assert "{cpp:any}`nn::helper`" in rendered
    assert "`Outsider`" in rendered


def test_related_block_lists_functions_relates_points_at(gen: Generator, store: Store) -> None:
    # `\relates Circle` sits on geo::scale, and Doxygen lists such a function
    # under the class rather than only on its own page.
    rendered = gen.render_symbol(_symbol(store, "geo::Circle"))
    assert "**Related functions**" in rendered
    assert "{cpp:any}`geo::scale`" in rendered
    # The function keeps its own documentation; `\relates` only adds a listing.
    assert "Return a scaled copy of a circle." in gen.render_symbol(_symbol(store, "geo::scale"))


def test_related_block_absent_without_relates(gen: Generator, store: Store) -> None:
    # Shape has no related functions, so the section must not appear at all.
    assert "**Related functions**" not in gen.render_symbol(_symbol(store, "geo::Shape"))


def test_related_block_keeps_documented_cross_file_relates_when_hiding_undocumented(
    multifile_db: Path,
    tmp_path: Path,
) -> None:
    # A file-scoped page still renders a class's documented related functions
    # even when those functions are declared in another file.
    con = sqlite3.connect(multifile_db)
    con.execute(
        "INSERT INTO symbols(usr, parent_usr, kind, spelling, qualified_name, display_name, "
        "signature, type_repr, access, is_definition, is_documented, content_hash, file_id, line) "
        "VALUES(?, 'c:@N@app', 5, ?, ?, ?, ?, ?, 0, 1, 1, ?, 2, 0)",
        (
            "c:@N@app@F@link_to_alpha",
            "link_to_alpha",
            "app::link_to_alpha",
            "app::link_to_alpha",
            "void link_to_alpha()",
            "void ()",
            "hash-link-to-alpha",
        ),
    )
    con.execute(
        "INSERT INTO comments(symbol_usr, raw_text, format, fields_json) VALUES(?, '/// fixture', 'doxygen', '')",
        ("c:@N@app@F@link_to_alpha",),
    )
    con.executemany(
        "INSERT INTO comment_fields(symbol_usr, name, arg, value, ordinal) VALUES(?, ?, '', ?, ?)",
        [
            ("c:@N@app@F@link_to_alpha", "brief", "Docs for link_to_alpha.", 0),
            ("c:@N@app@F@link_to_alpha", "relates", "Alpha", 1),
        ],
    )
    con.execute(
        "INSERT INTO symbols(usr, parent_usr, kind, spelling, qualified_name, display_name, "
        "signature, type_repr, access, is_definition, is_documented, content_hash, file_id, line) "
        "VALUES(?, 'c:@N@app', 5, ?, ?, ?, ?, ?, 0, 1, 0, ?, 2, 0)",
        (
            "c:@N@app@F@hidden_link",
            "hidden_link",
            "app::hidden_link",
            "app::hidden_link",
            "void hidden_link()",
            "void ()",
            "hash-hidden-link",
        ),
    )
    con.execute(
        "INSERT INTO comments(symbol_usr, raw_text, format, fields_json) VALUES(?, '/// fixture', 'doxygen', '')",
        ("c:@N@app@F@hidden_link",),
    )
    con.execute(
        "INSERT INTO comment_fields(symbol_usr, name, arg, value, ordinal) VALUES(?, 'relates', '', 'Alpha', 0)",
        ("c:@N@app@F@hidden_link",),
    )
    con.commit()
    con.close()

    with Store.open(multifile_db) as store:
        assert _symbol(store, "app::link_to_alpha").is_documented
        assert not _symbol(store, "app::hidden_link").is_documented
        pages = Generator(store, include_undocumented=False).generate(tmp_path / "api", group_by="file")

    assert sorted(pages) == ["alpha_hpp", "beta_hpp"]
    alpha = (tmp_path / "api" / "alpha_hpp.md").read_text()
    assert "{cpp:any}`app::link_to_alpha`" in alpha
    assert "{cpp:any}`app::hidden_link`" not in alpha


def test_related_functions_bust_the_class_page_fingerprint(fixture_db: Path) -> None:
    # The class page reads another symbol's comment, which is the one incoming
    # edge in the dependency walk; editing the related function has to change
    # the record's fingerprint or its page stays cached and stale.
    def fingerprint() -> str:
        with Store.open(fixture_db) as opened:
            generator = Generator(opened)
            # group_by="class" is the mode where a record gets its own page.
            plan = next(p for p in generator.plan_pages(group_by="class") if p.stem == "geo_Circle")
            return generator.page_fingerprint(plan)

    before = fingerprint()
    con = sqlite3.connect(fixture_db)
    con.execute("UPDATE symbols SET content_hash = 'changed' WHERE qualified_name = 'geo::scale'")
    con.commit()
    con.close()
    assert fingerprint() != before


def test_group_pages_appended_and_render_members(m7_gen: Generator, tmp_path: Path) -> None:
    pages = m7_gen.generate(tmp_path / "api")
    assert "group_grp" in pages
    page = (tmp_path / "api" / "group_grp.md").read_text()
    assert page.startswith("# Grouped API")
    assert "{cpp:any}`nn::Box`" in page
    assert "{cpp:any}`nn::helper`" in page


def test_no_group_pages_when_db_has_no_groups(gen: Generator, tmp_path: Path) -> None:
    # The geo fixture defines no groups, so output is unchanged (no group pages).
    pages = gen.generate(tmp_path / "api")
    assert not any(stem.startswith("group_") for stem in pages)


def test_rendered_myst_builds_as_cpp_domain_objects(gen: Generator, tmp_path: Path) -> None:
    sphinx = pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.application import Sphinx  # noqa: PLC0415

    src = tmp_path / "src"
    gen.generate(src)
    (src / "conf.py").write_text('project = "fix"\nextensions = ["myst_parser"]\nmaster_doc = "index"\n')

    out = tmp_path / "out"
    warnings = tmp_path / "warnings.txt"
    app = Sphinx(
        str(src),
        str(src),
        str(out),
        str(tmp_path / "doctree"),
        "html",
        warningiserror=True,
        status=None,
        warning=warnings.open("w"),
    )
    app.build()

    html = (out / "geo.html").read_text()
    # The C++ domain produced mangled object ids and the {cpp:any} role linked to one.
    assert "_CPPv4N3geo6CircleE" in html
    assert 'href="#_CPPv4N3geo6CircleE"' in html
    assert sphinx.__version__  # silence unused-import concerns


@pytest.fixture
def spec_store(spec_db: Path) -> Iterator[Store]:
    with Store.open(spec_db) as opened:
        yield opened


@pytest.fixture
def spec_gen(spec_store: Store) -> Generator:
    return Generator(spec_store)


def _spec_symbol(store: Store, display_name: str) -> Symbol:
    """Look a specialization up by ``display_name`` (its ``qualified_name`` is shared)."""
    sym = next((s for s in store.symbols() if s.display_name == display_name), None)
    assert sym is not None, f"missing fixture symbol with display {display_name!r}"
    return sym


def test_specialization_class_signature_carries_spec_args(spec_gen: Generator, spec_store: Store) -> None:
    # Each specialization names its argument list so the C++ domain does not see
    # every specialization as a duplicate of the bare template.
    dense = _spec_symbol(spec_store, "ContainerFactory<demo::DenseVector<S>>")
    field = _spec_symbol(spec_store, "ContainerFactory<demo::FieldVector<S, 4>>")
    assert spec_gen.signature(dense) == "template<class S> demo::ContainerFactory<demo::DenseVector<S>>"
    assert spec_gen.signature(field) == "template<class S> demo::ContainerFactory<demo::FieldVector<S, 4>>"


def test_primary_template_signature_has_no_spec_suffix(spec_gen: Generator, spec_store: Store) -> None:
    primary = _spec_symbol(spec_store, "ContainerFactory")
    assert spec_gen.signature(primary) == "template<class ContainerImp> demo::ContainerFactory"


def test_member_of_specialization_qualifies_with_spec_args(spec_gen: Generator, spec_store: Store) -> None:
    # The ``create`` of each specialization renders with the specialized parent
    # template-id and the parent's ``template<...>`` head, so the two members no
    # longer collide on the bare ``ContainerFactory::create``.
    sym = next(
        s for s in spec_store.symbols() if s.spelling == "create" and s.type_repr.startswith("demo::DenseVector")
    )
    assert spec_gen.signature(sym) == (
        "template<class S> static demo::DenseVector<S> "
        "demo::ContainerFactory<demo::DenseVector<S>>::create(const size_t size)"
    )


def test_plain_member_signature_unchanged(gen: Generator, store: Store) -> None:
    # A member whose parent is not a specialization keeps the legacy form.
    assert gen.signature(_symbol(store, "geo::Circle::area")) == "double geo::Circle::area() const"


def test_constructor_injected_template_id_and_recovery_defaults_are_stripped(
    spec_gen: Generator,
    spec_store: Store,
) -> None:
    from clangquill.store import SymbolKind  # noqa: PLC0415

    ctor = next(
        s for s in spec_store.symbols() if s.spelling == "AdaptationHelper" and s.kind == SymbolKind.CONSTRUCTOR
    )
    sig = spec_gen.signature(ctor)
    assert "<V, GV, RF>" not in sig
    assert "<recovery-expr>" not in sig
    assert sig == (
        "demo::AdaptationHelper::AdaptationHelper(GV &grd, "
        "const std::string &logging_prefix, const std::array<bool, 3> &logging_state)"
    )


def test_strip_injected_template_id_handles_nested_args(spec_gen: Generator) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    from clangquill.store import SymbolKind  # noqa: PLC0415

    ctor = SimpleNamespace(kind=SymbolKind.CONSTRUCTOR, spelling="Foo")
    assert spec_gen._strip_injected_template_id("Foo<Bar<X>>(int n)", ctor) == "Foo(int n)"  # noqa: SLF001
    # A non-ctor/dtor (e.g. a method whose own template-id is legitimate) is untouched.
    method = SimpleNamespace(kind=SymbolKind.METHOD, spelling="Foo")
    assert spec_gen._strip_injected_template_id("Foo<Bar<X>>(int n)", method) == "Foo<Bar<X>>(int n)"  # noqa: SLF001


def test_strip_recovery_defaults_removes_both_forms() -> None:
    from clangquill.generator import _strip_recovery_defaults  # noqa: PLC0415

    s = 'void f(const std::string &p = <recovery-expr>(""), const std::array<bool, 3> &st = <recovery-expr>())'
    assert _strip_recovery_defaults(s) == "void f(const std::string &p, const std::array<bool, 3> &st)"
    # Non-recovery defaults are left intact.
    assert _strip_recovery_defaults("void g(int n = 0, T *p = nullptr)") == "void g(int n = 0, T *p = nullptr)"


def test_specialization_pages_build_without_duplicate_or_parse_warnings(
    spec_gen: Generator,
    tmp_path: Path,
) -> None:
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    import io  # noqa: PLC0415

    from sphinx.application import Sphinx  # noqa: PLC0415

    src = tmp_path / "src"
    spec_gen.generate(src)
    (src / "conf.py").write_text('project = "spec"\nextensions = ["myst_parser"]\nmaster_doc = "index"\n')

    # Capture warnings instead of asserting statuscode or relying on build() to
    # raise: instantiating several Sphinx apps in one test process re-registers
    # nodes and emits unrelated "node class already registered" warnings (which
    # bump statuscode), so we assert specifically that the four C++-domain warning
    # classes this PR fixes never appear in the build output.
    warning_stream = io.StringIO()
    app = Sphinx(
        str(src),
        str(src),
        str(tmp_path / "out"),
        str(tmp_path / "doctree"),
        "html",
        status=None,
        warning=warning_stream,
    )
    app.build()
    captured = warning_stream.getvalue()
    for marker in (
        "Duplicate C++ declaration",
        "Too many template argument lists",
        "Parsing of expression failed",
        "recovery-expr",
    ):
        assert marker not in captured, f"unexpected C++ domain warning ({marker}):\n{captured}"


def test_m7_kinds_build_as_domain_objects(m7_gen: Generator, tmp_path: Path) -> None:
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.application import Sphinx  # noqa: PLC0415

    src = tmp_path / "src"
    m7_gen.generate(src)
    (src / "conf.py").write_text('project = "m7"\nextensions = ["myst_parser"]\nmaster_doc = "index"\n')

    warnings = tmp_path / "warnings.txt"
    # warningiserror catches dangling cross-references, unknown directives, and
    # malformed C++/C-domain signatures, so a clean build validates every kind.
    app = Sphinx(
        str(src),
        str(src),
        str(tmp_path / "out"),
        str(tmp_path / "doctree"),
        "html",
        warningiserror=True,
        status=None,
        warning=warnings.open("w"),
    )
    app.build()

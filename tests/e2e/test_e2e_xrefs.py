r"""Nitpicky end-to-end build over the fixture that links to every hard shape.

The question it answers: do the generated cross-references actually resolve?

:mod:`tests.e2e.test_e2e_build` proves the generated pages *parse* -- a default
Sphinx build is warning-free. That is a weaker property than it looks: outside
nitpicky mode an unresolved ``{cpp:any}`` target is not a warning at all, it just
renders as plain text. Every inter-symbol link clangquill emits could be dead and
the suite would stay green.

So this module builds the ``xrefs.hpp`` fixture -- class templates and their
members, specializations, a class template nested inside a class template, enums,
operators, an overload set, each of them linked to by ``\ref`` -- with ``nitpicky`` on and warnings as errors,
which is what ``sphinx-build -n -W`` does. The only warnings tolerated are the
shapes listed in :data:`KNOWN_UNRESOLVABLE`, which no output clangquill could
emit would make resolvable; anything else fails the build.

:func:`test_a_dangling_reference_fails_the_strict_build` is the canary's own
canary: it introduces a reference to a symbol that does not exist and requires
the strict build to reject it, so a future change that neuters the gate cannot
pass silently.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from clangquill import _core

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")

FIXTURE = Path(__file__).resolve().parents[1] / "cpp" / "fixtures" / "xrefs.hpp"

#: Reference shapes the Sphinx C++ domain cannot resolve whatever clangquill
#: writes, as ``(role, target pattern)`` pairs for ``nitpick_ignore_regex``.
#: Neither is a link clangquill emitted; both are the domain rendering a
#: declaration:
#:
#: * ``xr`` -- a namespace is scope, not an object, so the domain indexes none
#:   and renders the namespace component of every qualified declaration as a
#:   ``cpp:identifier`` that resolves to nothing.
#: * a class nested in a class template (``Buffer::Cursor``, ``Outer::Inner``)
#:   -- the domain spells the enclosing scope of that class's own members as the
#:   parent's whole declaration, template head included, and then fails to look
#:   that string up. Matched by shape rather than spelled out, because the head
#:   is libclang's pretty-printing and moves with the LLVM the wheel bundles.
#:
#: ``docs/development/cross-references.md`` explains both, along with the shapes
#: that are unreachable from Doxygen's syntax rather than from the domain.
KNOWN_UNRESOLVABLE = [
    ("cpp:identifier", r"xr"),
    ("cpp:identifier", r"template<.*> xr::\w+<.*>::\w+(?:<.*>)?"),
]

#: Every hard cross-reference the fixture writes, and what each one is for. The
#: gate is only worth something if the fixture really exercises these, so they
#: are asserted both as emitted ``{cpp:any}`` targets and as resolved links.
HARD_TARGETS = {
    "xr::Buffer": "class template",
    "xr::Buffer::data": "data member of a class template",
    "xr::Buffer::front": "method of a class template",
    "xr::Buffer::value_type": "member alias of a class template",
    "xr::Buffer::capacity": "static data member of a class template",
    "xr::Buffer::Cursor": "class nested in a class template",
    "xr::Buffer::Cursor::offset": "member of that nested class",
    "xr::Buffer::Cursor::advance": "method of that nested class",
    "xr::Buffer::Growth": "enum nested in a class template",
    "xr::Buffer::Growth::Eager": "enumerator of that nested enum",
    "xr::Traits": "primary template that has specializations",
    "xr::Traits::describe": "method of that primary template",
    "xr::is_foo_v": "variable template",
    "xr::Addable": "concept",
    "xr::Colour": "scoped enum",
    "xr::Colour::Red": "scoped enumerator",
    "xr::Plain": "unscoped enum",
    "xr::PlainRed": "unscoped enumerator, named without its enum",
    "xr::Vec::x": "plain field",
    "xr::Vec::operator[]": "subscript operator",
    "xr::Vec::operator+=": "compound assignment operator",
    "xr::Vec::Tag": "class nested in a plain struct",
    "xr::Vec::Tag::id": "member of that nested class",
    "xr::operator==": "free equality operator",
    "xr::operator+": "free arithmetic operator",
    "xr::DefaultBuffer": "alias of a template specialization",
    "xr::overloaded": "overload set",
    "xr::Outer": "class template holding a class template",
    "xr::Outer::Inner": "class template nested in a class template",
    "xr::Outer::Inner::value": "member of that nested class template",
    "xr::Outer::Inner::get": "method of that nested class template",
    "xr::Outer::Inner::Hold": "enum nested in a class template nested in a class template",
    "xr::Outer::Inner::Hold::Value": "enumerator of that doubly nested enum",
}

CONF = """
extensions = ["clangquill.sphinx_ext"]
master_doc = "index"
nitpicky = True
nitpick_ignore_regex = {ignore!r}
clangquill_input = ["xrefs.hpp"]
clangquill_output_dir = "api"
clangquill_cache_dir = {cache!r}
clangquill_compile_commands = "."
"""

ROOT_INDEX = """
# Project

```{toctree}
:maxdepth: 2

api/index
```
"""

#: One unresolved-reference warning, as Sphinx spells it.
_MISSING_RE = re.compile(r"WARNING: (?P<role>[\w:]+) reference target not found: (?P<target>.*?) \[ref\.")

#: One resolved intra-document link, as the HTML writer spells it.
_LINK_RE = re.compile(r'<a class="reference internal" href="(?P<href>#[^"]+)"[^>]*>(?P<text>.*?)</a>', re.DOTALL)


def _make_project(tmp_path: Path, *, ignore: list[tuple[str, str]], extra_header: str = "") -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "xrefs.hpp").write_text(FIXTURE.read_text(encoding="utf-8") + extra_header, encoding="utf-8")
    (src / "conf.py").write_text(CONF.format(cache=str(tmp_path / "cache"), ignore=ignore), encoding="utf-8")
    (src / "index.md").write_text(ROOT_INDEX, encoding="utf-8")
    entry = {
        "directory": str(src),
        "file": str(src / "xrefs.hpp"),
        "arguments": ["c++", "-std=c++20", "-xc++", "-c", str(src / "xrefs.hpp")],
    }
    (src / "compile_commands.json").write_text(json.dumps([entry]), encoding="utf-8")
    return src


@dataclasses.dataclass(frozen=True)
class _Build:
    """One ``sphinx-build -n -W`` run and the verdict it reached."""

    out: Path
    #: Sphinx's exit status. Since Sphinx 8, ``warningiserror`` no longer raises
    #: on the first warning: the build runs to the end and reports itself failed
    #: here, so a test that only watches for an exception passes over every
    #: warning it meant to catch.
    statuscode: int
    #: Every warning Sphinx emitted, verbatim.
    warnings: list[str]
    #: ``(role, target)`` of every unresolved reference Sphinx reported.
    missing: list[tuple[str, str]]


def _build(src: Path, build_root: Path) -> _Build:
    from sphinx.application import Sphinx  # noqa: PLC0415
    from sphinx.util.docutils import docutils_namespace  # noqa: PLC0415

    build_root.mkdir(parents=True, exist_ok=True)
    warnings_path = build_root / "warnings.txt"
    # ``docutils_namespace`` isolates docutils' *global* node/directive/role
    # registries for this build. Without it, the second and later Sphinx
    # applications in one interpreter re-register Sphinx's own nodes and warn
    # about it — harness noise that would fail every warnings-as-errors
    # assertion below for reasons the project cannot act on.
    with warnings_path.open("w", encoding="utf-8") as warning_file, docutils_namespace():
        app = Sphinx(
            str(src),
            str(src),
            str(build_root / "out"),
            str(build_root / "doctree"),
            "html",
            warningiserror=True,
            # Sphinx 6 and 7 raise on the *first* warning unless this is set, so
            # without it ``app.build()`` can exit before either assertion below
            # reads a warning or a status code. Sphinx 8 made keep-going the
            # only behaviour and ignores the flag; ``pyproject`` still allows
            # ``sphinx>=6``, so pass it.
            keep_going=True,
            status=None,
            warning=warning_file,
        )
        app.build()
        statuscode = app.statuscode
    text = warnings_path.read_text(encoding="utf-8")
    return _Build(
        out=build_root / "out",
        statuscode=statuscode,
        warnings=[line for line in text.splitlines() if line.strip()],
        missing=[(m["role"], m["target"]) for m in _MISSING_RE.finditer(text)],
    )


def _cpp_any_targets(api: Path) -> Iterator[str]:
    """Every ``{cpp:any}`` target written into the generated pages."""
    for page in sorted(api.glob("*.md")):
        yield from re.findall(r"\{cpp:any\}`([^`]+)`", page.read_text(encoding="utf-8"))


def _resolved_links(html: str) -> dict[str, str]:
    """Map the text of every resolved intra-page link to its anchor.

    The C++ domain renders a function reference with trailing ``()``; strip it so
    a link reads under the name the fixture wrote.
    """
    links = {}
    for match in _LINK_RE.finditer(html):
        text = re.sub(r"<[^>]+>", "", match["text"]).removesuffix("()")
        links[text] = match["href"]
    return links


def test_generated_cross_references_all_resolve(tmp_path: Path) -> None:
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")

    src = _make_project(tmp_path, ignore=KNOWN_UNRESOLVABLE)
    build = _build(src, tmp_path / "strict")
    assert build.missing == []
    assert build.warnings == []  # no other warning either -- this is ``-n -W``
    assert build.statuscode == 0

    # A fixture that stopped generating links would satisfy the assertions above
    # by writing nothing, so require the hard shapes to be present as targets.
    assert set(HARD_TARGETS) <= set(_cpp_any_targets(src / "api"))

    # And require every symbol to have reached the page as a domain object.
    # A declaration that collides with one the page already emitted falls back
    # to a plain code block, which is warning-free, unreferenceable, and exactly
    # what the member alias of each ``Traits`` specialization used to become.
    page = (src / "api" / "xr.md").read_text(encoding="utf-8")
    assert "```cpp\n" not in page, "a declaration degraded to a plain code block"


def test_hard_targets_render_as_links(tmp_path: Path) -> None:
    # Warning-free is what the build above asserts; this asserts the other half,
    # that resolution produced an actual anchor into the C++ domain rather than
    # literal text that merely happened not to warn.
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")

    src = _make_project(tmp_path, ignore=KNOWN_UNRESOLVABLE)
    build = _build(src, tmp_path / "strict")
    links = _resolved_links((build.out / "api" / "xr.html").read_text(encoding="utf-8"))

    for target, shape in HARD_TARGETS.items():
        assert target in links, f"{shape} ({target}) did not render as a link"
        # ``_CPPv4`` prefixes every C++ domain anchor; anything else would mean
        # the link went somewhere other than the documented declaration.
        assert links[target].startswith("#_CPPv4"), f"{shape} ({target}) linked to {links[target]}"


def test_the_only_unresolvable_shapes_are_the_documented_ones(tmp_path: Path) -> None:
    # Pin :data:`KNOWN_UNRESOLVABLE` to what the build actually reports, in both
    # directions: no unresolved reference may go unlisted (a real regression
    # hidden behind an ignore), and no listed shape may go unseen (a Sphinx
    # release that resolves one, leaving us ignoring warnings that no longer
    # occur).
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")

    src = _make_project(tmp_path, ignore=[])
    build = _build(src, tmp_path / "loose")

    unaccounted = [
        (role, target)
        for role, target in build.missing
        if not any(role == known and re.fullmatch(pattern, target) for known, pattern in KNOWN_UNRESOLVABLE)
    ]
    assert unaccounted == []
    for known, pattern in KNOWN_UNRESOLVABLE:
        assert any(role == known and re.fullmatch(pattern, target) for role, target in build.missing), (
            f"nothing matched the documented shape ({known}, {pattern!r}) any more"
        )


def test_a_dangling_reference_fails_the_strict_build(tmp_path: Path) -> None:
    # The canary's own canary: a reference to a symbol nothing declares must fail
    # the build above, or its green says nothing.
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")

    dangling = """
namespace xr {
/// Points at \\ref xr::NoSuchSymbol, which nothing declares.
void dangling();
}  // namespace xr
"""
    src = _make_project(tmp_path, ignore=KNOWN_UNRESOLVABLE, extra_header=dangling)
    build = _build(src, tmp_path / "strict")
    assert ("cpp:any", "xr::NoSuchSymbol") in build.missing
    assert build.statuscode == 1  # and so the warnings-as-errors build fails

"""End-to-end Sphinx build over the C++ fixtures.

Unlike :mod:`tests.test_sphinx_ext` (a minimal acceptance smoke test), this
drives a full ``sphinx-build`` over the rich ``m7.hpp`` fixture — every M7 kind
(templates, concepts, macros, friends, operators, Doxygen groups) — and asserts
the generated Markdown, the ``objects.inv`` inventory, resolving cross-domain
xrefs, and that a cached re-run reuses the IR instead of re-parsing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clangquill import _core

pytestmark = pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")

FIXTURE = Path(__file__).resolve().parents[1] / "cpp" / "fixtures" / "m7.hpp"

CONF = """
extensions = ["clangquill.sphinx_ext"]
master_doc = "index"
clangquill_input = ["m7.hpp"]
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


def _make_project(tmp_path: Path, cache: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "m7.hpp").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (src / "conf.py").write_text(CONF.format(cache=str(cache)), encoding="utf-8")
    (src / "index.md").write_text(ROOT_INDEX, encoding="utf-8")
    # The Sphinx extension requires a compilation database; the fixture needs no
    # flags beyond the standard, so a one-entry database is enough.
    entry = {
        "directory": str(src),
        "file": str(src / "m7.hpp"),
        "arguments": ["c++", "-std=c++20", "-xc++", "-c", str(src / "m7.hpp")],
    }
    (src / "compile_commands.json").write_text(json.dumps([entry]), encoding="utf-8")
    return src


def _build(src: Path, build_root: Path) -> None:
    from sphinx.application import Sphinx  # noqa: PLC0415

    build_root.mkdir(parents=True, exist_ok=True)
    with (build_root / "warnings.txt").open("w", encoding="utf-8") as warning_file:
        app = Sphinx(
            str(src),
            str(src),
            str(build_root / "out"),
            str(build_root / "doctree"),
            "html",
            warningiserror=True,  # any unresolved xref or bad directive fails the build
            status=None,
            warning=warning_file,
        )
        app.build()


def test_full_sphinx_build_over_fixtures(tmp_path: Path) -> None:
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.util.inventory import InventoryFile  # noqa: PLC0415

    cache = tmp_path / "cache"
    src = _make_project(tmp_path, cache)
    out = tmp_path / "b1"
    _build(src, out)

    # 1. The extension generated MyST pages for every partition.
    api = src / "api"
    assert (api / "index.md").is_file()
    namespace_page = (api / "m7.md").read_text(encoding="utf-8")
    assert "{cpp:concept} template<typename T> m7::Addable" in namespace_page
    assert "{cpp:class} template<typename T, int N = 4> m7::Buffer" in namespace_page
    assert "**Friends**" in namespace_page
    assert "{c:macro} CQ_MAX(a, b)" in (api / "CQ_MAX.md").read_text(encoding="utf-8")
    assert (api / "group_math.md").read_text(encoding="utf-8").startswith("# Math utilities")

    # 2. objects.inv lists the generated domain objects across cpp: and c:.
    with (out / "out" / "objects.inv").open("rb") as handle:
        inv = InventoryFile.load(handle, "", lambda a, b: f"{a}/{b}")
    names = {name for domain, entries in inv.items() if domain.startswith(("cpp:", "c:")) for name in entries}
    assert {"m7::Buffer", "m7::Addable", "m7::add", "m7::max_value"} <= names
    assert "CQ_MAX" in names  # the function-like macro is a C-domain object

    # 3. Cross-references resolved: the group page links to its member objects,
    #    which live on the namespace page (an unresolved {cpp:any} would have
    #    failed the warningiserror build above).
    group_html = (out / "out" / "api" / "group_math.html").read_text(encoding="utf-8")
    assert "m7.html#" in group_html


def test_incremental_rebuild_reuses_cached_ir(tmp_path: Path) -> None:
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")

    cache = tmp_path / "cache"
    src = _make_project(tmp_path, cache)

    _build(src, tmp_path / "b1")
    assert next(cache.glob("*.sqlite"), None) is not None  # the IR was cached
    pages = sorted((src / "api").glob("*.md"))
    first_mtimes = {p.name: p.stat().st_mtime_ns for p in pages}

    # A second build with unchanged input must serve the IR from the cache
    # (no re-parse) and rewrite none of the generated pages — so every page's
    # mtime is unchanged. (The IR file's own mtime is not asserted: SQLite WAL
    # bookkeeping can touch it even on a read-only reuse.)
    _build(src, tmp_path / "b2")
    second_mtimes = {p.name: p.stat().st_mtime_ns for p in (src / "api").glob("*.md")}
    assert second_mtimes == first_mtimes

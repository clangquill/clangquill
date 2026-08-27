"""End-to-end Sphinx build over the C++ fixtures.

Unlike :mod:`tests.test_sphinx_ext` (a minimal acceptance smoke test), this
drives a full ``sphinx-build`` over the rich ``m7.hpp`` fixture — every M7 kind
(templates, concepts, macros, friends, operators, Doxygen groups) — and asserts
the generated Markdown, the ``objects.inv`` inventory, resolving cross-domain
xrefs, and that a cached re-run reuses the IR instead of re-parsing.

It also covers the two claims :func:`clangquill.sphinx_ext.setup` makes about
how Sphinx may drive the extension — ``parallel_read_safe`` and the ``"env"``
rebuild scope of every ``clangquill_*`` config value — which only a real build
can check.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from clangquill import _core

if TYPE_CHECKING:
    from sphinx.environment import BuildEnvironment

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


def _read_dir(directory: Path, pattern: str) -> dict[str, str]:
    """Map filename -> contents for every match, for comparing two builds."""
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(directory.glob(pattern))}


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


@dataclasses.dataclass(frozen=True)
class _Build:
    """What one :func:`_build` call did, beyond writing its output."""

    out: Path
    #: docnames Sphinx decided to (re-)read, as seen by ``env-before-read-docs``.
    read: list[str]
    #: ``env.config_status`` at that moment — it is reset to ``CONFIG_OK`` once
    #: reading finishes, so it can only be observed from inside the build.
    config_status: int
    #: The reason Sphinx logs for a changed config, e.g. ``" ('clangquill_group_by')"``.
    config_status_extra: str


def _build(
    src: Path,
    build_root: Path,
    *,
    parallel: int = 1,
    confoverrides: dict[str, Any] | None = None,
) -> _Build:
    from sphinx.application import Sphinx  # noqa: PLC0415

    build_root.mkdir(parents=True, exist_ok=True)
    read: list[str] = []
    seen: dict[str, Any] = {"status": None, "extra": ""}

    def record(app: Sphinx, env: BuildEnvironment, docnames: list[str]) -> None:  # noqa: ARG001
        read.extend(docnames)
        seen["status"] = env.config_status
        seen["extra"] = env.config_status_extra

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
            parallel=parallel,
            confoverrides=confoverrides or {},
        )
        app.connect("env-before-read-docs", record)
        app.build()

    return _Build(
        out=build_root / "out",
        read=sorted(read),
        config_status=seen["status"],
        config_status_extra=seen["extra"],
    )


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


@pytest.mark.skipif(os.name != "posix", reason="Sphinx only forks read workers on POSIX")
def test_parallel_read_produces_the_serial_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ``setup()`` returns parallel_read_safe/parallel_write_safe, which is a
    # promise about pickled env state surviving forked workers — the kind of
    # claim that breaks silently, since Sphinx just believes it. Build the same
    # project serially and with ``parallel=2`` and require the two outputs to
    # match exactly.
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.builders import Builder  # noqa: PLC0415
    from sphinx.util.parallel import parallel_available  # noqa: PLC0415

    if not parallel_available:
        pytest.skip("this Sphinx cannot fork read workers here")

    cache = tmp_path / "cache"
    src = _make_project(tmp_path, cache)

    serial = _build(src, tmp_path / "serial")
    serial_pages = _read_dir(src / "api", "*.md")
    serial_html = _read_dir(serial.out / "api", "*.html")
    assert serial_pages  # the project generated something to read in parallel

    # Sphinx silently falls back to a serial read whenever anything makes
    # parallelism unavailable, so record that the forking path was really taken
    # rather than trusting the ``parallel=2`` argument.
    forked: dict[str, tuple[list[str], int]] = {}

    def spy(name: str):  # noqa: ANN202
        wrapped = getattr(Builder, name)

        def record(self: Builder, docnames: list[str], nproc: int) -> None:
            forked[name] = (sorted(docnames), nproc)
            return wrapped(self, docnames, nproc)

        return record

    # Both halves of the claim: reading forks workers that unpickle the env,
    # writing forks workers that pickle their results back.
    monkeypatch.setattr(Builder, "_read_parallel", spy("_read_parallel"))
    monkeypatch.setattr(Builder, "_write_parallel", spy("_write_parallel"))
    parallel = _build(src, tmp_path / "parallel", parallel=2)

    assert "_read_parallel" in forked, "Sphinx read serially; parallel_read_safe was never exercised"
    assert "_write_parallel" in forked, "Sphinx wrote serially; parallel_write_safe was never exercised"
    docnames, nproc = forked["_read_parallel"]
    assert nproc == 2
    # More documents than workers, so the read is genuinely split across forks
    # instead of one worker taking everything.
    assert len(docnames) > nproc

    # The generated Markdown is a cached no-op the second time round, so the
    # comparison is about what Sphinx made of it, not about regeneration.
    assert _read_dir(src / "api", "*.md") == serial_pages
    assert _read_dir(parallel.out / "api", "*.html") == serial_html


def test_config_change_rebuilds_the_same_doctree(tmp_path: Path) -> None:
    # Every clangquill_* value is registered with rebuild scope "env", meaning
    # "changing this invalidates what Sphinx read". Nothing checked that: the
    # e2e re-run above uses a fresh build root and an unchanged config, where
    # the scope cannot show. Rebuild into the *same* srcdir and doctree dir,
    # once unchanged and once with a flipped clangquill_group_by.
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.environment import CONFIG_CHANGED, CONFIG_NEW, CONFIG_OK  # noqa: PLC0415

    cache = tmp_path / "cache"
    src = _make_project(tmp_path, cache)
    root = tmp_path / "b"

    first = _build(src, root)
    assert first.config_status == CONFIG_NEW  # no previous env to compare against
    assert "api/index" in first.read

    # Same config, same doctree: the IR is cached, no page is rewritten, and so
    # Sphinx finds nothing to re-read. This is the baseline the next build has
    # to differ from for the assertion below to mean anything.
    unchanged = _build(src, root)
    assert unchanged.config_status == CONFIG_OK
    assert unchanged.read == []

    # Flip one env-scoped value. Sphinx must attribute the invalidation to that
    # value by name and re-read the generated pages, and the extension must
    # regenerate them in the new layout.
    changed = _build(src, root, confoverrides={"clangquill_group_by": "namespace"})
    assert changed.config_status == CONFIG_CHANGED
    assert "clangquill_group_by" in changed.config_status_extra
    assert "api/index" in changed.read

    # namespace mode splits each member onto its own page; the hub page keeps
    # only the toctree. Those pages are new documents, so they are both
    # generated and read.
    hub = (src / "api" / "m7.md").read_text(encoding="utf-8")
    assert "```{toctree}" in hub
    assert "{cpp:class}" not in hub
    member_pages = sorted(p.stem for p in (src / "api").glob("m7_*.md"))
    assert member_pages, "namespace mode generated no per-member pages"
    assert {f"api/{stem}" for stem in member_pages} <= set(changed.read)
    for stem in member_pages:
        assert (changed.out / "api" / f"{stem}.html").is_file()

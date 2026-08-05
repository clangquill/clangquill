"""Acceptance test: a minimal Sphinx project driven by the extension.

Builds generated ``api/*.md``, asserts the build is warning-free, and checks
that the generated ``cpp:`` domain objects appear in ``objects.inv``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from clangquill import _core

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

pytestmark = pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")

HEADER = """
/// Geometry primitives.
namespace geo {

/// Abstract base for shapes.
struct Shape {
  /// Compute the area.
  /// @return the area in square units.
  virtual double area() const = 0;
};

/// A circle.
struct Circle : Shape {
  /// Construct from a radius.
  /// @param r the radius
  explicit Circle(double r);
  /// Compute the area.
  double area() const;
};

/// Return a scaled copy of a circle.
/// @param c the circle to scale
/// @see geo::Circle
Circle scale(const Circle &c, double factor);

}  // namespace geo
"""

CONF = """
extensions = ["clangquill.sphinx_ext"]
master_doc = "index"
clangquill_input = ["geo.hpp"]
clangquill_output_dir = "api"
clangquill_compile_commands = "."
"""

ROOT_INDEX = """
# Project

```{toctree}
:maxdepth: 2

api/index
```
"""

# Every conf.py below sets ``clangquill_compile_commands``: the extension now
# refuses to guess compile flags, so a project without a database is an error
# (covered by ``test_missing_compile_commands_raises_a_clean_extension_error``).
CONF_WITHOUT_COMPILE_COMMANDS = """
extensions = ["clangquill.sphinx_ext"]
master_doc = "index"
clangquill_input = ["geo.hpp"]
clangquill_output_dir = "api"
"""


def _write_compile_commands(directory: Path, sources: Iterable[str], *, std: str = "c++20") -> None:
    """Write a ``compile_commands.json`` covering ``sources`` into ``directory``."""
    entries = [
        {
            "directory": str(directory),
            "file": str(directory / name),
            "arguments": ["c++", f"-std={std}", "-xc++", "-c", str(directory / name)],
        }
        for name in sources
    ]
    (directory / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")


def test_minimal_sphinx_project_builds(tmp_path: Path) -> None:
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.application import Sphinx  # noqa: PLC0415
    from sphinx.util.inventory import InventoryFile  # noqa: PLC0415

    src = tmp_path / "src"
    src.mkdir()
    (src / "geo.hpp").write_text(HEADER)
    (src / "conf.py").write_text(CONF)
    (src / "index.md").write_text(ROOT_INDEX)
    _write_compile_commands(src, ["geo.hpp"])

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

    # The extension generated MyST pages under the srcdir.
    assert (src / "api" / "index.md").is_file()
    assert (src / "api" / "geo.md").is_file()

    # objects.inv lists the expected cpp: domain objects.
    with (out / "objects.inv").open("rb") as handle:
        inv = InventoryFile.load(handle, "", lambda a, b: f"{a}/{b}")
    cpp_objects = {name for domain, entries in inv.items() if domain.startswith("cpp:") for name in entries}
    assert "geo::Circle" in cpp_objects
    assert "geo::scale" in cpp_objects

    # geo.html resolved the {cpp:any} cross-reference to a generated object id.
    html = (out / "api" / "geo.html").read_text()
    assert "_CPPv4N3geo6CircleE" in html


def test_typoed_config_value_is_flagged(tmp_path: Path) -> None:
    """A misspelled ``clangquill_*`` name must warn instead of vanishing silently."""
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.application import Sphinx  # noqa: PLC0415

    src = tmp_path / "src"
    src.mkdir()
    # ``clangquill_inputs`` (plural) is not a recognised option.
    (src / "conf.py").write_text(
        'extensions = ["clangquill.sphinx_ext"]\nmaster_doc = "index"\nclangquill_inputs = ["geo.hpp"]\n',
    )
    (src / "index.md").write_text("# Project\n")

    warnings = tmp_path / "warnings.txt"
    app = Sphinx(
        str(src),
        str(src),
        str(tmp_path / "out"),
        str(tmp_path / "doctree"),
        "html",
        status=None,
        warning=warnings.open("w", encoding="utf-8"),
    )
    app.build()
    assert "unknown config value 'clangquill_inputs'" in warnings.read_text()


def test_missing_input_raises_a_clean_extension_error(tmp_path: Path) -> None:
    """An input pattern matching nothing must fail with an actionable message."""
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.application import Sphinx  # noqa: PLC0415
    from sphinx.errors import ExtensionError  # noqa: PLC0415

    src = tmp_path / "src"
    src.mkdir()
    (src / "conf.py").write_text(
        'extensions = ["clangquill.sphinx_ext"]\nmaster_doc = "index"\n'
        'clangquill_input = ["missing_*.hpp"]\nclangquill_compile_commands = "."\n',
    )
    (src / "index.md").write_text("# Project\n")
    _write_compile_commands(src, ["geo.hpp"])

    with pytest.raises(ExtensionError, match="clangquill input matched no files"):
        Sphinx(
            str(src),
            str(src),
            str(tmp_path / "out"),
            str(tmp_path / "doctree"),
            "html",
            status=None,
            warning=(tmp_path / "warnings.txt").open("w", encoding="utf-8"),
        )


def test_invalid_config_value_raises_a_clean_extension_error(tmp_path: Path) -> None:
    """A ConfigError from validation converts to ExtensionError like FileNotFoundError does."""
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.application import Sphinx  # noqa: PLC0415
    from sphinx.errors import ExtensionError  # noqa: PLC0415

    src = tmp_path / "src"
    src.mkdir()
    (src / "geo.hpp").write_text(HEADER)
    (src / "conf.py").write_text(
        'extensions = ["clangquill.sphinx_ext"]\nmaster_doc = "index"\n'
        'clangquill_input = ["geo.hpp"]\nclangquill_group_by = "nonsense"\n'
        'clangquill_compile_commands = "."\n',
    )
    (src / "index.md").write_text(ROOT_INDEX)
    _write_compile_commands(src, ["geo.hpp"])

    with pytest.raises(ExtensionError, match="clangquill_group_by must be one of"):
        Sphinx(
            str(src),
            str(src),
            str(tmp_path / "out"),
            str(tmp_path / "doctree"),
            "html",
            status=None,
            warning=(tmp_path / "warnings.txt").open("w", encoding="utf-8"),
        )


def test_missing_include_dir_warns(tmp_path: Path) -> None:
    """A ``clangquill_include_dirs`` entry that doesn't exist must warn, not fail."""
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.application import Sphinx  # noqa: PLC0415

    src = tmp_path / "src"
    src.mkdir()
    (src / "geo.hpp").write_text(HEADER)
    (src / "conf.py").write_text(
        'extensions = ["clangquill.sphinx_ext"]\nmaster_doc = "index"\n'
        'clangquill_input = ["geo.hpp"]\nclangquill_output_dir = "api"\n'
        'clangquill_include_dirs = ["does_not_exist"]\nclangquill_compile_commands = "."\n',
    )
    (src / "index.md").write_text(ROOT_INDEX)
    _write_compile_commands(src, ["geo.hpp"])

    warnings = tmp_path / "warnings.txt"
    app = Sphinx(
        str(src),
        str(src),
        str(tmp_path / "out"),
        str(tmp_path / "doctree"),
        "html",
        status=None,
        warning=warnings.open("w", encoding="utf-8"),
    )
    app.build()
    assert "clangquill_include_dirs entry does not exist: 'does_not_exist'" in warnings.read_text()
    # The build itself still succeeds despite the dangling include dir.
    assert (src / "api" / "geo.md").is_file()


def test_unresolved_input_pattern_warns_before_the_build_error(tmp_path: Path) -> None:
    """An unresolved ``clangquill_input`` entry warns at config time too."""
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.application import Sphinx  # noqa: PLC0415
    from sphinx.errors import ExtensionError  # noqa: PLC0415

    src = tmp_path / "src"
    src.mkdir()
    (src / "conf.py").write_text(
        'extensions = ["clangquill.sphinx_ext"]\nmaster_doc = "index"\n'
        'clangquill_input = ["missing_*.hpp"]\nclangquill_compile_commands = "."\n',
    )
    (src / "index.md").write_text("# Project\n")
    _write_compile_commands(src, ["geo.hpp"])

    warnings = tmp_path / "warnings.txt"
    with pytest.raises(ExtensionError, match="clangquill input matched no files"):
        Sphinx(
            str(src),
            str(src),
            str(tmp_path / "out"),
            str(tmp_path / "doctree"),
            "html",
            status=None,
            warning=warnings.open("w", encoding="utf-8"),
        )
    assert "clangquill_input entry does not resolve to an existing file: 'missing_*.hpp'" in warnings.read_text()


def test_directory_only_input_entries_still_warn(tmp_path: Path) -> None:
    """A literal directory, or a glob matching only directories, isn't a resolved input."""
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.application import Sphinx  # noqa: PLC0415

    src = tmp_path / "src"
    src.mkdir()
    (src / "geo.hpp").write_text(HEADER)
    (src / "empty_dir").mkdir()
    (src / "dirs_only" / "subdir").mkdir(parents=True)
    (src / "conf.py").write_text(
        'extensions = ["clangquill.sphinx_ext"]\nmaster_doc = "index"\n'
        'clangquill_input = ["geo.hpp", "empty_dir", "dirs_only/*"]\n'
        'clangquill_output_dir = "api"\nclangquill_compile_commands = "."\n',
    )
    (src / "index.md").write_text(ROOT_INDEX)
    _write_compile_commands(src, ["geo.hpp"])

    warnings = tmp_path / "warnings.txt"
    app = Sphinx(
        str(src),
        str(src),
        str(tmp_path / "out"),
        str(tmp_path / "doctree"),
        "html",
        status=None,
        warning=warnings.open("w", encoding="utf-8"),
    )
    app.build()
    warning_text = warnings.read_text()
    assert "clangquill_input entry does not resolve to an existing file: 'empty_dir'" in warning_text
    assert "clangquill_input entry does not resolve to an existing file: 'dirs_only/*'" in warning_text
    # The valid entry alongside the bogus ones must not spuriously warn, and the build still succeeds.
    assert "'geo.hpp'" not in warning_text
    assert (src / "api" / "geo.md").is_file()


def test_missing_compile_commands_raises_a_clean_extension_error(tmp_path: Path) -> None:
    """The extension refuses to guess compile flags: no database is a build error."""
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.application import Sphinx  # noqa: PLC0415
    from sphinx.errors import ExtensionError  # noqa: PLC0415

    src = tmp_path / "src"
    src.mkdir()
    (src / "geo.hpp").write_text(HEADER)
    (src / "conf.py").write_text(CONF_WITHOUT_COMPILE_COMMANDS)
    (src / "index.md").write_text(ROOT_INDEX)

    with pytest.raises(ExtensionError, match="clangquill_compile_commands is not configured"):
        Sphinx(
            str(src),
            str(src),
            str(tmp_path / "out"),
            str(tmp_path / "doctree"),
            "html",
            status=None,
            warning=(tmp_path / "warnings.txt").open("w", encoding="utf-8"),
        )


def test_unloadable_compile_commands_reports_where_it_looked(tmp_path: Path) -> None:
    """A database that isn't there names every path that was searched."""
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.application import Sphinx  # noqa: PLC0415
    from sphinx.errors import ExtensionError  # noqa: PLC0415

    src = tmp_path / "src"
    src.mkdir()
    (src / "geo.hpp").write_text(HEADER)
    (src / "conf.py").write_text(
        CONF_WITHOUT_COMPILE_COMMANDS + 'clangquill_compile_commands = "build"\n',
    )
    (src / "index.md").write_text(ROOT_INDEX)

    with pytest.raises(ExtensionError) as excinfo:
        Sphinx(
            str(src),
            str(src),
            str(tmp_path / "out"),
            str(tmp_path / "doctree"),
            "html",
            status=None,
            warning=(tmp_path / "warnings.txt").open("w", encoding="utf-8"),
        )
    message = str(excinfo.value)
    assert "looked for:" in message
    # The searched path is spelled out in full, resolved against the srcdir.
    assert str(src / "build" / "compile_commands.json") in message


def test_coexists_with_a_preconfigured_myst_parser(tmp_path: Path) -> None:
    """A pre-configured MyST parser must not be double-registered.

    Listing ``myst_parser`` (or ``myst_nb``) alongside the extension previously
    raised ``source_suffix '.md' is already registered``.
    """
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.application import Sphinx  # noqa: PLC0415

    src = tmp_path / "src"
    src.mkdir()
    (src / "geo.hpp").write_text(HEADER)
    # myst_parser listed explicitly, before the clangquill extension.
    (src / "conf.py").write_text(
        'extensions = ["myst_parser", "clangquill.sphinx_ext"]\n'
        'master_doc = "index"\nclangquill_input = ["geo.hpp"]\nclangquill_output_dir = "api"\n'
        'clangquill_compile_commands = "."\n',
    )
    (src / "index.md").write_text(ROOT_INDEX)
    _write_compile_commands(src, ["geo.hpp"])

    app = Sphinx(
        str(src),
        str(src),
        str(tmp_path / "out"),
        str(tmp_path / "doctree"),
        "html",
        warningiserror=True,
        status=None,
        warning=(tmp_path / "warnings.txt").open("w", encoding="utf-8"),
    )
    app.build()
    assert (src / "api" / "geo.md").is_file()


# A warning clang always emits under the default flags. Unlike an error it must
# never reach the Sphinx warning stream, so a -W build stays green.
WARNING_HEADER = '#warning "geo is on its way out"\n' + HEADER

# A redefinition: an error, which *does* still reach the warning stream.
ERROR_HEADER = HEADER + "\nnamespace geo { struct Circle { int x; }; }\n"

DIAGNOSTICS_LOG_CONF = CONF + 'clangquill_diagnostics_log = "_build/parse.log"\n'


def _build_project(tmp_path: Path, header: str, conf: str, *, strict: bool = True) -> tuple[Path, str]:
    """Build a one-header Sphinx project, returning ``(srcdir, warnings text)``."""
    pytest.importorskip("sphinx")
    pytest.importorskip("myst_parser")
    from sphinx.application import Sphinx  # noqa: PLC0415

    src = tmp_path / "src"
    src.mkdir()
    (src / "geo.hpp").write_text(header)
    (src / "conf.py").write_text(conf)
    (src / "index.md").write_text(ROOT_INDEX)
    _write_compile_commands(src, ["geo.hpp"])

    warnings = tmp_path / "warnings.txt"
    app = Sphinx(
        str(src),
        str(src),
        str(tmp_path / "out"),
        str(tmp_path / "doctree"),
        "html",
        warningiserror=strict,
        status=None,
        warning=warnings.open("w", encoding="utf-8"),
    )
    app.build()
    return src, warnings.read_text()


def test_diagnostics_log_config_value_is_recognised(tmp_path: Path) -> None:
    """``clangquill_diagnostics_log`` must not trip our own unknown-config hook.

    The option is a ``Config`` dataclass field, so it lands in ``CONFIG_FIELDS``
    and ``_warn_unknown_config`` knows about it. Registering it directly with
    ``app.add_config_value`` instead would have it flagged as a typo.
    """
    src, warnings = _build_project(tmp_path, HEADER, DIAGNOSTICS_LOG_CONF)

    assert "unknown config value" not in warnings
    assert (src / "_build" / "parse.log").is_file()


def test_warnings_go_to_the_log_and_never_fail_a_strict_build(tmp_path: Path) -> None:
    """The end-to-end guarantee: full detail on disk, an unchanged -W build."""
    src, warnings = _build_project(tmp_path, WARNING_HEADER, DIAGNOSTICS_LOG_CONF)

    # warningiserror=True did not trip: the clang warning never became a Sphinx
    # warning, so it cannot fail a strict docs build.
    assert "clangquill" not in warnings
    assert "geo is on its way out" in (src / "_build" / "parse.log").read_text()


def test_errors_still_reach_the_warning_stream(tmp_path: Path) -> None:
    """Enabling the log must not quietly silence the errors a build reports."""
    src, warnings = _build_project(tmp_path, ERROR_HEADER, DIAGNOSTICS_LOG_CONF, strict=False)

    assert "redefinition" in warnings
    # And the log carries the same error plus the note explaining it.
    log = (src / "_build" / "parse.log").read_text()
    assert "redefinition" in log
    assert "previous definition" in log


def test_errors_stay_suppressible_with_the_log_enabled(tmp_path: Path) -> None:
    conf = DIAGNOSTICS_LOG_CONF + 'suppress_warnings = ["clangquill.parse"]\n'
    src, warnings = _build_project(tmp_path, ERROR_HEADER, conf)

    assert "redefinition" not in warnings
    assert "redefinition" in (src / "_build" / "parse.log").read_text()

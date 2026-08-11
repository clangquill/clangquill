"""Tests for `clangquill` package."""

import contextlib
import re
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

import clangquill
from clangquill import _core, cli

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

requires_libclang = pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")

# A documented header that parses cleanly, and the same header behind a #warning
# clang always emits under the default flags.
CLEAN_HEADER = "/// A demo namespace.\nnamespace demo {\n/// A demo function.\nint f();\n}\n"
WARNING_HEADER = '#warning "demo is on its way out"\n' + CLEAN_HEADER


def _condense(text: str) -> str:
    """Strip ANSI styling and all whitespace from CLI help output.

    Typer renders ``--help`` through Rich as a panel whose width depends on the
    environment; in a non-tty CI shell long option names can wrap or be split by
    style spans. Collapsing styling and whitespace makes substring checks robust
    to that layout while still proving the option is present.
    """
    return "".join(_ANSI_RE.sub("", text).split())


def test_version():
    assert clangquill.__version__


def test_import():
    pass


def test_command_line_interface():
    """The typer app exposes a documented ``build`` command."""
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "build" in _condense(result.output)

    build_help = runner.invoke(cli.app, ["build", "--help"])
    assert build_help.exit_code == 0
    assert "--output-dir" in _condense(build_help.output)


def test_version_flag():
    """``--version`` reports the package version and exits cleanly."""
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    # Strip ANSI per line (not _condense, which collapses the line structure) so
    # both emitted lines can be asserted exactly and a format regression fails.
    lines = [stripped for raw in result.output.splitlines() if (stripped := _ANSI_RE.sub("", raw).strip())]
    assert len(lines) == 2
    assert lines[0] == f"clangquill {clangquill.__version__}"
    # Either the linked libclang version or the stub-backend note.
    assert lines[1].startswith("libclang: ")


def test_build_requires_inputs():
    """Invoking ``build`` with no inputs fails with usage help."""
    runner = CliRunner()
    result = runner.invoke(cli.app, ["build"])
    assert result.exit_code != 0


def _streams(result: Result) -> str:
    """Both captured streams as one string.

    Click separates stdout from stderr in newer versions and merges them in
    older ones, and the strict-mode verdict deliberately writes to stderr —
    joining them keeps these assertions independent of which behaviour is
    installed.
    """
    parts = [result.output]
    with contextlib.suppress(ValueError):  # merged-stream click has no separate .stderr
        parts.append(result.stderr)
    return "".join(parts)


def _run_build(tmp_path: Path, header: str, *extra: str) -> Result:
    """Run ``build`` over a single ``demo.hpp`` holding ``header``."""
    (tmp_path / "demo.hpp").write_text(header)
    runner = CliRunner()
    return runner.invoke(
        cli.app,
        ["build", str(tmp_path / "demo.hpp"), "-o", str(tmp_path / "api"), *extra],
    )


@requires_libclang
def test_build_ignores_warnings_by_default(tmp_path: Path):
    """A warning is not a failure unless the caller asks for it to be."""
    result = _run_build(tmp_path, WARNING_HEADER)

    assert result.exit_code == 0
    assert "on its way out" not in _streams(result)


@requires_libclang
def test_warnings_as_errors_fails_and_names_the_warning(tmp_path: Path):
    result = _run_build(tmp_path, WARNING_HEADER, "--warnings-as-errors")

    assert result.exit_code == 1
    output = _streams(result)
    assert "on its way out" in output
    assert "1 warning(s)" in output
    assert "--warnings-as-errors" in output


@requires_libclang
def test_warnings_as_errors_passes_a_clean_parse(tmp_path: Path):
    result = _run_build(tmp_path, CLEAN_HEADER, "--warnings-as-errors")

    assert result.exit_code == 0
    assert "Parse is clean" in _streams(result)


@requires_libclang
def test_diagnostics_log_option_writes_the_file(tmp_path: Path):
    """The log was reachable from Sphinx but not from the CLI until now."""
    log = tmp_path / "parse.log"
    result = _run_build(tmp_path, WARNING_HEADER, "--diagnostics-log", str(log))

    assert result.exit_code == 0
    assert "on its way out" in log.read_text()
    assert str(log) in _streams(result)

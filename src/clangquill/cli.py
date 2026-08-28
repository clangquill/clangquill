"""Standalone command line interface for clangquill.

``clangquill build`` runs the same pipeline as the Sphinx extension but without
Sphinx: it parses the given C++ inputs and writes MyST pages to an output
directory. It is handy for previewing output or wiring clangquill into a build
system that is not Sphinx-driven.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from clangquill import __version__, _core
from clangquill._lock import BuildLockTimeoutError
from clangquill.config import GROUP_BY_CHOICES, Config, ConfigError
from clangquill.pipeline import build as run_pipeline
from clangquill.pipeline import warnings_or_worse

if TYPE_CHECKING:
    from clangquill.pipeline import BuildResult

# Kept as a module constant rather than an inline f-string in the option
# annotation: autoapi/autodoc cannot statically parse an f-string (JoinedStr)
# inside a function arglist.
_GROUP_BY_HELP = f"Page partitioning: {' | '.join(GROUP_BY_CHOICES)}."

app = typer.Typer(
    add_completion=False,
    help="Parse C++ and generate MyST Markdown API documentation.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:  # noqa: FBT001 - typer eager-option callback signature
    """Print the clangquill and libclang versions, then exit (``--version``).

    The package version comes from installed metadata (see
    :data:`clangquill.__version__`); the libclang line mirrors what is baked into
    the SQLite artifact, so a bug report can cite the exact backend. The package
    version is on the first line so tools that scrape ``--version`` (the
    benchmark harness records it this way) get a clean ``clangquill <ver>``.
    """
    if not value:
        return
    typer.echo(f"clangquill {__version__}")
    if _core.have_libclang():
        typer.echo(f"libclang: {_core.libclang_version()}")
    else:
        typer.echo("libclang: not linked (stub backend)")
    raise typer.Exit


@app.callback()
def _root(
    version: Annotated[  # noqa: FBT002 - typer renders this bool as an eager --version flag
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the clangquill and libclang versions and exit.",
            is_eager=True,
            callback=_version_callback,
        ),
    ] = False,
) -> None:
    """clangquill: parse C++ and generate MyST Markdown API documentation.

    A near-no-op callback so the single ``build`` command keeps its name as a
    required subcommand instead of being collapsed into the root invocation; it
    also hosts the eager ``--version`` option.
    """


def _parse_template_overrides(pairs: list[str]) -> dict[str, str]:
    """Turn repeated ``--template KIND=STEM`` options into the ``templates`` mapping."""
    overrides: dict[str, str] = {}
    for pair in pairs:
        kind, sep, stem = pair.partition("=")
        if not sep or not kind or not stem:
            msg = f"--template expects KIND=STEM (e.g. class=my_class), got {pair!r}"
            raise typer.BadParameter(msg)
        overrides[kind] = stem
    return overrides


@app.command("build")
def build(  # noqa: PLR0913
    inputs: Annotated[list[Path], typer.Argument(help="C++ headers/sources (paths or globs) to parse.")],
    output_dir: Annotated[Path, typer.Option("--output-dir", "-o", help="Directory for generated pages.")] = Path(
        "api",
    ),
    std: Annotated[str, typer.Option("--std", help="C++ standard, e.g. c++20.")] = "c++20",
    include_dir: Annotated[
        list[Path] | None,
        typer.Option("--include-dir", "-I", help="Add an include directory."),
    ] = None,
    define: Annotated[
        list[str] | None,
        typer.Option("--define", "-D", help="Add a preprocessor definition (NAME or NAME=value)."),
    ] = None,
    compile_commands: Annotated[
        Path | None,
        typer.Option(
            "--compile-commands",
            help="Directory with a compile_commands.json (the file itself works too). "
            "Optional here, unlike in the Sphinx extension, but it overrides --std/-I/-D "
            "with the flags your build system actually uses.",
        ),
    ] = None,
    compile_arg: Annotated[
        list[str] | None,
        typer.Option("--compile-arg", help="Extra compiler argument (repeatable)."),
    ] = None,
    clang_resource_dir: Annotated[
        Path | None,
        typer.Option("--clang-resource-dir", help="Clang resource directory (-resource-dir)."),
    ] = None,
    template_dir: Annotated[
        list[Path] | None,
        typer.Option("--template-dir", help="Directory searched before bundled templates."),
    ] = None,
    template: Annotated[
        list[str] | None,
        typer.Option("--template", help="Per-kind template override as KIND=STEM (repeatable)."),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option("--cache-dir", help="Keep the SQLite IR here instead of a temp file."),
    ] = None,
    include_undocumented: Annotated[  # noqa: FBT002 - typer renders this as a --flag/--no-flag option
        bool,
        typer.Option("--include-undocumented/--no-undocumented", help="Emit symbols lacking a doc comment."),
    ] = True,
    extract_anonymous_namespaces: Annotated[  # noqa: FBT002 - typer renders this as a --flag/--no-flag option
        bool,
        typer.Option(
            "--extract-anonymous-namespaces/--no-extract-anonymous-namespaces",
            help="Document what anonymous namespaces contain (internal linkage; hidden by default).",
        ),
    ] = False,
    comment_parser: Annotated[
        str | None,
        typer.Option("--comment-parser", help="Comment-parser override (name or dotted path)."),
    ] = None,
    group_by: Annotated[
        str,
        typer.Option("--group-by", help=_GROUP_BY_HELP),
    ] = "symbol",
    toctree_maxdepth: Annotated[int, typer.Option("--toctree-maxdepth", help="Generated toctree depth.")] = 2,
    root_document: Annotated[
        str,
        typer.Option("--root-document", help="Stem of the generated index/toctree page."),
    ] = "index",
    path_base: Annotated[
        Path | None,
        typer.Option("--path-base", help="Directory rendered file paths are shown relative to."),
    ] = None,
    jobs: Annotated[
        int,
        typer.Option("--jobs", "-j", help="Parse threads (0 = auto-detect CPU count, 1 = serial)."),
    ] = 0,
    tu_batch: Annotated[
        int,
        typer.Option("--tu-batch", help="Inputs grouped per translation unit (0 = auto, 1 = one TU per input)."),
    ] = 0,
    diagnostics_log: Annotated[
        Path | None,
        typer.Option("--diagnostics-log", help="Write every libclang diagnostic of the run to this file."),
    ] = None,
    warnings_as_errors: Annotated[  # noqa: FBT002 - typer renders this as a --flag/--no-flag option
        bool,
        typer.Option(
            "--warnings-as-errors/--no-warnings-as-errors",
            help="Exit non-zero if the parse produced any warning or worse (forces a full parse).",
        ),
    ] = False,
) -> None:
    """Parse C++ inputs and generate MyST Markdown into the output directory."""
    config = Config(
        input=[str(p) for p in inputs],
        compile_commands=str(compile_commands) if compile_commands else None,
        compile_args=list(compile_arg or []),
        include_dirs=[str(p) for p in include_dir or []],
        std=std,
        defines=list(define or []),
        clang_resource_dir=str(clang_resource_dir) if clang_resource_dir else None,
        output_dir=str(output_dir),
        template_dirs=[str(p) for p in template_dir or []],
        templates=_parse_template_overrides(template or []),
        cache_dir=str(cache_dir) if cache_dir else None,
        include_undocumented=include_undocumented,
        extract_anonymous_namespaces=extract_anonymous_namespaces,
        comment_parser=comment_parser,
        group_by=group_by,
        toctree_maxdepth=toctree_maxdepth,
        root_document=root_document,
        path_base=str(path_base) if path_base else None,
        jobs=jobs,
        tu_batch=tu_batch,
        diagnostics_log=str(diagnostics_log) if diagnostics_log else None,
        warnings_as_errors=warnings_as_errors,
    )
    try:
        config.validate()
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if not _core.have_libclang():
        # Fail before touching the pipeline: parse_to_sqlite would otherwise
        # raise a raw RuntimeError from the C++ stub backend. Unlike the
        # Sphinx extension there is no toctree to keep resolving, so there is
        # nothing graceful to degrade to — report it cleanly and stop.
        typer.echo(
            "Error: clangquill was built without libclang (stub backend) — cannot parse. "
            "Install a distribution with libclang bundled, or build with "
            "-DCLANGQUILL_WITH_LIBCLANG=ON.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        result = run_pipeline(config, base_dir=Path.cwd())
    except (FileNotFoundError, BuildLockTimeoutError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if result.db_is_temporary:
        result.db_path.unlink(missing_ok=True)

    typer.echo(f"Parsed {result.symbol_count} symbol(s) from {result.file_count} file(s).")
    typer.echo(f"Wrote {len(result.pages)} page(s) to {result.output_dir}.")
    if result.diagnostics_log is not None:
        typer.echo(f"Wrote {result.diagnostics_log_count} diagnostic(s) to {result.diagnostics_log}.")
    if not warnings_as_errors:
        # Default reporting: errors only, one line each. Warnings stay off the
        # console because a header that warns still documents fine.
        for diagnostic in result.diagnostics:
            typer.echo(f"  diagnostic: {diagnostic}", err=True)
        return
    _report_strict(result)


def _report_strict(result: BuildResult) -> None:
    """Print the strict-mode verdict and exit non-zero if anything warned.

    The pages have already been written by the time this runs: the check is a
    verdict on the parse, not an abort, so a failing run still leaves the
    (possibly incomplete) output behind to inspect.
    """
    offenders = warnings_or_worse(result.diagnostic_records)
    for record in offenders:
        typer.echo(f"  {record.text}", err=True)
    totals = ", ".join(f"{count} {name}(s)" for name, count in result.diagnostic_counts.items())
    if not offenders:
        typer.echo(f"Parse is clean ({totals or 'no diagnostics'}).")
        return
    typer.echo(f"Parse produced {totals} — failing because --warnings-as-errors is set.", err=True)
    raise typer.Exit(code=1)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()

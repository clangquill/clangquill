"""Sphinx extension that runs the clangquill pipeline at build time.

Enable it with ``extensions = ["clangquill.sphinx_ext"]`` and point
``clangquill_input`` at your headers. On ``builder-inited`` the extension
parses the C++, renders MyST pages into ``clangquill_output_dir`` (under the
Sphinx srcdir), and writes a toctree index — so the generated ``cpp:`` domain
objects participate in cross-references and ``objects.inv`` like any other
page. ``myst_parser`` is pulled in automatically since the output is MyST.

Every knob is a ``clangquill_*`` config value mirroring a field of
:class:`clangquill.config.Config`; see that module for the full list.
"""

from __future__ import annotations

import glob
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sphinx.errors import ExtensionError
from sphinx.util import logging

from clangquill import __version__, _core
from clangquill.config import CONFIG_FIELDS, CONFIG_PREFIX, Config, ConfigError
from clangquill.pipeline import build

if TYPE_CHECKING:
    from sphinx.application import Sphinx
    from sphinx.config import Config as SphinxConfig

logger = logging.getLogger(__name__)


def _warn_unknown_config(app: Sphinx, config: SphinxConfig) -> None:  # noqa: ARG001
    """``config-inited`` hook: flag ``clangquill_*`` names that match no option.

    ``Config.from_mapping`` deliberately ignores unknown keys (so any superset
    mapping can be passed), which means a conf.py typo like ``clangquill_inputs``
    would otherwise vanish silently — Sphinx itself accepts any variable in
    conf.py. Suppressible via ``suppress_warnings = ["clangquill.config"]``.
    """
    known = {name for name, _ in CONFIG_FIELDS}
    for name in config._raw_config:  # noqa: SLF001 - the conf.py namespace has no public accessor
        if name.startswith(CONFIG_PREFIX) and name not in known:
            logger.warning(
                "unknown config value %r — no clangquill option has that name (see clangquill.config.Config)",
                name,
                type="clangquill",
                subtype="config",
            )


def _warn_unresolved_paths(app: Sphinx, sphinx_config: SphinxConfig) -> None:
    """``config-inited`` hook: flag ``clangquill_input``/``include_dirs`` entries missing on disk.

    Paths are checked relative to the srcdir. This is a warning rather than a build failure: ``_run`` already raises a
    clean :class:`ExtensionError` for an input pattern that matches no files
    once parsing is attempted, and a dangling ``include_dirs`` entry is often
    harmless (e.g. an optional vendor directory). Suppressible via
    ``suppress_warnings = ["clangquill.paths"]``.
    """
    config = Config.from_mapping({name: getattr(sphinx_config, name) for name, _ in CONFIG_FIELDS})
    base = Path(app.srcdir)

    for pattern in config.input:
        candidate = Path(pattern)
        if not candidate.is_absolute():
            candidate = base / candidate
        if candidate.is_file() or any(
            Path(match).is_file()
            for match in glob.iglob(str(candidate), recursive=True)  # noqa: PTH207
        ):
            continue
        logger.warning(
            "clangquill_input entry does not resolve to an existing file: %r",
            pattern,
            type="clangquill",
            subtype="paths",
        )

    for entry in config.include_dirs:
        candidate = Path(entry)
        if not candidate.is_absolute():
            candidate = base / candidate
        if not candidate.is_dir():
            logger.warning(
                "clangquill_include_dirs entry does not exist: %r",
                entry,
                type="clangquill",
                subtype="paths",
            )


def _run(app: Sphinx) -> None:
    """``builder-inited`` hook: parse, render, and index into the srcdir."""
    config = Config.from_mapping({name: getattr(app.config, name) for name, _ in CONFIG_FIELDS})
    if not config.input:
        logger.info("clangquill: no clangquill_input configured; skipping generation")
        return
    if not _core.have_libclang():
        # Degrade gracefully where the core was built without libclang (e.g. a
        # docs environment lacking the dev headers) rather than failing the
        # whole Sphinx build. Still write a placeholder root document so any
        # toctree pointing at the output keeps resolving. Suppressible via
        # ``suppress_warnings = ["clangquill.libclang"]`` (e.g. for -W builds).
        logger.warning(
            "core built without libclang; skipping API generation",
            type="clangquill",
            subtype="libclang",
        )
        _write_placeholder(app, config)
        return
    try:
        result = build(config, base_dir=app.srcdir)
    except (ConfigError, FileNotFoundError) as exc:
        # Anticipated user-input failures (a bad clangquill_* value, an input
        # pattern matching nothing) become a clean build error instead of a
        # raw traceback.
        msg = f"clangquill: {exc}"
        raise ExtensionError(msg) from exc
    # Remembered for the build-finished hook so a throwaway IR can be removed.
    app._clangquill_temp_db = result.db_path if result.db_is_temporary else None  # noqa: SLF001
    logger.info(
        "clangquill: wrote %d page(s) from %d symbol(s) to %s",
        len(result.pages),
        result.symbol_count,
        result.output_dir,
    )
    for diagnostic in result.diagnostics:
        logger.warning("%s", diagnostic, type="clangquill", subtype="parse")


def _write_placeholder(app: Sphinx, config: Config) -> None:
    """Write a stub root document so a toctree referencing the output resolves."""
    out = Path(app.srcdir) / config.output_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{config.root_document}.md").write_text(
        "# API Reference\n\nAPI generation was skipped (libclang unavailable).\n",
        encoding="utf-8",
    )


def _cleanup(app: Sphinx, exception: Exception | None) -> None:  # noqa: ARG001
    """``build-finished`` hook: drop the throwaway IR when not caching.

    Stale *page* pruning happens during generation (see
    :func:`clangquill.pipeline.build`); here we only remove the temporary SQLite
    database so a build leaves no artifacts behind unless ``clangquill_cache_dir``
    asked for a persistent one.
    """
    db_path = getattr(app, "_clangquill_temp_db", None)
    if db_path is not None:
        db_path.unlink(missing_ok=True)
        app._clangquill_temp_db = None  # noqa: SLF001


def setup(app: Sphinx) -> dict[str, Any]:
    """Register config values and hooks; return extension metadata."""
    # Generated pages are MyST, so a MyST parser must be active to read them.
    # Pull in myst_parser only when no MyST parser is already configured —
    # myst_nb supersedes it and registering both for ``.md`` raises a conflict.
    # Inspect both already-loaded extensions and the full configured list, so the
    # result does not depend on where the extension sits in conf.py's order.
    configured = set(app.extensions) | set(app.config.extensions)
    if not ({"myst_parser", "myst_nb"} & configured):
        app.setup_extension("myst_parser")

    for name, default in CONFIG_FIELDS:
        app.add_config_value(name, default, "env")

    app.connect("config-inited", _warn_unknown_config)
    app.connect("config-inited", _warn_unresolved_paths)
    app.connect("builder-inited", _run)
    app.connect("build-finished", _cleanup)

    return {
        "version": __version__,
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }

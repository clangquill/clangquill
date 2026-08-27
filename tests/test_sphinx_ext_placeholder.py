"""Unit tests for ``sphinx_ext`` helpers that do not need a libclang-backed build.

Unlike ``test_sphinx_ext.py`` (a full Sphinx build, skipped without libclang),
these call ``_write_placeholder`` and ``_warn_unknown_config`` directly against
minimal stand-ins, so they run regardless of whether the core was built with
libclang.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from clangquill.config import Config
from clangquill.pipeline import MANIFEST_NAME

if TYPE_CHECKING:
    from pathlib import Path


def test_write_placeholder_prunes_pages_from_a_prior_real_build(tmp_path: Path) -> None:
    # sphinx_ext imports `sphinx` at module level; skip (not import) at
    # collection time when it is not installed (e.g. the `ci` extra), matching
    # every other sphinx_ext-touching test in this suite.
    pytest.importorskip("sphinx")
    from clangquill.sphinx_ext import _write_placeholder  # noqa: PLC0415

    # A libclang-enabled run writes real pages and a manifest; a later run that
    # falls back to the placeholder (libclang unavailable) must not leave those
    # pages behind with no toctree entry pointing at them.
    config = Config(input=["geo.hpp"], output_dir="api")
    out = tmp_path / "api"
    out.mkdir()
    (out / "index.md").write_text("# API Reference\n\nreal content\n", encoding="utf-8")
    (out / "geo.md").write_text("# Namespace geo\n", encoding="utf-8")
    (out / MANIFEST_NAME).write_text(json.dumps(["index.md", "geo.md"]), encoding="utf-8")

    app = SimpleNamespace(srcdir=str(tmp_path))
    _write_placeholder(app, config)

    assert not (out / "geo.md").exists()
    assert (out / "index.md").is_file()
    assert "API generation was skipped" in (out / "index.md").read_text(encoding="utf-8")
    assert json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8")) == ["index.md"]


def test_write_placeholder_leaves_unmanaged_files_alone(tmp_path: Path) -> None:
    pytest.importorskip("sphinx")
    from clangquill.sphinx_ext import _write_placeholder  # noqa: PLC0415

    # Only files a previous clangquill run tracked in its manifest are pruned;
    # a hand-written file sharing the output directory must survive.
    config = Config(input=["geo.hpp"], output_dir="api")
    out = tmp_path / "api"
    out.mkdir()
    (out / "handwritten.md").write_text("# Not ours\n", encoding="utf-8")

    app = SimpleNamespace(srcdir=str(tmp_path))
    _write_placeholder(app, config)

    assert (out / "handwritten.md").is_file()


def test_warn_unknown_config_survives_missing_raw_config() -> None:
    pytest.importorskip("sphinx")
    from clangquill.sphinx_ext import _warn_unknown_config  # noqa: PLC0415

    # If a future Sphinx version renames or drops the private `_raw_config`
    # attribute this check reaches into, it must degrade to a no-op instead of
    # raising and crashing `config-inited` (and therefore the whole build).
    config = SimpleNamespace()  # no `_raw_config` at all
    app = SimpleNamespace()
    _warn_unknown_config(app, config)  # must not raise

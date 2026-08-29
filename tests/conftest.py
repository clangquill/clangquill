"""Makes sure all our fixtures are available to all tests.

Individual test modules MUST NOT import fixtures from `tests.fixtures`,
as this can have strange side effects.
"""

from __future__ import annotations

import pytest

from clangquill import _core

pytest_plugins = [
    "tests.fixtures",
]

# Attributes the Python side reads out of the compiled core at import time.
# Editable installs do not rebuild the extension, so a stale `_core` surfaces
# as an AttributeError during collection -- which reads like a broken checkout
# rather than a missing rebuild.
_REQUIRED_CORE_ATTRS = (
    "SCHEMA_DDL",
    "SYMBOL_KINDS",
    "CONTENT_HASH_FIELDS",
    "COMMENT_FIELDS",
    "parse_doxygen_comment",
    "split_param_arg",
)


def pytest_collection(session: pytest.Session) -> None:
    """Fail loudly, and with the fix, when the compiled core is out of date."""
    missing = [name for name in _REQUIRED_CORE_ATTRS if not hasattr(_core, name)]
    if missing:
        pytest.exit(
            "clangquill._core is stale: missing "
            + ", ".join(missing)
            + "\nRun `uv sync --reinstall-package clangquill` -- editable installs "
            "do not rebuild the extension when src/cpp changes.",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    _ = session

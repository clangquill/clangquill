"""The Python enum mirrors must match the C++ enums they are declared from.

``SymbolKind``, ``AccessKind`` and ``RefKind`` in ``clangquill.store`` are
hand-written ``IntEnum``s, and their integer values are the on-disk schema: a
fixture or a generator branch that names the wrong member still reads and
writes a well-formed database, so drift between the two sides is silent
everywhere else in the suite. Issue #300 is what that costs.

They are still declared by hand, deliberately -- the declaration is where the
per-member docstrings and the static names a type checker and an IDE can see
live. What changed is the other side of the comparison: the core now exports
its own tables (``_core.SYMBOL_KINDS`` and friends, built from the enumerators
themselves), so this compares two things the *installed package* exposes
instead of regex-parsing headers that are absent from a wheel.

The C++ tables are additionally self-checking: they spell their values as
enumerator references, so a rename does not compile, and a ``static_assert`` on
their size catches an enumerator added without a row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from clangquill import _core
from clangquill.store import AccessKind, RefKind, SymbolKind

if TYPE_CHECKING:
    from enum import IntEnum


@pytest.mark.parametrize(
    ("mirror", "exported"),
    [
        (SymbolKind, "SYMBOL_KINDS"),
        (AccessKind, "ACCESS_KINDS"),
        (RefKind, "REF_KINDS"),
    ],
    ids=lambda arg: arg if isinstance(arg, str) else arg.__name__,
)
def test_python_mirror_matches_the_cpp_enum(mirror: type[IntEnum], exported: str) -> None:
    """Member-for-member, name and value, against the core's own table."""
    assert {m.name: int(m) for m in mirror} == getattr(_core, exported)


@pytest.mark.parametrize(
    "exported",
    ["SYMBOL_KINDS", "ACCESS_KINDS", "REF_KINDS", "STORAGE_KINDS", "TEMPLATE_PARAM_KINDS"],
)
def test_the_exported_enum_tables_are_populated(exported: str) -> None:
    """Guard the guard: an empty export would make every comparison above vacuous."""
    table = getattr(_core, exported)
    assert table
    assert all(isinstance(name, str) and isinstance(value, int) for name, value in table.items())
    # The values are an unbroken run from zero, which is what lets the C++
    # static_assert compare a table's size against its last enumerator.
    assert sorted(table.values()) == list(range(len(table)))


def test_the_enums_without_a_python_mirror_are_still_exported() -> None:
    """``StorageKind`` and ``TemplateParameter::Kind`` are persisted as integers too.

    Neither has an ``IntEnum`` in ``clangquill.store`` today -- ``storage`` is
    not read back at all and ``param_kind`` is kept as a plain ``int``. They are
    exported anyway so that adding either mirror later is a comparison rather
    than a transcription.
    """
    assert _core.STORAGE_KINDS["NONE"] == 0
    assert _core.TEMPLATE_PARAM_KINDS == {"TYPE": 0, "NON_TYPE": 1, "TEMPLATE": 2}

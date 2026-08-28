"""The Python enum mirrors must match the C++ enums they are copied from.

``SymbolKind``, ``AccessKind`` and ``RefKind`` in ``clangquill.store`` are
hand-maintained transcriptions of ``clangquill::model``'s enums, and their
integer values are the on-disk schema: a fixture or a generator branch that
names the wrong member still reads and writes a well-formed database, so drift
between the two sides is silent everywhere else in the suite. Parsing the
headers and comparing member-for-member is the only thing that makes an added,
removed or reordered C++ enumerator fail loudly on the Python side.

The bindings do not export these enums, so the C++ side is read from the
headers -- the same approach ``fixtures.py`` already takes for the schema DDL.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from clangquill.store import AccessKind, RefKind, SymbolKind

if TYPE_CHECKING:
    from enum import IntEnum

_MODEL_DIR = Path(__file__).resolve().parents[1] / "src" / "cpp" / "model"

# `Name`, `Name = 0`, either followed by a `///<` comment, one per line.
_ENUMERATOR_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:=\s*(\d+)\s*)?,")


def _cpp_enum(header: str, name: str) -> dict[str, int]:
    """Return ``{ENUMERATOR_NAME: value}`` for the C++ enum ``name``.

    Names come back in the Python mirror's SCREAMING_SNAKE spelling so the two
    sides compare directly, and values follow C++'s implicit-numbering rule
    (one past the previous enumerator unless the source pins it).
    """
    text = (_MODEL_DIR / header).read_text()
    match = re.search(rf"enum class {name} \{{(.*?)\n\}};", text, re.DOTALL)
    assert match is not None, f"no `enum class {name}` in {header}"

    members: dict[str, int] = {}
    nxt = 0
    for line in match.group(1).splitlines():
        found = _ENUMERATOR_RE.match(line)
        if found is None:
            continue  # a doc comment or a blank line between enumerators
        if found.group(2) is not None:
            nxt = int(found.group(2))
        # `TypeAlias` -> `TYPE_ALIAS`, `Class` -> `CLASS`.
        members[re.sub(r"(?<!^)(?=[A-Z])", "_", found.group(1)).upper()] = nxt
        nxt += 1
    assert members, f"parsed no enumerators out of `enum class {name}`"
    return members


@pytest.mark.parametrize(
    ("mirror", "header", "cpp_name"),
    [
        (SymbolKind, "symbol.hpp", "SymbolKind"),
        (AccessKind, "symbol.hpp", "AccessKind"),
        (RefKind, "reference.hpp", "RefKind"),
    ],
    ids=["SymbolKind", "AccessKind", "RefKind"],
)
def test_python_mirror_matches_the_cpp_enum(mirror: type[IntEnum], header: str, cpp_name: str) -> None:
    assert {m.name: int(m) for m in mirror} == _cpp_enum(header, cpp_name)


def test_the_parser_would_notice_a_drifted_enumerator() -> None:
    """Guard the guard: a parse that silently returned ``{}`` would pass everything above.

    Pins the two properties the comparison rests on -- implicit numbering, and
    the CamelCase-to-SCREAMING_SNAKE spelling -- against known members rather
    than only against the mirror this test is meant to police.
    """
    kinds = _cpp_enum("symbol.hpp", "SymbolKind")
    assert kinds["UNKNOWN"] == 0  # the only explicitly numbered enumerator
    assert kinds["NAMESPACE"] == 1  # implicitly numbered from it
    assert kinds["TYPE_ALIAS"] == 14  # the multi-word spelling, far down the list
    assert len(kinds) == len(SymbolKind)

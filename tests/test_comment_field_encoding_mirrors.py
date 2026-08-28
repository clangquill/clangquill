"""Drift test for the ``comment_fields`` arg-encoding contract (see #312).

A directed ``@param``/``@tparam`` has to travel through one ``comment_fields``
row with a single ``arg`` slot, so the C++ writer (``param_arg()`` in
``parser/comment_parser.cpp``) packs the parameter's direction into that slot
as a bracketed prefix (``"[out] result"``), and the Python reader
(:func:`clangquill.comments._split_direction`) splits it back off. Until now
this was pinned only indirectly: ``tests/test_comments.py`` asserts
``_split_direction``/``model_from_fields`` against *hardcoded* strings like
``"[out] result"``, which would keep passing even if ``param_arg()`` changed
its bracket format on the C++ side alone (a mismatched real ``comment_fields``
row would only surface as a mangled ``direction`` at generator time).

This regex-parses the real C++ source instead of assuming its shape --
the same technique ``tests/test_enum_mirrors.py`` uses for the enum
mirrors -- so a reformat on either side breaks loudly here.
"""

from __future__ import annotations

import re
from pathlib import Path

from clangquill.comments import _split_direction

_CPP_PARSER_DIR = Path(__file__).resolve().parents[1] / "src" / "cpp" / "parser"
_COMMENT_PARSER_CPP = _CPP_PARSER_DIR / "comment_parser.cpp"
_DOXYGEN_PARSER_CPP = _CPP_PARSER_DIR / "doxygen_comment_parser.cpp"

# param_arg() has an empty-direction early return, then wraps a non-empty
# direction in two literal string pieces glued around `p.direction`.
# Captures those two pieces rather than assuming them, so a reformat
# (spacing, brackets, delimiter) is actually noticed instead of silently
# matched by a hardcoded literal.
_PARAM_ARG_RE = re.compile(
    r"std::string\s+param_arg\([^)]*\)\s*\{\s*"
    r"if\s*\(p\.direction\.empty\(\)\)\s*return\s+p\.name;\s*"
    r'return\s+"([^"]*)"\s*\+\s*p\.direction\s*\+\s*"([^"]*)"\s*\+\s*p\.name;',
    re.DOTALL,
)

# Every `return "<value>";` inside `canonical_direction()` for a non-empty
# result is a spelling `p.direction` -- and so `param_arg()` -- can actually
# carry; the empty-string return (`return {};`) is the "no direction" case
# `param_arg()` handles separately (the `.empty()` branch above).
_CANONICAL_DIRECTION_RE = re.compile(r'return\s+"([a-z,]+)";')


def _param_arg_format() -> tuple[str, str]:
    """Return the literal ``(prefix, suffix)`` C++ wraps a direction in."""
    text = _COMMENT_PARSER_CPP.read_text(encoding="utf-8")
    match = _PARAM_ARG_RE.search(text)
    assert match is not None, (
        "could not find param_arg()'s bracket format in comment_parser.cpp -- "
        "its shape changed; update this test's regex to match, then check "
        "_split_direction still parses it"
    )
    return match.group(1), match.group(2)


def _canonical_directions() -> set[str]:
    """Every non-empty direction ``canonical_direction()`` can return."""
    text = _DOXYGEN_PARSER_CPP.read_text(encoding="utf-8")
    match = re.search(r"std::string canonical_direction\([^)]*\)\s*\{(.*?)\n\}", text, re.DOTALL)
    assert match is not None, "could not find canonical_direction() in doxygen_comment_parser.cpp"
    return set(_CANONICAL_DIRECTION_RE.findall(match.group(1)))


def test_the_parser_would_notice_a_drifted_canonical_direction() -> None:
    """Guard the guard: pin the known direction spellings against a vacuous match."""
    assert _canonical_directions() == {"in", "out", "in,out"}


def test_split_direction_round_trips_every_direction_param_arg_can_write() -> None:
    prefix, suffix = _param_arg_format()
    for direction in _canonical_directions():
        arg = f"{prefix}{direction}{suffix}result"
        assert _split_direction(arg) == ("result", direction)


def test_split_direction_leaves_an_undirected_param_arg_untouched() -> None:
    # param_arg()'s first branch: an empty p.direction returns p.name
    # verbatim, no brackets at all.
    assert _split_direction("result") == ("result", "")


def test_split_direction_tolerates_the_inout_spelling_param_arg_never_writes() -> None:
    """Python also accepts the bare ``inout`` spelling; a deliberate asymmetry, not a drift.

    ``canonical_direction`` already folds it into ``"in,out"`` before it ever
    reaches ``param_arg``, so C++ never *writes* the bare spelling, but a
    ``comment_fields`` row is not guaranteed to have come only from this
    writer, so the reader stays the more permissive of the two on purpose.
    """
    assert _split_direction("[inout] result") == ("result", "in,out")

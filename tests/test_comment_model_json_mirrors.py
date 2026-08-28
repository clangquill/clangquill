"""Drift test for the ``to_fields_json`` / ``CommentModel`` field-set contract (see #312).

``tests/test_comment_corpus.py`` already asserts, per corpus fixture, that
``doxygen_parse`` (Python) and ``DoxygenCommentParser::parse_raw_text`` (C++)
produce the *same values* -- but only for whatever fields the corpus fixtures
happen to exercise. A field added to :class:`clangquill.comments.CommentModel`
(or ``CommentParam``/``CommentRetval``/``CommentThrow``) without a matching key
in the C++ ``to_fields_json``/``params_to_json`` -- or vice versa -- would not
fail that test at all: ``dataclasses.asdict(model) == case["expected"]`` never
even looks at a dataclass field the corpus JSON doesn't mention, and the C++
side never emits a key nobody asked for. This pins the *field sets themselves*
against the real C++ source (the same technique ``tests/test_enum_mirrors.py``
uses for the enum mirrors), independent of any fixture's content.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from clangquill.comments import CommentModel, CommentParam, CommentRetval, CommentThrow

_COMMENT_PARSER_CPP = Path(__file__).resolve().parents[1] / "src" / "cpp" / "parser" / "comment_parser.cpp"

# A field name spelled `{"name", ...}` (in fielded object literals like
# `json j = { {"brief", m.brief}, ... };` or
# `arr.push_back({{"name", p.name}, ...});`).
_KEY_RE = re.compile(r'\{"([a-zA-Z_]+)",')


def _cpp_text() -> str:
    return _COMMENT_PARSER_CPP.read_text(encoding="utf-8")


def _braced_block(text: str, *, after: str) -> str:
    """Return the ``{ ... }`` block whose opening brace follows ``after``."""
    start = text.index(after) + len(after)
    start = text.index("{", start)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    msg = f"unbalanced braces looking for the block after {after!r}"
    raise AssertionError(msg)


def _to_fields_json_keys() -> list[str]:
    """Return the top-level JSON keys ``to_fields_json`` emits for a ``CommentModel``."""
    text = _cpp_text()
    block = _braced_block(text, after="json j =")
    return _KEY_RE.findall(block)


def _params_to_json_keys() -> list[str]:
    """Return the keys ``params_to_json`` emits per ``CommentParam``."""
    text = _cpp_text()
    block = _braced_block(text, after="arr.push_back(")
    return _KEY_RE.findall(block)


def _retval_json_keys() -> list[str]:
    text = _cpp_text()
    block = _braced_block(text, after="retvals.push_back(")
    return _KEY_RE.findall(block)


def _throw_json_keys() -> list[str]:
    text = _cpp_text()
    block = _braced_block(text, after="throws.push_back(")
    return _KEY_RE.findall(block)


def _dataclass_field_names(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def test_comment_model_fields_match_to_fields_json_keys() -> None:
    keys = _to_fields_json_keys()
    assert keys, "found no keys in to_fields_json's json object literal -- did its shape change?"
    assert set(keys) == _dataclass_field_names(CommentModel)


def test_comment_param_fields_match_params_to_json_keys() -> None:
    keys = _params_to_json_keys()
    assert keys, "found no keys in params_to_json's json object literal -- did its shape change?"
    assert set(keys) == _dataclass_field_names(CommentParam)


def test_comment_retval_fields_match_to_fields_json_keys() -> None:
    keys = _retval_json_keys()
    assert keys, "found no keys building a retval json object -- did its shape change?"
    assert set(keys) == _dataclass_field_names(CommentRetval)


def test_comment_throw_fields_match_to_fields_json_keys() -> None:
    keys = _throw_json_keys()
    assert keys, "found no keys building a throw json object -- did its shape change?"
    assert set(keys) == _dataclass_field_names(CommentThrow)

"""Drift tests for the by-convention C++<->Python mirrors inventoried in issue #312.

``tests/test_enum_mirrors.py`` proved the pattern: a hand-synced contract
between the C++ core and the Python read side drifts silently unless
something parses (or loads) both sides and compares them member-for-member.
This module does the same for the contracts that were still only pinned
*indirectly* -- through corpus fixtures that happen to exercise both sides,
rather than through an assertion that would fail if one side changed alone.

See ``docs/development/mirror-contracts.md`` for the full inventory, including
the two contracts that turned out to already be immune to drift (the schema
DDL and ``SCHEMA_VERSION``) and so only get a light guard-the-guard test here.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from clangquill import _core
from clangquill.comments import CommentModel, CommentParam, CommentRetval, CommentThrow, _split_direction
from clangquill.store import Symbol, TemplateParameter

_ROOT = Path(__file__).resolve().parents[1]
_COMMENT_PARSER_CPP = _ROOT / "src" / "cpp" / "parser" / "comment_parser.cpp"
_DOXYGEN_PARSER_CPP = _ROOT / "src" / "cpp" / "parser" / "doxygen_comment_parser.cpp"
_CONTENT_HASH_CPP = _ROOT / "src" / "cpp" / "hash" / "content_hash.cpp"
_GENERATOR_PY = _ROOT / "src" / "clangquill" / "generator.py"
_SCHEMA_HPP = _ROOT / "src" / "cpp" / "store" / "schema.hpp"


# --- 1. comment_fields arg encoding: param_arg() (C++) <-> _split_direction (Python) --


def _cpp_param_directions() -> set[str]:
    """Return every ``direction`` spelling the C++ parsers can hand to ``param_arg``.

    ``canonical_direction`` (the raw-text path) and ``explicit_direction`` (the
    libclang-AST path) are the only two places that produce a ``CommentParam``'s
    ``direction``; whatever they can return is what ``param_arg`` -- and so
    ``_split_direction`` -- must round-trip.
    """
    text = _DOXYGEN_PARSER_CPP.read_text(encoding="utf-8")
    directions = {""}
    for fn in ("canonical_direction", "explicit_direction"):
        match = re.search(rf"std::string {fn}\(.*?\)\s*\{{(.*?)\n\}}", text, re.DOTALL)
        assert match is not None, f"no `{fn}` function found in {_DOXYGEN_PARSER_CPP.name}"
        directions.update(re.findall(r'return "([^"]*)"', match.group(1)))
    return directions


def test_the_direction_parser_would_notice_a_drifted_spelling() -> None:
    """Guard the guard: pin the known direction spellings against a parse that returned too few."""
    assert _cpp_param_directions() == {"", "in", "out", "in,out"}


def test_split_direction_inverts_param_arg_for_every_direction_cpp_can_write() -> None:
    """``_split_direction`` must invert ``param_arg``'s bracket format exactly.

    ``param_arg`` (``comment_parser.cpp``) writes a directed parameter as
    ``"[" + direction + "] " + name``, and an undirected one as bare ``name``.
    Pin that literal format so a reformat on the C++ side (extra spaces, a
    different bracket) fails here instead of only showing up as a misparsed
    argument downstream, then check the Python regex actually inverts it for
    every direction the C++ parsers can produce.
    """
    text = _COMMENT_PARSER_CPP.read_text(encoding="utf-8")
    match = re.search(r"std::string param_arg\(.*?\)\s*\{(.*?)\n\}", text, re.DOTALL)
    assert match is not None, "no `param_arg` function found in comment_parser.cpp"
    body = match.group(1)
    assert "p.direction.empty()" in body
    assert '"[" + p.direction + "] " + p.name' in body

    for direction in _cpp_param_directions():
        arg = f"[{direction}] name" if direction else "name"
        assert _split_direction(arg) == ("name", direction)


# --- 2. schema DDL: schema.hpp <-> tests/fixtures.py -----------------------------------
#
# `tests/fixtures.py` no longer copies the DDL -- it extracts it from
# `schema.hpp` at test time with the same `R"SQL(...)SQL"` split `test_enum_mirrors.py`
# uses for enums, so there is nothing left here that can drift out of step. The
# only remaining risk is the extraction marker itself silently matching nothing,
# which this guards.


def test_the_schema_ddl_markers_still_bracket_the_ddl() -> None:
    text = _SCHEMA_HPP.read_text(encoding="utf-8")
    ddl = text.split('R"SQL(', 1)[1].rsplit(')SQL"', 1)[0]
    for table in (
        "meta",
        "files",
        "symbols",
        "function_parameters",
        "template_parameters",
        "enumerators",
        "references_",
        "comments",
        "comment_fields",
        "groups",
        "group_members",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table} " in ddl, f"`{table}` missing from the extracted DDL"


# --- 3. fingerprint composition: content_hash (C++) <-> _wide_tokens (Python) ----------


def _cpp_content_hash_symbol_fields() -> list[str]:
    """Return the ``Symbol`` fields ``content_hash`` folds in, in hashing order."""
    text = _CONTENT_HASH_CPP.read_text(encoding="utf-8")
    match = re.search(r"std::string content_hash\(.*?\)\s*\{(.*?)\n\}", text, re.DOTALL)
    assert match is not None, "no `content_hash` function found in content_hash.cpp"
    return re.findall(r"sym\.(\w+)", match.group(1))


def _wide_tokens_body() -> str:
    text = _GENERATOR_PY.read_text(encoding="utf-8")
    start = text.index("def _wide_tokens(self, symbol: Symbol)")
    end = text.index("\n    def ", start)
    return text[start:end]


def test_the_content_hash_parser_would_notice_a_dropped_field() -> None:
    """Guard the guard: pin the known hashed fields against a parse that returned too few."""
    assert set(_cpp_content_hash_symbol_fields()) == {
        "usr",
        "kind",
        "qualified_name",
        "signature",
        "type_repr",
        "access",
        "storage",
        "is_definition",
    }


def test_wide_tokens_covers_exactly_the_symbol_fields_content_hash_leaves_out() -> None:
    """``_wide_tokens`` must widen the fingerprint by exactly what ``content_hash`` skips.

    ``page_fingerprint``'s docstring claims ``wide`` adds "the symbol-row fields
    content_hash leaves out (spelling, display name, parent, documented flag,
    declaring file and line)". If a field were added to the Python ``Symbol``
    mirror without either the C++ hash or the wide fingerprint covering it, a
    custom template reading it would silently escape cache invalidation.

    ``storage`` is hashed on the C++ side (``StorageKind``) but has no Python
    mirror at all -- there is nothing on the Symbol row for either side to
    widen for, so it is excluded rather than asserted on.
    """
    hash_covered = set(_cpp_content_hash_symbol_fields())
    wide_covered = set(re.findall(r"symbol\.(\w+)", _wide_tokens_body())) - {"usr"}
    dataclass_fields = {f.name for f in dataclasses.fields(Symbol)} - {"content_hash"}

    assert dataclass_fields - hash_covered == wide_covered


def test_wide_tokens_covers_every_template_parameter_field() -> None:
    """``_wide_tokens`` is the *only* token that reads template parameters at all."""
    tp_covered = set(re.findall(r"tp\.(\w+)", _wide_tokens_body()))
    assert tp_covered == {f.name for f in dataclasses.fields(TemplateParameter)}


# --- 4. corpus JSON encoding: to_fields_json (C++) <-> CommentModel (Python) -----------


def _cpp_json_object_keys(text: str, *, start_marker: str) -> list[str]:
    start = text.index(start_marker)
    end = text.index("};", start)
    return re.findall(r'\{"(\w+)",', text[start:end])


def test_to_fields_json_key_set_matches_comment_model() -> None:
    """An explicit key-set check, since the corpus alone only catches a field with a non-default case.

    ``tests/comment_corpus/`` pins the shape both parsers produce -- but only
    for the fields a corpus case happens to populate. A field added to
    ``CommentModel`` (Python) or the C++ ``to_fields_json`` object literal
    without a matching addition on the other side, and never exercised by a
    corpus case, would pass every corpus test while the two models silently
    diverge. Compare the key sets directly instead.
    """
    text = _COMMENT_PARSER_CPP.read_text(encoding="utf-8")
    cpp_keys = set(_cpp_json_object_keys(text, start_marker="json j = {"))
    python_keys = {f.name for f in dataclasses.fields(CommentModel)}
    assert cpp_keys == python_keys


def test_to_fields_json_nested_key_sets_match_their_python_dataclasses() -> None:
    text = _COMMENT_PARSER_CPP.read_text(encoding="utf-8")

    params_start = text.index("json params_to_json(")
    params_end = text.index("}\n", params_start)
    param_keys = set(re.findall(r'\{"(\w+)",', text[params_start:params_end]))
    assert param_keys == {f.name for f in dataclasses.fields(CommentParam)}

    retvals_start = text.index("json retvals = json::array();")
    retvals_end = text.index(";", retvals_start + len("json retvals = json::array();"))
    retval_keys = set(re.findall(r'\{"(\w+)",', text[retvals_start:retvals_end]))
    assert retval_keys == {f.name for f in dataclasses.fields(CommentRetval)}

    throws_start = text.index("json throws = json::array();")
    throws_end = text.index(";", throws_start + len("json throws = json::array();"))
    throw_keys = set(re.findall(r'\{"(\w+)",', text[throws_start:throws_end]))
    assert throw_keys == {f.name for f in dataclasses.fields(CommentThrow)}


# --- 5. SCHEMA_VERSION (Python-bound) <-> kSchemaVersion (C++) -------------------------
#
# `module.cpp` binds `m.attr("SCHEMA_VERSION")` straight to
# `clangquill::store::kSchemaVersion`, so `_core.SCHEMA_VERSION` cannot drift
# from the constant by editing the Python side -- there is no second integer
# to keep in sync. The only way this contract breaks is `module.cpp` binding a
# stale literal instead of the constant, which this test still catches.


def test_schema_version_binding_matches_the_cpp_constant() -> None:
    text = _SCHEMA_HPP.read_text(encoding="utf-8")
    match = re.search(r"kSchemaVersion\s*=\s*(\d+)", text)
    assert match is not None, "no `kSchemaVersion` found in schema.hpp"
    assert int(match.group(1)) == _core.SCHEMA_VERSION

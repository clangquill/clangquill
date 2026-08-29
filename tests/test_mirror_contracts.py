"""What is left of the C++<->Python contracts once the mirrors were removed.

Issue #312 asked for every by-convention contract between the C++ core and the
Python read side to be *either* eliminated *or* given a drift test. The first
round added drift tests; this module is what remains after the second round
eliminated the duplication instead.

Four of the six contracts no longer exist. The core exports its own
definitions -- ``_core.SCHEMA_DDL``, ``SYMBOL_KINDS``, ``CONTENT_HASH_FIELDS``,
``COMMENT_FIELDS`` -- and Python derives from them, so the tests below compare
two things the *installed package* exposes rather than regex-scraping
``src/cpp``. That matters: a scrape passes vacuously against a wheel, where
there is no C++ source to read.

See ``docs/development/mirror-contracts.md`` for the inventory and what
replaced each row.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from clangquill import _core
from clangquill.comments import CommentModel, CommentParam, CommentRetval, CommentThrow, _split_direction
from clangquill.generator import _WIDE_SYMBOL_FIELDS, _WIDE_TEMPLATE_PARAM_FIELDS
from clangquill.store import Store, Symbol, TemplateParameter

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_HPP = _ROOT / "src" / "cpp" / "store" / "schema.hpp"
_MODULE_CPP = _ROOT / "src" / "cpp" / "bindings" / "module.cpp"
_CORPUS_DIR = _ROOT / "tests" / "comment_corpus"


# --- 1. comment_fields arg encoding -------------------------------------------
#
# Was: `param_arg()` in C++ against a `_split_direction` regex in Python, pinned
# by asserting the C++ source contained a particular string literal.
# Now: `_split_direction` *is* `_core.split_param_arg`, the encoder's own
# inverse. What is left to check is that the round trip actually closes, which
# is a property of the pair rather than of either side's spelling.


@pytest.mark.parametrize("direction", ["", "in", "out", "in,out"])
def test_a_parameter_direction_survives_the_field_encoding(direction: str) -> None:
    """Every direction the parsers can produce must round-trip through one `arg` slot."""
    arg = f"[{direction}] result" if direction else "result"
    assert _split_direction(arg) == ("result", direction)


def test_the_python_decoder_inverts_the_cpp_encoder() -> None:
    """Decoding rows in Python and in C++ must reach the same model.

    ``comment_fields_roundtrip`` decodes the rows with the C++
    ``from_comment_fields`` and re-encodes them; feeding it rows that Python's
    ``model_from_fields`` also understands, and getting the same rows back,
    pins the two decoders against each other without binding ``CommentModel``.
    """
    rows = [
        ("brief", "", "A summary."),
        ("detail", "", "The long story."),
        ("param", "[out] result", "where the answer goes"),
        ("tparam", "", "T"),
        ("retval", "0", "success"),
        ("throws", "std::bad_alloc", "when out of memory"),
        ("note", "", "worth knowing"),
        ("madeup", "", "an unrecognized command"),
    ]
    assert _core.comment_fields_roundtrip(rows) == rows


# --- 2. Schema DDL ------------------------------------------------------------
#
# Was: `tests/fixtures.py` sliced the DDL out of `schema.hpp` between the
# `R"SQL(` markers, guarded by a test that the markers still bracketed anything.
# Now: `_core.SCHEMA_DDL` is the constant itself, so there are no markers to
# guard -- but the *columns* `store.py` names in its queries were never checked
# against it at all, which is contract 6 below.


def test_the_bound_schema_ddl_creates_the_ir_tables() -> None:
    """Guard the guard: an empty or truncated export would make contract 6 vacuous."""
    with closing(sqlite3.connect(":memory:")) as con:
        con.executescript(_core.SCHEMA_DDL)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"meta", "files", "symbols", "references_", "comments", "comment_fields"} <= tables


# --- 3. Fingerprint composition ----------------------------------------------
#
# Was: regexes over both `content_hash()` and `_wide_tokens()`'s function bodies.
# Now: `content_hash()` walks a table that `_core.CONTENT_HASH_FIELDS` exports,
# and `_wide_tokens` is built from an explicit tuple, so the invariant is set
# algebra over two exported lists.
#
# `storage` is hashed on the C++ side but has no Python `Symbol` field, so it
# drops out of the difference rather than being asserted on.


def test_wide_tokens_covers_exactly_the_symbol_fields_content_hash_leaves_out() -> None:
    """A per-symbol page's fingerprint must cover the whole `Symbol` row, once.

    An overlap wastes work; a gap lets a custom template render from a field no
    fingerprint tracks, so an edit to it would not invalidate the cached page.
    """
    hashed = set(_core.CONTENT_HASH_FIELDS)
    wide = set(_WIDE_SYMBOL_FIELDS)
    columns = {f.name for f in dataclasses.fields(Symbol)} - {"content_hash"}

    assert not (hashed & wide), "a field is both hashed and in the wide fingerprint"
    assert columns - hashed == wide


def test_wide_tokens_covers_every_template_parameter_field() -> None:
    """`content_hash` covers no template parameter at all, so the wide token covers them all."""
    assert set(_WIDE_TEMPLATE_PARAM_FIELDS) == {f.name for f in dataclasses.fields(TemplateParameter)}


def test_the_content_hash_export_is_not_empty() -> None:
    """Guard the guard: an empty export would make the set algebra above vacuous."""
    assert set(_core.CONTENT_HASH_FIELDS) >= {"usr", "kind", "qualified_name", "signature"}


# --- 4. CommentModel shape ----------------------------------------------------
#
# Was: regexes over `to_fields_json`'s `json j = {` object literal and the
# nested array builders.
# Now: `_core.COMMENT_FIELDS` is the encoder's own field table, and the nested
# entry shapes are pinned by the corpus -- which the last test here proves
# actually exercises every one of them, closing the "a field no case populates
# can drift" hole that motivated the original scrape.


def test_comment_model_fields_match_the_cores_field_table() -> None:
    members = {member for member, _shape in _core.COMMENT_FIELDS.values()}
    assert members | {"custom"} == {f.name for f in dataclasses.fields(CommentModel)}


def test_the_corpus_populates_every_comment_field() -> None:
    """Every `CommentModel` field, and every field of a nested entry, must appear.

    The corpus holds both parsers' output equal by example, so a field that no
    case ever populates is a field that could drift without failing anything.
    """
    populated: set[str] = set()
    nested: dict[str, set[str]] = {}
    for path in sorted(_CORPUS_DIR.glob("*.json")):
        expected = json.loads(path.read_text(encoding="utf-8"))["expected"]
        for name, value in expected.items():
            if value:
                populated.add(name)
            if name in {"params", "tparams", "retvals", "throws"}:
                for entry in value:
                    nested.setdefault(name, set()).update(k for k, v in entry.items() if v)

    assert populated == {f.name for f in dataclasses.fields(CommentModel)}
    for name, entry_type in (
        ("params", CommentParam),
        ("tparams", CommentParam),
        ("retvals", CommentRetval),
        ("throws", CommentThrow),
    ):
        assert nested.get(name, set()) == {f.name for f in dataclasses.fields(entry_type)}, name


# --- 5. SCHEMA_VERSION (Python-bound) <-> kSchemaVersion (C++) ----------------
#
# Never a mirror: `module.cpp` binds `_core.SCHEMA_VERSION` straight to the
# constant, so there is no second integer. This is the one remaining check that
# reads C++ source, and it is here only because a binding pointing at a stale
# literal would look identical from Python.


def test_schema_version_binding_matches_the_cpp_constant() -> None:
    text = _SCHEMA_HPP.read_text(encoding="utf-8")
    match = re.search(r"kSchemaVersion\s*=\s*(\d+)", text)
    assert match is not None, "no `kSchemaVersion` found in schema.hpp"
    assert int(match.group(1)) == _core.SCHEMA_VERSION


def test_schema_version_is_bound_from_the_constant_not_a_literal() -> None:
    text = _MODULE_CPP.read_text(encoding="utf-8")
    assert 'm.attr("SCHEMA_VERSION") = clangquill::store::kSchemaVersion;' in text


# --- 6. store.py's column lists <-> the schema (new) --------------------------
#
# `_SYMBOL_COLUMNS` and ~10 sibling column tuples are spelled out in `store.py`
# and were never checked against the DDL. Rather than string-matching them,
# build a database from the bound DDL and run every reader: sqlite validates
# every column name in every query, including the ones buried in method bodies
# that a `_SYMBOL_COLUMNS`-only check would miss.


def test_every_store_query_names_columns_the_schema_has(tmp_path: Path) -> None:
    db = tmp_path / "empty.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(_core.SCHEMA_DDL)
        con.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(_core.SCHEMA_VERSION),),
        )
        con.commit()

    with Store.open(db) as store:
        for empty in (
            store.symbols(),
            store.roots(),
            store.file_roots(1),
            store.children("nope"),
            store.bases("nope"),
            store.friends("nope"),
            store.parameters("nope"),
            store.template_parameters("nope"),
            store.enumerators("nope"),
            store.references("nope"),
            store.groups(),
            store.root_groups(),
            store.subgroups("nope"),
            store.group_members("nope"),
            store.group_symbols("nope"),
            store.files(),
        ):
            assert empty == []

        assert store.related_by_name() == {}
        assert store.symbol("nope") is None
        assert store.group("nope") is None
        assert store.raw_comment("nope") is None
        assert store.comment("nope") is None
        assert store.symbol_count() == 0
        assert store.reference_count() == 0
        assert store.file_count() == 0


def test_the_symbol_column_list_matches_the_schema(tmp_path: Path) -> None:
    """Guard the guard: a reader that stopped naming columns would pass the smoke test."""
    db = tmp_path / "empty.sqlite"
    with closing(sqlite3.connect(db)) as con:
        con.executescript(_core.SCHEMA_DDL)
        schema_columns = {r[1] for r in con.execute("PRAGMA table_info(symbols)")}

    named = {c.strip() for c in Store._SYMBOL_COLUMNS.split(",")}  # noqa: SLF001
    assert named
    assert named <= schema_columns


# --- 8. The CommentModel table in the parser guide (new) ----------------------
#
# `docs/guides/comment-parsers.md` documents the model field by field, which
# made it a third hand-written copy of the field list next to the C++ macro and
# the Python dataclass. It stays hand-written -- it carries prose the other two
# do not -- but it no longer gets to be silently incomplete.

_PARSER_GUIDE = _ROOT / "docs" / "guides" / "comment-parsers.md"


def test_the_parser_guide_documents_every_comment_field() -> None:
    """A field added to the model must be documented, or the guide is a lie."""
    table = _PARSER_GUIDE.read_text(encoding="utf-8")
    start = table.index("| Field | Type | From (Doxygen) |")
    end = table.index("\n\n", start)

    documented: set[str] = set()
    for line in table[start:end].splitlines()[2:]:
        first_column = line.split("|")[1]
        documented.update(re.findall(r"`(\w+)`", first_column))

    assert documented == {f.name for f in dataclasses.fields(CommentModel)}

"""End-to-end: C++ parses Doxygen comments; Python reads the structured model."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from clangquill import _core
from clangquill.comments import OVERRIDE_ENV, CommentModel
from clangquill.store import Store

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

FIXTURE = """
namespace doc {
/**
 * Brief summary line.
 *
 * Longer detail paragraph.
 *
 * @param x the input value
 * @return the doubled value
 * @retval 0 when x is zero
 * @throws std::runtime_error on overflow
 * @note rounds toward zero
 * @since 2.0
 */
int doubler(int x);
}
"""


def _usr_for(store: Store, qualified_name: str) -> str:
    for sym in store.symbols():
        if sym.qualified_name == qualified_name:
            return sym.usr
    msg = f"symbol {qualified_name!r} not found"
    raise AssertionError(msg)


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_comment_reconstructed_from_fields(tmp_path: Path) -> None:
    header = tmp_path / "doc.hpp"
    header.write_text(FIXTURE)
    db = tmp_path / "out.sqlite"
    _core.parse_to_sqlite([str(header)], str(db), _core.ParseOptions())

    with Store.open(db) as store:
        usr = _usr_for(store, "doc::doubler")

        raw = store.raw_comment(usr)
        assert raw is not None
        assert raw.format == "doxygen"
        assert "@param x" in raw.raw_text

        model = store.comment(usr)
        assert model is not None
        assert model.brief == "Brief summary line."
        assert model.detail == ["Longer detail paragraph."]
        assert model.params[0].name == "x"
        assert "input value" in model.params[0].description
        assert model.returns == "the doubled value"
        assert model.retvals[0].value == "0"
        assert model.throws[0].exception == "std::runtime_error"
        assert model.note == ["rounds toward zero"]
        assert model.since == ["2.0"]


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_comment_undocumented_symbol_is_none(tmp_path: Path) -> None:
    header = tmp_path / "doc.hpp"
    header.write_text("int undocumented(int x);\n")
    db = tmp_path / "out.sqlite"
    _core.parse_to_sqlite([str(header)], str(db), _core.ParseOptions())

    with Store.open(db) as store:
        usr = _usr_for(store, "undocumented")
        assert store.comment(usr) is None
        assert store.raw_comment(usr) is None


# A Python parser override referenced by dotted path; ignores the structure and
# tags the brief so the test can prove the override path ran.
def tagging_parser(raw: str) -> CommentModel:
    return CommentModel(brief="OVERRIDDEN", detail=[raw.strip()])


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_comment_python_override_replaces_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    header = tmp_path / "doc.hpp"
    header.write_text(FIXTURE)
    db = tmp_path / "out.sqlite"
    _core.parse_to_sqlite([str(header)], str(db), _core.ParseOptions())

    monkeypatch.setenv(OVERRIDE_ENV, "tests.test_comment_store.tagging_parser")
    with Store.open(db) as store:
        usr = _usr_for(store, "doc::doubler")
        model = store.comment(usr)
        assert model is not None
        assert model.brief == "OVERRIDDEN"
        # The override receives the verbatim raw comment, not the C++ parse.
        assert "@param x" in model.detail[0]


def _first_documented_usr(store: Store) -> str:
    for symbol in store.symbols():
        if store.raw_comment(symbol.usr) is not None:
            return symbol.usr
    msg = "the fixture database has no documented symbol"
    raise AssertionError(msg)


@contextmanager
def _counted_queries(store: Store) -> Iterator[list[str]]:
    """Record the SQL a store runs inside the block."""
    seen: list[str] = []
    store._con.set_trace_callback(seen.append)  # noqa: SLF001
    try:
        yield seen
    finally:
        store._con.set_trace_callback(None)  # noqa: SLF001


def test_comment_is_memoised_per_usr(fixture_db: Path) -> None:
    """A symbol's model is built once, however many pages render it."""
    with Store.open(fixture_db) as store:
        usr = _first_documented_usr(store)
        first = store.comment(usr)
        assert first is not None

        with _counted_queries(store) as queries:
            # The memo hands back the very object the first read built, so the
            # `comments` + `comment_fields` queries do not run again.
            assert store.comment(usr) is first
        assert queries == []


def test_comment_memo_holds_an_undocumented_symbol(fixture_db: Path) -> None:
    """``None`` is memoised too, so an undocumented symbol is not re-queried."""
    with Store.open(fixture_db) as store:
        undocumented = next(s.usr for s in store.symbols() if store.raw_comment(s.usr) is None)
        assert store.comment(undocumented) is None

        with _counted_queries(store) as queries:
            assert store.comment(undocumented) is None
        assert queries == []


def test_comment_memo_is_dropped_when_the_parser_changes(fixture_db: Path) -> None:
    """Switching parsers re-reads rather than serving the other parser's models."""
    with Store.open(fixture_db) as store:
        usr = _first_documented_usr(store)
        default = store.comment(usr)
        assert default is not None
        assert default.brief != "OVERRIDDEN"

        overridden = store.comment(usr, parser=tagging_parser)
        assert overridden is not None
        assert overridden.brief == "OVERRIDDEN"

        # ... and back again.
        assert store.comment(usr) == default

"""End-to-end test of the C++ parse -> SQLite -> Python read boundary."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from clangquill import _core
from clangquill.store import Store, StoreVersionError, SymbolKind

if TYPE_CHECKING:
    from pathlib import Path

FIXTURE = """
/// A documented namespace.
namespace demo {
/// A documented widget.
struct Widget {
  /// the width
  int width;
};
int undocumented_free_function(int x);
}
"""


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_parse_to_sqlite_round_trip(tmp_path: Path) -> None:
    header = tmp_path / "demo.hpp"
    header.write_text(FIXTURE)
    db = tmp_path / "out.sqlite"

    result = _core.parse_to_sqlite([str(header)], str(db), _core.ParseOptions())

    assert result.symbol_count > 0
    assert result.file_count == 1
    assert not result.diagnostics

    with Store.open(db) as store:
        assert store.meta("schema_version") == str(_core.SCHEMA_VERSION)

        by_name = {s.qualified_name: s for s in store.symbols()}
        assert "demo" in by_name
        assert by_name["demo"].kind == SymbolKind.NAMESPACE

        widget = by_name["demo::Widget"]
        assert widget.kind == SymbolKind.STRUCT
        assert widget.is_documented

        undoc = by_name["demo::undocumented_free_function"]
        assert not undoc.is_documented

        # Every symbol gets a content hash for later incremental caching.
        assert all(s.content_hash for s in store.symbols())


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
@pytest.mark.parametrize("jobs", [1, 2, 4])
def test_parse_to_sqlite_parallel_matches_serial(tmp_path: Path, jobs: int) -> None:
    # A handful of independent headers so the work fans out across threads.
    headers = []
    for i in range(6):
        header = tmp_path / f"h{i}.hpp"
        header.write_text(
            f"/// widget {i}\nnamespace n{i} {{ struct W{i} {{ int field{i}; }}; int fn{i}(int a); }}\n",
        )
        headers.append(str(header))

    def parse(db_name: str, n_jobs: int) -> tuple[int, int, int, list[tuple]]:
        opts = _core.ParseOptions()
        opts.jobs = n_jobs
        db = tmp_path / db_name
        result = _core.parse_to_sqlite(headers, str(db), opts)
        with Store.open(db) as store:
            rows = sorted((s.usr, s.qualified_name, s.kind) for s in store.symbols())
        return result.symbol_count, result.reference_count, result.file_count, rows

    serial = parse("serial.sqlite", 1)
    parallel = parse(f"parallel{jobs}.sqlite", jobs)

    # Parallelism must not change the extracted IR — same counts, same symbols.
    assert parallel == serial


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_parse_to_sqlite_reports_per_tu_files(tmp_path: Path) -> None:
    detail = tmp_path / "detail.hpp"
    detail.write_text("#pragma once\nusing Width = int;\n")
    a = tmp_path / "a.hpp"
    a.write_text('#include "detail.hpp"\n/// ns a\nnamespace a { /// f\nWidth f(); }\n')
    b = tmp_path / "b.hpp"
    b.write_text("/// ns b\nnamespace b { /// g\nint g(); }\n")
    db = tmp_path / "out.sqlite"

    result = _core.parse_to_sqlite([str(a), str(b)], str(db), _core.ParseOptions())

    deps = {tu.input: set(tu.files) for tu in result.translation_units}
    # Each input reports its own file set: a pulls in detail.hpp, b does not.
    assert deps[str(a)] == {str(a), str(detail)}
    assert deps[str(b)] == {str(b)}


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_parse_tu_to_sqlite_replaces_only_that_unit(tmp_path: Path) -> None:
    a = tmp_path / "a.hpp"
    a.write_text("/// ns a\nnamespace a { /// f\nint f(); }\n")
    b = tmp_path / "b.hpp"
    b.write_text("/// ns b\nnamespace b { /// g\nint g(); }\n")
    db = tmp_path / "out.sqlite"
    _core.parse_to_sqlite([str(a), str(b)], str(db), _core.ParseOptions())

    # Edit a.hpp: drop f, add h. Re-parse only that translation unit.
    a.write_text("/// ns a\nnamespace a { /// h\nint h(); }\n")
    result = _core.parse_tu_to_sqlite(str(a), str(db), _core.ParseOptions())
    assert [tu.input for tu in result.translation_units] == [str(a)]

    with Store.open(db) as store:
        names = {s.qualified_name for s in store.symbols()}
    # a's removed symbol is gone, its new symbol is present, and b is untouched.
    assert "a::f" not in names
    assert "a::h" in names
    assert "b::g" in names


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_parse_tu_failure_does_not_wipe_existing_rows(tmp_path: Path) -> None:
    a = tmp_path / "a.hpp"
    a.write_text("/// ns a\nnamespace a { /// f\nint f(); }\n")
    db = tmp_path / "out.sqlite"
    _core.parse_to_sqlite([str(a)], str(db), _core.ParseOptions())

    # A hard parse failure (no translation unit at all) must raise rather than
    # delete a.hpp's rows and replace them with an empty re-parse.
    with pytest.raises(RuntimeError, match="failed to parse"):
        _core.parse_tu_to_sqlite(str(tmp_path / "missing.hpp"), str(db), _core.ParseOptions())

    with Store.open(db) as store:
        assert {s.qualified_name for s in store.symbols()} >= {"a", "a::f"}


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_parse_tus_to_sqlite_replaces_multiple_units_atomically(tmp_path: Path) -> None:
    a = tmp_path / "a.hpp"
    a.write_text("/// ns a\nnamespace a { /// f\nint f(); }\n")
    b = tmp_path / "b.hpp"
    b.write_text("/// ns b\nnamespace b { /// g\nint g(); }\n")
    c = tmp_path / "c.hpp"
    c.write_text("/// ns c\nnamespace c { /// k\nint k(); }\n")
    db = tmp_path / "out.sqlite"
    _core.parse_to_sqlite([str(a), str(b)], str(db), _core.ParseOptions())

    # Re-parse both stale units in one call; an untouched third input joins in.
    a.write_text("/// ns a\nnamespace a { /// h\nint h(); }\n")
    result = _core.parse_tus_to_sqlite([str(a), str(c)], str(db), _core.ParseOptions())
    assert [tu.input for tu in result.translation_units] == [str(a), str(c)]

    with Store.open(db) as store:
        names = {s.qualified_name for s in store.symbols()}
    assert "a::f" not in names
    assert {"a::h", "b::g", "c::k"}.issubset(names)


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_parse_tus_does_not_wipe_an_included_sibling_input(tmp_path: Path) -> None:
    # base.hpp is itself an input *and* #included by user.hpp. Re-parsing only
    # user.hpp must leave base.hpp's symbols intact even though base.hpp appears
    # in user.hpp's file set.
    base = tmp_path / "base.hpp"
    base.write_text("#pragma once\n/// ns base\nnamespace base { /// id\nusing Id = int; }\n")
    user = tmp_path / "user.hpp"
    user.write_text('#include "base.hpp"\n/// ns user\nnamespace user { /// u\nbase::Id u(); }\n')
    db = tmp_path / "out.sqlite"
    _core.parse_to_sqlite([str(base), str(user)], str(db), _core.ParseOptions())

    user.write_text('#include "base.hpp"\n/// ns user\nnamespace user { /// v\nbase::Id v(); }\n')
    _core.parse_tus_to_sqlite([str(user)], str(db), _core.ParseOptions())

    with Store.open(db) as store:
        names = {s.qualified_name for s in store.symbols()}
    assert {"base", "base::Id", "user", "user::v"}.issubset(names)
    assert "user::u" not in names


BATCH_FIXTURE_ONE = """
/// \\defgroup util Utilities
/// Helpers shared by everything.

/// A documented macro.
#define ONE_MAX(a, b) ((a) > (b) ? (a) : (b))

/// \\ingroup util
/// A grouped function.
int clamp_one(int x);
"""

BATCH_FIXTURE_TWO = """
/// Another documented macro.
#define TWO_MIN(a, b) ((a) < (b) ? (a) : (b))

/// ns two
namespace two { /// widget
struct Widget { int w; }; }
"""


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_batched_parse_extracts_macros_and_groups_per_file(tmp_path: Path) -> None:
    # Macro doc comments and free-floating \defgroup blocks are recovered by
    # scanning each input's tokens; with several inputs sharing one umbrella TU
    # every member file must still be scanned (and line numbers must not collide
    # across files).
    one = tmp_path / "one.hpp"
    one.write_text(BATCH_FIXTURE_ONE)
    two = tmp_path / "two.hpp"
    two.write_text(BATCH_FIXTURE_TWO)
    db = tmp_path / "out.sqlite"

    opts = _core.ParseOptions()
    assert opts.tu_batch == 0  # default batching groups both inputs into one TU
    _core.parse_to_sqlite([str(one), str(two)], str(db), opts)

    with Store.open(db) as store:
        by_name = {s.qualified_name: s for s in store.symbols()}
        documented = {s.qualified_name for s in store.symbols() if s.is_documented}
        groups = {g.id: g for g in store.groups()}

    assert {"ONE_MAX", "TWO_MIN", "clamp_one", "two::Widget"}.issubset(by_name)
    assert {"ONE_MAX", "TWO_MIN"}.issubset(documented)
    assert "util" in groups
    assert groups["util"].title == "Utilities"


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_batched_parse_matches_per_file_parse(tmp_path: Path) -> None:
    # For self-contained headers the umbrella batching is an optimisation only:
    # the stored IR must be identical to fully isolated per-file parsing.
    headers = []
    for i in range(5):
        header = tmp_path / f"h{i}.hpp"
        header.write_text(
            f"/// widget {i}\nnamespace n{i} {{ /// w\nstruct W{i} {{ int f{i}; }}; /// fn\nint fn{i}(int a); }}\n",
        )
        headers.append(str(header))

    def rows(db_name: str, tu_batch: int) -> list[tuple]:
        opts = _core.ParseOptions()
        opts.tu_batch = tu_batch
        db = tmp_path / db_name
        _core.parse_to_sqlite(headers, str(db), opts)
        with Store.open(db) as store:
            return sorted((s.usr, s.qualified_name, s.kind, s.is_documented, s.content_hash) for s in store.symbols())

    assert rows("batched.sqlite", 0) == rows("isolated.sqlite", 1)


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_parse_is_order_independent(tmp_path: Path) -> None:
    # The IR is a function of the input *set*. `b.hpp` spells its own type with
    # a macro `a.hpp` defines and does not include it, so before inputs were
    # canonicalised this pair stored a differently named symbol depending only
    # on which path came first in the argument list.
    a = tmp_path / "a.hpp"
    a.write_text("#pragma once\n#define ORDER_NAME Tagged\nusing OrderIndex = int;\n")
    b = tmp_path / "b.hpp"
    b.write_text("#pragma once\n/// tagged\nstruct ORDER_NAME { OrderIndex value; };\n")

    def rows(db_name: str, inputs: list[Path]) -> list[tuple]:
        opts = _core.ParseOptions()
        db = tmp_path / db_name
        _core.parse_to_sqlite([str(p) for p in inputs], str(db), opts)
        with Store.open(db) as store:
            return sorted((s.usr, s.qualified_name, s.kind, s.is_documented) for s in store.symbols())

    assert rows("forward.sqlite", [a, b]) == rows("reversed.sqlite", [b, a])


def test_schema_version_exposed() -> None:
    assert isinstance(_core.SCHEMA_VERSION, int)
    assert _core.SCHEMA_VERSION >= 1


def test_store_open_rejects_incompatible_schema_version(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    con.execute("INSERT INTO meta(key, value) VALUES('schema_version', '0')")
    con.commit()
    con.close()

    with pytest.raises(StoreVersionError, match="schema version '0'"), Store.open(db):
        pass


def test_store_open_rejects_a_missing_path(tmp_path: Path) -> None:
    # mode=ro would fail at connect() with an unhelpful OperationalError; the
    # pre-check names the path instead.
    with pytest.raises(FileNotFoundError, match=r"nope\.sqlite"), Store.open(tmp_path / "nope.sqlite"):
        pass


def test_store_open_rejects_a_non_ir_database(tmp_path: Path) -> None:
    db = tmp_path / "other.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE unrelated (x INTEGER)")
    con.commit()
    con.close()

    with pytest.raises(StoreVersionError, match="not a clangquill IR database"), Store.open(db):
        pass

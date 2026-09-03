"""End-to-end test of the C++ parse -> SQLite -> Python read boundary."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import PurePath
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


def _tu_deps(result: _core.ParseResult) -> dict[str, set[str]]:
    """Rebuild ``{input: {dependency, ...}}`` from the interned per-TU file map."""
    paths = result.tu_dep_paths
    return {
        input_path: {paths[i] for i in ids} for input_path, ids in zip(result.tu_inputs, result.tu_dep_ids, strict=True)
    }


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

    deps = _tu_deps(result)
    # Each input reports its own file set: a pulls in detail.hpp, b does not.
    assert deps[str(a)] == {str(a), str(detail)}
    assert deps[str(b)] == {str(b)}


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_per_tu_files_are_interned_across_inputs(tmp_path: Path) -> None:
    """A header two inputs share crosses the binding once, not once per input."""
    shared = tmp_path / "shared.hpp"
    shared.write_text("#pragma once\nusing Width = int;\n")
    inputs = []
    for name in ("a", "b", "c"):
        header = tmp_path / f"{name}.hpp"
        header.write_text(f'#include "shared.hpp"\n/// ns {name}\nnamespace {name} {{ /// f\nWidth f(); }}\n')
        inputs.append(str(header))
    db = tmp_path / "out.sqlite"

    result = _core.parse_to_sqlite(inputs, str(db), _core.ParseOptions())

    paths = result.tu_dep_paths
    assert paths.count(str(shared)) == 1
    assert len(paths) == len(set(paths))
    # The interning is only a transport detail: every input still reports the
    # shared header among its dependencies.
    deps = _tu_deps(result)
    assert all(str(shared) in deps[inp] for inp in inputs)


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
    assert result.tu_inputs == [str(a)]

    with Store.open(db) as store:
        names = {s.qualified_name for s in store.symbols()}
    # a's removed symbol is gone, its new symbol is present, and b is untouched.
    assert "a::f" not in names
    assert "a::h" in names
    assert "b::g" in names


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_parse_to_sqlite_fully_replaces_an_existing_ir(tmp_path: Path) -> None:
    """A caller may point parse_to_sqlite at a database an earlier parse filled.

    The streamed write builds the fresh IR into a temporary database and
    replaces db_path with it atomically (see SqliteStore::write_streamed_full_parse)
    rather than clearing db_path in place, so this must still fully replace the
    old contents rather than merely adding to them.
    """
    first = tmp_path / "first.hpp"
    first.write_text("/// ns first\nnamespace first { /// f\nint f(); }\n")
    db = tmp_path / "out.sqlite"
    _core.parse_to_sqlite([str(first)], str(db), _core.ParseOptions())
    with Store.open(db) as store:
        assert {s.qualified_name for s in store.symbols()} >= {"first", "first::f"}

    second = tmp_path / "second.hpp"
    second.write_text("/// ns second\nnamespace second { /// g\nint g(); }\n")
    _core.parse_to_sqlite([str(second)], str(db), _core.ParseOptions())

    with Store.open(db) as store:
        names = {s.qualified_name for s in store.symbols()}
    assert {"second", "second::g"}.issubset(names)
    assert "first" not in names
    assert "first::f" not in names


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
    assert result.tu_inputs == [str(a), str(c)]

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


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_forward_declaration_does_not_displace_the_definition(tmp_path: Path) -> None:
    # `symbols.usr` is a primary key written with INSERT OR REPLACE, so if a bare
    # forward declaration produced a row it would settle the definition's
    # identity by whichever file the store happened to write last.
    decl = tmp_path / "a_decl.hpp"
    decl.write_text("#pragma once\n#include <memory>\nstruct Owner { std::unique_ptr<class Opaque> held; };\n")
    defn = tmp_path / "b_def.hpp"
    defn.write_text(
        "#pragma once\n/// The definition, and the only documentation.\nclass Opaque { public: int v = 0; };\n",
    )

    def opaque(db_name: str, tu_batch: int) -> list[tuple]:
        opts = _core.ParseOptions()
        opts.tu_batch = tu_batch
        db = tmp_path / db_name
        _core.parse_to_sqlite([str(decl), str(defn)], str(db), opts)
        with Store.open(db) as store:
            paths = {f.id: f.path for f in store.files()}
            return [
                (s.qualified_name, s.is_documented, PurePath(paths[s.file_id]).name)
                for s in store.symbols()
                if s.qualified_name == "Opaque"
            ]

    assert opaque("batched.sqlite", 0) == [("Opaque", True, "b_def.hpp")]
    assert opaque("isolated.sqlite", 1) == [("Opaque", True, "b_def.hpp")]


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_forward_declaration_of_an_external_type_is_dropped_cleanly(tmp_path: Path) -> None:
    # The shape every dependency-heavy project has: the input header names a tag
    # that an upstream header, not itself an input, defines and documents.
    # libclang hands the forward declaration that upstream comment, so dropping
    # the declaration's row must drop the comment with it -- comments.symbol_usr
    # is a foreign key onto symbols.usr, and the definition never gets a row
    # because it is outside the input set.
    upstream = tmp_path / "upstream.hpp"
    upstream.write_text("#pragma once\n/// Defined and documented elsewhere.\nclass Hidden { public: int v = 0; };\n")
    header = tmp_path / "input.hpp"
    header.write_text(
        '#pragma once\n#include <memory>\n#include "upstream.hpp"\n'
        "/// Owns one.\nstruct Owner { std::unique_ptr<class Hidden> held; };\n",
    )

    db = tmp_path / "out.sqlite"
    _core.parse_to_sqlite([str(header)], str(db), _core.ParseOptions())

    with Store.open(db) as store:
        names = {s.qualified_name for s in store.symbols()}
    assert "Owner" in names
    assert "Hidden" not in names


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_inputs_reached_through_a_forced_include_are_attributed(tmp_path: Path) -> None:
    # The Eigen shape: the inputs are not translation units, so a prologue is
    # force-included and pulls them in itself, by a different spelling than the
    # umbrella uses. libclang names a file by the path it was requested with, so
    # matching those names drops every symbol -- the file has to be identified,
    # not spelled.
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "part.hpp").write_text("#pragma once\n/// Documented.\nstruct Part { int v = 0; };\n")
    prologue = tmp_path / "prologue.hpp"
    prologue.write_text('#pragma once\n#include "sub/part.hpp"\n')

    def count(db_name: str, tu_batch: int) -> int:
        opts = _core.ParseOptions()
        opts.tu_batch = tu_batch
        opts.include_dirs = [str(tmp_path)]
        opts.extra_args = ["-include", "prologue.hpp"]
        db = tmp_path / db_name
        # Two inputs so tu_batch>1 really builds an umbrella rather than
        # delegating to the single-file path.
        inputs = [str(tmp_path / "sub" / "part.hpp"), str(prologue)]
        _core.parse_to_sqlite(inputs, str(db), opts)
        with Store.open(db) as store:
            return sum(1 for s in store.symbols() if s.qualified_name == "Part")

    assert count("isolated.sqlite", 1) == 1
    assert count("batched.sqlite", 2) == 1


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_structural_block_resolves_the_same_under_any_batching(tmp_path: Path) -> None:
    # A structural block is resolved against the file it was written in, not the
    # translation unit, precisely so batching cannot change the answer: umbrella
    # batches are 64 inputs wide, so a module-wide lookup would find a target in
    # a sibling file or not depending on which batch it landed in, and never
    # under tu_batch=1.
    one = tmp_path / "one.hpp"
    one.write_text(
        "#pragma once\n"
        "/** \\class Local\n  * \\brief Documented from a block above it.\n  */\n"
        "\nnamespace filler { int x = 0; }\n"
        "\nclass Local { public: int v = 0; };\n",
    )
    # Names an entity in the *other* file: must stay unresolved either way.
    two = tmp_path / "two.hpp"
    two.write_text(
        "#pragma once\n"
        "/** \\class Elsewhere\n  * \\brief Should not reach across files.\n  */\n"
        "\nnamespace other { int y = 0; }\n",
    )
    three = tmp_path / "three.hpp"
    three.write_text("#pragma once\nclass Elsewhere { public: int v = 0; };\n")

    def documented(db_name: str, tu_batch: int) -> set[tuple]:
        opts = _core.ParseOptions()
        opts.tu_batch = tu_batch
        db = tmp_path / db_name
        _core.parse_to_sqlite([str(one), str(two), str(three)], str(db), opts)
        with Store.open(db) as store:
            return {(s.qualified_name, s.is_documented) for s in store.symbols()}

    batched = documented("batched.sqlite", 0)
    isolated = documented("isolated.sqlite", 1)
    assert batched == isolated
    assert ("Local", True) in batched
    assert ("Elsewhere", False) in batched


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_a_shared_namespace_comment_is_recorded_once(tmp_path: Path) -> None:
    # libclang answers clang_Cursor_getRawCommentText for a redeclaration with
    # the comment written on another one, so every unit that reaches the header
    # documenting a namespace sees that comment on its own reopening of it. The
    # documentation must land in the database once all the same: dune-gdt's
    # `Dune` page rendered the same block 198 times, once per unit.
    shared = tmp_path / "shared.hpp"
    shared.write_text(
        "#pragma once\n/** \\brief The shared namespace. */\nnamespace shared { struct Thing {}; }\n",
    )
    users = []
    for i in range(6):
        user = tmp_path / f"user{i}.hpp"
        user.write_text(f'#pragma once\n#include "shared.hpp"\nnamespace shared {{ int f{i}(); }}\n')
        users.append(user)

    def briefs(db_name: str, tu_batch: int) -> list[str]:
        opts = _core.ParseOptions()
        opts.tu_batch = tu_batch
        db = tmp_path / db_name
        _core.parse_to_sqlite([str(shared), *(str(u) for u in users)], str(db), opts)
        with Store.open(db) as store:
            usr = next(s.usr for s in store.symbols() if s.qualified_name == "shared")
            with sqlite3.connect(db) as con:
                rows = con.execute(
                    "SELECT value FROM comment_fields WHERE symbol_usr = ? AND name = 'brief'",
                    (usr,),
                ).fetchall()
        return [row[0] for row in rows]

    # tu_batch=1 is the interesting one (a write per header, which is how
    # dune-gdt builds), but the umbrella path must not regress either.
    assert briefs("isolated.sqlite", 1) == ["The shared namespace."]
    assert briefs("batched.sqlite", 0) == ["The shared namespace."]


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_a_def_block_is_retargeted_onto_the_macro_it_names(tmp_path: Path) -> None:
    # `\def` names the macro it documents, so a block carrying it belongs to
    # that macro wherever it was written -- here below the `#define`, which is
    # out of reach of the doc-comment-above-the-line scan that macros otherwise
    # rely on. dune-gdt writes exactly this shape and its macro came out
    # undocumented.
    header = tmp_path / "tuple.hpp"
    header.write_text(
        "#pragma once\n"
        "#define TUPLE_TYPEDEFS_2_TUPLE(t_, s_) int\n"
        "\n"
        "/**\n"
        " * \\def TUPLE_TYPEDEFS_2_TUPLE( t_, s_ )\n"
        " *\n"
        " * \\brief extracts the types of a tuple's elements.\n"
        " */\n"
        "\n"
        "namespace outer::inner { struct Thing {}; }\n",
    )
    db = tmp_path / "out.sqlite"
    _core.parse_to_sqlite([str(header)], str(db), _core.ParseOptions())

    with Store.open(db) as store:
        macro = next(s for s in store.symbols() if s.qualified_name == "TUPLE_TYPEDEFS_2_TUPLE")
        assert macro.kind == SymbolKind.MACRO
        comment = store.comment(macro.usr)
        assert comment is not None
        assert comment.brief == "extracts the types of a tuple's elements."


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_parse_releases_the_gil(tmp_path: Path) -> None:
    """Another Python thread must keep running while a parse is in flight.

    The parse is bound with ``nb::call_guard<nb::gil_scoped_release>()``; without
    it the calling thread holds the GIL for the whole (multi-minute, on a real
    project) parse, which is what makes Ctrl-C look dead and starves every other
    thread in the process.
    """
    header = tmp_path / "demo.hpp"
    header.write_text(FIXTURE)
    db = tmp_path / "out.sqlite"

    ticks = 0
    stop = threading.Event()

    def tick() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            time.sleep(0.001)

    worker = threading.Thread(target=tick, daemon=True)
    worker.start()
    try:
        # Wait for the first tick so a zero delta below can only mean the worker
        # was blocked, never that it had not started yet.
        while ticks == 0:
            time.sleep(0.001)
        before = ticks
        _core.parse_to_sqlite([str(header)], str(db), _core.ParseOptions())
        during = ticks - before
    finally:
        stop.set()
        worker.join(timeout=5)

    assert during > 0


def test_schema_version_exposed() -> None:
    assert isinstance(_core.SCHEMA_VERSION, int)
    assert _core.SCHEMA_VERSION >= 1


@pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")
def test_ir_carries_no_dead_weight(tmp_path: Path) -> None:
    """The IR holds one representation of a comment, and no unwritten tables.

    ``comments.fields_json`` used to duplicate — as a serialized blob, on every
    documented symbol — the model ``comment_fields`` already spells out, and
    nothing read it. The ``outputs`` table was declared but never written or
    read (the build cache keeps its own, differently shaped one in a separate
    database). Both are gone; this guards against either coming back.
    """
    header = tmp_path / "demo.hpp"
    header.write_text(FIXTURE)
    db = tmp_path / "out.sqlite"
    _core.parse_to_sqlite([str(header)], str(db), _core.ParseOptions())

    con = sqlite3.connect(db)
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "comment_fields" in tables
        assert "outputs" not in tables

        columns = {row[1] for row in con.execute("PRAGMA table_info(comments)")}
        assert columns == {"symbol_usr", "raw_text", "format"}
    finally:
        con.close()


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

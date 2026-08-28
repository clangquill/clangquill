"""Targeted regression tests for ``benchmarks/verify.py`` comment checks."""

from __future__ import annotations

import importlib
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace


def _load_verify(monkeypatch) -> object:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "benchmarks"))
    return importlib.import_module("verify")


def _write_ir_copy(fixture_db: Path, tmp_path: Path) -> Path:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    ir_path = cache_dir / "clangquill.sqlite"
    shutil.copy2(fixture_db, ir_path)
    return ir_path


def _insert_symbol_with_ingroup(ir_path: Path, *, usr: str, qname: str, ingroup_value: str) -> None:
    con = sqlite3.connect(ir_path)
    con.execute(
        "INSERT INTO symbols(usr, parent_usr, kind, spelling, qualified_name, display_name, "
        "signature, type_repr, access, is_definition, is_documented, content_hash, file_id, line) "
        "VALUES(?, 'c:@N@geo', 5, ?, ?, ?, 'void ()', 'void ()', 0, 1, 1, ?, 1, 0)",
        (usr, qname.rsplit("::", 1)[-1], qname, qname, "hash-" + usr),
    )
    con.execute(
        "INSERT INTO comments(symbol_usr, raw_text, format) VALUES(?, '/// fixture', 'doxygen')",
        (usr,),
    )
    con.execute(
        "INSERT INTO comment_fields(symbol_usr, name, arg, value, ordinal) VALUES(?, 'ingroup', '', ?, 0)",
        (usr, ingroup_value),
    )
    con.commit()
    con.close()


def test_check_comments_allows_multi_id_ingroup_values(fixture_db: Path, tmp_path: Path, monkeypatch) -> None:
    verify = _load_verify(monkeypatch)
    ir_path = _write_ir_copy(fixture_db, tmp_path)
    _insert_symbol_with_ingroup(
        ir_path,
        usr="c:@N@geo@F@ingroup_ok",
        qname="geo::ingroup_ok",
        ingroup_value="Geometry_Module Another_Group",
    )
    result = verify.check_comments(SimpleNamespace(cache_dir=ir_path.parent))
    assert result.passed


def test_check_comments_still_flags_swallowed_prose_in_ingroup(
    fixture_db: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    verify = _load_verify(monkeypatch)
    ir_path = _write_ir_copy(fixture_db, tmp_path)
    _insert_symbol_with_ingroup(
        ir_path,
        usr="c:@N@geo@F@ingroup_bad",
        qname="geo::ingroup_bad",
        # "?" is not allowed by GROUP_ID_RE, so this simulates swallowed prose.
        ingroup_value="Geometry_Module prose?",
    )
    result = verify.check_comments(SimpleNamespace(cache_dir=ir_path.parent))
    assert not result.passed
    assert any("geo::ingroup_bad" in line for line in result.detail)


# --------------------------------------------------------------------------- #
# The extraction gate itself
#
# Every other check in the suite reads `verify.py` as ground truth. These read
# it as code under test: each pair below shows the gate passing on the shape it
# is meant to accept and failing on the shape it exists to catch, so a change
# that quietly retires a gate fails here instead of painting a run green.
# --------------------------------------------------------------------------- #
def _doxygen_xml(xml_dir: Path, *, file: str, name: str, documented: bool = True) -> None:
    """Write one Doxygen compound XML documenting ``name`` in ``file``."""
    xml_dir.mkdir(parents=True, exist_ok=True)
    description = "<para>Documented.</para>" if documented else ""
    (xml_dir / f"{name.replace('::', '_')}.xml").write_text(
        '<?xml version="1.0"?>'
        '<doxygen version="1.9"><compounddef id="x" kind="struct">'
        f"<compoundname>{name}</compoundname>"
        f"<briefdescription>{description}</briefdescription>"
        "<detaileddescription></detaileddescription>"
        f'<location file="{file}" line="1"/>'
        "</compounddef></doxygen>",
        encoding="utf-8",
    )


def _ctx(source_dir: Path, cache_dir: Path, *, min_documented_ratio: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        source_dir=source_dir,
        cache_dir=cache_dir,
        config=SimpleNamespace(min_documented_ratio=min_documented_ratio),
    )


def test_extraction_flags_a_file_only_doxygen_documented(
    fixture_db: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    # The canary: Doxygen documented an entity in a file the IR knows nothing
    # about. This is the exact shape of "a regression stopped attaching doc
    # comments", and the gate has to fail on it.
    verify = _load_verify(monkeypatch)
    ir_path = _write_ir_copy(fixture_db, tmp_path)
    source = tmp_path / "src"
    (source / "other").mkdir(parents=True)
    (source / "other" / "lonely.hpp").touch()
    xml_dir = tmp_path / "xml"
    _doxygen_xml(xml_dir, file="other/lonely.hpp", name="other::Lonely")

    check, stats = verify.check_extraction(_ctx(source, ir_path.parent), xml_dir)
    assert not check.passed
    assert "other/lonely.hpp" in stats["missed_files"]
    assert any("other/lonely.hpp" in line for line in check.detail)


def test_extraction_accepts_a_file_both_tools_documented(
    fixture_db: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    # The other half of the pair. The fixture IR documents `geo::Shape` in
    # `geo.hpp`, so Doxygen documenting the same file must clear the gate --
    # otherwise the failure above would prove nothing about the gate's aim.
    verify = _load_verify(monkeypatch)
    ir_path = _write_ir_copy(fixture_db, tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "geo.hpp").touch()
    xml_dir = tmp_path / "xml"
    _doxygen_xml(xml_dir, file="geo.hpp", name="geo::Shape")

    check, stats = verify.check_extraction(_ctx(source, ir_path.parent), xml_dir)
    assert check.passed, check.summary
    assert stats["missed_files"] == []
    # And the IR side really was read: a vacuous green would count zero.
    assert stats["clangquill_documented"] > 0


def test_extraction_reads_an_ir_that_records_relative_paths(
    fixture_db: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    # The IR spells a file the way the compile command reached it, so a forced
    # -include prologue leaves `./geo.hpp` behind. Resolving that against the
    # process's working directory rather than the project root drops every
    # symbol in the file, deflating the ratio (or emptying the comparison) with
    # nothing to show for it. Run from a directory that is *not* the project so
    # a CWD-relative resolution cannot accidentally succeed.
    verify = _load_verify(monkeypatch)
    ir_path = _write_ir_copy(fixture_db, tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "geo.hpp").touch()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    per_file, total, _ = verify.clangquill_extraction(ir_path, source.resolve())
    assert per_file.get("geo.hpp"), per_file
    assert total == per_file["geo.hpp"]


def test_extraction_does_not_drop_a_configured_floor_when_doxygen_documents_nothing(
    fixture_db: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    # `min_documented_ratio` says "this project has a Doxygen reference and
    # clangquill reaches a share of it". If the reference collapses -- a
    # Doxyfile regression, a moved input dir -- passing here would retire the
    # gate without anyone noticing, which is the one thing a floor exists to
    # prevent.
    verify = _load_verify(monkeypatch)
    ir_path = _write_ir_copy(fixture_db, tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "geo.hpp").touch()
    xml_dir = tmp_path / "xml"
    _doxygen_xml(xml_dir, file="geo.hpp", name="geo::Shape", documented=False)

    ungated, _ = verify.check_extraction(_ctx(source, ir_path.parent), xml_dir)
    assert ungated.passed  # no floor configured: nothing to compare, nothing to fail

    gated, _ = verify.check_extraction(_ctx(source, ir_path.parent, min_documented_ratio=0.5), xml_dir)
    assert not gated.passed
    assert "no reference" in gated.summary


def test_xref_health_counts_targets_that_name_no_parsed_symbol(
    fixture_db: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    # The link-health signal: an unresolved `{cpp:any}` renders as plain text
    # rather than warning, so nothing else in the pipeline would notice.
    verify = _load_verify(monkeypatch)
    ir_path = _write_ir_copy(fixture_db, tmp_path)
    myst = tmp_path / "api"
    myst.mkdir()
    (myst / "geo.md").write_text(
        "See {cpp:any}`geo::Shape` and {cpp:any}`geo::Circle::area`, "
        "but also {cpp:any}`geo::NoSuchThing` and {cpp:any}`the title <geo::AlsoMissing>`.\n",
        encoding="utf-8",
    )

    health = verify.xref_health(myst, ir_path)
    assert health["targets"] == 4
    assert health["unresolved"] == 2
    assert sorted(health["examples"]) == ["geo::AlsoMissing", "geo::NoSuchThing"]

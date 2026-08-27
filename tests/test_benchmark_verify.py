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


def _insert_doc_with_group_command(ir_path: Path, *, usr: str, qname: str, ingroup_value: str) -> None:
    con = sqlite3.connect(ir_path)
    con.execute(
        "INSERT INTO symbols(usr, parent_usr, kind, spelling, qualified_name, display_name, "
        "signature, type_repr, access, is_definition, is_documented, content_hash, file_id, line) "
        "VALUES(?, 'c:@N@geo', 5, ?, ?, ?, 'void ()', 'void ()', 0, 1, 1, ?, 1, 0)",
        (usr, qname.rsplit("::", 1)[-1], qname, qname, "hash-" + usr),
    )
    con.execute(
        "INSERT INTO comments(symbol_usr, raw_text, format, fields_json) VALUES(?, '/// fixture', 'doxygen', '')",
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
    _insert_doc_with_group_command(
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
    _insert_doc_with_group_command(
        ir_path,
        usr="c:@N@geo@F@ingroup_bad",
        qname="geo::ingroup_bad",
        ingroup_value="Geometry_Module prose?",
    )
    result = verify.check_comments(SimpleNamespace(cache_dir=ir_path.parent))
    assert not result.passed
    assert any("geo::ingroup_bad" in line for line in result.detail)

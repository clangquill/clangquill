"""Tests for the shared configuration dataclass and its validation."""

from __future__ import annotations

from dataclasses import fields

import pytest

from clangquill.config import _TYPE_CHECKS, CONFIG_FIELDS, CONFIG_PREFIX, Config, ConfigError


def test_type_checks_cover_every_field():
    # Every Config field must have an up-front type check, so a new field
    # cannot silently skip validation (the two lists are maintained by hand).
    checked = {name for name, _, _ in _TYPE_CHECKS}
    assert checked == {f.name for f in fields(Config)}


def test_defaults_match_issue_contract():
    cfg = Config(input=["a.hpp"])
    assert cfg.output_dir == "api"
    assert cfg.include_undocumented is True
    assert cfg.group_by == "symbol"
    assert cfg.path_base is None
    assert cfg.diagnostics_log is None
    assert cfg.std == "c++20"
    assert cfg.toctree_maxdepth == 2
    assert cfg.root_document == "index"
    assert cfg.jobs == 0
    assert cfg.tu_batch == 0


def test_config_fields_cover_every_documented_value():
    names = {name for name, _ in CONFIG_FIELDS}
    expected = {
        "clangquill_input",
        "clangquill_compile_commands",
        "clangquill_compile_args",
        "clangquill_include_dirs",
        "clangquill_std",
        "clangquill_defines",
        "clangquill_clang_resource_dir",
        "clangquill_jobs",
        "clangquill_tu_batch",
        "clangquill_output_dir",
        "clangquill_template_dirs",
        "clangquill_templates",
        "clangquill_cache_dir",
        "clangquill_include_undocumented",
        "clangquill_comment_parser",
        "clangquill_group_by",
        "clangquill_path_base",
        "clangquill_diagnostics_log",
        "clangquill_toctree_maxdepth",
        "clangquill_root_document",
    }
    assert expected <= names
    assert all(name.startswith(CONFIG_PREFIX) for name in names)


def test_from_mapping_strips_prefix_and_normalises_input():
    cfg = Config.from_mapping(
        {
            "clangquill_input": "single.hpp",
            "clangquill_std": "c++23",
            "unrelated_key": "ignored",
        },
    )
    assert cfg.input == ["single.hpp"]
    assert cfg.std == "c++23"


def test_validate_rejects_empty_input():
    with pytest.raises(ConfigError, match="at least one"):
        Config(input=[]).validate()


def test_validate_rejects_unknown_group_by():
    with pytest.raises(ConfigError, match="group_by"):
        Config(input=["a.hpp"], group_by="nonsense").validate()


@pytest.mark.parametrize("mode", ["symbol", "file", "class", "namespace"])
def test_validate_accepts_known_group_by(mode: str):
    assert Config(input=["a.hpp"], group_by=mode).validate().group_by == mode


def test_validate_rejects_bad_maxdepth():
    with pytest.raises(ConfigError, match="toctree_maxdepth"):
        Config(input=["a.hpp"], toctree_maxdepth=0).validate()


def test_validate_rejects_negative_jobs():
    with pytest.raises(ConfigError, match="jobs"):
        Config(input=["a.hpp"], jobs=-1).validate()


@pytest.mark.parametrize("jobs", [0, 1, 8])
def test_validate_accepts_non_negative_jobs(jobs: int):
    assert Config(input=["a.hpp"], jobs=jobs).validate().jobs == jobs


def test_validate_rejects_negative_tu_batch():
    with pytest.raises(ConfigError, match="tu_batch"):
        Config(input=["a.hpp"], tu_batch=-1).validate()


@pytest.mark.parametrize("tu_batch", [0, 1, 32])
def test_validate_accepts_non_negative_tu_batch(tu_batch: int):
    assert Config(input=["a.hpp"], tu_batch=tu_batch).validate().tu_batch == tu_batch


def test_validate_returns_self():
    cfg = Config(input=["a.hpp"])
    assert cfg.validate() is cfg


# -- wrong-typed values reject with ConfigError, not a bare TypeError ----------


@pytest.mark.parametrize("field", ["jobs", "tu_batch", "toctree_maxdepth"])
def test_validate_rejects_non_int_fields(field: str):
    # A string flows in untyped via from_mapping (e.g. clangquill_tu_batch = "4").
    cfg = Config(input=["a.hpp"], **{field: "4"})
    with pytest.raises(ConfigError, match=f"{field} must be an integer"):
        cfg.validate()


@pytest.mark.parametrize("field", ["jobs", "tu_batch", "toctree_maxdepth"])
def test_validate_rejects_bool_for_int_fields(field: str):
    cfg = Config(input=["a.hpp"], **{field: True})
    with pytest.raises(ConfigError, match=f"{field} must be an integer"):
        cfg.validate()


@pytest.mark.parametrize("field", ["std", "output_dir", "root_document", "group_by"])
def test_validate_rejects_non_str_fields(field: str):
    cfg = Config(input=["a.hpp"], **{field: 123})
    with pytest.raises(ConfigError, match=f"{field} must be a string"):
        cfg.validate()


@pytest.mark.parametrize(
    "field",
    ["compile_commands", "clang_resource_dir", "cache_dir", "comment_parser", "path_base"],
)
def test_validate_rejects_non_str_optional_fields(field: str):
    cfg = Config(input=["a.hpp"], **{field: 123})
    with pytest.raises(ConfigError, match=f"{field} must be a string or None"):
        cfg.validate()


@pytest.mark.parametrize(
    "field",
    ["compile_commands", "clang_resource_dir", "cache_dir", "comment_parser", "path_base"],
)
def test_validate_accepts_none_for_optional_str_fields(field: str):
    assert Config(input=["a.hpp"], **{field: None}).validate() is not None


@pytest.mark.parametrize("field", ["compile_args", "include_dirs", "defines", "template_dirs"])
def test_validate_rejects_non_list_fields(field: str):
    cfg = Config(input=["a.hpp"], **{field: "not-a-list"})
    with pytest.raises(ConfigError, match=f"{field} must be a list of strings"):
        cfg.validate()


@pytest.mark.parametrize("field", ["compile_args", "include_dirs", "defines", "template_dirs"])
def test_validate_rejects_list_with_non_str_items(field: str):
    cfg = Config(input=["a.hpp"], **{field: ["ok", 5]})
    with pytest.raises(ConfigError, match=f"{field} must be a list of strings"):
        cfg.validate()


def test_validate_rejects_non_bool_include_undocumented():
    cfg = Config(input=["a.hpp"], include_undocumented="yes")
    with pytest.raises(ConfigError, match="include_undocumented must be a boolean"):
        cfg.validate()


def test_validate_rejects_non_dict_templates():
    cfg = Config(input=["a.hpp"], templates=["class"])
    with pytest.raises(ConfigError, match="templates must be a mapping"):
        cfg.validate()


def test_validate_rejects_templates_with_non_str_values():
    cfg = Config(input=["a.hpp"], templates={"class": 1})
    with pytest.raises(ConfigError, match="templates must be a mapping"):
        cfg.validate()


# Falsy wrong types (0, {}) must report the type error, not the emptiness error,
# proving type validation runs before the "at least one file" check.
@pytest.mark.parametrize("bad_input", ["a.hpp", 0, {}])
def test_validate_rejects_non_list_input(bad_input: object):
    cfg = Config(input=bad_input)
    with pytest.raises(ConfigError, match="input must be a list of strings"):
        cfg.validate()


def test_diagnostics_log_accepts_none_and_a_path():
    assert Config(input=["a.hpp"], diagnostics_log=None).validate().diagnostics_log is None
    cfg = Config(input=["a.hpp"], diagnostics_log="_build/parse.log").validate()
    assert cfg.diagnostics_log == "_build/parse.log"


@pytest.mark.parametrize("value", [1, ["parse.log"], ""])
def test_diagnostics_log_rejects_bad_values(value):
    with pytest.raises(ConfigError, match="clangquill_diagnostics_log"):
        Config(input=["a.hpp"], diagnostics_log=value).validate()

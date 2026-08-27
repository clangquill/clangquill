"""Unit tests for the format-agnostic comment model, parser, and registry."""

from __future__ import annotations

import pytest

from clangquill import comments
from clangquill.comments import (
    OVERRIDE_ENV,
    CommentModel,
    CommentParam,
    available_parsers,
    doxygen_parse,
    get_parser,
    model_from_fields,
    register_parser,
    resolve_override,
)

DOXYGEN = """
/**
 * Computes the quotient of two integers.
 *
 * Second paragraph is detail.
 *
 * @param numerator the value to divide
 * @param denominator the divisor
 * @tparam T element type
 * @return the integer quotient
 * @retval 0 when the numerator is zero
 * @throws std::domain_error if denominator is zero
 * @note truncates toward zero
 * @warning undefined for INT_MIN
 * @since 1.2
 * @deprecated use divide2
 * @see multiply
 * @author Ada
 */
"""


def test_doxygen_parse_covers_commands() -> None:
    model = doxygen_parse(DOXYGEN)

    assert model.brief == "Computes the quotient of two integers."
    assert model.detail == ["Second paragraph is detail."]
    assert [(p.name, p.description) for p in model.params] == [
        ("numerator", "the value to divide"),
        ("denominator", "the divisor"),
    ]
    assert model.tparams == [model.tparams[0]]
    assert model.tparams[0].name == "T"
    assert model.returns == "the integer quotient"
    assert model.retvals[0].value == "0"
    assert "numerator is zero" in model.retvals[0].description
    assert model.throws[0].exception == "std::domain_error"
    assert model.note == ["truncates toward zero"]
    assert model.warning == ["undefined for INT_MIN"]
    assert model.since == ["1.2"]
    assert model.deprecated == ["use divide2"]
    assert model.see == ["multiply"]
    # Unknown command falls into the custom bucket keyed by its name.
    assert model.custom == {"author": ["Ada"]}


def test_doxygen_parse_triple_slash_brief() -> None:
    model = doxygen_parse("/// @brief Multiplies two values.\n/// @param a first factor\n")
    assert model.brief == "Multiplies two values."
    assert model.params[0].name == "a"


def test_doxygen_parse_autobrief_without_command() -> None:
    model = doxygen_parse("/// A short summary line.")
    assert model.brief == "A short summary line."
    assert model.detail == []


@pytest.mark.parametrize(
    "raw",
    [
        "///< trailing member doc",
        "//!< trailing member doc",
        "/**< trailing member doc */",
        "/*!< trailing member doc */",
    ],
)
def test_doxygen_parse_strips_post_item_markers(raw: str) -> None:
    # The Doxygen "<" post-item markers must not leak the '<' into the text.
    model = doxygen_parse(raw)
    assert model.brief == "trailing member doc"


PARAGRAPHS = """
/**
 * @brief A blank line ends the brief.
 *
 * This paragraph is the detailed description.
 *
 * @param a the input value
 *
 * A closing paragraph documents the function.
 */
"""


def test_doxygen_parse_blank_line_ends_a_paragraph_command() -> None:
    # A paragraph command runs to the next blank line; the paragraphs below one
    # document the entity rather than extending @brief or the last @param.
    model = doxygen_parse(PARAGRAPHS)
    assert model.brief == "A blank line ends the brief."
    assert [(p.name, p.description) for p in model.params] == [("a", "the input value")]
    assert model.detail == [
        "This paragraph is the detailed description.",
        "A closing paragraph documents the function.",
    ]


DIRECTED = """
/**
 * @brief Fills a buffer.
 * @param[out] result where the answer is written
 * @param[in] value the input value
 * @param[in,out] scratch reused working storage
 * @param plain no direction attribute
 * @tparam[in] T symmetric with param
 * @unknown[bracket] keeps its full spelling
 */
"""


def test_doxygen_parse_reads_param_directions() -> None:
    model = doxygen_parse(DIRECTED)
    assert [(p.name, p.direction) for p in model.params] == [
        ("result", "out"),
        ("value", "in"),
        ("scratch", "in,out"),
        ("plain", ""),
    ]
    assert model.params[0].description == "where the answer is written"
    assert [(p.name, p.direction) for p in model.tparams] == [("T", "in")]
    # A bracket suffix that is not a direction stays part of the command name.
    assert model.custom == {"unknown[bracket]": ["keeps its full spelling"]}


def test_model_from_fields_splits_the_direction_off_the_arg() -> None:
    # The C++ projection has one slot for a field argument, so a directed
    # parameter arrives as "[out] result" and is split back apart here.
    model = model_from_fields(
        [
            ("param", "[out] result", "the answer"),
            ("param", "[in,out] scratch", "working storage"),
            ("param", "plain", "no direction"),
            ("tparam", "[in] T", "a type"),
        ],
    )
    assert [(p.name, p.direction, p.description) for p in model.params] == [
        ("result", "out", "the answer"),
        ("scratch", "in,out", "working storage"),
        ("plain", "", "no direction"),
    ]
    assert model.tparams == [CommentParam("T", "a type", "in")]


VERBATIM = """
/**
 * Squares a value.
 * @code{.py}
 *   y = square(3)
 *   if y:
 *       print(y)
 * @endcode
 * Prose written after the block stays after it.
 * @verbatim
 *   +---+
 *   | x |
 *   +---+
 * @endverbatim
 */
"""


def test_doxygen_parse_keeps_verbatim_blocks_intact() -> None:
    # Since the output is Markdown, a code example's newlines and relative
    # indentation are load-bearing; collapsing them mangles every example.
    model = doxygen_parse(VERBATIM)
    assert model.brief == "Squares a value."
    assert model.detail == [
        "```py\ny = square(3)\nif y:\n    print(y)\n```",
        "Prose written after the block stays after it.",
        "```\n+---+\n| x |\n+---+\n```",
    ]


def test_doxygen_parse_fences_around_nested_backticks() -> None:
    model = doxygen_parse("/// @code\n/// ``` not the end\n/// @endcode\n")
    assert model.detail == ["````cpp\n``` not the end\n````"]


def test_doxygen_parse_leading_block_is_not_the_brief() -> None:
    # A code example is never a one-line summary, so the brief is the first
    # prose paragraph and the block keeps its place in the detail.
    model = doxygen_parse("/// @code\n/// x = 1\n/// @endcode\n/// The summary.\n")
    assert model.brief == "The summary."
    assert model.detail == ["```cpp\nx = 1\n```"]


def test_model_from_fields_round_trips() -> None:
    rows = [
        ("brief", "", "A brief."),
        ("detail", "", "Detail block."),
        ("param", "x", "the x"),
        ("returns", "", "a value"),
        ("retval", "0", "on success"),
        ("throws", "Error", "on failure"),
        ("note", "", "a note"),
        ("author", "", "Ada"),
    ]
    model = model_from_fields(rows)
    assert model.brief == "A brief."
    assert model.detail == ["Detail block."]
    assert model.params[0].name == "x"
    assert model.returns == "a value"
    assert model.retvals[0].value == "0"
    assert model.throws[0].exception == "Error"
    assert model.note == ["a note"]
    assert model.custom == {"author": ["Ada"]}


def test_registry_default_and_registration() -> None:
    assert "doxygen" in available_parsers()
    assert get_parser("doxygen") is doxygen_parse

    sentinel = CommentModel(brief="custom")
    register_parser("mine", lambda _raw: sentinel)
    try:
        assert get_parser("mine")("anything") is sentinel
        assert "mine" in available_parsers()
    finally:
        del comments._REGISTRY["mine"]  # noqa: SLF001


# A module-level callable referenced by dotted path in the override test.
def shouting_parser(raw: str) -> CommentModel:
    return CommentModel(brief=raw.strip().upper())


def test_resolve_override_none() -> None:
    assert resolve_override(None) is None


def test_resolve_override_callable() -> None:
    assert resolve_override(shouting_parser) is shouting_parser


def test_resolve_override_registered_name() -> None:
    # A registered format name resolves via the registry, not a dotted import.
    assert resolve_override("doxygen") is doxygen_parse


def test_resolve_override_dotted_path() -> None:
    parser = resolve_override("tests.test_comments.shouting_parser")
    assert parser is shouting_parser
    assert parser("hi").brief == "HI"


def test_resolve_override_colon_path() -> None:
    parser = resolve_override("tests.test_comments:shouting_parser")
    assert parser is shouting_parser


def test_resolve_override_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERRIDE_ENV, "tests.test_comments.shouting_parser")
    assert resolve_override() is shouting_parser


def test_resolve_override_rejects_non_callable() -> None:
    with pytest.raises(TypeError):
        resolve_override("tests.test_comments.OVERRIDE_NOT_CALLABLE")


OVERRIDE_NOT_CALLABLE = 42

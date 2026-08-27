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
 * @copydoc other::divide(int, int)
 * @myalias an aliased command nothing defines
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
    # A copy command nothing performs degrades to a cross-reference to the
    # entity it names, reduced to the name the C++ domain can resolve.
    assert model.see == ["multiply", "other::divide"]
    assert model.author == ["Ada"]
    # Unknown command falls into the custom bucket keyed by its name -- which is
    # where a Doxygen ALIASES command lands, since nothing here defines one.
    assert model.custom == {"myalias": ["an aliased command nothing defines"]}


def test_doxygen_parse_triple_slash_brief() -> None:
    model = doxygen_parse("/// @brief Multiplies two values.\n/// @param a first factor\n")
    assert model.brief == "Multiplies two values."
    assert model.params[0].name == "a"


def test_doxygen_parse_autobrief_without_command() -> None:
    model = doxygen_parse("/// A short summary line.")
    assert model.brief == "A short summary line."
    assert model.detail == []


def test_doxygen_parse_routes_prose_and_section_commands() -> None:
    """@details and friends are documentation, not unrecognized commands."""
    model = doxygen_parse(
        "/**\n"
        " * @brief A summary.\n"
        " * @details The long story.\n"
        " * @par Rationale\n"
        " * Because it is.\n"
        " * @remark worth knowing\n"
        " * @invariant the buffer stays sorted\n"
        " * @todo handle the empty case\n"
        " * @bug loops on a null node\n"
        " * @author Ada\n"
        " * @version 2.1\n"
        " * @date 2026-08-01\n"
        " */\n",
    )

    assert model.brief == "A summary."
    assert model.detail == ["The long story.", "Rationale Because it is."]
    assert model.note == ["worth knowing"]
    assert model.invariant == ["the buffer stays sorted"]
    assert model.todo == ["handle the empty case"]
    assert model.bug == ["loops on a null node"]
    assert model.author == ["Ada"]
    assert model.version == ["2.1"]
    assert model.date == ["2026-08-01"]
    assert model.custom == {}


def test_copy_commands_degrade_to_a_cross_reference() -> None:
    """A @copydoc-only comment must render *something*: where it points."""
    model = doxygen_parse(
        "/** @copydoc DenseCoeffsBase<Derived,ReadOnlyAccessors>::coeff(Index,Index) const */",
    )
    assert model.see == ["DenseCoeffsBase::coeff"]
    assert model.custom == {}


def test_doxygen_parse_joins_a_second_brief() -> None:
    """Doxygen joins repeated @brief text; dropping it lost half the summary."""
    model = doxygen_parse("/// @brief First half.\n/// @brief Second half.\n")
    assert model.brief == "First half. Second half."


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


INLINE = """
/// @brief A wrapped sentence about
/// @ref Widget stays one sentence.
///
/// Emphasis: @b bold, @e italic, @c code and @p x. See
/// @ref divide "the divide function" too.
/// An address like user@b.example is left alone, and @c foo. keeps its stop.
"""

BRACKETED = """
/// @brief Punctuation stays outside the markup.
///
/// The input set (see @ref parse_files) is fixed, as are @p paths) and @p usr:.
/// A target that is not a C++ name, like @ref some-page, is not a role at all.
"""


def test_doxygen_parse_renders_inline_markup() -> None:
    model = doxygen_parse(INLINE)
    # A wrapped line beginning with @ref is prose, not a block command: the
    # sentence stays whole and nothing lands in custom["ref"].
    assert model.brief == "A wrapped sentence about {cpp:any}`Widget` stays one sentence."
    assert "ref" not in model.custom
    expected = (
        "Emphasis: **bold**, *italic*, `code` and `x`. "
        "See {cpp:any}`the divide function <divide>` too. "
        "An address like user@b.example is left alone, and `foo`. keeps its stop."
    )
    assert model.detail == [expected]


def test_doxygen_parse_keeps_closing_punctuation_out_of_markup() -> None:
    # `(see @ref parse_files)` used to carry the `)` into the role, producing an
    # "Unparseable C++ cross-reference" that fails a warnings-as-errors docs
    # build. A target that is not a C++ name degrades to a code span for the
    # same reason.
    expected = (
        "The input set (see {cpp:any}`parse_files`) is fixed, "
        "as are `paths`) and `usr`:. "
        "A target that is not a C++ name, like `some-page`, is not a role at all."
    )
    assert doxygen_parse(BRACKETED).detail == [expected]


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
        ("todo", "", "handle the empty case"),
        ("myalias", "", "an aliased command"),
    ]
    model = model_from_fields(rows)
    assert model.brief == "A brief."
    assert model.detail == ["Detail block."]
    assert model.params[0].name == "x"
    assert model.returns == "a value"
    assert model.retvals[0].value == "0"
    assert model.throws[0].exception == "Error"
    assert model.note == ["a note"]
    assert model.author == ["Ada"]
    assert model.todo == ["handle the empty case"]
    # Only a command the model does not name lands in the custom bucket.
    assert model.custom == {"myalias": ["an aliased command"]}


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

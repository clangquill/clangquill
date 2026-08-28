"""Format-agnostic comment model, parser registry, and the default parser.

The C++ core parses each symbol's documentation comment into a structured model
and persists it (``comments`` / ``comment_fields``). This module mirrors that
model as Python dataclasses and provides the read-side machinery:

* :class:`CommentModel` and friends mirror ``clangquill::model::CommentModel``.
* a parser *registry* maps a format name to a ``str -> CommentModel`` callable.
* :func:`doxygen_parse` is the built-in default, a pure-Python Doxygen scanner
  that closely tracks ``DoxygenCommentParser::parse_raw_text`` (the C++ raw-text
  path in ``parser/doxygen_comment_parser.cpp``) -- the two are independent
  implementations, not a shared engine, so treat "mirrors" as an intent the
  ``tests/comment_corpus/`` conformance corpus checks rather than a guarantee.
* :func:`resolve_override` honours the ``CLANGQUILL_COMMENT_PARSER`` dotted-path
  hook so a project can swap in its own parser without recompiling the core.

Keep the field names in sync with the C++ model and the ``comment_fields``
projection written by :mod:`clangquill` (see ``parser/comment_parser.cpp``).

``tests/comment_corpus/`` holds raw-comment fixtures with an expected
``CommentModel`` JSON each, asserted both here (:mod:`tests.test_comment_corpus`,
against :func:`doxygen_parse`) and in C++
(``tests/cpp/test_comment_corpus.cpp``, against
``DoxygenCommentParser::parse_raw_text``). A behavior fixed in one parser
should get a case added to the corpus so the same fix is pinned in both.
"""

from __future__ import annotations

import importlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# Environment variable holding a dotted path to a ``str -> CommentModel``
# callable that replaces the default parser.
OVERRIDE_ENV = "CLANGQUILL_COMMENT_PARSER"


@dataclass(frozen=True)
class CommentParam:
    """A documented parameter or template parameter (``@param`` / ``@tparam``).

    ``direction`` is Doxygen's parameter-passing attribute without its brackets
    -- ``"in"``, ``"out"``, ``"in,out"``, or ``""`` when the comment did not
    spell one out. Doxygen writes it on the command (``@param[out] result``),
    so it describes the entry rather than being part of its description.
    """

    name: str
    description: str
    direction: str = ""


@dataclass(frozen=True)
class CommentRetval:
    """A documented named return value (``@retval``)."""

    value: str
    description: str


@dataclass(frozen=True)
class CommentThrow:
    """A documented thrown exception (``@throws`` / ``@throw`` / ``@exception``)."""

    exception: str
    description: str


@dataclass
class CommentModel:
    """Mirror of ``clangquill::model::CommentModel`` (keep in sync with the C++)."""

    brief: str = ""
    detail: list[str] = field(default_factory=list)
    params: list[CommentParam] = field(default_factory=list)
    tparams: list[CommentParam] = field(default_factory=list)
    returns: str = ""
    retvals: list[CommentRetval] = field(default_factory=list)
    throws: list[CommentThrow] = field(default_factory=list)
    see: list[str] = field(default_factory=list)
    since: list[str] = field(default_factory=list)
    deprecated: list[str] = field(default_factory=list)
    note: list[str] = field(default_factory=list)
    warning: list[str] = field(default_factory=list)
    pre: list[str] = field(default_factory=list)
    post: list[str] = field(default_factory=list)
    invariant: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)
    bug: list[str] = field(default_factory=list)
    author: list[str] = field(default_factory=list)
    version: list[str] = field(default_factory=list)
    date: list[str] = field(default_factory=list)
    custom: dict[str, list[str]] = field(default_factory=dict)


# A parser turns a raw comment string into a structured model.
CommentParser = Callable[[str], CommentModel]


# --- Building a model from the persisted comment_fields projection -----------

# The bracketed direction the C++ ``to_comment_fields`` prefixes onto a directed
# parameter's ``arg`` (``comment_fields`` has one slot for a field argument).
_DIRECTION_RE = re.compile(r"^\[\s*(in|out|in,\s*out|inout)\s*\]\s*(.*)$", re.IGNORECASE)


def _split_direction(arg: str) -> tuple[str, str]:
    """Split ``"[out] result"`` into ``("result", "out")``; else ``(arg, "")``."""
    match = _DIRECTION_RE.match(arg)
    if match is None:
        return arg, ""
    direction = match.group(1).lower().replace(" ", "")
    return match.group(2), "in,out" if direction == "inout" else direction


# Field names whose value is a single string accumulated into a list.
_LIST_FIELDS = {
    "detail": "detail",
    "see": "see",
    "since": "since",
    "deprecated": "deprecated",
    "note": "note",
    "warning": "warning",
    "pre": "pre",
    "post": "post",
    "invariant": "invariant",
    "todo": "todo",
    "bug": "bug",
    "author": "author",
    "version": "version",
    "date": "date",
}


def model_from_fields(rows: Iterable[tuple[str, str, str]]) -> CommentModel:
    """Reconstruct a :class:`CommentModel` from ``(name, arg, value)`` rows.

    ``rows`` are the ``comment_fields`` of one symbol in ordinal order; this is
    the inverse of the ``to_comment_fields`` flattening done by the C++ core.
    Unknown field names are collected into :attr:`CommentModel.custom`.
    """
    model = CommentModel()
    for name, arg, value in rows:
        if name == "brief":
            model.brief = value
        elif name == "returns":
            model.returns = value
        elif name == "param":
            pname, direction = _split_direction(arg)
            model.params.append(CommentParam(pname, value, direction))
        elif name == "tparam":
            pname, direction = _split_direction(arg)
            model.tparams.append(CommentParam(pname, value, direction))
        elif name == "retval":
            model.retvals.append(CommentRetval(arg, value))
        elif name == "throws":
            model.throws.append(CommentThrow(arg, value))
        elif name in _LIST_FIELDS:
            getattr(model, _LIST_FIELDS[name]).append(value)
        else:
            model.custom.setdefault(name, []).append(value)
    return model


# --- Default Doxygen parser (pure Python) ------------------------------------

_WS_RUN_RE = re.compile(r"\s+")


def _normalize_ws(text: str) -> str:
    """Collapse whitespace runs to single spaces and trim the ends.

    Mirrors ``normalize_ws`` in the C++ parser. A description wrapped over
    several comment lines keeps whatever indentation each continuation line
    carried past its marker (``*     enough to hold``), and the two parsers have
    to agree on the text that reaches ``comment_fields``.
    """
    return _WS_RUN_RE.sub(" ", text).strip()


_MARKER_RE = re.compile(r"^\s*(/\*\*<|/\*!<|/\*\*|/\*!|/\*|///<|///|//!<|//!|//)")
# A command word, optionally carrying the attribute Doxygen glues onto it: a
# direction on ``@param[in,out] buf ...``, a language on ``@code{.py}``. Without
# the attribute group the brackets would be read as the parameter's name and the
# real name as its description. The leading ``\s*`` is what lets a command still
# be recognized now that _strip_markers keeps a line's indentation.
_COMMAND_RE = re.compile(r"^\s*[@\\](\w+)((?:\[[^\]\s]*\])|(?:\{[^}\s]*\}))?\s*(.*)$")

# Commands opening a block whose lines are carried through verbatim.
_VERBATIM_CMDS = frozenset({"code", "verbatim"})

# Doxygen's inline commands: markup that decorates the next word inside a
# sentence rather than opening a block, mapped to the MyST that says the same
# thing. ``@n`` (a hard line break) has no equivalent that survives whitespace
# normalization, so it is dropped -- by ``_N_RE`` rather than through this
# table, since it is the only one of these that takes no argument. It still
# needs an entry here so ``_is_inline_command`` recognizes it as inline markup
# instead of a block command.
# Values are (prefix, suffix, is_cross_reference).
_INLINE_MARKUP: dict[str, tuple[str, str, bool]] = {
    "c": ("`", "`", False),
    "p": ("`", "`", False),
    "b": ("**", "**", False),
    "e": ("*", "*", False),
    "em": ("*", "*", False),
    "a": ("*", "*", False),
    "ref": ("{cpp:any}`", "`", True),
    "link": ("{cpp:any}`", "`", True),
    "n": ("", "", False),
}

# An overloaded operator's name, which the C++ domain indexes and resolves like
# any other member (``Vec::operator==``) but which is not spelled out of
# identifier characters. Alternatives are longest-first so ``<<=`` is not read as
# ``<<`` followed by stray text. The named forms (``operator new``,
# ``operator co_await``) are deliberately absent: they carry a space, and a
# Doxygen ``@ref`` argument ends at the first one.
_CPP_OPERATOR = r"operator(?:<=>|<<=|>>=|->\*|\+\+|--|->|<<|>>|<=|>=|==|!=|&&|\|\||\+=|-=|\*=|/=|%=|\^=|&=|\|=|\(\)|\[\]|[-+*/%^&|~!=<>,])"

# A plain (optionally qualified) C++ name, or such a name ending in an operator
# -- what the C++ domain can actually resolve. A ``{cpp:any}`` role over anything
# else is an "Unparseable C++ cross-reference", which a warnings-as-errors docs
# build turns into a hard failure, so such a target degrades to a code span
# instead. The leading guard rejects a target that stops at the bare ``operator``
# keyword, which names nothing: ``@ref Vec::operator bool`` is a conversion
# operator whose name carries a space, so the argument ends before the type.
_CPP_NAME_RE = re.compile(
    rf"^(?!(?:[A-Za-z_]\w*::)*operator$)"
    rf"(?:[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*(?:::{_CPP_OPERATOR})?|{_CPP_OPERATOR})$",
)

# A command and the word it decorates, where the command starts a word (so an
# address like ``user@b.example`` is left alone). The optional quoted string is
# Doxygen's ``@ref target "a title"`` link text.
_INLINE_RE = re.compile(
    r"""(?<![^\s(\[{"'])[@\\](?P<cmd>\w+)[ ](?P<arg>\S+)(?:[ ]"(?P<title>[^"]*)")?""",
)

# ``@n``/``\n`` on its own, matched separately because -- unlike every other
# entry in ``_INLINE_MARKUP`` -- it takes no argument. Folding it into
# ``_INLINE_RE`` would make its mandatory ``arg`` group swallow the next word
# in the sentence instead of leaving it alone.
_N_RE = re.compile(r"""(?<![^\s(\[{"'])[@\\][nN](?=[^\w]|$)""")

# Punctuation that closes the sentence or the clause around the decorated word
# rather than belonging to it. Doxygen prose is full of ``(see @ref target)``,
# where the ``)`` would otherwise be carried into the markup -- and into a
# cross-reference target, where it does not parse.
_TRAILING = ".,;:!?()[]{}"


def _is_inline_command(name: str) -> bool:
    """Report whether ``name`` is inline markup rather than a block command."""
    return name in _INLINE_MARKUP


def split_xref_target(token: str) -> tuple[str, str] | None:
    """Split ``token`` into a C++ cross-reference target and its trailing punctuation.

    ``_TRAILING`` cannot simply be stripped from the right before the target is
    checked: ``[]``, ``()`` and ``,`` are sentence punctuation in
    ``(see @ref parse_files)`` but part of the name in ``@ref Vec::operator[]``.
    So the longest prefix that is a whole C++ name wins, and only characters from
    ``_TRAILING`` may follow it. ``None`` means no prefix qualifies, and the
    caller degrades the reference to a code span.
    """
    for cut in range(len(token), 0, -1):
        if any(c not in _TRAILING for c in token[cut:]):
            break
        if _CPP_NAME_RE.match(token[:cut]):
            return token[:cut], token[cut:]
    return None


def _split_argument(arg: str, *, is_xref: bool) -> tuple[str, str, bool]:
    """Split a decorated word into ``(text, trailing punctuation, is a target)``.

    A cross-reference command gets the longest-match treatment
    (:func:`split_xref_target`), which keeps the ``[]`` of ``operator[]``; a word
    that does not name a C++ entity -- and every non-cross-reference command --
    simply loses its trailing punctuation, so ``@c foo.`` reads as a code span
    followed by a full stop.
    """
    if is_xref:
        split = split_xref_target(arg)
        if split is not None:
            return split[0], split[1], True
    tail = ""
    while arg and arg[-1] in _TRAILING:
        arg, tail = arg[:-1], arg[-1] + tail
    return arg, tail, False


def _render_inline_markup(text: str) -> str:
    """Rewrite Doxygen's inline commands in ``text`` as MyST markup.

    Without this ``@c x``, ``@p arg`` and ``@ref X`` reach the output as literal
    backslash text. Trailing sentence punctuation is left outside the markup, so
    ``@c foo.`` reads as a code span followed by a full stop.
    """

    def replace(match: re.Match[str]) -> str:
        markup = _INLINE_MARKUP.get(match["cmd"].lower())
        if markup is None:
            return match[0]
        prefix, suffix, is_xref = markup
        if not prefix:
            return ""
        title = match["title"]
        arg, tail, kept_xref = _split_argument(match["arg"], is_xref=is_xref)
        # A cross-reference the C++ domain could not parse fails the docs build,
        # so one that does not name a C++ entity becomes a plain code span.
        # Reducing it to a non-xref here lets the title handling below decide its
        # quoted title, if any, the same way it would for a real code span.
        if is_xref and not kept_xref:
            prefix, suffix = "`", "`"
        is_xref = kept_xref
        if not arg:
            return match[0]
        kept_title = ""
        if title is not None:
            if is_xref:
                if tail:
                    return match[0]
                arg = f"{title} <{arg}>"
            else:
                # The quoted-title link text is only Doxygen's for a
                # cross-reference command; elsewhere the quote is just prose
                # that happened to follow the decorated word, so it stays
                # where it was written rather than being folded into the
                # markup.
                kept_title = f' "{title}"'
        return f"{prefix}{arg}{suffix}{tail}{kept_title}"

    text = _N_RE.sub("", text)
    text = _INLINE_RE.sub(replace, text)
    return _WS_RUN_RE.sub(" ", text).strip()


# Command aliases collapsed onto a canonical model field/handler.
_RETURN_CMDS = {"return", "returns", "result"}
_BRIEF_CMDS = {"brief", "short"}

# Commands whose text is appended verbatim to a list attribute.
_LIST_APPEND = {
    "see": "see",
    "sa": "see",
    "warning": "warning",
    "attention": "warning",
    "since": "since",
    "deprecated": "deprecated",
    "note": "note",
    # Doxygen sets a @remark off from the prose exactly as it does a note.
    "remark": "note",
    "remarks": "note",
    "pre": "pre",
    "post": "post",
    "invariant": "invariant",
    "todo": "todo",
    "bug": "bug",
    "author": "author",
    "authors": "author",
    "version": "version",
    "date": "date",
}

# Commands whose text is prose: @details is the detailed description, and
# @par [title] text a paragraph of it. Neither is an unrecognized command.
_DETAIL_CMDS = frozenset({"details", "par"})

# Doxygen's copy commands, which name an entity whose documentation should be
# pulled in here. Nothing in this pipeline performs the copy, and a command left
# in ``custom`` renders as nothing at all -- so a comment that is only a
# ``@copydoc`` would publish an empty description. They degrade to a
# cross-reference to the entity whose documentation was asked for: true,
# navigable, and where the reader was being sent. The parse reports the
# unperformed copy separately (see ``ast_visitor.cpp``).
_COPY_CMDS = frozenset({"copydoc", "copybrief", "copydetails"})


def _copy_target(text: str) -> str:
    """Reduce a copy command's argument to the qualified name it points at.

    Doxygen accepts a whole declaration after a copy command, so the argument
    can carry template arguments, a parameter list and trailing qualifiers --
    ``DenseCoeffsBase<Derived,ReadOnlyAccessors>::coeff(Index,Index) const``
    has to come down to ``DenseCoeffsBase::coeff``, the only shape that can be
    pointed at and the only one the C++ domain resolves.

    Template arguments are dropped by counting ``<``/``>`` depth rather than by
    substituting an innermost ``<...>``, so a nested list -- Eigen's
    ``Matrix<Scalar,Rows,Cols>::Base<Derived<T>>`` shape -- comes out whole
    instead of leaving the outer brackets behind. The parameter list ends the
    name only at depth zero: the ``(`` of a function-type template argument,
    ``Registry<std::function<void(int)>>::add``, belongs to the argument being
    dropped, and cutting there would strip the member the copy named. This
    mirrors ``copy_target`` in ``src/cpp/parser/doxygen_comment_parser.cpp``
    command for command; the two parsers must agree here (see
    ``tests/comment_corpus``).
    """
    out = []
    depth = 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(depth - 1, 0)
        elif ch == "(" and depth == 0:
            break
        elif depth == 0:
            out.append(ch)
    name = "".join(out).split(maxsplit=1)
    return name[0].removeprefix("::") if name else ""


# Commands whose text is "<arg> <description>"; mapped to (attribute, dataclass).
_TUPLE_APPEND: dict[str, tuple[str, type]] = {
    "param": ("params", CommentParam),
    "tparam": ("tparams", CommentParam),
    "retval": ("retvals", CommentRetval),
    "throw": ("throws", CommentThrow),
    "throws": ("throws", CommentThrow),
    "exception": ("throws", CommentThrow),
}

# The subset of the above that may carry a direction attribute.
_PARAM_CMDS = frozenset({"param", "tparam"})


def _is_continuation_marker(head: str) -> bool:
    """Report whether ``head`` opens with a Javadoc continuation ``*``.

    Only a lone ``*`` followed by whitespace or the end of the line is a marker.
    Markdown reaching the same position is content: ``* item`` is a bullet whose
    own ``*`` must survive, ``**bold**`` and ``*emphasis*`` glue the ``*`` to the
    word they decorate.
    """
    return head.startswith("*") and (len(head) == 1 or head[1] in " \t")


def _strip_markers(raw: str) -> list[str]:
    """Strip comment markers, returning the documentation lines.

    Whitespace *after* the marker is kept: inside a ``@code`` block it is the
    only record of the example's indentation. Only the single space that
    conventionally separates the marker from the text is removed, and a line
    holding nothing but markers comes back empty.
    """
    # A continuation `*` only exists inside a `/* ... */` block; in a `///` or
    # `//!` block every line carries its own marker, so a `*` there is content.
    block_style = raw.lstrip().startswith("/*")
    out: list[str] = []
    for original in raw.splitlines():
        line = original.rstrip().removesuffix("*/").rstrip()
        marker = _MARKER_RE.match(line)
        if marker:
            line = line[marker.end() :]
            stripped = True
        else:
            stripped = False
            head = line.lstrip()
            if block_style and _is_continuation_marker(head):
                line = head[1:]
                stripped = True
        if stripped:
            line = line.removeprefix(" ")
        out.append(line.rstrip() if line.strip() else "")
    return out


def _route(model: CommentModel, name: str, text: str, direction: str = "") -> None:
    """Route one command into the model (mirrors the C++ ``route_command``)."""
    if name in _BRIEF_CMDS:
        # Doxygen joins a second @brief onto the first rather than dropping it.
        model.brief = f"{model.brief} {text}".strip() if model.brief else text
    elif name in _RETURN_CMDS:
        model.returns = f"{model.returns} {text}".strip()
    elif name in _PARAM_CMDS:
        attr, cls = _TUPLE_APPEND[name]
        pname, description = _split_first(text)
        getattr(model, attr).append(cls(pname, description, direction))
    elif name in _TUPLE_APPEND:
        attr, cls = _TUPLE_APPEND[name]
        getattr(model, attr).append(cls(*_split_first(text)))
    elif name in _DETAIL_CMDS:
        if text:
            model.detail.append(text)
    elif name in _COPY_CMDS:
        target = _copy_target(text)
        if target:
            model.see.append(target)
    elif name in _LIST_APPEND:
        getattr(model, _LIST_APPEND[name]).append(text)
    else:
        model.custom.setdefault(name, []).append(text)


def _split_command_word(name: str, attr: str | None) -> tuple[str, str]:
    """Split a command word into its name and the attribute Doxygen glues on.

    Returns the command and its attribute value: a direction for
    ``@param``/``@tparam``, a highlighting language for ``@code``. Any other
    suffix stays glued to the command name, so an unknown ``@foo[bar]`` still
    reaches ``custom`` under its full spelling.
    """
    if attr is None:
        return name, ""
    text = attr[1:-1].lower()
    if attr.startswith("[") and name in _PARAM_CMDS:
        if text in {"in", "out"}:
            return name, text
        if text in {"inout", "in,out"}:
            return name, "in,out"
    elif attr.startswith("{") and name == "code":
        language = text.removeprefix(".")
        return name, "" if language == "unparsed" else language
    return name + attr, ""


def _fenced_block(kind: str, language: str, lines: list[str]) -> str:
    """Render a verbatim block as a MyST fenced code block, line structure intact.

    The output target is Markdown, where a code example's newlines and relative
    indentation are load-bearing; the block is carried as a ready-to-emit fence
    in ``detail`` so it keeps its position among the prose paragraphs.
    """
    body = list(lines)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if not body:
        return ""
    # The comment marker left a common indent on every line; only what each
    # line has beyond it is the example's own structure.
    indent = min(len(line) - len(line.lstrip()) for line in body if line.strip())
    ticks = max(3, *(_longest_backtick_run(line) + 1 for line in body))
    # A @verbatim block is preformatted text rather than code, so it gets no
    # info string; a @code block without an explicit language is C++.
    info = language or ("cpp" if kind == "code" else "")
    fence = "`" * ticks
    rendered = "\n".join(line[indent:] if line.strip() else "" for line in body)
    return f"{fence}{info}\n{rendered}\n{fence}"


def _longest_backtick_run(line: str) -> int:
    longest = run = 0
    for char in line:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    return longest


def _is_fenced_block(text: str) -> bool:
    """Report whether ``text`` is a ``detail`` entry :func:`_fenced_block` produced."""
    return text.startswith("```")


class _VerbatimBlock:
    """The body of an open ``@code`` / ``@verbatim`` block being collected.

    Inside such a block every line is body text -- commands, blank lines and
    all -- until the matching ``@endcode`` / ``@endverbatim``. The finished
    block is appended to ``out``, the same list the prose paragraphs go to, so
    it keeps its place among them.
    """

    def __init__(self, out: list[str]) -> None:
        self._out = out
        self.kind = ""
        self.language = ""
        self.lines: list[str] = []

    def open(self, kind: str, language: str, first_line: str) -> None:
        """Start a block; anything after the command is already body text."""
        self.kind = kind
        self.language = language
        self.lines = [first_line] if first_line.strip() else []

    def feed(self, line: str) -> None:
        """Take one line, closing the block when its terminator arrives."""
        match = _COMMAND_RE.match(line)
        if match and match.group(1).lower() == "end" + self.kind:
            self.close()
        else:
            self.lines.append(line)

    def close(self) -> None:
        """Emit and reset the block; a no-op when none is open.

        An unterminated block still carries documentation, so closing one out
        at the end of a comment keeps what it holds.
        """
        if not self.kind:
            return
        rendered = _fenced_block(self.kind, self.language, self.lines)
        if rendered:
            self._out.append(rendered)
        self.kind = ""
        self.language = ""
        self.lines = []


def _split_first(text: str) -> tuple[str, str]:
    parts = text.split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


@dataclass
class _Scan:
    """The open section of a comment while :func:`doxygen_parse` walks it.

    ``buf`` accumulates either lead prose (``cmd`` is ``None``) or the argument
    of the active command, and :meth:`flush` closes whichever it is.
    """

    model: CommentModel
    lead: list[str] = field(default_factory=list)
    explicit_brief: bool = False
    cmd: str | None = None
    direction: str = ""
    buf: list[str] = field(default_factory=list)
    have_lead_para: bool = False

    def flush(self) -> None:
        """Close the open section, routing what it collected into the model."""
        text = _render_inline_markup(_normalize_ws(" ".join(self.buf)))
        self.buf.clear()
        if self.cmd is None:
            if text:
                self.lead.append(text)
            self.have_lead_para = False
        else:
            if self.cmd in _BRIEF_CMDS:
                self.explicit_brief = True
            _route(self.model, self.cmd, text, self.direction)
        self.cmd = None
        self.direction = ""


def doxygen_parse(raw: str) -> CommentModel:
    """Parse a raw Doxygen comment into a :class:`CommentModel`.

    A pure-Python scanner used as the default registry parser and as the model
    behind the Python override hook. It mirrors the C++ Doxygen parser closely
    enough that either side produces an equivalent structured model.
    """
    scan = _Scan(CommentModel())
    block = _VerbatimBlock(scan.lead)

    for line in _strip_markers(raw):
        if block.kind:
            block.feed(line)
            continue
        match = _COMMAND_RE.match(line)
        # Inline markup is not a block command: a wrapped prose line can begin
        # with one (``@ref Foo is the ...``), and taking it for a command
        # flushed the open section and swallowed the rest of the paragraph.
        if match and not _is_inline_command(match.group(1).lower()):
            scan.flush()
            cmd, attribute = _split_command_word(match.group(1).lower(), match.group(2))
            if cmd in _VERBATIM_CMDS:
                block.open(cmd, attribute, match.group(3))
                continue
            scan.cmd = cmd
            scan.direction = attribute
            scan.buf.append(match.group(3))
        elif not line:
            # A blank line ends whatever section is open: a Doxygen paragraph
            # command runs only to the next blank line, so the paragraphs below
            # one document the entity rather than extending ``@brief`` or the
            # last ``@param``.
            if scan.cmd is not None or scan.have_lead_para:
                scan.flush()
        else:
            scan.buf.append(line)
            # The flag says what ``buf`` is holding: lead prose, or the text of
            # an active command.
            scan.have_lead_para = scan.cmd is None
    block.close()
    scan.flush()

    _assign_lead(scan.model, scan.lead, explicit_brief=scan.explicit_brief)
    return scan.model


def _assign_lead(model: CommentModel, lead: list[str], *, explicit_brief: bool) -> None:
    """Promote leading paragraphs: the first is the brief unless one was given.

    A verbatim block is skipped over rather than promoted -- it is never a
    one-line summary, and a comment opening with a code example would otherwise
    end up with no brief at all.
    """
    brief_at = None
    if not explicit_brief:
        brief_at = next(
            (i for i, text in enumerate(lead) if not _is_fenced_block(text)),
            None,
        )
    if brief_at is not None:
        model.brief = lead[brief_at]
    model.detail.extend(text for i, text in enumerate(lead) if i != brief_at)


# --- Parser registry & override hook -----------------------------------------

_REGISTRY: dict[str, CommentParser] = {"doxygen": doxygen_parse}


def register_parser(name: str, parser: CommentParser) -> None:
    """Register (or replace) the parser used for comment ``format`` ``name``."""
    _REGISTRY[name] = parser


def available_parsers() -> list[str]:
    """Return the registered format names, sorted."""
    return sorted(_REGISTRY)


def get_parser(name: str = "doxygen") -> CommentParser:
    """Return the parser registered for ``name``.

    Raises :class:`KeyError` if no parser is registered for the format.
    """
    return _REGISTRY[name]


def _import_dotted(path: str) -> CommentParser:
    """Import a ``module.attr`` (or ``module:attr``) dotted path to a callable."""
    module_name, _, attr = path.replace(":", ".").rpartition(".")
    if not module_name:
        msg = f"{OVERRIDE_ENV} must be a dotted path like 'pkg.module.func', got {path!r}"
        raise ValueError(msg)
    obj = getattr(importlib.import_module(module_name), attr)
    if not callable(obj):
        msg = f"comment parser override {path!r} is not callable"
        raise TypeError(msg)
    return obj


def resolve_override(override: str | CommentParser | None = None) -> CommentParser | None:
    """Resolve a comment-parser override, or ``None`` if none is configured.

    The override may be passed directly (a callable, a registered format name,
    or a dotted path string) or left to the ``CLANGQUILL_COMMENT_PARSER``
    environment variable, which holds a registered name or a dotted path to a
    ``str -> CommentModel`` callable.
    """
    if override is None:
        override = os.environ.get(OVERRIDE_ENV) or None
    if override is None:
        return None
    if callable(override):
        return override
    if override in _REGISTRY:
        return _REGISTRY[override]
    return _import_dotted(override)

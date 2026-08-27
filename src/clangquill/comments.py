"""Format-agnostic comment model, parser registry, and the default parser.

The C++ core parses each symbol's documentation comment into a structured model
and persists it (``comments`` / ``comment_fields``). This module mirrors that
model as Python dataclasses and provides the read-side machinery:

* :class:`CommentModel` and friends mirror ``clangquill::model::CommentModel``.
* a parser *registry* maps a format name to a ``str -> CommentModel`` callable.
* :func:`doxygen_parse` is the built-in default, a pure-Python Doxygen scanner.
* :func:`resolve_override` honours the ``CLANGQUILL_COMMENT_PARSER`` dotted-path
  hook so a project can swap in its own parser without recompiling the core.

Keep the field names in sync with the C++ model and the ``comment_fields``
projection written by :mod:`clangquill` (see ``parser/comment_parser.cpp``).
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
# normalization, so it is dropped.
_INLINE_MARKUP: dict[str, tuple[str, str]] = {
    "c": ("`", "`"),
    "p": ("`", "`"),
    "b": ("**", "**"),
    "e": ("*", "*"),
    "em": ("*", "*"),
    "a": ("*", "*"),
    "ref": ("{cpp:any}`", "`"),
    "link": ("{cpp:any}`", "`"),
    "n": ("", ""),
}

# A command and the word it decorates, where the command starts a word (so an
# address like ``user@b.example`` is left alone). The optional quoted string is
# Doxygen's ``@ref target "a title"`` link text.
_INLINE_RE = re.compile(
    r"""(?<![^\s(\[{"'])[@\\](?P<cmd>\w+)[ ](?P<arg>\S+)(?:[ ]"(?P<title>[^"]*)")?""",
)

# Punctuation that ends a sentence rather than belonging to the decorated word.
_SENTENCE_END = ".,;:!?"


def _is_inline_command(name: str) -> bool:
    """Report whether ``name`` is inline markup rather than a block command."""
    return name in _INLINE_MARKUP


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
        prefix, suffix = markup
        if not prefix:
            return ""
        arg, title = match["arg"], match["title"]
        tail = ""
        while arg and arg[-1] in _SENTENCE_END:
            arg, tail = arg[:-1], arg[-1] + tail
        if not arg:
            return match[0]
        if title is not None and not tail:
            arg = f"{title} <{arg}>"
        elif title is not None:
            return match[0]
        return f"{prefix}{arg}{suffix}{tail}"

    return _INLINE_RE.sub(replace, text)


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
    "pre": "pre",
    "post": "post",
}

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


def _strip_markers(raw: str) -> list[str]:
    """Strip comment markers, returning the documentation lines.

    Whitespace *after* the marker is kept: inside a ``@code`` block it is the
    only record of the example's indentation. Only the single space that
    conventionally separates the marker from the text is removed, and a line
    holding nothing but markers comes back empty.
    """
    out: list[str] = []
    for original in raw.splitlines():
        line = original.rstrip().removesuffix("*/").rstrip()
        marker = _MARKER_RE.match(line)
        if marker:
            line = line[marker.end() :]
            stripped = True
        else:
            stripped = False
        # A leading '*' is a Javadoc continuation marker, not content.
        head = line.lstrip()
        if head.startswith("*"):
            line = head[1:]
            stripped = True
        if stripped:
            line = line.removeprefix(" ")
        out.append(line.rstrip() if line.strip() else "")
    return out


def _route(model: CommentModel, name: str, text: str, direction: str = "") -> None:
    """Route one command into the model (mirrors the C++ ``route_command``)."""
    if name in _BRIEF_CMDS:
        if not model.brief:
            model.brief = text
    elif name in _RETURN_CMDS:
        model.returns = f"{model.returns} {text}".strip()
    elif name in _PARAM_CMDS:
        attr, cls = _TUPLE_APPEND[name]
        pname, description = _split_first(text)
        getattr(model, attr).append(cls(pname, description, direction))
    elif name in _TUPLE_APPEND:
        attr, cls = _TUPLE_APPEND[name]
        getattr(model, attr).append(cls(*_split_first(text)))
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
        text = _render_inline_markup(" ".join(self.buf).strip())
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

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
# A command word, optionally carrying Doxygen's bracketed direction attribute
# (``@param[in,out] buf ...``). Without the attribute group the brackets would
# be read as the parameter's name and the real name as its description.
_COMMAND_RE = re.compile(r"^[@\\](\w+)(\[[^\]\s]*\])?\s*(.*)$")

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
    """Strip comment markers, returning the trimmed documentation lines."""
    out: list[str] = []
    for original in raw.splitlines():
        line = original.strip()
        marker = _MARKER_RE.match(line)
        if marker:
            line = line[marker.end() :]
        line = line.removesuffix("*/")
        line = line.strip()
        # A leading '*' is a Javadoc continuation marker, not content.
        if line.startswith("*") and not line.startswith("*/"):
            line = line[1:].strip()
        out.append(line)
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


def _split_command_direction(name: str, attr: str | None) -> tuple[str, str]:
    """Split a command word into its name and its direction attribute.

    Only ``@param``/``@tparam`` carry one, and only a direction Doxygen defines
    is taken; any other bracket suffix stays glued to the command name, so an
    unknown ``@foo[bar]`` still reaches ``custom`` under its full spelling.
    """
    if attr is None:
        return name, ""
    text = attr[1:-1].lower()
    if name in _PARAM_CMDS:
        if text in {"in", "out"}:
            return name, text
        if text in {"inout", "in,out"}:
            return name, "in,out"
    return name + attr, ""


def _split_first(text: str) -> tuple[str, str]:
    parts = text.split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def doxygen_parse(raw: str) -> CommentModel:
    """Parse a raw Doxygen comment into a :class:`CommentModel`.

    A pure-Python scanner used as the default registry parser and as the model
    behind the Python override hook. It mirrors the C++ Doxygen parser closely
    enough that either side produces an equivalent structured model.
    """
    model = CommentModel()
    lead: list[str] = []
    explicit_brief = False
    cmd: str | None = None
    direction = ""
    buf: list[str] = []
    have_lead_para = False

    def flush() -> None:
        nonlocal cmd, direction, explicit_brief, have_lead_para
        text = " ".join(buf).strip()
        buf.clear()
        if cmd is None:
            if text:
                lead.append(text)
            have_lead_para = False
        else:
            if cmd in _BRIEF_CMDS:
                explicit_brief = True
            _route(model, cmd, text, direction)
        cmd = None
        direction = ""

    for line in _strip_markers(raw):
        match = _COMMAND_RE.match(line)
        if match:
            flush()
            cmd, direction = _split_command_direction(match.group(1).lower(), match.group(2))
            buf.append(match.group(3))
        elif not line:
            # A blank line ends whatever section is open: a Doxygen paragraph
            # command runs only to the next blank line, so the paragraphs below
            # one document the entity rather than extending ``@brief`` or the
            # last ``@param``.
            if cmd is not None or have_lead_para:
                flush()
        else:
            buf.append(line)
            have_lead_para = have_lead_para or cmd is None
    flush()

    _assign_lead(model, lead, explicit_brief=explicit_brief)
    return model


def _assign_lead(model: CommentModel, lead: list[str], *, explicit_brief: bool) -> None:
    """Promote leading paragraphs: the first is the brief unless one was given."""
    if not explicit_brief and lead:
        model.brief = lead[0]
        model.detail.extend(lead[1:])
    else:
        model.detail.extend(lead)


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

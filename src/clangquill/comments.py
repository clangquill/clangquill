"""Format-agnostic comment model, parser registry, and the default parser.

The C++ core parses each symbol's documentation comment into a structured model
and persists it (``comments`` / ``comment_fields``). This module mirrors that
model as Python dataclasses and provides the read-side machinery:

* :class:`CommentModel` and friends mirror ``clangquill::model::CommentModel``.
* a parser *registry* maps a format name to a ``str -> CommentModel`` callable.
* :func:`doxygen_parse` is the built-in default, a thin wrapper over the
  compiled scanner (``clangquill::comment::doxygen_parse_raw``). There is one
  Doxygen grammar, not two that agree by convention.
* :func:`resolve_override` honours the ``CLANGQUILL_COMMENT_PARSER`` dotted-path
  hook so a project can swap in its own parser without recompiling the core.

The field routing below is derived from ``_core.COMMENT_FIELDS`` rather than
transcribed, so the encoder in ``comment/fields.cpp`` and this decoder cannot
disagree about which fields exist or what each is called.

``tests/comment_corpus/`` holds raw-comment fixtures with an expected
``CommentModel`` JSON each. :mod:`tests.test_comment_corpus` asserts the
scanner *plus this decoder* against them, while
``tests/cpp/test_comment_corpus.cpp`` asserts the scanner plus the C++
serializer -- so the two agreeing is what proves :func:`model_from_fields`
inverts the C++ flatten. Add a case there whenever you touch the grammar.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from clangquill import _core

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


def _split_direction(arg: str) -> tuple[str, str]:
    """Split ``"[out] result"`` into ``("result", "out")``; else ``(arg, "")``.

    ``comment_fields`` has one slot for a field's argument, so a directed
    parameter carries its direction there in the bracketed form Doxygen writes.
    The split is the core's own ``split_param_arg``, the exact inverse of the
    encoder, rather than a regex reconstructed from it.
    """
    name, direction = _core.split_param_arg(arg)
    return name, direction


# Row name -> ``CommentModel`` attribute, for the fields whose value is a single
# string accumulated into a list. Derived from the core's field table (the same
# list ``to_comment_fields`` walks) so a field added on one side cannot be
# missed here; the shapes that need an argument as well are routed explicitly
# in ``model_from_fields``.
_LIST_FIELDS = {row: member for row, (member, shape) in _core.COMMENT_FIELDS.items() if shape == "list"}


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


def split_xref_target(token: str) -> tuple[str, str] | None:
    """Split ``token`` into a C++ cross-reference target and its trailing punctuation.

    Trailing punctuation cannot simply be stripped from the right before the
    target is checked: ``[]``, ``()`` and ``,`` are sentence punctuation in
    ``(see @ref parse_files)`` but part of the name in ``@ref Vec::operator[]``.
    So the longest prefix that is a whole C++ name wins. ``None`` means no
    prefix qualifies, and the caller degrades the reference to a code span.

    The rule comes from the core, which is also what the scanner applies when it
    renders inline markup -- a ``@ref`` must not resolve differently depending
    on which of the two saw it.
    """
    return _core.split_xref_target(token)


# --- The default Doxygen parser ----------------------------------------------


def doxygen_parse(raw: str) -> CommentModel:
    """Parse a raw Doxygen comment into a :class:`CommentModel`.

    A thin wrapper over the compiled core: ``_core.parse_doxygen_comment``
    scans the text and returns the very ``comment_fields`` rows the IR would
    have persisted, which :func:`model_from_fields` then rebuilds. A stored
    comment and a freshly parsed one therefore travel the same decoder, and
    there is one Doxygen grammar rather than two that agree by convention.

    The scan needs no libclang, so this works in the stub backend too.
    """
    return model_from_fields(_core.parse_doxygen_comment(raw))


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

"""Render the SQLite IR into MyST Markdown with C++ domain cross-references.

The generator is the user-facing customization point. It wires a Jinja2
environment whose loader is a :class:`~jinja2.ChoiceLoader` of any user template
directories followed by the package's own ``templates/``; a user file named
``{kind}.md.jinja`` therefore overrides the bundled default *by name*.

Templates render real Sphinx C++ domain directives (``cpp:class``,
``cpp:function``, …) so every symbol becomes an indexed domain object, and
inter-symbol links use ``{cpp:any}`` roles resolved from the ``references``
table. The Python side of this module supplies the context helpers templates
call: :meth:`Generator.xref`, :meth:`Generator.render_comment`, the signature
builders, and the child/relation queries.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache, partial
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, StrictUndefined

from clangquill.store import AccessKind, RefKind, SymbolKind

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from jinja2 import Template

    from clangquill.comments import CommentModel
    from clangquill.store import Enumerator, Group, Parameter, Reference, SourceFile, Store, Symbol

# Per-kind Jinja template (the ``{kind}.md.jinja`` override seam). Several
# SymbolKinds share a rendering, e.g. struct/union reuse the class template.
_TEMPLATE_FOR: dict[SymbolKind, str] = {
    SymbolKind.NAMESPACE: "namespace",
    SymbolKind.CLASS: "class",
    SymbolKind.STRUCT: "class",
    SymbolKind.UNION: "class",
    SymbolKind.CLASS_TEMPLATE: "class",
    SymbolKind.FUNCTION: "function",
    SymbolKind.METHOD: "function",
    SymbolKind.CONSTRUCTOR: "function",
    SymbolKind.DESTRUCTOR: "function",
    SymbolKind.FUNCTION_TEMPLATE: "function",
    SymbolKind.ENUM: "enum",
    SymbolKind.VARIABLE: "variable",
    SymbolKind.FIELD: "variable",
    SymbolKind.TYPEDEF: "typedef",
    SymbolKind.TYPE_ALIAS: "typedef",
    SymbolKind.CONCEPT: "concept",
    SymbolKind.MACRO: "macro",
}

# The C++ domain object directive emitted for each kind.
_DIRECTIVE_FOR: dict[SymbolKind, str] = {
    SymbolKind.NAMESPACE: "cpp:namespace",
    SymbolKind.CLASS: "cpp:class",
    SymbolKind.STRUCT: "cpp:struct",
    SymbolKind.UNION: "cpp:union",
    SymbolKind.CLASS_TEMPLATE: "cpp:class",
    SymbolKind.FUNCTION: "cpp:function",
    SymbolKind.METHOD: "cpp:function",
    SymbolKind.CONSTRUCTOR: "cpp:function",
    SymbolKind.DESTRUCTOR: "cpp:function",
    SymbolKind.FUNCTION_TEMPLATE: "cpp:function",
    SymbolKind.ENUM: "cpp:enum",
    SymbolKind.ENUMERATOR: "cpp:enumerator",
    SymbolKind.VARIABLE: "cpp:var",
    SymbolKind.FIELD: "cpp:member",
    SymbolKind.TYPEDEF: "cpp:type",
    SymbolKind.TYPE_ALIAS: "cpp:type",
    SymbolKind.CONCEPT: "cpp:concept",
    SymbolKind.MACRO: "c:macro",
}

# Human-readable label used in section headings.
_LABEL_FOR: dict[SymbolKind, str] = {
    SymbolKind.NAMESPACE: "Namespace",
    SymbolKind.CLASS: "Class",
    SymbolKind.STRUCT: "Struct",
    SymbolKind.UNION: "Union",
    SymbolKind.CLASS_TEMPLATE: "Class template",
    SymbolKind.FUNCTION: "Function",
    SymbolKind.METHOD: "Method",
    SymbolKind.CONSTRUCTOR: "Constructor",
    SymbolKind.DESTRUCTOR: "Destructor",
    SymbolKind.FUNCTION_TEMPLATE: "Function template",
    SymbolKind.ENUM: "Enum",
    SymbolKind.VARIABLE: "Variable",
    SymbolKind.FIELD: "Field",
    SymbolKind.TYPEDEF: "Typedef",
    SymbolKind.TYPE_ALIAS: "Type alias",
    SymbolKind.CONCEPT: "Concept",
    SymbolKind.MACRO: "Macro",
}

_ACCESS_KEYWORD: dict[AccessKind, str] = {
    AccessKind.PUBLIC: "public",
    AccessKind.PROTECTED: "protected",
    AccessKind.PRIVATE: "private",
}

_FUNCTION_KINDS = frozenset(
    {
        SymbolKind.FUNCTION,
        SymbolKind.METHOD,
        SymbolKind.CONSTRUCTOR,
        SymbolKind.DESTRUCTOR,
        SymbolKind.FUNCTION_TEMPLATE,
    },
)

# Record kinds that earn their own page under ``group_by="class"``: each renders
# in full (members and nested types stay on the record's page).
_RECORD_KINDS = frozenset(
    {
        SymbolKind.CLASS,
        SymbolKind.STRUCT,
        SymbolKind.UNION,
        SymbolKind.CLASS_TEMPLATE,
    },
)

# Container kinds the class-granular split walks into rather than rendering
# inline: a namespace is transparent (descended through) and a record becomes
# its own page. Everything else is a leaf collected onto its namespace's page.
_CONTAINER_KINDS = _RECORD_KINDS | {SymbolKind.NAMESPACE}

# Leaf kinds collected onto a namespace's "Types" page under
# ``group_by="namespace"`` (type-like declarations that do not earn a page).
_TYPE_LEAF_KINDS = frozenset(
    {
        SymbolKind.ENUM,
        SymbolKind.TYPEDEF,
        SymbolKind.TYPE_ALIAS,
        SymbolKind.CONCEPT,
    },
)

# Leaf kinds collected onto a namespace's "Constants" page under
# ``group_by="namespace"`` (value declarations: namespace-scope variables and,
# at global scope, macros).
_CONST_LEAF_KINDS = frozenset(
    {
        SymbolKind.VARIABLE,
        SymbolKind.MACRO,
    },
)

_SLUG_RE = re.compile(r"[^0-9A-Za-z]+")
_BLANKS_RE = re.compile(r"\n{3,}")
# Free-form ``@see``/``@sa`` text classification: a leading URL scheme is turned
# into a link, and trailing call/punctuation noise (``foo().``) is stripped so
# the cleaned name stays a parseable C++ cross-reference.
_XREF_URL_RE = re.compile(r"^(?:https?|mailto):", re.IGNORECASE)
_XREF_TRAILING_RE = re.compile(r"(?:\(\s*\)|[.,;:()\s])+$")
# libclang's pretty-printer occasionally splits the first ``==`` of a SFINAE /
# ``enable_if`` expression into ``= =`` (e.g. ``G::dimension = = 2``). That text
# is stored verbatim in the signature, and ``= =`` is not valid C++, so the
# Sphinx C++ domain fails to parse the emitted directive. ``= =`` can never occur
# in a well-formed declaration, so rejoining it to ``==`` is a safe repair.
_SPLIT_EQEQ_RE = re.compile(r"=\s+=")


def _repair_split_operators(text: str) -> str:
    """Rejoin a ``==`` libclang's pretty-printer rendered as ``= =`` (see :data:`_SPLIT_EQEQ_RE`)."""
    return _SPLIT_EQEQ_RE.sub("==", text)


def _spec_suffix(symbol: Symbol) -> str:
    """Return the ``<...>`` specialization-arg suffix from ``display_name``.

    For a class-template partial/explicit specialization, libclang's
    ``display_name`` carries the specialization args after the spelling
    (``ContainerFactory<CommonDenseVector<S>>``) while ``qualified_name`` is the
    bare template name. The suffix lets the emitted ``cpp:class`` directive name
    the specialization so the C++ domain does not see every specialization as a
    duplicate of the primary template. A primary template's ``display_name`` has
    no ``<...>`` (it equals the spelling), so the suffix is empty and the output
    is unchanged.
    """
    display = symbol.display_name or ""
    spelling = symbol.spelling or ""
    if not spelling or not display.startswith(spelling):
        return ""
    suffix = display[len(spelling) :]
    return suffix if suffix.startswith("<") else ""


#: A default argument clang could not evaluate, emitted verbatim as
#: ``<recovery-expr>`` (optionally followed by a balanced ``(...)`` of arguments).
_RECOVERY_DEFAULT_RE = re.compile(r"\s*=\s*<recovery-expr>")


def _strip_recovery_defaults(signature: str) -> str:
    """Remove ``= <recovery-expr>(...)`` defaults clang could not evaluate.

    When a default-argument expression references a declaration absent from the
    parsed translation unit (the external DUNE headers are not present during the
    docs parse), clang emits ``<recovery-expr>(...)`` instead of the value. That
    text is not parseable C++, so the whole ``= <recovery-expr>(...)`` default is
    dropped, leaving the parameter without a default (its type and name intact).
    A balanced ``(...)`` group after ``<recovery-expr>`` is consumed so both
    ``<recovery-expr>("")`` and ``<recovery-expr>()`` are removed cleanly; a bare
    ``<recovery-expr>`` with no parens drops just the ``= <recovery-expr>``.
    """
    out: list[str] = []
    i = 0
    while True:
        m = _RECOVERY_DEFAULT_RE.search(signature, i)
        if m is None:
            out.append(signature[i:])
            break
        out.append(signature[i : m.start()])
        j = m.end()  # just past "<recovery-expr>"
        if j < len(signature) and signature[j] == "(":
            depth = 0
            while j < len(signature):
                if signature[j] == "(":
                    depth += 1
                elif signature[j] == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
        i = j
    return "".join(out)


#: A ``<...>`` template-argument list allowing one level of nested ``<...>``
#: (covers the injected-class-name of the documented constructors, e.g.
#: ``Foo<Bar<X>>``). Used as an f-string fragment of :func:`_ctor_template_id_re`.
_BALANCED_ANGLES = r"<(?:[^<>]|<[^<>]*>)*>"

# The two signature patterns below are built per *symbol name*, so compiling
# them inline made every symbol render pay an re.compile — ~1,200 compiles on
# an eigen render — while thrashing the re module's own 512-entry cache with
# distinct names. Memoising by name keeps one compiled pattern per distinct
# spelling; the bound protects a long-lived process (the Sphinx extension)
# from unbounded growth while comfortably holding a large project's names.


@lru_cache(maxsize=4096)
def _ctor_template_id_re(name: str) -> re.Pattern[str]:
    """Pattern matching ``name``'s injected-class-name template-id before ``(``."""
    return re.compile(rf"(?<![\w:]){re.escape(name)}\s*{_BALANCED_ANGLES}\s*(?=\()")


@lru_cache(maxsize=4096)
def _spelling_before_call_re(spelling: str) -> re.Pattern[str]:
    """Pattern matching the bare ``spelling`` right before its parameter list."""
    return re.compile(rf"(?<![\w:]){re.escape(spelling)}(?=\s*\()")


def _slug(name: str) -> str:
    """Turn a qualified name into a filesystem-safe page stem."""
    slug = _SLUG_RE.sub("_", name).strip("_")
    return slug or "global"


def _normalize(text: str) -> str:
    """Collapse runs of blank lines and guarantee a single trailing newline."""
    return _BLANKS_RE.sub("\n\n", text).strip("\n") + "\n"


@dataclass(frozen=True)
class RenderedPage:
    """One rendered output page held in memory before it is written.

    ``stem`` is the filename without extension, ``label`` the human-readable
    toctree caption, and ``text`` the full MyST content. Keeping the content
    separate from the write lets the pipeline hash it and skip unchanged pages.
    ``top_level`` marks pages that belong in the root ``index`` toctree; under a
    hierarchical grouping (``group_by="namespace"``) only the top namespaces are
    top-level and every deeper page is reached through a parent's own toctree.
    """

    stem: str
    label: str
    text: str
    top_level: bool = True


# Record separator between dependency tokens, and unit separator within a token
# (mirrors the ``\x1f`` field separator the C++ content hash uses). Both are
# control characters that never appear in a USR, name, or content hash, so they
# delimit the key unambiguously.
_DEP_RECORD_SEP = "\x1e"
_DEP_FIELD_SEP = "\x1f"


@dataclass(frozen=True)
class PagePlan:
    """One page the renderer will emit, decoupled from running the Jinja pass.

    Splitting *which* pages exist (and what each one reads from the IR) from the
    actual render lets the incremental pipeline compute a per-page dependency
    fingerprint via :meth:`Generator.page_fingerprint` and replay the cached
    text for any page whose inputs are unchanged.

    ``render`` produces the page's MyST text on demand. ``subtree_seeds`` are the
    symbols the page renders in full (their whole child subtree is read), while
    ``shallow_seeds`` are symbols whose own row is read but whose children render
    elsewhere (the namespace node of a class-grouped namespace page). ``group``
    is set instead for a documentation-group page. ``file_scope`` mirrors the
    file id a file-grouped page renders under, so the dependency walk stays
    inside that file exactly like the render does.

    ``top_level`` marks the pages the root ``index`` toctree links (every page
    for the flat groupings; only the top namespaces for ``group_by="namespace"``).
    ``toctree`` lists the child page stems a hub page embeds in its own toctree,
    so the per-page fingerprint busts the hub when its child set changes.
    """

    stem: str
    label: str
    render: Callable[[], str]
    subtree_seeds: tuple[Symbol, ...] = ()
    shallow_seeds: tuple[Symbol, ...] = ()
    group: Group | None = None
    file_scope: int | None = field(default=None)
    top_level: bool = True
    toctree: tuple[str, ...] = ()


class Generator:
    """Render symbols from a :class:`~clangquill.store.Store` into MyST Markdown.

    ``template_dirs`` are searched before the bundled templates, so a user file
    named like a bundled one (``class.md.jinja`` …) transparently replaces it.
    """

    def __init__(  # noqa: PLR0913
        self,
        store: Store,
        *,
        template_dirs: Sequence[str | Path] | None = None,
        templates: Mapping[str, str] | None = None,
        include_undocumented: bool = True,
        comment_parser: str | None = None,
        path_base: str | Path | None = None,
    ) -> None:
        """Bind a store and build the Jinja environment with overrides first.

        ``templates`` maps a symbol kind (its lowercase :class:`SymbolKind`
        name, e.g. ``"method"``, or the bundled template stem, e.g. ``"class"``)
        to a replacement template stem. ``include_undocumented`` controls
        whether symbols lacking a documentation comment are emitted.
        ``comment_parser`` overrides the comment format (a registered name or a
        dotted import path) for every symbol. ``path_base`` is the directory
        that rendered file paths are shown relative to; ``None`` leaves the
        absolute paths libclang reports unchanged (see :meth:`_relpath`).
        """
        self.store = store
        self.include_undocumented = include_undocumented
        self.comment_parser = comment_parser
        self._path_base = str(path_base) if path_base is not None else None
        self._template_overrides = {k.lower(): v for k, v in (templates or {}).items()}
        self._documented_descendant: dict[tuple[int | None, str], bool] = {}
        # When rendering a single file's page (group_by="file"), this holds that
        # file's id so the otherwise file-agnostic child walk stays inside the
        # file: a namespace spans files, but its page-local section must only
        # show the members physically declared in the file being rendered.
        self._file_scope: int | None = None
        # Memoise the (visible) file-roots per file: file grouping asks for them
        # twice (the page gate and the render), and the result is stable for a
        # given store.
        self._file_roots_cache: dict[int, list[Symbol]] = {}
        # The stem each group page was actually planned under (see
        # :meth:`group_stem`): dedup can suffix the natural slug, and the
        # subgroup links templates render must point at the planned page.
        self._group_stems: dict[str, str] = {}
        # Lazily built by `related`; None until a record page first asks.
        self._related_by_name: dict[str, list[Symbol]] | None = None
        # Template objects (and the two partial macros below) are immutable for
        # the lifetime of the environment, but Environment.get_template — and
        # Template.module, which re-evaluates the module body on every access —
        # ran once per symbol render (~14k lookups on an eigen render). They are
        # memoised here instead; see :meth:`_template`.
        self._template_cache: dict[str, Template] = {}
        self._comment_macro: Callable[[CommentModel | None], object] | None = None
        self._fields_macro: Callable[[CommentModel], object] | None = None
        # Declarations already emitted on the page being rendered, mapping the
        # declared name to its conflict class (see :meth:`_register_declaration`).
        # Degraded extraction (a project parsed without its full include tree)
        # can mis-extract the same name several times with conflicting kinds —
        # e.g. a template function emitted as colliding ``cpp:var`` declarations
        # — and Sphinx's C++ domain crashes on such duplicates instead of
        # warning. Tracking what a page already declared lets later conflicting
        # declarations degrade to plain code blocks, so one mis-extracted symbol
        # can never take down the whole Sphinx build.
        self._page_decls: dict[str, str] = {}
        loaders: list[FileSystemLoader | PackageLoader] = []
        if template_dirs:
            loaders.append(FileSystemLoader([str(d) for d in template_dirs]))
        loaders.append(PackageLoader("clangquill", "templates"))
        self.env = Environment(
            loader=ChoiceLoader(loaders),
            autoescape=False,  # noqa: S701 - output is Markdown, not HTML; escaping would corrupt it
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
            undefined=StrictUndefined,
        )
        self._install_context()

    # -- environment wiring ---------------------------------------------------

    def _template(self, name: str) -> Template:
        """Return template ``name``, memoised for this generator's lifetime.

        The environment's loaders never change after construction, so a looked-up
        template object is stable; going through :meth:`jinja2.Environment.get_template`
        for every symbol render is pure constant-factor overhead on large renders.
        """
        template = self._template_cache.get(name)
        if template is None:
            template = self.env.get_template(name)
            self._template_cache[name] = template
        return template

    def _install_context(self) -> None:
        """Expose the helpers/globals templates rely on."""
        g = self.env.globals
        g["gen"] = self
        g["store"] = self.store
        g["SymbolKind"] = SymbolKind
        g["RefKind"] = RefKind
        g["xref"] = self.xref
        g["render_comment"] = self.render_comment
        g["field_list"] = self.field_list
        self.env.filters["relpath"] = self._relpath

    # -- relation / child queries (thin pass-throughs for templates) ----------

    def children(self, symbol: Symbol) -> list[Symbol]:
        """Return the visible direct children of ``symbol``.

        Under a file scope (file-grouped pages) children declared in other files
        are dropped, so a namespace re-opened across files only contributes the
        members that physically belong to the page being rendered.
        """
        return [c for c in self.store.children(symbol.usr) if self._visible(c)]

    def roots(self) -> list[Symbol]:
        """Return the visible top-level symbols of the database."""
        return [r for r in self.store.roots() if self._visible(r)]

    def file_roots(self, file_id: int) -> list[Symbol]:
        """Return the visible top-of-file symbols declared in ``file_id`` (memoised)."""
        cached = self._file_roots_cache.get(file_id)
        if cached is not None:
            return cached
        previous = self._file_scope
        self._file_scope = file_id
        try:
            roots = [s for s in self.store.file_roots(file_id) if self._visible(s)]
        finally:
            self._file_scope = previous
        self._file_roots_cache[file_id] = roots
        return roots

    def _visible(self, symbol: Symbol) -> bool:
        """Whether ``symbol`` should appear, honouring ``include_undocumented``.

        An undocumented container is still shown when it transitively contains a
        documented symbol, so suppressing undocumented leaves never hides the
        scope that holds documented members. Under a file scope, a symbol from a
        different file is never shown (and the descendant search likewise stays
        within the file), so a file page lists only its own declarations.
        """
        if self._file_scope is not None and symbol.file_id != self._file_scope:
            return False
        if self.include_undocumented or symbol.is_documented:
            return True
        return self._has_documented_descendant(symbol.usr)

    def _has_documented_descendant(self, usr: str) -> bool:
        key = (self._file_scope, usr)
        cached = self._documented_descendant.get(key)
        if cached is not None:
            return cached
        # Guard against pathological cycles in malformed IR.
        self._documented_descendant[key] = False
        children = self.store.children(usr)
        if self._file_scope is not None:
            children = [c for c in children if c.file_id == self._file_scope]
        result = any(child.is_documented or self._has_documented_descendant(child.usr) for child in children)
        self._documented_descendant[key] = result
        return result

    def parameters(self, symbol: Symbol) -> list[Parameter]:
        """Return the function parameters of ``symbol``."""
        return self.store.parameters(symbol.usr)

    def enumerators(self, symbol: Symbol) -> list[Enumerator]:
        """Return the enumerators of an enum ``symbol``."""
        return self.store.enumerators(symbol.usr)

    def bases(self, symbol: Symbol) -> list[Reference]:
        """Return the base-class references of ``symbol``."""
        return self.store.bases(symbol.usr)

    def friends(self, symbol: Symbol) -> list[Reference]:
        """Return the friend references of a record ``symbol``."""
        return self.store.friends(symbol.usr)

    def related(self, symbol: Symbol) -> list[Symbol]:
        r"""Return the free functions Doxygen's ``\relates`` ties to ``symbol``.

        ``\relates X`` names its class in prose rather than by USR, so the two
        are paired here by name — matched against the record's qualified name
        and, since the command is usually written unqualified, its spelling.
        The map is built once per generator: every record page consults it, and
        ``comment_fields`` is indexed by symbol rather than by field name.
        """
        if self._related_by_name is None:
            self._related_by_name = self.store.related_by_name()
        seen: set[str] = set()
        out: list[Symbol] = []
        for key in (symbol.qualified_name, symbol.spelling):
            for candidate in self._related_by_name.get(key, ()):
                if not self.include_undocumented and not candidate.is_documented:
                    continue
                if candidate.usr != symbol.usr and candidate.usr not in seen:
                    seen.add(candidate.usr)
                    out.append(candidate)
        return out

    def group_symbols(self, group: Group) -> list[Symbol]:
        """Return the member symbols of ``group`` (skipping any now absent)."""
        return self.store.group_symbols(group.id)

    def subgroups(self, group: Group) -> list[Group]:
        """Return the groups nested directly under ``group``."""
        return self.store.subgroups(group.id)

    def group_stem(self, group: Group) -> str:
        """Return the page stem used for ``group``.

        Prefers the stem :meth:`_plan_group_pages` actually assigned (which may
        carry a dedup suffix when the natural slug collided with another page),
        so the subgroup links ``group.md.jinja`` renders always match the page
        that was planned. Falls back to the natural slug when the group pages
        have not been planned by this generator (e.g. a bare
        :meth:`render_group` call).
        """
        stem = self._group_stems.get(group.id)
        return stem if stem is not None else _slug(f"group_{group.id}")

    def comment(self, symbol: Symbol) -> CommentModel | None:
        """Return the structured comment for ``symbol``, or ``None``."""
        return self.store.comment(symbol.usr, parser=self.comment_parser)

    # -- presentation helpers -------------------------------------------------

    def directive(self, symbol: Symbol) -> str:
        """Return the C++ domain directive name for ``symbol`` (e.g. ``cpp:class``)."""
        return _DIRECTIVE_FOR.get(symbol.kind, "cpp:type")

    def label(self, symbol: Symbol) -> str:
        """Return the human-readable kind label used in headings."""
        return _LABEL_FOR.get(symbol.kind, "Symbol")

    def _relpath(self, path: str) -> str:
        """Re-root an absolute file path under ``path_base`` for display.

        Backs the ``relpath`` Jinja filter used by ``file.md.jinja`` to keep the
        rendered "File" headings free of build-machine paths. With no
        ``path_base`` configured, or when ``path`` lies outside the base (the
        relative path would escape via ``..``), the path is returned unchanged.
        """
        if self._path_base is None:
            return path
        try:
            rel = os.path.relpath(path, self._path_base)
        except ValueError:  # different drives on Windows
            return path
        if rel == ".." or rel.startswith(".." + os.sep):
            return path  # outside the base — keep it absolute
        return rel.replace(os.sep, "/")

    def template_name(self, symbol: Symbol) -> str:
        """Return the ``{kind}.md.jinja`` template selected for ``symbol``.

        A ``templates`` override keyed by the kind name (e.g. ``"method"``)
        wins; failing that one keyed by the bundled stem (e.g. ``"class"``)
        applies, so an override can target a single kind or a whole family.
        """
        base = _TEMPLATE_FOR.get(symbol.kind, "variable")
        if not self._template_overrides:
            return base
        return self._template_overrides.get(symbol.kind.name.lower()) or self._template_overrides.get(base, base)

    def base_clause(self, symbol: Symbol) -> str:
        """Return the ``: public Base, …`` clause for a class, or ``""``."""
        bases = self.bases(symbol)
        if not bases:
            return ""
        parts = []
        for base in bases:
            keyword = _ACCESS_KEYWORD.get(base.access)
            spelling = base.to_spelling
            parts.append(f"{keyword} {spelling}" if keyword else spelling)
        return " : " + ", ".join(parts)

    def signature(self, symbol: Symbol) -> str:  # noqa: PLR0911
        """Return the directive argument (the text after ``{cpp:...}``).

        The argument carries the *qualified* name so the C++ domain attaches an
        out-of-line declaration to the right parent scope without any directive
        nesting.
        """
        if symbol.kind in _FUNCTION_KINDS:
            sig = _repair_split_operators(symbol.signature) if symbol.signature else f"{symbol.spelling}()"
            sig = self._strip_injected_template_id(sig, symbol)
            sig = _strip_recovery_defaults(sig)
            return self._qualify(sig, symbol)
        if symbol.kind in (SymbolKind.VARIABLE, SymbolKind.FIELD):
            return self._variable_declaration(symbol)
        if symbol.kind in (SymbolKind.TYPEDEF, SymbolKind.TYPE_ALIAS):
            target = self._underlying(symbol)
            return f"{symbol.qualified_name} = {target}" if target else symbol.qualified_name
        if symbol.kind == SymbolKind.MACRO:
            # ``signature`` is the function-like macro's ``NAME(a, b)`` (or the
            # bare name for an object-like macro).
            return symbol.signature or symbol.spelling
        if symbol.kind == SymbolKind.CONCEPT:
            # ``signature`` holds the ``template<...>`` head; the cpp:concept
            # directive wants ``template<...> Name`` (no ``= constraint``).
            head = f"{_repair_split_operators(symbol.signature)} " if symbol.signature else ""
            return head + symbol.qualified_name
        if symbol.kind in (SymbolKind.CLASS, SymbolKind.STRUCT, SymbolKind.UNION, SymbolKind.CLASS_TEMPLATE):
            # For a class template ``signature`` is the leading ``template<...>``
            # head; prepend it so the directive indexes a template object. A
            # specialization carries its argument list in ``display_name``; append
            # it so the directive names the specialization rather than collapsing
            # every specialization onto the bare template name (a duplicate).
            head = f"{_repair_split_operators(symbol.signature)} " if symbol.signature else ""
            name = symbol.qualified_name + _spec_suffix(symbol)
            return head + name + self.base_clause(symbol)
        return symbol.qualified_name

    #: Trailing C array extent(s) on a type spelling, e.g. ``[8]`` or ``[2][3]``.
    _ARRAY_EXTENT = re.compile(r"(?:\s*\[[^\]]*\])+$")

    def _variable_declaration(self, symbol: Symbol) -> str:
        """Build the ``type name`` directive argument for a variable/field.

        clang spells an array type with the extent on the type (``int[8]``), but
        a C++ declaration places it after the declarator (``int name[8]``); move a
        trailing extent across the name so the ``cpp:`` domain can parse it.

        Only a *plain* array (``T[N]``) is rearranged. Complex declarators where
        the name belongs inside parentheses -- pointer/reference to array
        (``int (*)[8]``) or an array of function pointers -- carry a ``(`` in the
        base, and splicing the name on the end would still be invalid C++; those
        are left untouched (they do not occur in the documented headers).
        """
        type_repr = symbol.type_repr.strip()
        extent = self._ARRAY_EXTENT.search(type_repr)
        if extent:
            base = type_repr[: extent.start()].strip()
            if "(" not in base:
                return f"{base} {symbol.qualified_name}{extent.group().strip()}".strip()
        return f"{type_repr} {symbol.qualified_name}".strip()

    def _strip_injected_template_id(self, signature: str, symbol: Symbol) -> str:
        """Drop the injected-class-name template-id on a ctor/dtor.

        A class-template constructor pretty-prints as ``Name<Args>(...)``; the
        ``cpp:function`` directive has no template parameter list, so the trailing
        ``<Args>`` before ``(`` is "too many template argument lists". Constructors
        and destructors never carry their own template-id, so removing the one that
        immediately precedes the call parens is safe and leaves ordinary methods
        (which are not ctor/dtor) untouched.
        """
        if symbol.kind not in (SymbolKind.CONSTRUCTOR, SymbolKind.DESTRUCTOR):
            return signature
        name = symbol.spelling.lstrip("~")
        if not name:
            return signature
        return _ctor_template_id_re(name).sub(name, signature, count=1)

    def _qualify(self, signature: str, symbol: Symbol) -> str:
        """Inject the qualified name into a bare pretty-printed signature.

        When the member belongs to a class-template specialization the parent's
        name is replaced by its specialized template-id and the specialization's
        ``template<...>`` head is prepended, so the out-of-line member declaration
        is unambiguous (every specialization's member would otherwise collide on
        the bare parent name).
        """
        if not symbol.spelling:
            return signature
        head, qualified = self._member_qualifier(symbol)
        if qualified == symbol.spelling and not head:
            return signature
        new, count = _spelling_before_call_re(symbol.spelling).subn(qualified, signature, count=1)
        if not count:
            return signature
        return f"{head}{new}" if head else new

    def _member_qualifier(self, symbol: Symbol) -> tuple[str, str]:
        """Return ``(template_head, qualified_name)`` for a member declaration.

        For a member of a class-template specialization the parent qualifier carries
        the specialization args (``ContainerFactory<CommonDenseVector<S>>::create``)
        and ``template_head`` is the parent's ``template<...>`` head so the spec
        args' parameters (``S``) are declared for the standalone out-of-line
        directive. A plain member returns ``("", qualified_name)`` (legacy
        behaviour).
        """
        qualified = symbol.qualified_name
        if not symbol.parent_usr:
            return "", qualified
        parent = self.store.symbol(symbol.parent_usr)
        if parent is None:
            return "", qualified
        suffix = _spec_suffix(parent)
        if not suffix:
            return "", qualified
        prefix = f"{parent.qualified_name}::"
        if qualified.startswith(prefix):
            qualified = f"{parent.qualified_name}{suffix}::{qualified[len(prefix) :]}"
        head = f"{_repair_split_operators(parent.signature)} " if parent.signature else ""
        return head, qualified

    def _underlying(self, symbol: Symbol) -> str:
        """Return the typedef/alias target spelling, or ``""``."""
        refs = self.store.references(symbol.usr, kind=RefKind.UNDERLYING_TYPE)
        return refs[0].to_spelling if refs else ""

    # -- cross-references -----------------------------------------------------

    def xref(self, target: str | Symbol | Reference, *, role: str = "any") -> str:
        """Return a MyST C++ domain cross-reference role for ``target``.

        ``target`` may be a name/USR string, a :class:`~clangquill.store.Symbol`,
        or a :class:`~clangquill.store.Reference`. The role links by *name* (which
        is how the C++ domain resolves ``{cpp:any}``): a USR is first resolved to
        its qualified name via the ``symbols`` table, a :class:`Reference` uses its
        target's qualified name, and a bare string is taken to be a name already.
        An *unresolved* reference (a builtin, template parameter, or out-of-TU
        type) degrades to inline code of its written spelling, since there is no
        domain object to point at.

        A bare string comes from free-form ``@see``/``@sa`` text, which may be a
        symbol name, a USR, a URL, or prose. A name (or USR) becomes a
        ``{cpp:any}`` role for the C++ domain to resolve, but a URL or prose is
        *not* valid C++ syntax, so it degrades by shape (URL -> link, prose ->
        plain text). With ``nitpicky`` off an unresolved-but-parseable role is
        silent, whereas a URL/prose role is an "Unparseable C++ cross-reference"
        warning -- hence the split.
        """
        if isinstance(target, str):
            return self._xref_string(target, role)
        to_usr = getattr(target, "to_usr", None)
        if to_usr is not None:  # a Reference
            resolved = self.store.symbol(to_usr) if to_usr else None
            if resolved is not None:
                return f"{{cpp:{role}}}`{resolved.qualified_name}`"
            # No documented target to point at (a builtin, an out-of-TU type, or
            # a befriended entity declared elsewhere): degrade to inline code so
            # the output carries no dangling cross-reference.
            return f"`{target.to_spelling}`" if target.to_spelling else ""
        name = target.qualified_name or target.spelling
        return f"{{cpp:{role}}}`{name}`" if name else ""

    def _xref_string(self, target: str, role: str) -> str:
        """Render free-form ``@see``/``@sa`` text by shape (see :meth:`xref`)."""
        text = target.strip()
        if not text:
            return ""
        # A URL -> MyST autolink (myst_url_schemes allows http/https/mailto).
        if _XREF_URL_RE.match(text):
            return f"<{text}>"
        # Multi-word prose is not a C++ name -> render verbatim so the domain
        # never tries (and fails) to parse it as a cross-reference.
        if any(c.isspace() for c in text):
            return text
        # A USR resolves to its qualified name; a plain name is left for the
        # domain to resolve. Trailing call/punctuation noise (``foo().``) is
        # stripped so the cleaned name stays parseable.
        resolved = self.store.symbol(text)
        if resolved is not None:
            return f"{{cpp:{role}}}`{resolved.qualified_name}`"
        name = _XREF_TRAILING_RE.sub("", text)
        return f"{{cpp:{role}}}`{name}`" if name else ""

    # -- comment rendering ----------------------------------------------------

    def render_comment(self, model: CommentModel | None) -> str:
        """Render the prose block of a comment (brief, detail, admonitions).

        ``None`` (an undocumented symbol) renders a clear, present placeholder
        rather than nothing so the symbol still appears in the output.
        """
        if self._comment_macro is None:
            # ``Template.module`` re-evaluates the module body on every access,
            # so the macro itself is cached, not just the template.
            self._comment_macro = self._template("partials/comment-block.md.jinja").module.body
        return str(self._comment_macro(model)).strip()

    def field_list(self, model: CommentModel | None) -> str:
        """Render the Sphinx field list (``:param:``, ``:returns:`` …) of a comment."""
        if model is None:
            return ""
        if self._fields_macro is None:
            self._fields_macro = self._template("partials/param-table.md.jinja").module.fields
        return str(self._fields_macro(model)).strip()

    # -- rendering ------------------------------------------------------------

    def render_symbol(self, symbol: Symbol, *, level: int = 1) -> str:
        """Render ``symbol`` (and, for containers, its descendants) to MyST.

        ``level`` is the Markdown heading depth used for section symbols; every
        page-root call renders at level 1 (recursive calls from the container
        templates go deeper), which is what scopes the duplicate-declaration
        registry to one page. A symbol whose declaration conflicts with one the
        page already emitted (see :meth:`_register_declaration`) renders through
        ``degraded.md.jinja`` — a plain code block instead of a ``cpp:*``
        directive — so mis-extracted duplicates cannot crash Sphinx's C++
        domain.
        """
        if level == 1:
            self._page_decls.clear()
        name = "degraded" if not self._register_declaration(symbol) else self.template_name(symbol)
        template = self._template(f"{name}.md.jinja")
        return _normalize(template.render(symbol=symbol, level=level))

    def _register_declaration(self, symbol: Symbol) -> bool:
        """Record ``symbol``'s declaration on the current page; ``False`` on conflict.

        The C++ domain crashes (``_object_hierarchy_parts`` on ``None``) when a
        page declares the same name twice with conflicting kinds or duplicates a
        non-overloadable declaration — which degraded extraction can produce.
        Function kinds share one conflict class so legitimate overloads still
        render as sibling ``cpp:function`` directives, and the registry key
        carries the specialization suffix so a class template and its
        specializations (distinct domain objects) never collide. Namespaces are
        exempt: re-declaring a namespace is ordinary C++ and safe in the domain.
        """
        if symbol.kind == SymbolKind.NAMESPACE:
            return True
        name = (symbol.qualified_name or symbol.spelling) + _spec_suffix(symbol)
        if not name:
            return True
        conflict_class = "function" if symbol.kind in _FUNCTION_KINDS else _DIRECTIVE_FOR.get(symbol.kind, "cpp:type")
        seen = self._page_decls.get(name)
        if seen is None:
            self._page_decls[name] = conflict_class
            return True
        return seen == conflict_class == "function"

    def render_file(self, source_file: SourceFile, *, level: int = 1) -> str:
        """Render every top-of-file symbol declared in ``source_file``.

        Top-of-file symbols are this file's :meth:`~clangquill.store.Store.file_roots`
        (declarations whose enclosing scope lives in another file or at global
        scope), not just global roots — otherwise a file holding only
        namespace-nested symbols would render nothing.
        """
        self._page_decls.clear()
        template = self._template("file.md.jinja")
        previous = self._file_scope
        self._file_scope = source_file.id
        try:
            symbols = self.file_roots(source_file.id)
            text = template.render(file=source_file, symbols=symbols, level=level)
        finally:
            self._file_scope = previous
        return _normalize(text)

    def render_group(self, group: Group, *, level: int = 1) -> str:
        """Render a single documentation group page (members + subgroups)."""
        self._page_decls.clear()
        template = self._template("group.md.jinja")
        return _normalize(template.render(group=group, level=level))

    def render_namespace_page(self, symbol: Symbol, members: Sequence[Symbol], *, level: int = 1) -> str:
        """Render a namespace's own page under ``group_by="class"``.

        Unlike :meth:`render_symbol`, which recurses through every child, this
        renders only ``members`` — the namespace's leaf declarations (free
        functions, typedefs, variables, enums, …). Its child classes and nested
        namespaces are emitted as separate pages, so listing them here too would
        duplicate their content.
        """
        self._page_decls.clear()
        template = self._template("namespace-page.md.jinja")
        return _normalize(template.render(symbol=symbol, symbols=members, level=level))

    def render_namespace_hub(
        self,
        symbol: Symbol | None,
        children: Sequence[tuple[str, str]],
        *,
        level: int = 1,
    ) -> str:
        """Render a namespace's navigational hub page under ``group_by="namespace"``.

        The hub carries the namespace heading and its own documentation, then a
        ``toctree`` of ``children`` (``(stem, label)`` pairs for its
        sub-namespaces, classes, per-name function pages and the grouped
        operators/types/constants pages). It renders no member bodies itself —
        those live on the linked pages — so the page stays a compact index even
        for a huge namespace. ``symbol`` is ``None`` only for the synthetic
        global scope, which is rendered by the root index rather than here.
        """
        comment = self.comment(symbol) if symbol is not None else None
        name = (symbol.qualified_name if symbol is not None else "") or "(global namespace)"
        template = self._template("namespace-hub.md.jinja")
        return _normalize(template.render(name=name, comment=comment, children=children, level=level))

    def render_member_page(self, title: str, members: Sequence[Symbol], *, level: int = 1) -> str:
        """Render a titled page listing ``members`` in full (each via its kind template).

        Backs the per-name function pages and the grouped operators/types/
        constants pages of ``group_by="namespace"``: ``title`` is the rendered
        MyST heading text (e.g. ``Function `geo::scale``` or ``Operators in
        `geo```) and each member renders one level deeper.
        """
        self._page_decls.clear()
        template = self._template("member-page.md.jinja")
        return _normalize(template.render(title=title, symbols=members, level=level))

    def render_pages(self, *, group_by: str = "symbol", reserved_stems: Sequence[str] = ()) -> list[RenderedPage]:
        r"""Render every page in memory without writing, in toctree order.

        ``group_by`` selects the page partitioning: ``"symbol"`` yields one page
        per top-level symbol, ``"file"`` one page per parsed source file,
        ``"class"`` one page per documented class/namespace (splitting a single
        colossal namespace page into one page per member class), and
        ``"namespace"`` a browsable hierarchy (index → namespaces → per-symbol
        pages; see :meth:`_plan_namespace_pages`). Pages for any
        Doxygen ``\defgroup`` groups are appended after the symbol/file/class
        pages; when there are no groups nothing is appended, so output for
        group-free projects is unchanged. ``reserved_stems`` names page stems the
        caller will write itself (the toctree index), so no symbol page can
        collide with them. The caller decides how (and whether)
        to persist each :class:`RenderedPage`, which is what lets the
        incremental pipeline skip unchanged outputs.
        """
        return [
            RenderedPage(plan.stem, plan.label, plan.render(), top_level=plan.top_level)
            for plan in self.plan_pages(group_by=group_by, reserved_stems=reserved_stems)
        ]

    def plan_pages(self, *, group_by: str = "symbol", reserved_stems: Sequence[str] = ()) -> list[PagePlan]:
        r"""Plan every page (stem, label, render thunk, dependencies) in order.

        This is the page set :meth:`render_pages` materialises, but without
        running the Jinja pass: each :class:`PagePlan` carries a ``render``
        callable and the symbols it reads. The incremental pipeline plans first,
        keys each page via :meth:`page_fingerprint`, and only calls ``render``
        for pages whose key changed — replaying cached text for the rest.

        ``reserved_stems`` are excluded from the planned stems (a symbol named
        ``index`` must not collide with the root document the caller writes).
        One ``seen`` set spans the symbol/file/class/namespace pages *and* the
        group pages, so no two planned pages can ever share a filename.
        """
        seen = {stem.casefold() for stem in reserved_stems}
        if group_by == "file":
            plans = self._plan_file_pages(seen)
        elif group_by == "class":
            plans = self._plan_class_pages(seen)
        elif group_by == "namespace":
            plans = self._plan_namespace_pages(seen)
        else:
            plans = self._plan_symbol_pages(seen)
        return plans + self._plan_group_pages(seen)

    def render_index(
        self,
        pages: Sequence[RenderedPage | PagePlan],
        *,
        toctree_maxdepth: int = 2,
    ) -> str:
        """Render the toctree index page that links ``pages`` in order.

        Only each entry's ``stem``, ``label`` and ``top_level`` are read, so a
        list of rendered pages or of unrendered :class:`PagePlan` objects works
        interchangeably. Pages flagged not ``top_level`` are omitted: under a
        hierarchical grouping they are reached through a parent's toctree, so the
        root index lists only the top namespaces rather than every page.
        """
        index = self._template("index.md.jinja")
        entries = [(p.stem, p.label) for p in pages if getattr(p, "top_level", True)]
        return index.render(pages=entries, maxdepth=toctree_maxdepth)

    def generate(
        self,
        out_dir: str | Path,
        *,
        group_by: str = "symbol",
        toctree_maxdepth: int = 2,
        root_document: str = "index",
    ) -> list[str]:
        """Render the IR into ``out_dir`` and write a toctree index.

        ``group_by`` selects the page partitioning: ``"symbol"`` writes one page
        per top-level symbol, ``"file"`` one page per parsed source file,
        ``"class"`` one page per documented class/namespace, and ``"namespace"``
        a browsable index → namespace → per-symbol hierarchy.
        ``toctree_maxdepth`` and ``root_document`` shape the generated index
        page (written as ``<root_document>.md``). Returns the page stems written
        (excluding the index), in toctree order.
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        pages = self.render_pages(group_by=group_by, reserved_stems=(root_document,))
        for page in pages:
            (out / f"{page.stem}.md").write_text(page.text, encoding="utf-8")
        (out / f"{root_document}.md").write_text(
            self.render_index(pages, toctree_maxdepth=toctree_maxdepth),
            encoding="utf-8",
        )
        return [page.stem for page in pages]

    def _plan_symbol_pages(self, seen: set[str]) -> list[PagePlan]:
        """Plan one page per visible root symbol."""
        plans: list[PagePlan] = []
        for root in self.roots():
            stem = self._unique_stem(_slug(root.qualified_name or root.spelling), seen)
            label = root.qualified_name or root.spelling
            plans.append(PagePlan(stem, label, partial(self.render_symbol, root), subtree_seeds=(root,)))
        return plans

    def _plan_file_pages(self, seen: set[str]) -> list[PagePlan]:
        """Plan one page per parsed source file that declares any symbol.

        A file qualifies on its :meth:`file_roots` (the symbols physically
        declared in it), so files whose content lives entirely inside a
        namespace opened elsewhere still get a page — they would otherwise
        vanish, since only global roots used to count.
        """
        plans: list[PagePlan] = []
        for source_file in self.store.files():
            roots = self.file_roots(source_file.id)
            if not roots:
                continue
            # The IR stores resolved (absolute) paths; page on the basename so
            # filenames stay short and do not leak the build machine layout.
            name = Path(source_file.path).name
            stem = self._unique_stem(_slug(name), seen)
            plans.append(
                PagePlan(
                    stem,
                    name,
                    partial(self.render_file, source_file),
                    subtree_seeds=tuple(roots),
                    file_scope=source_file.id,
                ),
            )
        return plans

    def _plan_class_pages(self, seen: set[str]) -> list[PagePlan]:
        """Plan one page per documented class, splitting big namespace pages.

        Where :meth:`_plan_symbol_pages` emits a single page per root symbol —
        collapsing an entire ``namespace Eigen`` into one colossal page — this
        descends *through* namespaces, emitting a page per documented record
        (class/struct/union/class template) and a page per namespace carrying
        only that namespace's own leaf members (free functions, typedefs,
        variables, enums). Each record renders in full, so its methods and
        nested types stay on the record's own page.
        """
        plans: list[PagePlan] = []
        for root in self.roots():
            self._emit_class_plans(root, plans, seen)
        return plans

    def _emit_class_plans(self, symbol: Symbol, plans: list[PagePlan], seen: set[str]) -> None:
        """Append the class-granular page plan(s) for ``symbol`` and its subtree."""
        name = symbol.qualified_name or symbol.spelling
        if symbol.kind != SymbolKind.NAMESPACE:
            # A record (or a non-container root, e.g. a global free function)
            # renders as one self-contained page.
            stem = self._unique_stem(_slug(name), seen)
            plans.append(PagePlan(stem, name, partial(self.render_symbol, symbol), subtree_seeds=(symbol,)))
            return
        # A namespace is transparent: each container child becomes its own page,
        # while its leaf members collect onto a page for the namespace itself.
        children = self.children(symbol)
        leaves = [c for c in children if c.kind not in _CONTAINER_KINDS]
        # Skip an empty, undocumented namespace shell (its classes still page),
        # but keep one that carries leaf members or its own documentation.
        if leaves or symbol.is_documented:
            label = symbol.qualified_name or "(global namespace)"
            stem = self._unique_stem(_slug(name), seen)
            # The namespace node itself is read shallowly (heading + comment); its
            # leaves render in full here, while its container children page apart.
            plans.append(
                PagePlan(
                    stem,
                    label,
                    partial(self.render_namespace_page, symbol, leaves),
                    subtree_seeds=tuple(leaves),
                    shallow_seeds=(symbol,),
                ),
            )
        for child in children:
            if child.kind in _CONTAINER_KINDS:
                self._emit_class_plans(child, plans, seen)

    def _plan_namespace_pages(self, seen: set[str]) -> list[PagePlan]:
        r"""Plan a browsable hierarchy: index → namespaces → per-symbol pages.

        Where :meth:`_plan_class_pages` still lists *every* class and namespace
        in one flat index, this builds a true tree. The root index lists only the
        top-level namespaces; each namespace gets a navigational *hub* page whose
        toctree links its sub-namespaces, one page per class/record, one page per
        free-function *name* (all overloads together), a single lumped
        *operators* page, and grouped *types* (enums/typedefs/aliases/concepts)
        and *constants* (variables/macros) pages. A single colossal flat index
        therefore becomes a navigable namespace hierarchy.
        """
        plans: list[PagePlan] = []
        # The global scope is the root index itself, so its direct entries (the
        # top namespaces and any global free symbols) are the top-level pages.
        self._emit_namespace_scope(None, self.roots(), plans, seen, top_level=True)
        return plans

    def _emit_namespace_scope(
        self,
        scope: Symbol | None,
        members: Sequence[Symbol],
        plans: list[PagePlan],
        seen: set[str],
        *,
        top_level: bool,
    ) -> list[tuple[str, str]]:
        """Append pages for ``members`` and return this scope's toctree entries.

        ``scope`` is the enclosing namespace (``None`` at global scope). The
        returned ``(stem, label)`` pairs are what the scope's own toctree links —
        the root index at global scope, or the namespace hub otherwise. Pages
        created directly here inherit ``top_level``; nested scopes recurse with
        ``top_level=False`` so only the outermost namespaces reach the index.
        """
        entries: list[tuple[str, str]] = []
        namespaces = sorted(
            (m for m in members if m.kind == SymbolKind.NAMESPACE),
            key=lambda s: s.spelling,
        )
        records = sorted(
            (m for m in members if m.kind in _RECORD_KINDS),
            key=lambda s: s.spelling,
        )
        functions = [m for m in members if m.kind in _FUNCTION_KINDS]
        types = [m for m in members if m.kind in _TYPE_LEAF_KINDS]
        constants = [m for m in members if m.kind in _CONST_LEAF_KINDS]

        for ns in namespaces:
            self._emit_namespace_hub(ns, plans, seen, top_level=top_level, entries=entries)
        for record in records:
            stem = self._unique_stem(_slug(record.qualified_name or record.spelling), seen)
            plans.append(
                PagePlan(
                    stem,
                    record.qualified_name or record.spelling,
                    partial(self.render_symbol, record),
                    subtree_seeds=(record,),
                    top_level=top_level,
                ),
            )
            entries.append((stem, record.spelling))
        self._emit_function_pages(scope, functions, plans, seen, top_level=top_level, entries=entries)
        self._emit_lumped_page("types", "Types", scope, types, plans, seen, top_level=top_level, entries=entries)
        self._emit_lumped_page(
            "constants",
            "Constants",
            scope,
            constants,
            plans,
            seen,
            top_level=top_level,
            entries=entries,
        )
        return entries

    def _emit_namespace_hub(
        self,
        ns: Symbol,
        plans: list[PagePlan],
        seen: set[str],
        *,
        top_level: bool,
        entries: list[tuple[str, str]],
    ) -> None:
        """Emit the hub page for ``ns`` (and its subtree) and link it in ``entries``.

        The subtree is planned first so the hub's toctree can list the stems its
        children received. An empty, undocumented namespace shell is dropped (it
        would render an empty toctree and nothing links to it); a documented one
        is kept as a landing page.
        """
        sub_entries = self._emit_namespace_scope(ns, self.children(ns), plans, seen, top_level=False)
        if not sub_entries and not ns.is_documented:
            return
        stem = self._unique_stem(_slug(ns.qualified_name or ns.spelling), seen)
        plans.append(
            PagePlan(
                stem,
                ns.qualified_name or ns.spelling,
                partial(self.render_namespace_hub, ns, sub_entries),
                shallow_seeds=(ns,),
                toctree=tuple(s for s, _ in sub_entries),
                top_level=top_level,
            ),
        )
        entries.append((stem, ns.spelling))

    def _emit_function_pages(  # noqa: PLR0913
        self,
        scope: Symbol | None,
        functions: Sequence[Symbol],
        plans: list[PagePlan],
        seen: set[str],
        *,
        top_level: bool,
        entries: list[tuple[str, str]],
    ) -> None:
        """Emit one page per free-function name plus one lumped operators page.

        Overloads sharing a name render together on that name's page; every free
        operator (whatever the symbol) collects onto a single ``operators`` page
        for the scope, since per-operator pages would have unreadable stems and
        a namespace can hold dozens of them.
        """
        named: dict[str, list[Symbol]] = {}
        operators: list[Symbol] = []
        for func in functions:
            if func.spelling.startswith("operator"):
                operators.append(func)
            else:
                named.setdefault(func.spelling, []).append(func)
        for name in sorted(named):
            overloads = sorted(named[name], key=lambda s: s.signature)
            qname = overloads[0].qualified_name or overloads[0].spelling
            stem = self._unique_stem(_slug(qname), seen)
            plans.append(
                PagePlan(
                    stem,
                    qname,
                    partial(self.render_member_page, f"Function `{qname}`", tuple(overloads)),
                    subtree_seeds=tuple(overloads),
                    top_level=top_level,
                ),
            )
            entries.append((stem, name))
        self._emit_lumped_page(
            "operators",
            "Operators",
            scope,
            operators,
            plans,
            seen,
            top_level=top_level,
            entries=entries,
        )

    def _emit_lumped_page(  # noqa: PLR0913
        self,
        suffix: str,
        label: str,
        scope: Symbol | None,
        members: Sequence[Symbol],
        plans: list[PagePlan],
        seen: set[str],
        *,
        top_level: bool,
        entries: list[tuple[str, str]],
    ) -> None:
        """Emit one combined page holding ``members`` (operators/types/constants).

        ``suffix`` names the page within its scope (``geo::types``) and ``label``
        is its short toctree caption (``Types``). Nothing is emitted for an empty
        ``members``, so a namespace without, say, any constants grows no page.
        """
        if not members:
            return
        members = sorted(members, key=lambda s: (s.spelling, s.signature))
        scope_name = scope.qualified_name if scope is not None else ""
        base = f"{scope_name}::{suffix}" if scope_name else suffix
        stem = self._unique_stem(_slug(base), seen)
        where = f"`{scope_name}`" if scope_name else "the global namespace"
        plans.append(
            PagePlan(
                stem,
                base,
                partial(self.render_member_page, f"{label} in {where}", tuple(members)),
                subtree_seeds=tuple(members),
                top_level=top_level,
            ),
        )
        entries.append((stem, label))

    def _plan_group_pages(self, seen: set[str]) -> list[PagePlan]:
        """Plan one page per documentation group, top-level groups first.

        Returns an empty list when the IR defines no groups, leaving output for
        group-free projects byte-identical to before.
        """
        groups = self.store.groups()
        if not groups:
            return []
        plans: list[PagePlan] = []
        # Top-level groups first, then nested ones, for a stable toctree order.
        ordered = self.store.root_groups()
        ordered_ids = {g.id for g in ordered}
        ordered += [g for g in groups if g.id not in ordered_ids]
        for group in ordered:
            stem = self._unique_stem(_slug(f"group_{group.id}"), seen)
            self._group_stems[group.id] = stem
            plans.append(PagePlan(stem, group.title or group.id, partial(self.render_group, group), group=group))
        return plans

    # -- per-page dependency fingerprint --------------------------------------

    def page_fingerprint(self, plan: PagePlan) -> str:
        """Hash everything ``plan`` reads from the IR into a render-cache key.

        The digest covers each symbol the page renders (its ``content_hash``,
        which folds in the symbol row, its parameters and its raw comment), the
        cross-references it resolves (so a renamed base class or referenced type
        busts the pages that point at it), and the enumerators/group members it
        lists. It is deliberately render-config-agnostic: the pipeline combines
        it with the render fingerprint (templates, grouping, …) for the full key,
        so this method need only track the IR data the bundled templates read.
        """
        tokens: list[str] = []
        # A hub page's text is its toctree of child stems, so a changed child set
        # (a class added/renamed, a function gained) must bust it even though the
        # namespace node itself is unchanged.
        tokens.extend(f"TOC{_DEP_FIELD_SEP}{stem}" for stem in plan.toctree)
        if plan.group is not None:
            self._collect_group_tokens(plan.group, tokens)
        else:
            visited: set[str] = set()
            previous = self._file_scope
            self._file_scope = plan.file_scope
            try:
                for symbol in plan.shallow_seeds:
                    self._collect_symbol_tokens(symbol, tokens, visited, recurse=False)
                for symbol in plan.subtree_seeds:
                    self._collect_symbol_tokens(symbol, tokens, visited, recurse=True)
            finally:
                self._file_scope = previous
        return hashlib.sha256(_DEP_RECORD_SEP.join(tokens).encode("utf-8")).hexdigest()

    def _related_tokens(self, symbol: Symbol) -> list[str]:
        r"""Return the dependency tokens for the functions ``\relates`` ties to ``symbol``.

        A record page lists them, so it depends on *their* comments and not only
        on its own. Nothing else in the dependency walk follows an incoming
        edge, and without these a `\relates` added or removed would leave the
        record's page cached and stale.
        """
        if symbol.kind not in _RECORD_KINDS:
            return []
        return [
            _DEP_FIELD_SEP.join(("L", symbol.usr, fn.usr, fn.qualified_name, fn.content_hash))
            for fn in self.related(symbol)
        ]

    def _collect_symbol_tokens(
        self,
        symbol: Symbol,
        tokens: list[str],
        visited: set[str],
        *,
        recurse: bool,
    ) -> None:
        r"""Append the dependency tokens for ``symbol`` (and, if ``recurse``, its subtree).

        Mirrors what ``render_symbol`` reads: the symbol's own content hash, its
        outgoing references (and the content hash of every resolved target, whose
        qualified name a cross-reference prints), the functions ``\relates``
        points *at* a record, and — for an enum — its enumerators. Children are walked only for the container kinds whose
        templates recurse (namespaces and records), matching the render exactly.
        """
        if symbol.usr in visited:
            return
        visited.add(symbol.usr)
        tokens.append(f"S{_DEP_FIELD_SEP}{symbol.usr}{_DEP_FIELD_SEP}{symbol.content_hash}")
        for ref in self.store.references(symbol.usr):
            tokens.append(
                _DEP_FIELD_SEP.join(
                    (
                        "R",
                        symbol.usr,
                        str(int(ref.ref_kind)),
                        str(ref.ordinal),
                        ref.to_usr,
                        ref.to_spelling,
                        str(int(ref.access)),
                    ),
                ),
            )
            if ref.to_usr:
                target = self.store.symbol(ref.to_usr)
                if target is not None:
                    tokens.append(f"T{_DEP_FIELD_SEP}{ref.to_usr}{_DEP_FIELD_SEP}{target.content_hash}")
        tokens.extend(self._related_tokens(symbol))
        if symbol.kind == SymbolKind.ENUM:
            for en in self.enumerators(symbol):
                tokens.append(
                    _DEP_FIELD_SEP.join(("N", en.usr, en.name, str(en.value), str(int(en.value_is_signed)))),
                )
                enumerator = self.store.symbol(en.usr)
                if enumerator is not None:
                    tokens.append(f"E{_DEP_FIELD_SEP}{en.usr}{_DEP_FIELD_SEP}{enumerator.content_hash}")
        if recurse and (symbol.kind == SymbolKind.NAMESPACE or symbol.kind in _RECORD_KINDS):
            for child in self.children(symbol):
                self._collect_symbol_tokens(child, tokens, visited, recurse=True)

    def _collect_group_tokens(self, group: Group, tokens: list[str]) -> None:
        """Append the dependency tokens a group page reads (heading, members, subgroups)."""
        tokens.append(_DEP_FIELD_SEP.join(("G", group.id, group.title, group.brief, group.detail)))
        # The rendered subgroup links carry the *planned* stem (which dedup may
        # suffix), so the stem is part of what this page reads.
        tokens.extend(
            f"GS{_DEP_FIELD_SEP}{sub.id}{_DEP_FIELD_SEP}{self.group_stem(sub)}" for sub in self.subgroups(group)
        )
        tokens.extend(
            f"GM{_DEP_FIELD_SEP}{member.usr}{_DEP_FIELD_SEP}{member.content_hash}"
            for member in self.group_symbols(group)
        )

    @staticmethod
    def _unique_stem(stem: str, seen: set[str]) -> str:
        """Disambiguate ``stem`` against ``seen``, recording the result.

        ``seen`` holds casefolded stems and the comparison is case-insensitive:
        ``Foo`` and ``foo`` are distinct names but the same file on a
        case-insensitive filesystem (macOS/Windows), so they must not share a
        stem or the second page silently overwrites the first.
        """
        while stem.casefold() in seen:
            stem += "_"
        seen.add(stem.casefold())
        return stem


def render_symbol(store: Store, symbol: Symbol, **kwargs: object) -> str:
    """Render a single symbol with a throwaway generator (convenience wrapper)."""
    return Generator(store).render_symbol(symbol, **kwargs)  # type: ignore[arg-type]


def generate(store: Store, out_dir: str | Path, **kwargs: object) -> list[str]:
    """Render ``store`` into ``out_dir`` with a throwaway generator (convenience wrapper)."""
    return Generator(store, **kwargs).generate(out_dir)  # type: ignore[arg-type]


__all__ = ["Generator", "PagePlan", "RenderedPage", "generate", "render_symbol"]

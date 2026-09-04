"""Shared pytest fixtures.

The generator reads the SQLite IR, so its tests can run against a database
built directly in Python — no libclang needed. To stay faithful to the real
schema, the DDL comes from the compiled core (``_core.SCHEMA_DDL``) rather
than being duplicated here, then a small but representative set of symbols is
inserted.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from clangquill import _core
from clangquill.store import AccessKind, RefKind, SymbolKind

if TYPE_CHECKING:
    from pathlib import Path


def _schema_ddl() -> str:
    """Return the IR schema DDL, straight from the compiled core.

    ``_core.SCHEMA_DDL`` is ``kSchemaDDL`` itself, so a fixture database is by
    construction the schema the writer creates -- there is nothing to keep in
    sync, and nothing to extract from a source tree that an installed wheel
    does not ship.
    """
    return _core.SCHEMA_DDL


def _build_fixture_db(path: Path) -> None:
    """Populate ``path`` with a small, documented ``geo`` namespace.

    Covers the cases the generator must handle: a base class and a derived
    class, a const method, a field, a free function with parameters and a
    ``@see`` cross-reference, an enum with enumerators, a typedef, a variable,
    and a deliberately undocumented function.
    """
    con = sqlite3.connect(path)
    try:
        con.executescript(_schema_ddl())
        con.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(_core.SCHEMA_VERSION),))
        con.execute("INSERT INTO files(id, path, sha256, size_bytes) VALUES(1, 'geo.hpp', 'deadbeef', 512)")

        def sym(  # noqa: PLR0913
            usr: str,
            parent: str,
            kind: SymbolKind,
            spelling: str,
            qname: str,
            *,
            signature: str = "",
            type_repr: str = "",
            access: AccessKind = AccessKind.NONE,
            documented: bool = True,
            line: int = 0,
        ) -> None:
            con.execute(
                "INSERT INTO symbols(usr, parent_usr, kind, spelling, qualified_name, "
                "display_name, signature, type_repr, access, is_definition, "
                "is_documented, content_hash, file_id, line) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 1, ?)",
                (
                    usr,
                    parent,
                    kind,
                    spelling,
                    qname,
                    qname,
                    signature,
                    type_repr,
                    access,
                    int(documented),
                    "hash-" + usr,
                    line,
                ),
            )

        ns = "c:@N@geo"
        shape = "c:@N@geo@S@Shape"
        circle = "c:@N@geo@S@Circle"
        area = "c:@N@geo@S@Circle@F@area"
        radius = "c:@N@geo@S@Circle@FI@radius"
        scale = "c:@N@geo@F@scale"
        color = "c:@N@geo@E@Color"
        distance = "c:@N@geo@T@Distance"
        pi = "c:@N@geo@pi"
        mystery = "c:@N@geo@F@mystery"

        sym(ns, "", SymbolKind.NAMESPACE, "geo", "geo", line=1)
        sym(shape, ns, SymbolKind.CLASS, "Shape", "geo::Shape", line=3)
        sym(circle, ns, SymbolKind.CLASS, "Circle", "geo::Circle", line=10)
        sym(
            area,
            circle,
            SymbolKind.METHOD,
            "area",
            "geo::Circle::area",
            signature="double area() const",
            type_repr="double () const",
            access=AccessKind.PUBLIC,
            line=14,
        )
        sym(
            radius,
            circle,
            SymbolKind.FIELD,
            "radius",
            "geo::Circle::radius",
            type_repr="double",
            access=AccessKind.PROTECTED,
            line=18,
        )
        sym(
            scale,
            ns,
            SymbolKind.FUNCTION,
            "scale",
            "geo::scale",
            signature="Circle scale(const Circle &c, double factor)",
            type_repr="Circle (const Circle &, double)",
            line=22,
        )
        sym(color, ns, SymbolKind.ENUM, "Color", "geo::Color", line=30)
        sym(distance, ns, SymbolKind.TYPEDEF, "Distance", "geo::Distance", type_repr="double", line=36)
        sym(pi, ns, SymbolKind.VARIABLE, "pi", "geo::pi", type_repr="const double", line=38)
        sym(
            mystery,
            ns,
            SymbolKind.FUNCTION,
            "mystery",
            "geo::mystery",
            signature="void mystery()",
            type_repr="void ()",
            documented=False,
            line=44,
        )

        con.executemany(
            "INSERT INTO enumerators(usr, enum_usr, name, value, value_is_signed, idx) VALUES(?, ?, ?, ?, 1, ?)",
            [
                (color + "@Red", color, "Red", 0, 0),
                (color + "@Green", color, "Green", 1, 1),
                (color + "@Blue", color, "Blue", 2, 2),
            ],
        )

        # Circle : public Shape  (a resolved base-class reference)
        con.execute(
            "INSERT INTO references_(from_usr, ref_kind, to_usr, to_spelling, is_resolved, access, ordinal) "
            "VALUES(?, ?, ?, 'Shape', 1, ?, 0)",
            (circle, RefKind.BASE_CLASS, shape, AccessKind.PUBLIC),
        )
        # typedef Distance -> double  (an unresolved underlying-type reference)
        con.execute(
            "INSERT INTO references_(from_usr, ref_kind, to_usr, to_spelling, is_resolved, access, ordinal) "
            "VALUES(?, ?, '', 'double', 0, ?, 0)",
            (distance, RefKind.UNDERLYING_TYPE, AccessKind.NONE),
        )

        def comment(usr: str, fields: list[tuple[str, str, str]]) -> None:
            con.execute(
                "INSERT INTO comments(symbol_usr, raw_text, format) VALUES(?, ?, 'doxygen')",
                (usr, "/// generated fixture comment"),
            )
            con.executemany(
                "INSERT INTO comment_fields(symbol_usr, name, arg, value, ordinal) VALUES(?, ?, ?, ?, ?)",
                [(usr, n, a, v, i) for i, (n, a, v) in enumerate(fields)],
            )

        comment(ns, [("brief", "", "Geometry primitives.")])
        comment(shape, [("brief", "", "Abstract base for shapes.")])
        comment(
            circle,
            [
                ("brief", "", "A circle."),
                ("detail", "", "Defined by its radius."),
                ("note", "", "The radius must be positive."),
            ],
        )
        comment(area, [("brief", "", "Compute the area."), ("returns", "", "the area in square units.")])
        comment(radius, [("brief", "", "The radius of the circle.")])
        comment(
            scale,
            [
                ("brief", "", "Return a scaled copy of a circle."),
                ("param", "c", "the circle to scale"),
                ("param", "factor", "the scale factor"),
                ("returns", "", "a new, scaled circle."),
                ("see", "", "geo::Circle"),
                # Doxygen's `\relates`, written unqualified as it usually is:
                # scale is a free function listed under Circle's documentation.
                ("relates", "", "Circle"),
            ],
        )
        comment(color, [("brief", "", "A named drawing color.")])
        comment(distance, [("brief", "", "A distance in meters.")])
        comment(pi, [("brief", "", "The circle constant.")])
        con.commit()
    finally:
        con.close()


def _build_m7_db(path: Path) -> None:
    r"""Populate ``path`` with the M7 kinds.

    Covers a class template (with a defaulted non-type parameter), a concept,
    object- and function-like macros, a struct with documented and undocumented
    friends, and a ``\defgroup`` group with members.
    """
    con = sqlite3.connect(path)
    try:
        con.executescript(_schema_ddl())
        con.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(_core.SCHEMA_VERSION),))
        con.execute("INSERT INTO files(id, path, sha256, size_bytes) VALUES(1, 'm7.hpp', 'cafef00d', 256)")

        def sym(  # noqa: PLR0913
            usr: str,
            parent: str,
            kind: SymbolKind,
            spelling: str,
            qname: str,
            *,
            signature: str = "",
            type_repr: str = "",
        ) -> None:
            con.execute(
                "INSERT INTO symbols(usr, parent_usr, kind, spelling, qualified_name, "
                "display_name, signature, type_repr, access, is_definition, "
                "is_documented, content_hash, file_id, line) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 1, ?, 1, 0)",
                (usr, parent, kind, spelling, qname, qname, signature, type_repr, "hash-" + usr),
            )

        ns = "c:@N@nn"
        box = "c:@N@nn@ST>2#T#NI@Box"
        addable = "c:@N@nn@CT@Addable"
        pi = "c:@macro@PI"
        maxm = "c:@macro@MAXM"
        pt = "c:@N@nn@S@Pt"
        helper = "c:@N@nn@F@helper"

        sym(ns, "", SymbolKind.NAMESPACE, "nn", "nn")
        sym(box, ns, SymbolKind.CLASS_TEMPLATE, "Box", "nn::Box", signature="template<typename T, int N = 4>")
        sym(addable, ns, SymbolKind.CONCEPT, "Addable", "nn::Addable", signature="template<typename T>")
        sym(pi, "", SymbolKind.MACRO, "PI", "PI", signature="PI")
        sym(maxm, "", SymbolKind.MACRO, "MAXM", "MAXM", signature="MAXM(a, b)")
        sym(pt, ns, SymbolKind.STRUCT, "Pt", "nn::Pt")
        sym(helper, ns, SymbolKind.FUNCTION, "helper", "nn::helper", signature="void helper()", type_repr="void ()")

        # Friends: one points at a documented symbol (nn::helper), one is an
        # out-of-TU entity that must degrade to inline code.
        con.executemany(
            "INSERT INTO references_(from_usr, ref_kind, to_usr, to_spelling, is_resolved, access, ordinal) "
            "VALUES(?, ?, ?, ?, ?, 0, ?)",
            [
                (pt, RefKind.FRIEND, helper, "nn::helper", 1, 0),
                (pt, RefKind.FRIEND, "", "Outsider", 0, 1),
            ],
        )

        con.executemany(
            "INSERT INTO template_parameters(owner_usr, idx, param_kind, name, type_repr, default_repr) VALUES(?, ?, ?, ?, ?, ?)",
            [
                (box, 0, 0, "T", "", ""),
                (box, 1, 1, "N", "int", "4"),
                (addable, 0, 0, "T", "", ""),
            ],
        )

        con.execute(
            "INSERT INTO groups(id, title, brief, detail, parent_group_id) VALUES('grp', 'Grouped API', 'A documented group.', '', NULL)",
        )
        con.executemany(
            "INSERT INTO group_members(group_id, member_usr, ordinal) VALUES('grp', ?, ?)",
            [(box, 0), (helper, 1)],
        )

        for usr, brief in (
            (ns, "A namespace."),
            (box, "A box."),
            (addable, "Addable types."),
            (pi, "Pi."),
            (maxm, "Max macro."),
            (pt, "A point."),
            (helper, "A helper."),
        ):
            con.execute(
                "INSERT INTO comments(symbol_usr, raw_text, format) VALUES(?, '/// fixture', 'doxygen')",
                (usr,),
            )
            con.execute(
                "INSERT INTO comment_fields(symbol_usr, name, arg, value, ordinal) VALUES(?, 'brief', '', ?, 0)",
                (usr, brief),
            )
        con.commit()
    finally:
        con.close()


def _build_uncommon_kinds_db(path: Path) -> None:
    """Populate ``path`` with the ``SymbolKind`` members no other fixture exercises.

    ``UNION``, ``TYPE_ALIAS`` and ``FUNCTION_TEMPLATE`` are all covered on the
    C++ side (``tests/cpp/fixtures/m7.hpp``), but nothing on the Python side
    ever inserts one -- so a drift in the hand-maintained mirror of this enum
    (see ``SymbolKind`` in ``store.py``) would keep passing here.
    """
    con = sqlite3.connect(path)
    try:
        con.executescript(_schema_ddl())
        con.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(_core.SCHEMA_VERSION),))
        con.execute("INSERT INTO files(id, path, sha256, size_bytes) VALUES(1, 'uncommon.hpp', 'unc', 64)")

        def sym(usr: str, kind: SymbolKind, spelling: str, *, signature: str = "", type_repr: str = "") -> None:
            con.execute(
                "INSERT INTO symbols(usr, parent_usr, kind, spelling, qualified_name, "
                "display_name, signature, type_repr, access, is_definition, "
                "is_documented, content_hash, file_id, line) "
                "VALUES(?, '', ?, ?, ?, ?, ?, ?, 0, 1, 1, ?, 1, 0)",
                (usr, kind, spelling, spelling, spelling, signature, type_repr, "hash-" + usr),
            )

        variant = "c:@U@Variant"
        handle = "c:@T@Handle"
        make = "c:@FT@>1#Tmake"

        sym(variant, SymbolKind.UNION, "Variant")
        sym(handle, SymbolKind.TYPE_ALIAS, "Handle", type_repr="int")
        sym(make, SymbolKind.FUNCTION_TEMPLATE, "make", signature="template<typename T> T make()", type_repr="T ()")

        # using Handle = int;  (an unresolved underlying-type reference, same
        # shape as geo::Distance in _build_fixture_db)
        con.execute(
            "INSERT INTO references_(from_usr, ref_kind, to_usr, to_spelling, is_resolved, access, ordinal) "
            "VALUES(?, ?, '', 'int', 0, ?, 0)",
            (handle, RefKind.UNDERLYING_TYPE, AccessKind.NONE),
        )

        for usr, brief in (
            (variant, "A union of shape payloads."),
            (handle, "An integer handle."),
            (make, "Make a default-constructed T."),
        ):
            con.execute(
                "INSERT INTO comments(symbol_usr, raw_text, format) VALUES(?, '/// fixture', 'doxygen')",
                (usr,),
            )
            con.execute(
                "INSERT INTO comment_fields(symbol_usr, name, arg, value, ordinal) VALUES(?, 'brief', '', ?, 0)",
                (usr, brief),
            )
        con.commit()
    finally:
        con.close()


def _build_multifile_db(path: Path) -> None:
    """Populate ``path`` with one namespace spanning two files.

    Models the real-world shape that ``group_by="file"`` must handle: a single
    ``app`` namespace (recorded once, against ``alpha.hpp``) re-opened in a
    second file ``beta.hpp`` that declares a class. ``app::Beta`` is therefore
    *not* a global root and its parent namespace lives in another file, so the
    file must still earn a page from its own declarations.
    """
    con = sqlite3.connect(path)
    try:
        con.executescript(_schema_ddl())
        con.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(_core.SCHEMA_VERSION),))
        con.execute("INSERT INTO files(id, path, sha256, size_bytes) VALUES(1, 'alpha.hpp', 'aa', 64)")
        con.execute("INSERT INTO files(id, path, sha256, size_bytes) VALUES(2, 'beta.hpp', 'bb', 64)")

        def sym(usr: str, parent: str, kind: SymbolKind, spelling: str, qname: str, file_id: int) -> None:  # noqa: PLR0913
            con.execute(
                "INSERT INTO symbols(usr, parent_usr, kind, spelling, qualified_name, "
                "display_name, signature, type_repr, access, is_definition, "
                "is_documented, content_hash, file_id, line) "
                "VALUES(?, ?, ?, ?, ?, ?, '', '', 0, 1, 1, ?, ?, 0)",
                (usr, parent, kind, spelling, qname, qname, "hash-" + usr, file_id),
            )

        ns = "c:@N@app"
        alpha = "c:@N@app@S@Alpha"
        alpha_run = "c:@N@app@S@Alpha@F@run"
        beta = "c:@N@app@S@Beta"
        # The namespace is recorded once, against the file libclang saw first.
        sym(ns, "", SymbolKind.NAMESPACE, "app", "app", 1)
        sym(alpha, ns, SymbolKind.CLASS, "Alpha", "app::Alpha", 1)
        # A method of Alpha shares Alpha's file: it must render under Alpha, not
        # as a separate top-of-file entry.
        sym(alpha_run, alpha, SymbolKind.METHOD, "run", "app::Alpha::run", 1)
        sym(beta, ns, SymbolKind.CLASS, "Beta", "app::Beta", 2)

        for usr in (ns, alpha, alpha_run, beta):
            con.execute(
                "INSERT INTO comments(symbol_usr, raw_text, format) VALUES(?, '/// fixture', 'doxygen')",
                (usr,),
            )
            con.execute(
                "INSERT INTO comment_fields(symbol_usr, name, arg, value, ordinal) VALUES(?, 'brief', '', ?, 0)",
                (usr, f"Doc for {usr}."),
            )
        con.commit()
    finally:
        con.close()


def _build_ns_db(path: Path) -> None:
    """Populate ``path`` with a nested namespace exercising ``group_by="namespace"``.

    ``app`` holds a sub-namespace (with its own class), a class, a free function
    with two overloads, two free operators, an enum, a typedef, and a variable —
    everything the hierarchical grouping must route to a hub toctree, per-name
    function pages, a lumped operators page, and grouped types/constants pages.
    """
    con = sqlite3.connect(path)
    try:
        con.executescript(_schema_ddl())
        con.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(_core.SCHEMA_VERSION),))
        con.execute("INSERT INTO files(id, path, sha256, size_bytes) VALUES(1, 'app.hpp', 'abc', 256)")

        def sym(  # noqa: PLR0913
            usr: str,
            parent: str,
            kind: SymbolKind,
            spelling: str,
            qname: str,
            *,
            signature: str = "",
            type_repr: str = "",
        ) -> None:
            con.execute(
                "INSERT INTO symbols(usr, parent_usr, kind, spelling, qualified_name, "
                "display_name, signature, type_repr, access, is_definition, "
                "is_documented, content_hash, file_id, line) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 1, ?, 1, 0)",
                (usr, parent, kind, spelling, qname, qname, signature, type_repr, "hash-" + usr),
            )

        app = "c:@N@app"
        sub = "c:@N@app@N@sub"
        gadget = "c:@N@app@N@sub@S@Gadget"
        widget = "c:@N@app@S@Widget"
        make1 = "c:@N@app@F@make#1"
        make2 = "c:@N@app@F@make#2"
        eq = "c:@N@app@F@operatoreq"
        shl = "c:@N@app@F@operatorshl"
        mode = "c:@N@app@E@Mode"
        size_t = "c:@N@app@T@Size"
        limit = "c:@N@app@limit"

        sym(app, "", SymbolKind.NAMESPACE, "app", "app")
        sym(sub, app, SymbolKind.NAMESPACE, "sub", "app::sub")
        sym(gadget, sub, SymbolKind.CLASS, "Gadget", "app::sub::Gadget")
        sym(widget, app, SymbolKind.CLASS, "Widget", "app::Widget")
        sym(make1, app, SymbolKind.FUNCTION, "make", "app::make", signature="Widget make()", type_repr="Widget ()")
        sym(
            make2,
            app,
            SymbolKind.FUNCTION,
            "make",
            "app::make",
            signature="Widget make(int n)",
            type_repr="Widget (int)",
        )
        sym(eq, app, SymbolKind.FUNCTION, "operator==", "app::operator==", signature="bool operator==(Widget, Widget)")
        sym(shl, app, SymbolKind.FUNCTION, "operator<<", "app::operator<<", signature="void operator<<(int, int)")
        sym(mode, app, SymbolKind.ENUM, "Mode", "app::Mode")
        sym(size_t, app, SymbolKind.TYPEDEF, "Size", "app::Size", type_repr="unsigned long")
        sym(limit, app, SymbolKind.VARIABLE, "limit", "app::limit", type_repr="const int")

        for usr, brief in (
            (app, "The app namespace."),
            (sub, "A sub-namespace."),
            (gadget, "A gadget."),
            (widget, "A widget."),
            (make1, "Make a widget."),
            (make2, "Make a widget from a count."),
            (eq, "Compare widgets."),
            (shl, "Shift ints."),
            (mode, "A mode."),
            (size_t, "A size alias."),
            (limit, "A limit."),
        ):
            con.execute(
                "INSERT INTO comments(symbol_usr, raw_text, format) VALUES(?, '/// fixture', 'doxygen')",
                (usr,),
            )
            con.execute(
                "INSERT INTO comment_fields(symbol_usr, name, arg, value, ordinal) VALUES(?, 'brief', '', ?, 0)",
                (usr, brief),
            )
        con.commit()
    finally:
        con.close()


def _build_spec_db(path: Path) -> None:
    """Populate ``path`` with class-template specializations and a template ctor.

    Models the cases that produced the dune-gdt docs warnings: a primary class
    template ``ContainerFactory`` with two partial specializations (whose
    ``display_name`` carries the specialization arguments while ``qualified_name``
    stays bare), each with a ``create`` member, plus a class template
    ``AdaptationHelper`` whose constructor pretty-prints with the injected
    template-id and ``<recovery-expr>`` default arguments.
    """
    con = sqlite3.connect(path)
    try:
        con.executescript(_schema_ddl())
        con.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(_core.SCHEMA_VERSION),))
        con.execute("INSERT INTO files(id, path, sha256, size_bytes) VALUES(1, 'spec.hpp', 'spec', 256)")

        def sym(  # noqa: PLR0913
            usr: str,
            parent: str,
            kind: SymbolKind,
            spelling: str,
            qname: str,
            *,
            display: str | None = None,
            signature: str = "",
            type_repr: str = "",
        ) -> None:
            con.execute(
                "INSERT INTO symbols(usr, parent_usr, kind, spelling, qualified_name, "
                "display_name, signature, type_repr, access, is_definition, "
                "is_documented, content_hash, file_id, line) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 1, ?, 1, 0)",
                (
                    usr,
                    parent,
                    kind,
                    spelling,
                    qname,
                    display if display is not None else qname,
                    signature,
                    type_repr,
                    "hash-" + usr,
                ),
            )

        demo = "c:@N@demo"
        cf_primary = "c:@N@demo@CT0@ContainerFactory"
        cf_dense = "c:@N@demo@CTPS1@ContainerFactory"
        cf_field = "c:@N@demo@CTPS2@ContainerFactory"
        cf_double = "c:@N@demo@CTS1@ContainerFactory"
        create_dense = cf_dense + "@F@create"
        create_field = cf_field + "@F@create"
        create_double = cf_double + "@F@create"
        helper = "c:@N@demo@CT@AdaptationHelper"
        helper_ctor = helper + "@F@AdaptationHelper"

        sym(demo, "", SymbolKind.NAMESPACE, "demo", "demo")
        # Primary template: display_name has no spec args (equals spelling).
        sym(
            cf_primary,
            demo,
            SymbolKind.CLASS_TEMPLATE,
            "ContainerFactory",
            "demo::ContainerFactory",
            display="ContainerFactory",
            signature="template<class ContainerImp>",
        )
        # Two specializations: display_name carries the spec args, qname is bare.
        sym(
            cf_dense,
            demo,
            SymbolKind.CLASS_TEMPLATE,
            "ContainerFactory",
            "demo::ContainerFactory",
            display="ContainerFactory<demo::DenseVector<S>>",
            signature="template<class S>",
        )
        sym(
            cf_field,
            demo,
            SymbolKind.CLASS_TEMPLATE,
            "ContainerFactory",
            "demo::ContainerFactory",
            display="ContainerFactory<demo::FieldVector<S, 4>>",
            signature="template<class S>",
        )
        # A *full* specialization: its head is empty (`template<>`), and unlike a
        # partial specialization that head must not repeat on its members.
        sym(
            cf_double,
            demo,
            SymbolKind.CLASS_TEMPLATE,
            "ContainerFactory",
            "demo::ContainerFactory",
            display="ContainerFactory<double>",
            signature="template<>",
        )
        sym(
            create_dense,
            cf_dense,
            SymbolKind.METHOD,
            "create",
            "demo::ContainerFactory::create",
            signature="static demo::DenseVector<S> create(const size_t size)",
            type_repr="demo::DenseVector<S> (const size_t)",
        )
        sym(
            create_field,
            cf_field,
            SymbolKind.METHOD,
            "create",
            "demo::ContainerFactory::create",
            signature="static demo::FieldVector<S, 4> create(const size_t size)",
            type_repr="demo::FieldVector<S, 4> (const size_t)",
        )
        sym(
            create_double,
            cf_double,
            SymbolKind.METHOD,
            "create",
            "demo::ContainerFactory::create",
            signature="static demo::DenseVector<double> create(const size_t size)",
            type_repr="demo::DenseVector<double> (const size_t)",
        )
        # A variable template and one specialization of it: kind VARIABLE, but
        # with a template head in ``signature`` and the argument list in the
        # display name (recovered from the declaration text by the parser).
        sym(
            "c:@N@demo@is_dense_v",
            demo,
            SymbolKind.VARIABLE,
            "is_dense_v",
            "demo::is_dense_v",
            display="is_dense_v",
            signature="template<class T>",
            type_repr="inline constexpr bool",
        )
        sym(
            "c:@N@demo@is_dense_v>#d",
            demo,
            SymbolKind.VARIABLE,
            "is_dense_v",
            "demo::is_dense_v",
            display="is_dense_v<double>",
            signature="template<>",
            type_repr="inline constexpr bool",
        )
        # Class template whose constructor carries the injected template-id and
        # <recovery-expr> default arguments clang could not evaluate.
        sym(
            helper,
            demo,
            SymbolKind.CLASS_TEMPLATE,
            "AdaptationHelper",
            "demo::AdaptationHelper",
            display="AdaptationHelper",
            signature="template<class V, class GV, class RF>",
        )
        sym(
            helper_ctor,
            helper,
            SymbolKind.CONSTRUCTOR,
            "AdaptationHelper",
            "demo::AdaptationHelper::AdaptationHelper",
            signature=(
                "AdaptationHelper<V, GV, RF>(GV &grd, "
                'const std::string &logging_prefix = <recovery-expr>(""), '
                "const std::array<bool, 3> &logging_state = <recovery-expr>())"
            ),
            type_repr="void (GV &, const std::string &, const std::array<bool, 3> &)",
        )
        # An enum nested in the *primary* template and one nested in the full
        # specialization: `cpp:enum` has no grammar for a template head, so both
        # are declared by their bare name inside a pushed scope -- which keeps
        # the specialization's argument list and drops the primary's parameter
        # names (issue #336, docs/development/cross-references.md).
        cf_mode = cf_primary + "@E@Mode"
        sym(
            cf_mode,
            cf_primary,
            SymbolKind.ENUM,
            "Mode",
            "demo::ContainerFactory::Mode",
        )
        cf_double_mode = cf_double + "@E@Mode"
        sym(
            cf_double_mode,
            cf_double,
            SymbolKind.ENUM,
            "Mode",
            "demo::ContainerFactory::Mode",
        )

        for usr, brief in (
            (demo, "Demo namespace."),
            (cf_primary, "Container factory (primary template)."),
            (cf_dense, "Container factory for dense vectors."),
            (cf_field, "Container factory for field vectors."),
            (create_dense, "Create a dense vector."),
            (create_field, "Create a field vector."),
            (cf_double, "Container factory for doubles."),
            (create_double, "Create a vector of doubles."),
            ("c:@N@demo@is_dense_v", "Whether the container is dense."),
            ("c:@N@demo@is_dense_v>#d", "Doubles are dense."),
            (helper, "Adaptation helper."),
            (helper_ctor, "Construct an adaptation helper."),
            (cf_mode, "How the factory picks a container implementation."),
            (cf_double_mode, "How the double factory picks a container implementation."),
        ):
            con.execute(
                "INSERT INTO comments(symbol_usr, raw_text, format) VALUES(?, '/// fixture', 'doxygen')",
                (usr,),
            )
            con.execute(
                "INSERT INTO comment_fields(symbol_usr, name, arg, value, ordinal) VALUES(?, 'brief', '', ?, 0)",
                (usr, brief),
            )
        con.commit()
    finally:
        con.close()


def _build_collision_db(path: Path) -> None:
    """Populate ``path`` with symbols whose page stems collide.

    ``index`` clashes with the default root document, and ``Foo``/``foo`` are
    distinct C++ names but the same filename on a case-insensitive filesystem.
    """
    con = sqlite3.connect(path)
    try:
        con.executescript(_schema_ddl())
        con.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(_core.SCHEMA_VERSION),))
        con.execute("INSERT INTO files(id, path, sha256, size_bytes) VALUES(1, 'clash.hpp', 'cc', 64)")
        for usr, name in (("c:@F@index", "index"), ("c:@F@Foo", "Foo"), ("c:@F@foo", "foo")):
            con.execute(
                "INSERT INTO symbols(usr, parent_usr, kind, spelling, qualified_name, "
                "display_name, signature, type_repr, access, is_definition, "
                "is_documented, content_hash, file_id, line) "
                "VALUES(?, '', ?, ?, ?, ?, ?, '', 0, 1, 1, ?, 1, 0)",
                (usr, SymbolKind.FUNCTION, name, name, name, f"void {name}()", "hash-" + usr),
            )
        con.commit()
    finally:
        con.close()


def _build_degraded_db(path: Path) -> None:
    """Populate ``path`` with the colliding declarations degraded extraction emits.

    Mirrors the eigen benchmark failure: a template function parsed without its
    include tree is mis-extracted as several variable declarations of the same
    qualified name (``int Eigen::plogical_shift_right`` twice), which Sphinx's
    C++ domain crashes on instead of warning. Legitimate function overloads of
    one name are included too, since those must keep rendering as directives.
    """
    con = sqlite3.connect(path)
    try:
        con.executescript(_schema_ddl())
        con.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(_core.SCHEMA_VERSION),))
        con.execute("INSERT INTO files(id, path, sha256, size_bytes) VALUES(1, 'degraded.hpp', 'dd', 64)")

        def sym(  # noqa: PLR0913
            usr: str,
            parent: str,
            kind: SymbolKind,
            spelling: str,
            qname: str,
            *,
            signature: str = "",
            type_repr: str = "",
            line: int = 0,
        ) -> None:
            con.execute(
                "INSERT INTO symbols(usr, parent_usr, kind, spelling, qualified_name, "
                "display_name, signature, type_repr, access, is_definition, "
                "is_documented, content_hash, file_id, line) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 1, ?, 1, ?)",
                (usr, parent, kind, spelling, qname, spelling, signature, type_repr, "hash-" + usr, line),
            )

        sym("c:@N@Eigen", "", SymbolKind.NAMESPACE, "Eigen", "Eigen")
        # The same mis-extracted name, twice as a variable declaration.
        sym(
            "c:degraded1",
            "c:@N@Eigen",
            SymbolKind.VARIABLE,
            "plogical_shift_right",
            "Eigen::plogical_shift_right",
            type_repr="int",
            line=1,
        )
        sym(
            "c:degraded2",
            "c:@N@Eigen",
            SymbolKind.VARIABLE,
            "plogical_shift_right",
            "Eigen::plogical_shift_right",
            type_repr="int",
            line=2,
        )
        # A third mis-extraction of the same name under a conflicting kind.
        sym(
            "c:degraded3",
            "c:@N@Eigen",
            SymbolKind.TYPEDEF,
            "plogical_shift_right",
            "Eigen::plogical_shift_right",
            type_repr="int",
            line=3,
        )
        # Legitimate overloads: same name, both must stay cpp:function directives.
        sym(
            "c:@F@scale#d#",
            "c:@N@Eigen",
            SymbolKind.FUNCTION,
            "scale",
            "Eigen::scale",
            signature="double scale(double f)",
            line=4,
        )
        sym(
            "c:@F@scale#i#",
            "c:@N@Eigen",
            SymbolKind.FUNCTION,
            "scale",
            "Eigen::scale",
            signature="int scale(int f)",
            line=5,
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def collision_db(tmp_path: Path) -> Path:
    """Return an IR database whose symbols produce colliding page stems."""
    path = tmp_path / "collision.sqlite"
    _build_collision_db(path)
    return path


@pytest.fixture
def degraded_db(tmp_path: Path) -> Path:
    """Return an IR database holding same-name conflicting declarations."""
    path = tmp_path / "degraded.sqlite"
    _build_degraded_db(path)
    return path


@pytest.fixture
def spec_db(tmp_path: Path) -> Path:
    """Return an IR database with class-template specializations and a template ctor."""
    path = tmp_path / "spec.sqlite"
    _build_spec_db(path)
    return path


@pytest.fixture
def ns_db(tmp_path: Path) -> Path:
    """Return an IR database with a nested namespace for hierarchical grouping."""
    path = tmp_path / "app.sqlite"
    _build_ns_db(path)
    return path


@pytest.fixture
def fixture_db(tmp_path: Path) -> Path:
    """Return the path to a freshly built fixture IR database."""
    path = tmp_path / "geo.sqlite"
    _build_fixture_db(path)
    return path


@pytest.fixture
def multifile_db(tmp_path: Path) -> Path:
    """Return an IR database with one namespace spanning two source files."""
    path = tmp_path / "multifile.sqlite"
    _build_multifile_db(path)
    return path


@pytest.fixture
def m7_db(tmp_path: Path) -> Path:
    """Return the path to a fixture IR database exercising the M7 kinds."""
    path = tmp_path / "m7.sqlite"
    _build_m7_db(path)
    return path


@pytest.fixture
def uncommon_kinds_db(tmp_path: Path) -> Path:
    """Return an IR database covering ``SymbolKind`` members no other fixture does."""
    path = tmp_path / "uncommon.sqlite"
    _build_uncommon_kinds_db(path)
    return path

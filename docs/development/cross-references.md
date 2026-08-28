# Cross-reference resolution

Templates render every symbol as a real Sphinx C++ domain directive
(``cpp:class``, ``cpp:function``, ``cpp:enum``, …) and every inter-symbol link
as a `{cpp:any}` role, so a link only renders if the domain actually indexes a
matching object. A member of a class template is the tricky case: it is
rendered out-of-line, on its own page, under its fully qualified name rather
than nested inside the class's own directive — so unless that qualified name
also carries the parent's `template<...>` head, the domain does not recognize
it as belonging to the template and quietly files it under a second, plain
symbol of the same (untemplated) name instead. For a function member of a
class-template *specialization*, `Generator._member_qualifier` supplies that
head (see `Generator.signature`, called from every member's directive).

That trick depends on the member's own directive grammar accepting a leading
`template<...>` head. Not every directive does.

## Known-unresolvable: an enum nested in a class template

Sphinx's `cpp:enum` directive accepts no template parameter list, so an enum
nested in a class template cannot be spelled in its real scope at all —
[issue #336](https://github.com/clangquill/clangquill/issues/336):

```cpp
namespace xr {
template <typename T>
struct Holder {
  enum class Mode { Eager, Lazy };
};
}
```

`template<typename T> xr::Holder<T>::Mode` is rejected (`Expected identifier
in nested name, got keyword: template`), and naming the arguments without a
head (`xr::Holder<T>::Mode`) fails too (`Too many template argument lists
compared to parameter lists`). The generator therefore emits the head-less
`xr::Holder::Mode` — the only spelling `cpp:enum` accepts — which documents
the enum on the page but is filed under a second, plain `Holder` symbol, so
neither `` {cpp:any}`xr::Holder::Mode` `` nor its enumerators resolve.

`Generator.signature()` deliberately leaves `SymbolKind.ENUM` out of the
member-qualification path: there is no directive spelling that would make
qualifying it worthwhile, and guessing at one only trades a silent broken
link for a build error. Do not "fix" this by prepending a template head to an
enum's directive argument.

Two things would actually resolve it, both larger than a generator change:

- An upstream `cpp:enum` that accepts a template parameter list, the same way
  `cpp:member` and `cpp:type` already do.
- Rendering the members of a class template inside a nested directive scope
  (so the enclosing `cpp:class` supplies the scope) rather than out-of-line
  under a fully qualified name — a much larger change to how the generator
  emits pages, and one that would need to apply to every member kind, not
  just enums, to stay consistent.

`tests/test_generator.py::test_enum_nested_in_class_template_has_no_qualifying_head`
pins the current head-less output so a future change to the qualification
logic has to look at this note rather than silently reintroducing an invalid
directive.

# Cross-reference resolution

**Status:** Living document · **Scope:** the `{cpp:any}` links clangquill emits
and what the Sphinx C++ domain does with them

Every inter-symbol link in the generated output is a `{cpp:any}` role over a
qualified C++ name, resolved by [Sphinx's C++
domain](https://www.sphinx-doc.org/en/master/usage/domains/cpp.html) against the
declarations clangquill emitted on the same build. Unlike a broken directive, a
link that fails to resolve is **silent**: outside `nitpicky` mode Sphinx does not
warn, it simply renders the target as plain text. A page can be warning-free and
still have every one of its links dead.

`tests/e2e/test_e2e_xrefs.py` is the gate against that. It builds
`tests/cpp/fixtures/xrefs.hpp` — class templates and their members,
specializations, a class template nested inside a class template, enums,
operators, an overload set, all of them linked to by `\ref` — with `nitpicky` on and warnings as errors, which is what
`sphinx-build -n -W` does, and requires zero unresolved references. It also
asserts that each hard target came out as a real anchor into the C++ domain, and
that a reference to a symbol nothing declares still fails the build, so the gate
cannot pass by having been switched off.

## What makes a reference resolve

Two things have to line up, and only the second is obvious.

**The target has to be the name the domain indexed.** That is the qualified
name, without template arguments: `xr::Buffer::data`, not
`xr::Buffer<T, N>::data`. Operators are indexed under their operator name
(`xr::Vec::operator[]`, `xr::operator==`), and an unscoped enumerator is indexed
both with and without its enum, so `xr::PlainRed` and `xr::Plain::PlainRed` both
work.

**The declaration has to have been filed in the right scope.** clangquill emits
every declaration out-of-line under its fully qualified name, so the domain has
to attach it to an enclosing scope it already knows. For a member of a class
template that only happens when the declaration repeats the enclosing template's
head *and* names its arguments on the qualifier:

````{code-block} markdown
:caption: what the domain files under the class template

```{cpp:member} template<typename T, int N = 4> T xr::Buffer<T, N>::data[N]
```
````

Written as `T xr::Buffer::data[N]` instead, the domain creates a *second*,
plain `Buffer` symbol and files `data` under that. The name in `objects.inv` is
identical either way, so nothing looks wrong — but `{cpp:any}` resolution walks
the symbol tree, finds the class template, and does not find `data` in it. The
`T` and `N` in the member's own type resolve to nothing as well. The same holds
for a nested class, a member type alias and a static data member, and the
qualification has to be applied at every enclosing level, not just the innermost
(`template<typename T> void xr::Outer<T>::Inner::go()`).

**An enum is the exception, and gets a pushed scope instead.** `cpp:enum` accepts
no template parameter list at all: with a head the directive is a parse error,
without one the enum lands under that second, plain symbol. `cpp:namespace-push`
*does* take a head, so clangquill pushes the enclosing scope, declares the enum
by its bare name inside it, and pops:

````{code-block} markdown
:caption: what puts the enum in the class template's scope

```{cpp:namespace-push} template<typename T> xr::Holder
```

```{cpp:enum} Mode
```

```{cpp:enumerator} Mode::Eager
```

```{cpp:namespace-pop}
```
````

Two details are load-bearing. The pushed name **drops the argument list of its
own last component** (`xr::Holder`, not `xr::Holder<T>`) when that component is a
primary template: the domain rewrites a primary's arguments to nothing before
filing it, but only for the *intermediate* components of a name, never the last,
so a trailing `<T>` would open a second symbol — the very bug being avoided. A
specialization is filed under its spelled-out name and therefore keeps its
arguments (`template<> xr::Traits<int>`). Every *enclosing* level keeps its
arguments as usual (`template<typename T> template<typename U> xr::Outer<T>::Inner`).

And the push only merges into a class **declared before it**, for the same
last-component reason in reverse. That holds because a nested enum renders on its
parent's own page, below the parent's directive; an enum whose enclosing scopes
are all plain is emitted under its qualified name as before, with no push at all.

## Known-unresolvable shapes

These are the shapes no output clangquill could emit would make resolvable. The
first two are asserted by `KNOWN_UNRESOLVABLE` in
`tests/e2e/test_e2e_xrefs.py`, which fails if the set ever changes — in either
direction.

**A namespace.** The C++ domain treats a namespace as scope, not as an object:
`cpp:namespace` declares nothing referenceable, and there is no directive that
would. So `\ref my_ns` cannot resolve, and — more visibly — the namespace
component of every qualified declaration is rendered as a `cpp:identifier`
cross-reference that finds nothing. On a nitpicky build that is one warning per
declaration. A project that builds with `-n` wants

```python
nitpick_ignore = [("cpp:identifier", "my_ns")]
```

for each documented namespace.

**A class nested in a class template, as seen from its own members.** When the
domain renders `xr::Buffer<T, N>::Cursor::advance`, it spells the enclosing
`Cursor` scope as that class's whole declaration — template head included — and
then fails to look that string up. The link *to* `xr::Buffer::Cursor` resolves
fine; it is the declaration of its members that warns, once each. The target is
the parent's declaration as libclang pretty-printed it, so match it by shape
rather than spelling out a head that moves with the LLVM version:

```python
nitpick_ignore_regex = [("cpp:identifier", r"template<.*> my_ns::\w+<.*>::\w+(?:<.*>)?")]
```

**A conversion operator, as a `\ref` target.** Doxygen's `\ref` argument ends at
the first space and `operator bool` has one, so the target can only ever be
`X::operator`, which names nothing. Rather than emit a link that would silently
die, clangquill degrades a target ending at the bare `operator` keyword to a code
span. Operators with symbolic names — `operator==`, `operator[]`, `operator()`,
`operator+=` — carry no space and do resolve.

## Where trailing punctuation stops

Doxygen prose is full of `(see \ref parse_files)`, so the renderer strips
sentence punctuation from a `\ref` argument before using it as a target.
`[]`, `()` and `,` are in that set *and* are part of `operator[]`, `operator()`
and `operator,`. The rule is therefore longest-match rather than strip-then-check:
the longest prefix of the argument that is a whole C++ name wins, and only
punctuation may follow it. `\ref Vec::operator[]` keeps its brackets;
`(see \ref parse_files)` loses its paren; `\ref Vec::operator[].` keeps the
brackets and loses the stop.

This lives in `is_cpp_name`/`split_xref_target`, implemented twice — in
`src/cpp/parser/doxygen_comment_parser.cpp` and mirrored in
`src/clangquill/comments.py` — and pinned against drift by the shared corpus case
`tests/comment_corpus/operator_xref_targets.json`, which both parsers assert.

## Tracking link health on real projects

The fixture gate above proves the shapes clangquill *can* emit resolve. It says
nothing about a real project, where a `\ref` may point at a symbol in a header
that was never parsed. `benchmarks/verify.py` reports an unresolved-reference
count per project for that — `xrefs["unresolved"]` in the JSON artifact, and the
"unresolved xrefs" column of the Markdown report: the number of `{cpp:any}`
targets in the generated pages that no generated declaration defines. It is a health signal, not a gate —
a project that documents links into third-party headers will always have some.

# Template-override guide

clangquill renders each symbol through a [Jinja2](https://jinja.palletsprojects.com/)
template, and templates are the primary customization point. The generator's
loader is a `ChoiceLoader` of *your* template directories followed by the
package's bundled `templates/`, so a file you provide with the same name
**overrides the bundled default by name** — no subclassing or registration.

## How lookup works

For a symbol, the generator picks a template stem from its kind (e.g. a
`SymbolKind.CLASS` uses `class.md.jinja`) and loads `"{stem}.md.jinja"`. Because
your directories are searched first, dropping a `class.md.jinja` into one of them
replaces just that kind.

Point the generator at your directories with `template_dirs`
(`clangquill_template_dirs` in Sphinx):

```python
from clangquill.generator import Generator
Generator(store, template_dirs=["my_templates"]).generate("docs/api")
```

## Bundled templates

| Stem | Used for kinds |
|------|----------------|
| `namespace` | namespaces |
| `class` | class, struct, union, class template |
| `function` | function, method, constructor, destructor, function template |
| `enum` | enums (and their enumerators) |
| `variable` | variables, fields |
| `typedef` | typedefs, type aliases |
| `concept` | concepts |
| `macro` | preprocessor macros |
| `group` | Doxygen `\defgroup` group pages |
| `index` | the generated toctree page |
| `file` | one page per source file (`group_by="file"`) |
| `namespace-page` | a namespace's own leaf members under `group_by="class"` |

Reusable macros live in `templates/partials/`: `signature.md.jinja`,
`param-table.md.jinja`, and `comment-block.md.jinja`.

## Targeting one kind without copying a whole family

Several kinds share a stem (struct/union reuse `class`; methods reuse
`function`). The `templates` map (`clangquill_templates`) remaps a single kind —
or a whole family — to a different stem without touching the others. Keys are
either a lowercase `SymbolKind` name (e.g. `"method"`) or a bundled stem (e.g.
`"class"`):

```python
# Only methods use my_method.md.jinja; free functions keep the default.
Generator(store, template_dirs=["my_templates"], templates={"method": "my_method"})
```

## What a template receives

Each `{stem}.md.jinja` is rendered with `symbol` (the
{py:class}`~clangquill.store.Symbol`) and `level` (the Markdown heading depth).
The `group` template receives `group` and `level` instead. These globals are
always in scope:

| Name | What it is |
|------|------------|
| `gen` | the {py:class}`~clangquill.generator.Generator` (relation/query/presentation helpers) |
| `SymbolKind`, `RefKind` | the IR enums, for `{% if symbol.kind == SymbolKind.CLASS %}` checks |
| `xref(target)` | a `{cpp:any}` cross-reference role for a USR, `Symbol`, or `Reference` (degrades to inline code when there is no documented target) |
| `render_comment(model)` | the prose block (brief, detail, admonitions) of a comment |
| `field_list(model)` | the Sphinx field list (`:param:`, `:returns:`, …) of a comment |

There is deliberately no raw `store` global: every IR query a template needs
goes through `gen`, so a `comment_parser` override (or any other generator
setting) always applies consistently. The most useful `gen` helpers are
`gen.comment(symbol)`, `gen.enumerator_comment(enumerator)`,
`gen.children(symbol)`, `gen.bases(symbol)`, `gen.friends(symbol)`,
`gen.enumerators(symbol)`, `gen.parameters(symbol)`, `gen.directive(symbol)`
(the `cpp:*`/`c:*` directive name), `gen.label(symbol)` (the heading label),
`gen.signature(symbol)` (the directive argument), and
`gen.render_symbol(child, level=...)` to recurse into a container's members.

## Custom templates and warm builds

With `clangquill_cache_dir` set, an incremental build memoises each rendered
page: a page whose symbols did not change replays its previous text instead of
running Jinja again (see [incremental builds](../usage.md#incremental-builds)).
That replay is only safe if the per-page dependency key covers everything the
templates read — and a custom template can read anything, including
`gen.roots()`, which reaches outside the page entirely. So by default **any
`template_dirs` override turns page memoisation off** and every page re-renders
on every build.

A template opts back in by declaring it, in a comment anywhere in the file:

```jinja
{# clangquill:page-cache #}
```

Declared templates are keyed on a wider fingerprint that covers every per-symbol
field the documented template context exposes: the symbol row in full (spelling,
display name, parent, documented flag, declaring file and line), its parameters,
template parameters, comment, cross-references and enumerators, plus the same
data for every symbol the page renders below it.

The declaration is a promise about the template, and it is yours to keep. It
says: *this template renders only the page's own symbols and what `gen` reaches
from them.* Do not declare a template that

- calls `gen.roots()` or `gen.file_roots(...)`, or otherwise renders symbols
  from outside the page (a global index of every class, a "see also everything
  in this namespace" section), or
- reads anything outside the IR that can change between builds — the clock, an
  environment variable, a file it opens itself.

Everything in [what a template receives](#what-a-template-receives) is otherwise
fair game. Editing the template still busts the whole cache, so a declaration
that turns out to be wrong is fixed by correcting the template.

The rule applies per build: one undeclared template file in your
`template_dirs` turns memoisation off for the whole build, and the setting is
about template *files* only — a `templates` mapping that points a kind at
another bundled stem keeps the bundled behaviour.

## Example: a minimal class template

The bundled `class.md.jinja` emits a heading, an "Inherits from"/"Friends" line,
the `cpp:class` directive, and recurses into members. A trimmed override:

````jinja
{% raw %}{% import "partials/signature.md.jinja" as sig %}
{{ "#" * level }} {{ gen.label(symbol) }} `{{ symbol.qualified_name }}`

```{{ "{" ~ gen.directive(symbol) ~ "}" }} {{ sig.directive_argument(symbol) }}

{{ render_comment(gen.comment(symbol)) }}
```
{% for child in gen.children(symbol) %}

{{ gen.render_symbol(child, level=level + 1) }}
{% endfor %}{% endraw %}
````

Keeping the `gen.directive(...)`/`sig.directive_argument(...)` line intact is
what makes the symbol a real C++ domain object; everything around it is yours to
restyle.

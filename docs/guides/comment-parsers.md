# Comment-parser override guide

clangquill stores each symbol's documentation comment **verbatim** in the IR and
parses it into a format-agnostic {py:class}`~clangquill.comments.CommentModel`
only when rendering. That parse step is pluggable, so you can support a comment
dialect other than the bundled Doxygen one — or post-process the default — by
supplying your own parser.

## The CommentModel

A `CommentModel` is a plain dataclass of structured fields the templates render:

| Field | Type | From (Doxygen) |
|-------|------|----------------|
| `brief` | `str` | `@brief` / first paragraph |
| `detail` | `list[str]` | remaining paragraphs, and `@code` / `@verbatim` blocks |
| `params`, `tparams` | `list[CommentParam]` | `@param`, `@tparam` |
| `returns` | `str` | `@return` |
| `retvals` | `list[CommentRetval]` | `@retval` |
| `throws` | `list[CommentThrow]` | `@throws` / `@exception` |
| `see`, `since`, `deprecated`, `note`, `warning`, `pre`, `post` | `list[str]` | the matching commands (`note` also takes `@remark`) |
| `invariant`, `todo`, `bug` | `list[str]` | `@invariant`, `@todo`, `@bug`, each rendered as its own admonition |
| `author`, `version`, `date` | `list[str]` | `@author` / `@authors`, `@version`, `@date` |
| `custom` | `dict[str, list[str]]` | **any unrecognized command**, keyed by its name |

`@details` and `@par` are prose: their text joins `detail`, the same as an
unmarked paragraph. A second `@brief` in one comment is joined onto the first
rather than dropped, which is what Doxygen does with it.

A `CommentParam` carries `name`, `description` and `direction`. `direction` is
Doxygen's parameter-passing attribute with the brackets removed — `"in"`,
`"out"`, `"in,out"`, or `""` when the comment did not spell one out. In the
persisted `comment_fields` projection it is prefixed onto the field's `arg` in
the form Doxygen itself writes (`[out] result`), since that table has a single
slot for a field argument; {py:func}`~clangquill.comments.model_from_fields`
splits it back off.

A `@code` or `@verbatim` block keeps its place among the prose paragraphs in
`detail`, rendered as a MyST fenced code block with its lines and relative
indentation intact — collapsing it to one line would mangle every example,
since the output target is Markdown. A `@code{.py}` attribute becomes the
fence's info string; a plain `@code` is `cpp` (the only language a
libclang-driven parser sees) and `@verbatim` gets none, being preformatted text
rather than code. Such a block is never promoted to the `brief`.

### Inline markup

Doxygen's inline commands are rewritten into the MyST that says the same thing,
in both the text of a command and in the prose: `@c x` / `@p x` become code
spans, `@b x` bold, `@e` / `@em` / `@a x` italic, and `@ref X` (optionally
`@ref X "a title"`) a `{cpp:any}` cross-reference role. A command has to start a
word, so an address like `user@b.example` is left alone, and trailing sentence
punctuation stays outside the markup. Because inline markup never opens a block,
a wrapped prose line beginning with one — `@ref Foo is the …` — stays prose
rather than becoming a command.

HTML in a comment (`<b>`, `<br>`, `<ul><li>…`) is passed through verbatim;
Markdown keeps raw inline HTML, so the emphasis and list structure survives.

The `custom` bucket is the graceful-degradation seam: a command the parser does
not recognize is never dropped — it lands in `custom["<name>"]` so a template can
still render it.

### Commands that are not resolved

`@copydoc`, `@copybrief` and `@copydetails` name another entity whose
documentation should be pulled in. Nothing in the pipeline resolves them, so
the text they promise is not in the rendered page: the command reaches
`custom["copydoc"]` and the parse records a **note**-severity diagnostic
naming the file, the line and the target. Notes never fail a
`warnings_as_errors` build — that setting is about the C++ libclang saw — but
they are counted, and `clangquill_diagnostics_log` writes them out.

Doxygen's `ALIASES` are likewise unsupported: an aliased command reaches
`custom` under the alias's own name, so a template can still render it.

### Structural commands

A few Doxygen commands name an entity rather than describe the one they are
attached to, and clangquill treats them specially.

`\class`, `\struct`, `\union`, `\enum`, `\namespace`, `\fn`, `\var` and
`\typedef` **retarget** a free-floating block onto the entity they name, which
is how a library documents a class from a block sitting some distance above it.
The entity is looked up **within the file the block was written in**, by
qualified name and then by unique suffix, filtered to the kind the command
implies. A name that matches nothing, or more than one thing — a bare `\fn`
naming an overload set — attaches nothing rather than guessing, and an entity
that carries its own comment always keeps it.

`\relates` is different: it stays on the free function it is written on and
lists that function under the named class. Because the class is usually in
another header, the pairing is made when pages are rendered rather than when the
file is parsed, so `\relates` reaches across files where the retargeting
commands do not. The bundled `class` template renders it as a
**Related functions** line.

All of these take a single name as their argument; anything after it is prose
belonging to the entity, not part of the command.

## A parser is just a callable

```python
CommentParser = Callable[[str], CommentModel]
```

It takes the raw comment text (markers included) and returns a `CommentModel`.

## Selecting a parser

A parser override is resolved (in {py:func}`clangquill.comments.resolve_override`)
from any of, in order:

1. a **registered name** — the built-in registry ships `"doxygen"`; add your own
   with {py:func}`~clangquill.comments.register_parser`;
2. a **dotted import path** to a callable, e.g. `"my_pkg.parsers:rst_parser"`;
3. the **`CLANGQUILL_COMMENT_PARSER`** environment variable (same two forms).

Wire it through whichever front end you use:

```python
# Sphinx (conf.py)
clangquill_comment_parser = "my_pkg.parsers:rst_parser"
```

```bash
clangquill build include/geo.hpp --comment-parser my_pkg.parsers:rst_parser
```

```python
# Python (a dotted path, or a name you registered — see below)
Generator(store, comment_parser="my_pkg.parsers:rst_parser").generate("docs/api")
```

## Example: register a custom parser

```python
from clangquill.comments import CommentModel, register_parser

def shouty_parser(raw: str) -> CommentModel:
    # Reuse the default and post-process, or build a CommentModel from scratch.
    from clangquill.comments import get_parser
    model = get_parser("doxygen")(raw)
    return CommentModel(brief=model.brief.upper(), detail=model.detail)

register_parser("shouty", shouty_parser)
# Now selectable as clangquill_comment_parser = "shouty"
```

Inspect what is registered with
{py:func}`~clangquill.comments.available_parsers`.

```{note}
The parser only affects *rendering*. The verbatim comment text is always
preserved in the IR, so changing parsers never requires re-parsing the C++.
```

# Usage

```{toctree}
:maxdepth: 1

examples/basic

```

## Generating MyST API docs

Once a project has been parsed into the SQLite IR, the generator renders it
into MyST Markdown that Sphinx indexes through its C++ domain:

```python
from clangquill.generator import Generator
from clangquill.store import Store

with Store.open("api.sqlite") as store:
    Generator(store).generate("docs/api")
```

This writes one page per top-level symbol plus an `index.md` toctree. Each
symbol becomes a real C++ domain object (`{cpp:class}`, `{cpp:function}`, …)
and inter-symbol links resolve through `{cpp:any}` roles.

Templates are the customization point. The Jinja environment looks up
`{kind}.md.jinja` (e.g. `class.md.jinja`, `function.md.jinja`) in your own
template directories *before* the bundled defaults, so dropping a file of the
same name overrides just that kind:

```python
Generator(store, template_dirs=["my_templates"]).generate("docs/api")
```

## Sphinx extension

For the common case you do not need to call the parser and generator yourself:
enable the bundled Sphinx extension and it runs the whole pipeline — parse →
SQLite → MyST — at build time, regenerating the pages into your source tree
before Sphinx reads them.

```python
# conf.py
extensions = ["clangquill.sphinx_ext"]  # pulls in myst_parser automatically

clangquill_input = ["../include/**/*.hpp"]
clangquill_compile_commands = "../build"  # dir holding compile_commands.json
clangquill_output_dir = "api"             # written under the Sphinx srcdir
```

`clangquill_compile_commands` is **required**: the extension parses with the
flags your build system actually uses rather than guessing them, so a `conf.py`
without it fails the build. Generate the database with
`cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON` (Meson writes one by default, and
[Bear](https://github.com/rizsotto/Bear) records one for anything else), and see
the [compile-database guide](guides/configuration.md#compile-databases) for what
happens when the file is missing or unloadable, and for how a header with no
entry of its own — the whole of a header-only library — still gets the right
flags.

Then reference the generated toctree from your root document:

````markdown
```{toctree}
api/index
```
````

Every knob is a `clangquill_*` config value mirroring a field of
`clangquill.config.Config`, including
`clangquill_defines`, `clangquill_template_dirs`, `clangquill_templates`
(per-kind overrides), `clangquill_include_undocumented`,
`clangquill_comment_parser`, `clangquill_group_by` (`symbol`, `file`, `class` or
`namespace` — the last builds a browsable index → namespace → per-symbol
hierarchy; recommended for libraries rooted in a single namespace, which would
otherwise collapse onto one giant page) and the
`clangquill_toctree_maxdepth` / `clangquill_root_document` toctree options. The
generated `cpp:` domain objects appear in `objects.inv` and cross-reference like
any hand-written page.

## Command line

The same pipeline is available standalone, which is handy for previewing output
or wiring clangquill into a non-Sphinx build:

```console
$ clangquill build include/geo.hpp -o docs/api --std c++20 -I include
Parsed 7 symbol(s) from 1 file(s).
Wrote 1 page(s) to /path/to/docs/api.
```

Run `clangquill build --help` for the full set of options, which mirror the
`clangquill_*` config values.

To gate CI on a clean parse, add `--warnings-as-errors` (exit 1 if libclang
emitted any warning or worse) and `--diagnostics-log` (the full diagnostic list,
all severities, as a file you can archive):

```console
$ clangquill build include/geo.hpp -o docs/api --std c++20 -I include \
      --warnings-as-errors --diagnostics-log build/clangquill.log
```

See [warnings as errors](guides/configuration.md#warnings-as-errors) for what
counts as an offender and why strict mode always re-parses.

## Incremental builds

Set `clangquill_cache_dir` to make rebuilds incremental. clangquill then keeps
the SQLite IR and a small bookkeeping cache in that directory between runs and:

- **skips the parse** when no input — or transitively `#include`d header —
  changed, reusing the cached IR instead of invoking libclang again;
- **re-parses only the stale translation units** when some files did change:
  the cache attributes every `#include`d file to the inputs that pull it in, so
  an edit re-parses just those inputs (in parallel) into the existing IR;
- **rewrites only the pages whose content changed**, comparing each rendered
  page against the hash recorded for the previous run; and
- **deletes pages whose symbols disappeared**, so removing a declaration removes
  its Markdown.

```python
# conf.py
clangquill_cache_dir = "_clangquill_cache"   # under the Sphinx srcdir
```

Re-running an unchanged build therefore regenerates nothing, touching one header
regenerates only the affected pages, and a removed symbol's page is cleaned up.
Without a cache directory the build is stateless: it re-parses into a throwaway
database and rewrites every page each time.

## Example notebooks statistics

```{nb-exec-table}
```

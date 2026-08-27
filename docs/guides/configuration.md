# Configuration reference

Every clangquill run — whether driven by the [Sphinx extension](../usage.md),
the `clangquill build` CLI, or the Python API — is described by a single
{py:class}`clangquill.config.Config`. The three front ends share this schema, so
they validate identically.

The field-name-to-front-end mapping is mechanical:

- **Sphinx**: a field named `output_dir` is the config value `clangquill_output_dir`.
- **CLI**: the same field is the flag `--output-dir` (`clangquill build --help`).
- **Python**: pass it as a keyword to `Config(...)`.

## Inputs

| Field | Sphinx value | Default | Description |
|-------|--------------|---------|-------------|
| `input` | `clangquill_input` | `[]` | Header/source paths (or globs) to parse, relative to the base directory (the Sphinx srcdir or CWD). |
| `compile_commands` | `clangquill_compile_commands` | `None` | Directory holding a `compile_commands.json` (the file itself is accepted too). **Required by the Sphinx extension**; optional for the CLI and the Python API. When set it supplies the compiler flags and **overrides** `std`/`include_dirs`/`defines`. Headers usually have no entry of their own; if `foo.hpp` isn't listed, clangquill falls back to the same-directory `foo.cpp`'s entry before giving up and using `std`/`include_dirs`/`defines`. See [compile databases](#compile-databases). |
| `compile_args` | `clangquill_compile_args` | `[]` | Extra compiler arguments appended verbatim when no compile database is used. |
| `include_dirs` | `clangquill_include_dirs` | `[]` | `-I` include directories. |
| `std` | `clangquill_std` | `"c++20"` | C++ standard, passed verbatim as `-std=<std>` (see note). |
| `defines` | `clangquill_defines` | `[]` | `-D` preprocessor definitions (`NAME` or `NAME=value`). |
| `clang_resource_dir` | `clangquill_clang_resource_dir` | `None` | Clang resource directory (`-resource-dir`); `None` lets clang decide. |
| `jobs` | `clangquill_jobs` | `0` | Number of threads used to parse translation units concurrently. `0` auto-detects the CPU count; `1` forces a serial parse. Has no effect on the generated output. |
| `tu_batch` | `clangquill_tu_batch` | `0` | Number of input files grouped into one libclang translation unit. Grouping amortises the dominant parse cost — re-parsing the shared `#include` closure — across the batch, which makes cold builds several times faster. `0` picks a sensible batch size; `1` parses every input as its own fully isolated translation unit. With `compile_commands` set this is an upper bound rather than the batch size: a translation unit can only be given one compiler command, so inputs are grouped by the command the database answers with first — see [batching under a compile database](#batching-under-a-compile-database). For self-contained headers the extracted IR is identical either way. Headers that are *not* self-contained (e.g. designed to be included only through an umbrella header) see the preprocessor state of the other files in their batch, which usually parses them *more* faithfully; which files those are is fixed by the input set — never by the order you list them in — but it does depend on this value, so set `1` if you need exact per-file isolation. |

## Output

| Field | Sphinx value | Default | Description |
|-------|--------------|---------|-------------|
| `output_dir` | `clangquill_output_dir` | `"api"` | Directory (under the srcdir / CWD) the generated pages are written to. |
| `template_dirs` | `clangquill_template_dirs` | `[]` | Directories searched before the bundled templates for overrides. See the [template-override guide](templates.md). |
| `templates` | `clangquill_templates` | `{}` | Per-kind template overrides, e.g. `{"class": "my_class"}`. |
| `cache_dir` | `clangquill_cache_dir` | `None` | Directory holding the persistent incremental cache. `None` disables caching (re-parse + rewrite every page each build). See [incremental builds](../usage.md#incremental-builds). |
| `include_undocumented` | `clangquill_include_undocumented` | `True` | Emit pages/sections for symbols that carry no documentation comment. |
| `comment_parser` | `clangquill_comment_parser` | `None` | Comment-parser override: a registered name or a dotted import path. See the [comment-parser guide](comment-parsers.md). |
| `group_by` | `clangquill_group_by` | `"symbol"` | How to partition output pages: `"symbol"` (one page per top-level symbol), `"file"` (one page per parsed source file), `"class"` (one page per documented class/namespace — descends through namespaces so a single huge namespace becomes one page per member class, keeping Sphinx's C++ domain resolver fast and giving each class its own URL), or `"namespace"` (a browsable index → namespace → per-symbol hierarchy). For namespace-rooted libraries — everything under one root namespace — prefer `"namespace"` or `"class"`: with `"symbol"`, the single root collapses the entire subtree onto one page, which renders slowest, serialises Sphinx's read phase (one giant document cannot be parsed in parallel), and is re-rendered on every symbol change. Splitting it into balanced pages parallelises the Sphinx build and keeps incremental rebuilds proportional to the edit. |
| `path_base` | `clangquill_path_base` | `None` | Directory (resolved against the srcdir / CWD) that the file paths in generated "File" headings are shown relative to. `None` keeps the absolute paths libclang reports, which leak the build-machine layout; set e.g. the project root for stable, reproducible headings. Files outside the base keep their absolute path. |
| `diagnostics_log` | `clangquill_diagnostics_log` | `None` | Path (resolved against the srcdir / CWD) of a plain-text file receiving **every** libclang diagnostic of the run. `None` disables it. See [the diagnostics log](#the-diagnostics-log). |
| `warnings_as_errors` | `clangquill_warnings_as_errors` | `False` | Fail the run when the parse produced any diagnostic of **warning** severity or worse. Off by default; turn it on in CI. See [warnings as errors](#warnings-as-errors). |

## The diagnostics log

By default a build reports only what it must: libclang's **error**-severity
diagnostics, as Sphinx warnings under the `clangquill.parse` subtype. Warnings,
remarks, and the `note:` follow-ups attached to an error — usually the half that
explains it — are dropped before they ever reach Python.

Set `clangquill_diagnostics_log` to get all of it in a file:

```python
clangquill_diagnostics_log = "_build/clangquill-diagnostics.log"
```

```text
# clangquill diagnostics
# generated: 2026-08-05T09:12:44+00:00
# inputs: 42 file(s)
# parse: full
# totals: 3 note(s), 17 warning(s), 2 error(s)

/src/foo.hpp:12:5: error: unknown type name 'Widget'
  /src/bar.hpp:3:1: note: 'Widget' declared here

/src/baz.hpp:8:9: warning: unused variable 'n' [-Wunused-variable]
```

**The console is unaffected.** Errors still go out as `clangquill.parse`
warnings exactly as before, and are still silenced by
`suppress_warnings = ["clangquill.parse"]`; everything the log adds goes only to
the file. A header full of warnings therefore cannot fail a `-W` build just
because you switched the log on.

Things worth knowing:

- **The file is rewritten each build, not appended to.** It is a snapshot of one
  run — the `generated` header line is how you tell whether it is current.
- **Enabling the option forces one full re-parse**, because a cached parse has
  no diagnostics left to replay. So does moving it to a path that does not exist
  yet, or deleting the log and rebuilding — the contents can only come from a
  parse. Once the file is there, rebuilds are incremental again.
- **With `cache_dir` set, an incremental build logs only the translation units
  it actually re-parsed**, and says so in the header
  (`parse: incremental — 3 of 42 translation unit(s) re-parsed`). A fully cached
  build re-parses nothing and leaves the previous log in place rather than
  truncating it to silence. Leave `cache_dir` unset for a complete log on
  every build.
- **The set of diagnostics depends on `tu_batch`.** Batched inputs share one
  translation unit, so a header that is not self-contained sees the preprocessor
  state of the others in its batch. Which files share a batch is a function of
  the input set — inputs are parsed in a canonical order, so listing them
  differently cannot change the result — but changing `tu_batch` regroups them.
  Errors are stable; warnings can shift between configurations or machines.
- Put the log outside `output_dir` — `_build/` is a good spot, since it is
  usually already ignored by version control.

### When an input cannot be parsed at all

A `failed to parse` entry is a different animal from an ordinary diagnostic:
libclang refused to build a translation unit for the file, and in that case it
hands back **no diagnostics whatsoever** — the driver's own complaints die with
the half-built unit and cannot be reached through the C API. A bare "failed to
parse" line would therefore be all the log could ever say about the files that
need explaining most.

So clangquill reconstructs the diagnosis and nests it under the failure:

```text
failed to parse: /src/oasys/core/cmake/compiler_info.h: libclang created no translation unit (CXError_ASTReadError)
  note: libclang reports no diagnostics when it cannot create a translation unit; the notes below are clangquill's diagnosis
  note: argument '/src/benchmarks/other.cpp' names a second input file; libclang creates no translation unit for a command with more than one input
  note: clang arguments (from the compilation database): --driver-mode=g++ -std=c++20 -DOASYS_CORE=1 -c /src/benchmarks/other.cpp -xc++
  note: re-parsed with '-std=c++20 -xc++' to recover libclang's own diagnostics; they describe the file under those flags, not under the project's build:
    /src/oasys/core/cmake/compiler_info.h:2:10: error: 'oasys/core/generated_config.h' file not found
```

The notes cover, in order:

- the exact `CXErrorCode` libclang returned;
- whether the input is missing, unreadable, or a directory;
- any argument that names a **second** input file — libclang builds no unit for
  a command with more than one input, which is how a `compile_commands.json`
  entry usually breaks a header parse;
- the full argument list, and whether it came from the compilation database or
  from the `std`/`include_dirs`/`defines` options;
- when the flags came from the database, the diagnostics from a re-parse under
  clangquill's own flags. That second parse exists purely to make the
  compiler's account of the file reachable — its results are never used for
  documentation, and it is skipped when the file is missing or when the failing
  flags already were the fallback ones (the retry would be the same command).
  If it reports nothing, the file is fine on its own and the flags are what
  libclang rejected.

Only the `failed to parse` line itself is a Sphinx warning; the notes go to the
log alone.

Umbrella batching has its own version of this: a member file libclang never
opened is reported as `failed to parse: … libclang never opened this file while
parsing its umbrella translation unit`, with the `#include` failure itself
logged against the batch's synthetic main file.

### Suppressing warnings

Every warning the extension emits carries a subtype, so a `-W` build can
silence one class without silencing the rest:

| `suppress_warnings` entry | Silences |
|---------------------------|----------|
| `clangquill.parse` | libclang's error-severity parse diagnostics. |
| `clangquill.config` | A `clangquill_*` name in `conf.py` matching no option (usually a typo). |
| `clangquill.paths` | A `clangquill_input` or `clangquill_include_dirs` entry that does not exist on disk. |
| `clangquill.libclang` | The core was built without libclang, so generation was skipped. |

Suppressing `clangquill.parse` discards those diagnostics entirely — pair it
with `clangquill_diagnostics_log` to keep the detail on disk while keeping the
build output clean.

## Warnings as errors

The diagnostics log tells you what happened; `warnings_as_errors` decides
whether it should have. Turn it on and any diagnostic of **warning** severity or
worse ends the run:

```python
clangquill_warnings_as_errors = True
```

```console
$ clangquill build include/geo.hpp -o docs/api --std c++20 -I include --warnings-as-errors
Parsed 42 symbol(s) from 3 file(s).
Wrote 12 page(s) to docs/api.
  include/geo.hpp:4:2: warning: "geo is on its way out" [-W#warnings]
Parse produced 1 warning(s) — failing because --warnings-as-errors is set.
$ echo $?
1
```

Things worth knowing:

- **The pages are written first.** The check is a verdict on the parse, not an
  abort, so a failing run still leaves its output behind to inspect.
- **It is not a `suppress_warnings` entry.** In Sphinx the failure is an
  `ExtensionError` naming every offender, not another `clangquill.parse`
  warning — the setting is opt-in, so silencing it again through the back door
  would serve nobody. A build with `warnings_as_errors` off behaves exactly as
  it always has: a header full of warnings still cannot fail a `-W` build.
- **Notes are not offenders.** A `note:` is the explanatory chain hanging off a
  diagnostic; the diagnostic it explains is what fails the build.
- **It re-parses everything, every run.** A verdict on the whole input set can
  only come from a parse of the whole input set: a cached build has no
  diagnostics at all, and an incremental one has them only for the translation
  units it re-parsed, so a warning in an untouched header would go unseen.
  `warnings_as_errors` therefore ignores `cache_dir` for the parse — leave it
  off for the edit-rebuild loop and turn it on in CI.
- **The set of warnings depends on `tu_batch`**, for the reason given
  [above](#the-diagnostics-log). Pin `tu_batch = 1` if you need a verdict that
  cannot shift with batch composition.

## Compile databases

A `compile_commands.json` records the exact command line the build system uses
for each translation unit. Handing clangquill that file — rather than a
hand-maintained `std`/`include_dirs`/`defines` triple — is what makes the parse
see the same code the compiler sees.

**The Sphinx extension requires it.** A `conf.py` with `clangquill_input` set but
no `clangquill_compile_commands` fails the build:

```text
Extension error:
clangquill: clangquill_compile_commands is not configured — the Sphinx extension
requires a compilation database. Set it to the directory holding your
compile_commands.json (e.g. a CMake build tree configured with
-DCMAKE_EXPORT_COMPILE_COMMANDS=ON), or drop clangquill_input to disable
generation.
```

Guessed flags do not fail loudly: they parse *something*, just not the same
thing the compiler sees, and the difference surfaces as missing or subtly wrong
API pages. The CLI and the Python API still accept the manual flags, which is
useful for previewing a project that has no database yet.

Most build systems can emit one:

```bash
cmake -S . -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON   # CMake
meson setup build                                        # Meson writes one by default
bear -- make                                             # anything else, via Bear
```

Then point at the directory that holds it (pointing at the JSON file itself
works too):

```python
# conf.py
clangquill_compile_commands = "../build"
```

Relative values resolve against the Sphinx srcdir (the CWD for the CLI).

### When the database cannot be loaded

libclang reports a database it cannot open exactly like it reports "no entry for
this file" — by handing back no flags — which would silently degrade into the
`std`/`include_dirs`/`defines` fallback. clangquill checks the database itself
instead, and a missing, unreadable, malformed or empty one is an error naming
every path that was searched:

```text
clangquill: clangquill_compile_commands='build' does not point at a
compile_commands.json (relative paths resolve against /home/me/project/docs);
looked for:
  /home/me/project/docs/build/compile_commands.json
```

If libclang still rejects a database that passed those checks, the parse
carries on with the fallback flags and reports a diagnostic naming the same
path, so the degraded run is visible rather than silent.

### Headers with no entry of their own

A compile database lists translation units (`.cpp`), never the headers they
include, so most documented headers have no entry. libclang fills the gap
itself: the database it hands back is wrapped in an *interpolating* one, which
answers a lookup for an unlisted file with the command of the closest listed
file — matched on path segments, stem and extension — and the filename
substituted for that entry's own.

In practice that means a header is parsed with the include directories, defines
and standard of whichever translation unit the build system compiles nearest to
it. **Header-only libraries work without special setup**: a library whose only
translation units are its tests gets its tests' flags, which are the flags those
headers are meant to be read with.

Interpolated flags are still a guess — the closest translation unit may define
something this header does not want, or miss an include directory only some
other target passes. clangquill reports the substitution as a warning naming
both files, so it shows up in the [diagnostics log](#the-diagnostics-log) and
fails a build run with `warnings_as_errors`:

```text
no compilation database entry for 'include/geo/api.hpp'; libclang supplied the
command of another file instead. Its include directories and defines may not
match this file.
```

Generating a database that lists the headers themselves — as `docs/conf.py` in
this repository does — is how to replace that guess with exact flags. It is an
accuracy improvement, not a prerequisite.

(batching-under-a-compile-database)=

### Batching under a compile database

Umbrella batching (`tu_batch`) parses several inputs as one translation unit so
the shared `#include` closure is lexed once instead of once per input. One unit
can only be handed one compiler command, so inputs are grouped by the command
the database answers with, and only inputs that agree on it share a unit.

On a CMake project the flag sets are per target, not per file, so the headers of
one target normalise to one command and the whole target batches together — a
handful of groups over thousands of headers. A header whose flags are genuinely
unique is parsed on its own, exactly as `tu_batch = 1` would.

The commands are compared after the adjustments described
[below](#how-an-entrys-command-line-is-replayed) — without argv[0], without the
source operand and without the flags that would write a file — so two headers
that borrowed the same entry compare equal even though libclang interpolated it
separately for each of them.

Two consequences worth knowing:

- Each header still gets its own "no compilation database entry" warning when it
  borrowed its flags, whether or not it was the member the batch's command was
  looked up for.
- Headers that are not self-contained now see their batch-mates' preprocessor
  state under a compile database too, the same way they already did without one.
  `tu_batch = 1` remains the way to ask for exact per-file isolation.

(how-an-entrys-command-line-is-replayed)=

### How an entry's command line is replayed

An entry's arguments are handed to libclang almost verbatim. Five adjustments
are made. Three exist because libclang appends arguments of its own — the
source file and `-fsyntax-only` — after whatever it is given:

- **The source operand is dropped**, however the database spells it (relative to
  the entry's `directory`, or with unresolved `..` segments). Leaving it in
  gives the driver two inputs, and a command with more than one input yields no
  translation unit at all.
- **A `--` separator is dropped.** Past it the driver reads every token as a
  file name, so the arguments appended afterwards become inputs
  (`error: no such file or directory: '-fsyntax-only'`) and, again, no
  translation unit is built. CMake writes exactly this shape for the header-set
  verification targets it generates
  (`c++ … -c -x c++-header … -- <header>`), which are often the only entries a
  documented header has. The operand the separator protected is the source
  file, dropped above and re-supplied by libclang, so nothing is left for it to
  separate.
- **The language is supplied only when the entry names none itself.** An entry
  carrying its own `-x` has already said what the file is, and `-x` applies to
  the inputs after it while libclang appends the source last, so anything
  clangquill added would override it. Otherwise `-xc++-header` is appended for a
  header — anything with a header extension (`.h`, `.hpp`, `.hh`, `.hxx`,
  `.inc`, `.ipp`, …) or no extension at all, the spelling the standard library
  uses — and `-xc++` for a translation unit.

  `c++-header` rather than `c++` because under `c++` a header's own
  `#pragma once` is in the main file, which clang reports
  (`[-Wpragma-once-outside-header]`) and a project's own `-Werror` turns into an
  error on a header that compiles cleanly. Nearly every documented header is
  affected: it has no entry of its own, so it borrows an interpolated command
  from a `.cpp`, which of course names no language.

The fourth is about where the entry's own paths point:

- **The entry's `directory` is replayed as `-working-directory`.** A
  `compile_commands.json` entry may spell its flags relative to that directory —
  the format allows it, and `-Iinclude` is a common way to write one. A build
  system runs the command from there; libclang does not `chdir`, so without
  this clang would resolve those paths against whatever directory the docs build
  runs in. An `-I` that does not resolve is not a loud failure for a header: it
  parses, and the declarations that needed the missing include quietly go
  missing from the output. It is prepended, so an entry carrying its own
  `-working-directory` still wins — clang takes the last one.

And the fifth is about what a parse may do to your disk:

- **Everything that writes a file is dropped** — `-o`, the `-M` dependency-list
  family (`-MD`, `-MF`, `-MT`, …) and `--serialize-diagnostics`. A parse is not
  a build: `-fsyntax-only` means those outputs are never legitimately produced,
  and clang reports them as `-Wunused-command-line-argument` anyway. Left in
  they are actively harmful, because the entry a header borrows is not its own:
  every documented header inherits one entry's `-MF` path and the parse threads
  race to write it — and a relative path resolves against the *process*
  directory (the Sphinx srcdir), not the entry's `directory`, so the files land
  next to your sources.

## Toctree / root

| Field | Sphinx value | Default | Description |
|-------|--------------|---------|-------------|
| `toctree_maxdepth` | `clangquill_toctree_maxdepth` | `2` | `:maxdepth:` of the generated root toctree. |
| `root_document` | `clangquill_root_document` | `"index"` | Stem of the generated index/toctree page within `output_dir`. |

```{note}
Doxygen `\defgroup` groups, when present, add one page per group after the
symbol/file/class pages; the toctree picks them up automatically.
```

```{note}
**Newer standards (C++23 / C++26).** Whatever `std` you set is handed straight
to clang, so any spelling clang accepts works — `c++17`/`c++20`/`c++23`/`c++26`,
the `c++2b`/`c++2c` aliases, and the GNU-extension variants `gnu++23`/`gnu++26`.
*Which* of them actually parse depends on the libclang the wheel was built
against: `c++23` needs clang ≥ 17 and full `c++26` a recent clang (the published
wheels target libclang 18+). No validation happens up front — an unsupported
spelling surfaces as a clang diagnostic during parsing.
```

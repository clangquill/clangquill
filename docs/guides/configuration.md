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
| `compile_commands` | `clangquill_compile_commands` | `None` | Directory holding a `compile_commands.json`. When set it supplies the compiler flags and **overrides** `std`/`include_dirs`/`defines`. Headers usually have no entry of their own; if `foo.hpp` isn't listed, clangquill falls back to the same-directory `foo.cpp`'s entry before giving up and using `std`/`include_dirs`/`defines`. |
| `compile_args` | `clangquill_compile_args` | `[]` | Extra compiler arguments appended verbatim when no compile database is used. |
| `include_dirs` | `clangquill_include_dirs` | `[]` | `-I` include directories. |
| `std` | `clangquill_std` | `"c++20"` | C++ standard, passed verbatim as `-std=<std>` (see note). |
| `defines` | `clangquill_defines` | `[]` | `-D` preprocessor definitions (`NAME` or `NAME=value`). |
| `clang_resource_dir` | `clangquill_clang_resource_dir` | `None` | Clang resource directory (`-resource-dir`); `None` lets clang decide. |
| `jobs` | `clangquill_jobs` | `0` | Number of threads used to parse translation units concurrently. `0` auto-detects the CPU count; `1` forces a serial parse. Has no effect on the generated output. |
| `tu_batch` | `clangquill_tu_batch` | `0` | Number of input files grouped into one libclang translation unit. Grouping amortises the dominant parse cost — re-parsing the shared `#include` closure — across the batch, which makes cold builds several times faster. `0` picks a sensible batch size; `1` parses every input as its own fully isolated translation unit. Forced to `1` when `compile_commands` is set (per-file flags cannot share a unit). For self-contained headers the extracted IR is identical either way; headers that are *not* self-contained (e.g. designed to be included only through an umbrella header) see the preprocessor state of earlier files in their batch, which usually parses them *more* faithfully — set `1` if you need exact per-file isolation. |

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

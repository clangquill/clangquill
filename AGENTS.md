# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.

## What this is

clangquill parses Doxygen-documented C++ with libclang and renders MyST Markdown
API pages for Sphinx. It is a **hybrid package**: a compiled C++ core
(`clangquill._core`, nanobind + scikit-build-core) does the parsing and writes a
SQLite IR; pure-Python code reads that IR and renders it.

## Commands

```bash
uv sync --extra dev                 # dev env (builds the C++ core; editable via scikit-build-core)
uv run pytest                       # Python suite (coverage flags come from pyproject addopts)
uv run pytest tests/test_generator.py::test_name   # single test
make cpp-test                       # configure+build+ctest the Catch2 suite in build-cpp/
make format                         # ruff format + ruff check --fix
uvx pre-commit run --all-files      # full lint gate (ruff, yamlfix, actionlint, markdownlint)
uv run make -C docs html            # docs build; docs/conf.py self-hosts clangquill on src/cpp
uv run clangquill build include/x.hpp -o out --std c++20 -I include
```

Editable installs do **not** auto-rebuild the extension: after touching
`src/cpp/**`, run `uv sync --reinstall-package clangquill` (or `make cpp-test`
if you only need the Catch2 binary) before the Python tests see the change.
`store.py` and `comments.py` read `_core` attributes at import, so a stale
extension fails collection; `tests/conftest.py` says so rather than letting it
look like a broken checkout.

Regenerating golden output when a render change is intentional:

```bash
CLANGQUILL_REGEN_GOLDENS=1 uv run pytest tests/test_generator.py
```

## Architecture

The pipeline is one path with three front-ends — `sphinx_ext.py`, `cli.py`, and
the Python API (`Generator` + `Store`) all funnel into `pipeline.build()`, so
behaviour must not diverge between them.

```text
C++ headers → _core.parse_*_to_sqlite (libclang) → SQLite IR → Store (read) → Generator (Jinja) → MyST pages
```

- **`src/cpp/`** — `comment/` (the raw Doxygen scanner and the `comment_fields`
  codec, both free of libclang so they build into every configuration),
  `parser/` (libclang AST visit, compile-db handling, the CXComment merge on top
  of the raw scanner), `store/` (SQLite writer; `schema.hpp` holds `kSchemaDDL`
  and `kSchemaVersion`), `hash/` (SHA-256 + per-symbol `content_hash` driving
  incremental rebuilds), `bindings/module.cpp` (the entire Python surface of the
  core — `have_libclang`, `libclang_version`, `parse_to_sqlite`,
  `parse_tus_to_sqlite`, `parse_tu_to_sqlite`, `parse_doxygen_comment`,
  `split_param_arg`, `split_xref_target`, `comment_fields_roundtrip`,
  `SCHEMA_VERSION`,
  `SCHEMA_DDL`, `SYMBOL_KINDS`, `ACCESS_KINDS`, `STORAGE_KINDS`, `REF_KINDS`,
  `TEMPLATE_PARAM_KINDS`, `CONTENT_HASH_FIELDS`, `COMMENT_FIELDS`). Everything
  but the three parse entry points works in the stub backend.
- **`src/clangquill/store.py`** — read-only typed layer over the IR via stdlib
  `sqlite3`. Queries evolve without recompiling the core.
- **`src/clangquill/generator.py`** — the customization point. A Jinja
  `ChoiceLoader` puts user `template_dirs` ahead of the bundled
  `templates/{kind}.md.jinja`, so a user file overrides one kind by name.
  Templates emit real Sphinx C++ domain directives; inter-symbol links go
  through `Generator.xref` and the `references` table. `group_by`
  (`symbol|file|class|namespace`) selects a `_plan_*_pages` strategy.
- **`src/clangquill/pipeline.py`** — orchestration and the incremental logic:
  parse-skip on an unchanged fingerprint, per-translation-unit re-parse of only
  stale TUs, per-page render memoisation keyed on symbol content hashes plus a
  render fingerprint, manifest-based pruning of vanished pages. Custom templates
  disable per-page memoisation (they may read IR the key does not track), and
  `warnings_as_errors` opts out of the parse cache entirely.
- **`src/clangquill/cache.py`** — the bookkeeping DB, deliberately separate from
  the IR because the IR is rewritten wholesale on every parse.
- **`src/clangquill/config.py`** — one `Config` dataclass is the schema for both
  front-ends; every Sphinx value is `clangquill_<field-name>`, derived by
  iterating the dataclass rather than repeating names. Add a knob here and both
  front-ends pick it up.

### libclang is optional

`CLANGQUILL_WITH_LIBCLANG` is `AUTO` by default; without libclang the core still
builds (store + hashing only) as the "stub backend" — same extension module, no
parser sources. Tests that need a parse are gated on
`pytest.mark.skipif(not _core.have_libclang(), ...)`, and CI has dedicated jobs
for both the no-libclang path and LLVM 22 (the version bundled in wheels).
Never let a parse-dependent test fail hard when the backend is absent, and never
let a stub-path test silently skip because a libclang happened to be linked.

## Conventions that bite

- **The core exports its own definitions; Python derives from them.** Enum
  values, the schema DDL, the `content_hash` field list and the `comment_fields`
  routing are `_core` attributes, not transcriptions —
  `docs/development/mirror-contracts.md` records what each replaced. Adding a
  structure both sides need means exporting it, not copying it. Where a C++
  table has to sit next to a declaration it cannot generate (the enum name
  tables, since generating the `enum class` would delete the `///<` docs the
  dogfood build renders), make it self-checking: values as enumerator
  references, plus a `static_assert` on the size. A drift test that regex-scrapes
  `src/cpp` is a last resort — it passes vacuously against an installed wheel.
- **Comment behaviour goes in the corpus.** There is one Doxygen grammar
  (`comment/doxygen_raw.cpp`); `doxygen_parse` binds onto it. The corpus is
  still where a fix belongs: `tests/test_comment_corpus.py` asserts scanner
  plus the Python decoder, `tests/cpp/test_comment_corpus.cpp` asserts scanner
  plus the C++ serializer, so the two agreeing is what proves the flatten and
  the rebuild are inverses. Prefer a corpus case to a parser-specific test.
- **Golden pages.** `tests/golden/` byte-compares generator output per
  `(fixture, group_by)` and collectively renders every bundled template;
  `test_every_bundled_template_is_behind_a_golden_tree` fails until a new
  template is covered. Regenerate and read the diff — that diff is the review.
- **Schema changes** mean bumping `kSchemaVersion` in `src/cpp/store/schema.hpp`;
  `Store._check_schema_version()` rejects mismatched databases.
- Ruff runs with `select = ["ALL"]`, line length 120, target py313. Per-file
  ignores exist for `tests/`, `tools/ci/`, `benchmarks/` — keep new code in
  `src/` clean rather than widening them.
- `benchmarks/` is a separate uv project (`benchmarks/pyproject.toml`) with two
  drivers sharing `harness.py`: `benchmark.py` times clangquill against Doxygen,
  `verify.py` asserts extraction correctness (any diagnostic fails).

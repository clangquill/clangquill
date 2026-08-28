# C++↔Python mirror contracts

Some parts of the on-disk IR and the read-side Python API are hand-maintained
transcriptions of C++ source rather than a single generated definition. Each
one is a place a change to one side can silently stop matching the other —
that is how issue [#300](https://github.com/clangquill/clangquill/issues/300)
happened, and `tests/test_enum_mirrors.py` (the `SymbolKind` / `AccessKind` /
`RefKind` enums) was added in response. Issue
[#312](https://github.com/clangquill/clangquill/issues/312) asked for the rest
of these contracts to be inventoried and each given a drift test that fails
loudly when one side changes alone.

This page is that inventory. "Drift test" below means a test that parses or
loads *both* sides and compares them, as opposed to a test that only exercises
one side, or a fixture that happens to pass on both sides without asserting
they agree on shape.

| # | Contract | C++ side | Python side | Drift risk | Coverage |
| --- | --- | --- | --- | --- | --- |
| 0 | Enum values (`SymbolKind`, `AccessKind`, `RefKind`) | `src/cpp/model/{symbol,reference}.hpp` | `clangquill.store` | Silent (already fixed by #300) | `tests/test_enum_mirrors.py` |
| 1 | `comment_fields` arg encoding | `param_arg()` in `src/cpp/parser/comment_parser.cpp` | `_split_direction()` in `src/clangquill/comments.py` | Real gap — only pinned indirectly through corpus fixtures that parse raw text, not the flatten/reconstruct round trip | `tests/test_mirror_contracts.py` |
| 2 | Schema DDL | `kSchemaDDL` in `src/cpp/store/schema.hpp` | `tests/fixtures.py::_schema_ddl()` | None — `_schema_ddl()` already extracts the DDL from `schema.hpp` at test time instead of copying it, the same technique `test_enum_mirrors.py` uses for enums | `tests/test_mirror_contracts.py` guards the extraction markers |
| 3 | Fingerprint composition (`content_hash` vs. the `wide` fingerprint) | `content_hash()` in `src/cpp/hash/content_hash.cpp` | `Generator._wide_tokens()` in `src/clangquill/generator.py` | Real gap — nothing asserted that `_wide_tokens` covers exactly the `Symbol` fields `content_hash` leaves out | `tests/test_mirror_contracts.py` |
| 4 | Corpus JSON encoding (`CommentModel`) | `to_fields_json()` in `src/cpp/parser/comment_parser.cpp` | `CommentModel` / `CommentParam` / `CommentRetval` / `CommentThrow` in `src/clangquill/comments.py` | Partial gap — the shared corpus (`tests/comment_corpus/`, issue #229) holds both sides equal by example, but a field neither the C++ nor Python side ever populates in a corpus case can drift without failing it | `tests/test_mirror_contracts.py` adds an explicit key-set assertion |
| 5 | `SCHEMA_VERSION` / `kSchemaVersion` | `kSchemaVersion` in `src/cpp/store/schema.hpp` | `clangquill._core.SCHEMA_VERSION`, consumed by `Store._check_schema_version()` | None — `src/cpp/bindings/module.cpp` binds `SCHEMA_VERSION` directly to the C++ constant, so there is no second integer to keep in sync (and no separate pure-Python `_core` stub exists; the "stub backend" is the same compiled extension built without libclang) | `tests/test_mirror_contracts.py` guards against the binding pointing at a stale literal |

Rows 1, 3, and 4 were the real gaps; each now has a `test_*` in
`tests/test_mirror_contracts.py` that parses the relevant C++ source and
compares it against the Python dataclass/regex it mirrors, following the
pattern `test_enum_mirrors.py` established. Rows 0, 2, and 5 turned out to
already be immune to drift — by generating from source (0, 2) or by binding
directly to the C++ constant (5) — so they only get a light guard-the-guard
test that catches the extraction mechanism itself silently breaking.

When a new by-convention contract shows up (a new hand-maintained mirror, or
a fixture that pins both sides only by example), prefer eliminating the
duplication outright — read the source of truth like `_schema_ddl()` does, or
bind directly like `SCHEMA_VERSION` does. When that is not practical, add a
`test_*_mirrors`-style test here rather than trusting an existing fixture to
catch drift by coincidence.

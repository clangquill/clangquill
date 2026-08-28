# C++ ↔ Python mirror contracts

clangquill's C++ core and Python package agree on several things "by
convention" rather than by sharing a single source of truth: a Python-side
enum, dataclass, or format string is a hand transcription of something the
C++ side defines, and nothing forces the two to move together. That is
exactly how the enum mirrors (`SymbolKind`/`AccessKind`/`RefKind` in
{py:mod}`clangquill.store`) and the Doxygen comment-parser conformance corpus
drifted before — see `tests/test_enum_mirrors.py` and
`tests/comment_corpus/`, both added *after* the drift was found, not before.

This page inventories every contract of that shape found during the 2026-08
review (issue #312) and records what guards it now.

| Contract | Hand-synced? | Guard |
|----------|---------------|-------|
| Enum values (`SymbolKind`, `AccessKind`, `RefKind`) | Yes | `tests/test_enum_mirrors.py` |
| Doxygen parser output (`doxygen_parse` vs `DoxygenCommentParser::parse_raw_text`) | Yes (independent implementations) | `tests/comment_corpus/*.json`, asserted by both `tests/test_comment_corpus.py` and `tests/cpp/test_comment_corpus.cpp` |
| `comment_fields` arg encoding (`param_arg()` vs `_split_direction()`) | Yes | `tests/test_comment_field_encoding_mirrors.py` (new) |
| Comment-model JSON field set (`to_fields_json`/`params_to_json` vs `CommentModel`/`CommentParam`/`CommentRetval`/`CommentThrow`) | Yes | `tests/test_comment_model_json_mirrors.py` (new) |
| Fingerprint field coverage (`content_hash()` vs `Generator._wide_tokens`) | Yes (by documented assumption) | `tests/test_content_hash_field_mirrors.py` (new) |
| Schema DDL (`schema.hpp` vs the generator-test fixtures) | **No** | `tests/fixtures.py` extracts the DDL live from `schema.hpp` (see `_schema_ddl()`); there is no second copy to drift |
| `SCHEMA_VERSION` / `kSchemaVersion` | **No** | `m.attr("SCHEMA_VERSION") = kSchemaVersion` in `bindings/module.cpp` is a live binding, not a duplicated Python constant |
| A pure-Python `_core` stand-in for testing without libclang | **Does not exist** | `_core` is always the real compiled extension; the libclang-less path is `_core.have_libclang()` returning `False`, exercised by monkeypatching that one function (e.g. `tests/test_clangquill.py`, `tests/test_sphinx_ext.py`) |

## New drift tests

Three contracts from the issue's candidate list were genuinely hand-synced
with only indirect (or no) coverage; each now has a dedicated
`test_*_mirrors`-style test that parses the real C++ source rather than
assuming its shape (the same technique `tests/test_enum_mirrors.py` uses),
so a reformat on either side breaks the test loudly instead of only
surfacing as corrupted output later:

- **`tests/test_comment_field_encoding_mirrors.py`** — regex-extracts the
  bracket format `param_arg()` wraps a directed parameter in and the set of
  direction spellings `canonical_direction()` can produce, then asserts
  `_split_direction()` round-trips every one of them. Existing coverage
  (`tests/test_comments.py::test_model_from_fields_splits_the_direction_off_the_arg`)
  pins the same behavior against *hardcoded* literal strings, which would
  keep passing even if the C++ format changed; this test would not.
- **`tests/test_comment_model_json_mirrors.py`** — regex-extracts the JSON
  key literals `to_fields_json`/`params_to_json` build and asserts each set
  equals the corresponding dataclass's field names
  (`CommentModel`/`CommentParam`/`CommentRetval`/`CommentThrow`). The
  existing corpus test (`tests/test_comment_corpus.py`) only compares
  *values* for whatever fields the fixture corpus happens to exercise, so a
  field added to one side alone would not be caught there.
- **`tests/test_content_hash_field_mirrors.py`** — pins the exact ordered
  field list `content_hash()` (C++) digests from `model::Symbol`/
  `model::FunctionParameter`, cross-checks those fields are still declared
  on the structs, and asserts the fields `Generator._wide_tokens` (Python)
  is documented to cover as content_hash's complement are (a) still absent
  from content_hash's coverage and (b) actually present on the Python
  `Symbol` row it reads them from. Without this, `content_hash` silently
  gaining or dropping a field would leave the per-page render cache either
  under-invalidating (a changed field never busts a page) or the
  `_wide_tokens` docstring's "leaves out" claim stale, with nothing failing.

## Contracts that turned out not to need one

Three of the issue's candidates were investigated and found to already be
self-consistent by construction rather than hand-duplicated, so no new test
was added for them (adding one would have pinned an assumption that doesn't
actually hold, or duplicated what already prevents drift structurally):

- The **schema DDL** is not copied into `tests/fixtures.py` — `_schema_ddl()`
  there reads `src/cpp/store/schema.hpp` and slices the DDL out of the raw
  `R"SQL(...)SQL"` literal at test time, so there is no second copy that
  could go stale.
- **`SCHEMA_VERSION`** is not a duplicated Python literal — `bindings/module.cpp`
  binds `_core.SCHEMA_VERSION` directly to `clangquill::store::kSchemaVersion`,
  so Python always reads the real C++ constant through the compiled module.
- The **pure-Python `_core` stand-in** described in the original review
  doesn't exist: `_core` is always the real compiled nanobind extension.
  What does exist is a compiled *runtime* fallback (`have_libclang()`
  returning `False`, exercised in tests by monkeypatching that one function),
  which is not a mirrored contract in the sense the rest of this page covers.

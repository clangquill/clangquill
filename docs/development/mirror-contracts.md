# C++↔Python mirror contracts

Parts of the on-disk IR and the read-side Python API used to be hand-maintained
transcriptions of C++ source. Each one was a place a change to one side could
silently stop matching the other — that is how issue
[#300](https://github.com/clangquill/clangquill/issues/300) happened. Issue
[#312](https://github.com/clangquill/clangquill/issues/312) asked for the rest
to be inventoried and each *either eliminated or given a drift test*.

They are eliminated. The C++ core exports its own definitions through the
bindings and Python derives from them, so most of these contracts no longer
exist as contracts. This page records what replaced each one, and what is left.

## What replaced them

| # | Contract | Then | Now |
| --- | --- | --- | --- |
| 0 | Enum values (`SymbolKind`, `AccessKind`, `RefKind`) | Python `IntEnum`s compared against a regex parse of `model/*.hpp` | `_core.SYMBOL_KINDS` / `ACCESS_KINDS` / `REF_KINDS`, built from the enumerators themselves; `tests/test_enum_mirrors.py` compares `IntEnum.__members__` against them |
| 1 | `comment_fields` arg encoding | `param_arg()` in C++ vs. a `_split_direction` regex in Python | `_split_direction` **is** `_core.split_param_arg` — the encoder's own inverse |
| 2 | Schema DDL | `tests/fixtures.py` sliced the DDL out of `schema.hpp` between `R"SQL(` markers | `_core.SCHEMA_DDL` |
| 3 | Fingerprint composition | Regexes over the bodies of `content_hash()` and `_wide_tokens()` | `content_hash()` walks a table `_core.CONTENT_HASH_FIELDS` exports; `_wide_tokens` is built from `_WIDE_SYMBOL_FIELDS`, and the invariant is set algebra over the two |
| 4 | `CommentModel` shape | Regexes over `to_fields_json`'s object literal | One `CLANGQUILL_COMMENT_FIELDS` list drives the flatten, the rebuild and the JSON; `_core.COMMENT_FIELDS` exports it and `clangquill.comments` derives its routing from it |
| 5 | `SCHEMA_VERSION` | Already bound to `kSchemaVersion` | Unchanged |
| 6 | The Doxygen grammar itself | Two independent implementations (972 lines of C++, 470 of Python) held equal by `tests/comment_corpus/` | One scanner. `doxygen_parse` is `model_from_fields(_core.parse_doxygen_comment(raw))` |
| 6b | Cross-reference target splitting | `split_xref_target` written twice, once for the scanner and once for the renderer | `_core.split_xref_target`; a `@ref` cannot resolve differently depending on which side saw it |
| 7 | `store.py`'s SQL column lists | *Not inventoried, not covered at all* | Every `Store` reader runs against a database built from `_core.SCHEMA_DDL`, so sqlite validates every column in every query |
| 8 | The `CommentModel` field table in `docs/guides/comment-parsers.md` | *Not inventoried, a third hand-copy* | Asserted against `dataclasses.fields(CommentModel)` |

Contract 6 is the one that kept costing. Three separate fixes — the
`copy_target` depth counting, the operator-name handling in `is_cpp_name`, and
`split_xref_target` itself — were each written twice, "command for command with
the C++", because a grammar change landing on one side alone published
different output. That is the work this removes.

It had also actually drifted, and nothing caught it: a
comment opening with `\ingroup`, `\class`, `\defgroup`, `\relates` or
`\internal` had its whole brief and detail swallowed into the command's
argument by the Python parser, because that side had no notion of a command
taking only an entity name. No corpus case exercised any of those commands.
The five cases named `*_keeps_its_prose` and
`no_argument_command_before_a_group` are that bug, pinned.

## The three techniques

When a new shared structure shows up, reach for these in order.

1. **Bind the constant.** `SCHEMA_DDL`, `SYMBOL_KINDS`, `CONTENT_HASH_FIELDS`,
   `COMMENT_FIELDS` are all just `m.attr(...)` in `bindings/module.cpp`. All of
   them are available in the stub backend, because they come from the
   libclang-free half of the core (`src/cpp/comment/`, `model/`, `hash/`,
   `store/schema.hpp`).
2. **Share the engine.** If both sides implement the same algorithm, bind one
   and delete the other. `doxygen_parse` is four lines now.
3. **Generate both sides from one list.** `CLANGQUILL_COMMENT_FIELDS` drives
   four C++ consumers and, through the bindings, the Python decoder's routing.
   `kSymbolHashFields` is both what `content_hash()` hashes and what it exports.

Where a C++ table must be maintained alongside a declaration it cannot
generate — the enum name tables, because generating the `enum class` body would
delete the per-enumerator `///<` docs the dogfood build renders — make the table
*self-checking*: spell its values as enumerator references (so a rename does not
compile and a reorder cannot change a value) and `static_assert` its size
against the last enumerator (so an addition without a row fails the build).
Drift becomes a compile error rather than a test failure.

## What is left

Two things, both deliberate.

- **`store.py`'s dataclasses and its SQL.** Python reads the IR with stdlib
  `sqlite3` *by design*, so queries can evolve without recompiling the core.
  Contract 7 above is the coverage; the `Symbol` dataclass deliberately omits
  the schema's `storage` and `col` columns.
- **The `IntEnum` declarations.** Kept hand-written so the per-member
  docstrings and the static names a type checker and an IDE can see exist at
  all. Contract 0 is the coverage.

One more pair is uncrosschecked but inert: `comments.format` defaults to
`"doxygen-raw"` in C++ (`model/comment.hpp`) while the Python parser registry
key is `"doxygen"`. Nothing reads the column today, so the two never meet.

## Writing a drift test now

A drift test compares two things the **installed package** exposes. The old
tests regex-scraped `src/cpp`, which passes vacuously against a wheel — there is
no C++ source to read. Only one such scrape survives
(`test_schema_version_is_bound_from_the_constant_not_a_literal`), and only
because a binding pointing at a stale literal would look identical from Python.

Prefer eliminating the duplication over testing it. If you cannot, export the
C++ side and compare against the export.

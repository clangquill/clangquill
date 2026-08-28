"""Drift test for the fingerprint composition contract (see #312).

The M6 per-page render cache combines two independent field lists into one
dependency key: C++'s ``content_hash()`` (``hash/content_hash.cpp``) digests
some fields of ``model::Symbol``/``model::FunctionParameter``, and Python's
``Generator._wide_tokens`` (``generator.py``) covers the rest *on the
documented assumption* of which fields those are (see its module comment:
"the symbol-row fields ``content_hash`` leaves out (spelling, display name,
parent, documented flag, declaring file and line)"). Nothing currently checks
that assumption against the real C++ source: if a field were added to (or
dropped from) ``content_hash``'s coverage without updating that assumption,
the render cache would either silently under-invalidate (a changed field
never busts a page) or the "leaves out" list would just go stale as
documentation -- neither fails a test today.

This pins ``content_hash``'s exact field list by parsing the real C++ source
(the same technique ``tests/test_enum_mirrors.py`` uses for the enum
mirrors), cross-checks those fields are still declared where the hash reads
them, and asserts the fields ``_wide_tokens`` in ``generator.py`` is
documented to (and needs to) cover are still both absent from
``content_hash``'s coverage and present on the Python ``Symbol`` row it reads
them from.
"""

from __future__ import annotations

import re
from pathlib import Path

from clangquill.store import Symbol

_CPP_DIR = Path(__file__).resolve().parents[1] / "src" / "cpp"
_CONTENT_HASH_CPP = _CPP_DIR / "hash" / "content_hash.cpp"
_SYMBOL_HPP = _CPP_DIR / "model" / "symbol.hpp"
_PARAMETERS_HPP = _CPP_DIR / "model" / "parameters.hpp"

# The exact fields content_hash() feeds into the digest, in source order --
# the contract this whole file pins. A change here (add, drop, or the field
# list moving without a matching change to the constants below) means the
# render cache's invalidation surface changed and _wide_tokens's "leaves out"
# assumption needs re-checking by hand.
_EXPECTED_SYMBOL_FIELDS = (
    "usr",
    "kind",
    "qualified_name",
    "signature",
    "type_repr",
    "access",
    "storage",
    "is_definition",
)
_EXPECTED_PARAM_FIELDS = ("type_repr", "name", "default_value")

# Symbol-row fields the generator.py module comment documents content_hash as
# leaving out, and which _wide_tokens's "W" token therefore has to cover
# itself (see Generator.page_fingerprint's docstring).
_WIDE_TOKEN_COVERED_SYMBOL_FIELDS = ("spelling", "display_name", "parent_usr", "is_documented", "file_id", "line")


def _function_body(text: str, *, signature_start: str) -> str:
    """Return the ``{ ... }`` body of the function whose definition starts with ``signature_start``."""
    start = text.index(signature_start)
    brace = text.index("{", start)
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace : i + 1]
    msg = f"unbalanced braces looking for the body of {signature_start!r}"
    raise AssertionError(msg)


def _content_hash_body() -> str:
    text = _CONTENT_HASH_CPP.read_text(encoding="utf-8")
    # Skip the anonymous-namespace `update_length` helper above it -- search
    # from the real definition, identified by its return type + name (the
    # header only has the declaration, so this text is unambiguous here).
    return _function_body(text, signature_start="std::string content_hash(")


def _referenced_fields(body: str, *, receiver: str) -> list[str]:
    """Every ``<receiver>.<field>`` reference in ``body``, in source order."""
    return re.findall(rf"\b{re.escape(receiver)}\.(\w+)", body)


def _struct_field_names(header_text: str, *, struct_name: str) -> set[str]:
    """Member names declared in ``struct <struct_name> { ... };``."""
    match = re.search(rf"struct {struct_name} \{{(.*?)\n\}};", header_text, re.DOTALL)
    assert match is not None, f"could not find struct {struct_name} in the header text"
    return set(
        re.findall(
            r"^\s*(?:std::string|unsigned|bool|int|SymbolKind|AccessKind|StorageKind)\s+(\w+)",
            match.group(1),
            re.MULTILINE,
        ),
    )


def test_content_hash_reads_exactly_the_expected_symbol_fields() -> None:
    body = _content_hash_body()
    assert _referenced_fields(body, receiver="sym") == list(_EXPECTED_SYMBOL_FIELDS)


def test_content_hash_reads_exactly_the_expected_parameter_fields() -> None:
    body = _content_hash_body()
    assert _referenced_fields(body, receiver="p") == list(_EXPECTED_PARAM_FIELDS)


def test_every_symbol_field_content_hash_reads_still_exists_on_the_struct() -> None:
    header_text = _SYMBOL_HPP.read_text(encoding="utf-8")
    declared = _struct_field_names(header_text, struct_name="Symbol")
    missing = set(_EXPECTED_SYMBOL_FIELDS) - declared
    assert not missing, f"content_hash reads model::Symbol fields no longer declared: {missing}"


def test_every_param_field_content_hash_reads_still_exists_on_the_struct() -> None:
    header_text = _PARAMETERS_HPP.read_text(encoding="utf-8")
    declared = _struct_field_names(header_text, struct_name="FunctionParameter")
    missing = set(_EXPECTED_PARAM_FIELDS) - declared
    assert not missing, f"content_hash reads model::FunctionParameter fields no longer declared: {missing}"


def test_wide_token_fields_are_not_already_covered_by_content_hash() -> None:
    """Guard generator.py's "leaves out" claim.

    ``_wide_tokens`` only earns its keep by covering fields ``content_hash``
    genuinely does not touch. If ``content_hash`` grew to cover one of these,
    the wide fingerprint would be redundant there -- harmless, but a sign the
    doc comment (and this pin) need updating to match.
    """
    assert set(_WIDE_TOKEN_COVERED_SYMBOL_FIELDS).isdisjoint(_EXPECTED_SYMBOL_FIELDS)


def test_wide_token_fields_are_readable_on_the_python_symbol_row() -> None:
    """Check the other half of the same claim.

    ``_wide_tokens`` has to actually be able to read what it says it covers
    off the Python ``Symbol`` row.
    """
    symbol_fields = {f.name for f in Symbol.__dataclass_fields__.values()}
    missing = set(_WIDE_TOKEN_COVERED_SYMBOL_FIELDS) - symbol_fields
    assert not missing, f"generator.py assumes Symbol carries {missing}, but it does not"

"""Runs the shared comment-parser conformance corpus against `doxygen_parse`.

`tests/comment_corpus/*.json` is asserted by both this suite and
`tests/cpp/test_comment_corpus.cpp`, against `doxygen_parse` and
`DoxygenCommentParser::parse_raw_text` respectively. A fixture's `expected`
model is the same JSON shape `to_fields_json` (C++) produces, so drift between
the two parsers fails one side of the corpus instead of shipping silently
(see issue #229).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from clangquill.comments import doxygen_parse

CORPUS_DIR = Path(__file__).parent / "comment_corpus"
CORPUS_CASES = sorted(CORPUS_DIR.glob("*.json"))


@pytest.mark.parametrize("case_path", CORPUS_CASES, ids=lambda p: p.stem)
def test_doxygen_parse_matches_corpus(case_path: Path) -> None:
    case = json.loads(case_path.read_text())
    model = doxygen_parse(case["raw"])
    assert dataclasses.asdict(model) == case["expected"]


def test_corpus_is_not_empty() -> None:
    # A glob that silently matched nothing would make every case above vacuous.
    assert CORPUS_CASES

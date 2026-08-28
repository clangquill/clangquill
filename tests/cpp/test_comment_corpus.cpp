// Runs the shared comment-parser conformance corpus (tests/comment_corpus/)
// against DoxygenCommentParser::parse_raw_text. The same corpus is asserted by
// tests/test_comment_corpus.py against doxygen_parse: a fixture's "expected"
// model is the JSON shape to_fields_json already produces, so a case that
// passes here and fails there (or vice versa) is drift between the two
// parsers caught by CI instead of shipping silently (see issue #229).

#include <catch2/catch_test_macros.hpp>
#include <nlohmann/json.hpp>

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#if defined(CLANGQUILL_HAVE_LIBCLANG)
#include "parser/comment_parser.hpp"
#include "parser/doxygen_comment_parser.hpp"
#endif

#ifndef CLANGQUILL_COMMENT_CORPUS_DIR
#define CLANGQUILL_COMMENT_CORPUS_DIR "tests/comment_corpus"
#endif

#if defined(CLANGQUILL_HAVE_LIBCLANG)

using namespace clangquill;
namespace fs = std::filesystem;
using nlohmann::json;

namespace {

std::vector<fs::path> corpus_cases() {
  std::vector<fs::path> paths;
  for (const auto& entry : fs::directory_iterator(CLANGQUILL_COMMENT_CORPUS_DIR)) {
    if (entry.path().extension() == ".json") paths.push_back(entry.path());
  }
  std::sort(paths.begin(), paths.end());
  return paths;
}

json load_json(const fs::path& path) {
  std::ifstream in(path);
  std::stringstream ss;
  ss << in.rdbuf();
  return json::parse(ss.str());
}

}  // namespace

TEST_CASE("the comment corpus is not empty", "[comments][corpus]") {
  CHECK_FALSE(corpus_cases().empty());
}

TEST_CASE("parse_raw_text matches the shared comment corpus", "[comments][corpus]") {
  for (const auto& path : corpus_cases()) {
    CAPTURE(path.filename().string());
    json case_json = load_json(path);
    std::string raw = case_json.at("raw").get<std::string>();
    json expected = case_json.at("expected");

    model::CommentModel model = parser::DoxygenCommentParser::parse_raw_text(raw);
    json actual = json::parse(parser::to_fields_json(model));

    CHECK(actual == expected);
  }
}

#else  // !CLANGQUILL_HAVE_LIBCLANG

TEST_CASE("comment corpus tests skipped without libclang", "[comments][corpus][!mayfail]") {
  SUCCEED("built without libclang");
}

#endif

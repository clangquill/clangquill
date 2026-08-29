// Runs the shared comment-parser conformance corpus (tests/comment_corpus/)
// against the raw Doxygen scanner. The same corpus is asserted from Python by
// tests/test_comment_corpus.py, which runs the *same* scanner through the
// bindings and then rebuilds the model from its flattened comment_fields rows.
// A case passing here and failing there is therefore a broken flatten/rebuild
// round trip rather than a second parser drifting (see issues #229, #312).
//
// The scanner needs no libclang, so this suite runs in the stub build too.

#include <catch2/catch_test_macros.hpp>
#include <nlohmann/json.hpp>

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "comment/doxygen_raw.hpp"
#include "comment/fields.hpp"

#ifndef CLANGQUILL_COMMENT_CORPUS_DIR
#define CLANGQUILL_COMMENT_CORPUS_DIR "tests/comment_corpus"
#endif

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

TEST_CASE("doxygen_parse_raw matches the shared comment corpus", "[comments][corpus]") {
  for (const auto& path : corpus_cases()) {
    CAPTURE(path.filename().string());
    json case_json = load_json(path);
    std::string raw = case_json.at("raw").get<std::string>();
    json expected = case_json.at("expected");

    model::CommentModel model = comment::doxygen_parse_raw(raw);
    json actual = json::parse(comment::to_fields_json(model));

    CHECK(actual == expected);
  }
}

// The comment_fields projection must be lossless: to_comment_fields flattens a
// CommentModel into the rows the IR persists, and from_comment_fields rebuilds
// it. The Python read side runs the same round trip (its own decoder against
// the C++ encoder), so a hole here is a hole there.

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

std::string raw_of(const fs::path& path) {
  std::ifstream in(path);
  std::stringstream ss;
  ss << in.rdbuf();
  return json::parse(ss.str()).at("raw").get<std::string>();
}

}  // namespace

TEST_CASE("comment_fields round-trips every corpus model", "[comments][fields]") {
  for (const auto& path : corpus_cases()) {
    CAPTURE(path.filename().string());
    const model::CommentModel model = comment::doxygen_parse_raw(raw_of(path));
    const model::CommentModel back =
        comment::from_comment_fields(comment::to_comment_fields("u", model));
    CHECK(comment::to_fields_json(back) == comment::to_fields_json(model));
  }
}

TEST_CASE("comment_fields round-trips an empty model", "[comments][fields]") {
  const model::CommentModel empty;
  CHECK(comment::to_comment_fields("u", empty).empty());
  CHECK(comment::from_comment_fields({}).empty());
}

TEST_CASE("an unrecognized command survives as a custom field",
          "[comments][fields]") {
  model::CommentModel m;
  m.custom["madeup"] = {"one", "two"};

  const std::vector<model::CommentField> rows = comment::to_comment_fields("u", m);
  REQUIRE(rows.size() == 2);
  CHECK(rows[0].name == "madeup");
  CHECK(rows[0].arg.empty());
  CHECK(rows[0].value == "one");

  CHECK(comment::from_comment_fields(rows).custom == m.custom);
}

TEST_CASE("a parameter direction survives the arg encoding",
          "[comments][fields]") {
  // The `arg` column has one slot, so a direction rides along in the bracketed
  // form Doxygen writes. Every direction the parsers can produce must survive.
  for (const std::string& direction : {"", "in", "out", "in,out"}) {
    CAPTURE(direction);
    model::CommentModel m;
    m.params.push_back(model::CommentParam{"result", "what it holds", direction});

    const model::CommentModel back =
        comment::from_comment_fields(comment::to_comment_fields("u", m));
    REQUIRE(back.params.size() == 1);
    CHECK(back.params[0].name == "result");
    CHECK(back.params[0].direction == direction);
    CHECK(back.params[0].description == "what it holds");
  }
}

TEST_CASE("a parameter named like a direction attribute is left alone",
          "[comments][fields]") {
  // `split_param_arg` only strips brackets holding a direction Doxygen defines,
  // so a name that merely looks bracketed round-trips unchanged.
  model::CommentModel m;
  m.params.push_back(model::CommentParam{"[sideways] x", "odd but legal", ""});

  const model::CommentModel back =
      comment::from_comment_fields(comment::to_comment_fields("u", m));
  REQUIRE(back.params.size() == 1);
  CHECK(back.params[0].name == "[sideways] x");
  CHECK(back.params[0].direction.empty());
}

TEST_CASE("the exported field table covers the flattened row names",
          "[comments][fields]") {
  // Guard the guard: the table the bindings export must be the same list the
  // encoder walks, or the Python side derives its routing from nothing.
  const auto& table = comment::comment_field_table();
  CHECK(table.size() == 20);

  model::CommentModel m;
  m.brief = "b";
  m.retvals.push_back(model::CommentRetval{"0", "ok"});
  for (const model::CommentField& row : comment::to_comment_fields("u", m)) {
    CAPTURE(row.name);
    CHECK(std::any_of(table.begin(), table.end(),
                      [&](const comment::CommentFieldInfo& info) {
                        return row.name == info.row_name;
                      }));
  }
}

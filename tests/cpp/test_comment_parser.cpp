#include <catch2/catch_test_macros.hpp>

#include <string>

#include "model/module.hpp"

#if defined(CLANGQUILL_HAVE_LIBCLANG)
#include "parser/parser.hpp"
#endif

#ifndef CLANGQUILL_FIXTURE_DIR
#define CLANGQUILL_FIXTURE_DIR "tests/cpp/fixtures"
#endif

using namespace clangquill;

#if defined(CLANGQUILL_HAVE_LIBCLANG)

namespace {

model::ParsedModule parse_fixture(const std::string& name) {
  parser::ParseOptions opts;
  parser::Parser p(opts);
  model::ParsedModule mod;
  p.parse_file(std::string(CLANGQUILL_FIXTURE_DIR) + "/" + name, mod);
  return mod;
}

const model::Symbol* find(const model::ParsedModule& m, const std::string& qn) {
  for (const auto& s : m.symbols) {
    if (s.qualified_name == qn) return &s;
  }
  return nullptr;
}

// Collects the comment_fields of one symbol into a list of (name, arg, value).
struct Field {
  std::string name, arg, value;
};
std::vector<Field> fields_of(const model::ParsedModule& m,
                             const std::string& usr) {
  std::vector<Field> out;
  for (const auto& f : m.comment_fields) {
    if (f.symbol_usr == usr) out.push_back({f.name, f.arg, f.value});
  }
  return out;
}

const Field* field(const std::vector<Field>& fs, const std::string& name,
                   const std::string& arg = "") {
  for (const auto& f : fs) {
    if (f.name == name && (arg.empty() || f.arg == arg)) return &f;
  }
  return nullptr;
}

}  // namespace

TEST_CASE("a structural block documents the entity it names", "[comments]") {
  auto m = parse_fixture("structural.hpp");

  // The Eigen shape: the block sits 40+ lines above `class Widget`, separated by
  // a blank line and an unrelated namespace, so nothing attaches it by adjacency.
  const auto* widget = find(m, "Widget");
  REQUIRE(widget != nullptr);
  CHECK(widget->is_documented);
  auto fs = fields_of(m, widget->usr);
  const Field* brief = field(fs, "brief");
  REQUIRE(brief != nullptr);
  CHECK(brief->value.find("nowhere near it") != std::string::npos);
  // The command's argument must not leak into the prose.
  CHECK(brief->value.find("Widget") == std::string::npos);

  // `\ingroup` inside the block still makes the target a member, which is what
  // Eigen's blocks rely on.
  bool in_group = false;
  for (const auto& gm : m.group_members) {
    if (gm.group_id == "shapes" && gm.member_usr == widget->usr) in_group = true;
  }
  CHECK(in_group);
}

TEST_CASE("structural blocks cover the other entity kinds", "[comments]") {
  auto m = parse_fixture("structural.hpp");
  for (const auto* qn : {"Gadget", "Colour", "deep", "Distance"}) {
    const auto* sym = find(m, qn);
    REQUIRE(sym != nullptr);
    CHECK(sym->is_documented);
  }
  // A signature after `\fn` has to be reduced to a qualified name.
  const auto* scale = find(m, "deep::scale");
  REQUIRE(scale != nullptr);
  CHECK(scale->is_documented);
}

TEST_CASE("an ambiguous or unresolvable structural block attaches nothing",
          "[comments]") {
  auto m = parse_fixture("structural.hpp");

  // Two overloads answer to `deep::over`; documenting either would be a guess.
  for (const auto& sym : m.symbols) {
    if (sym.qualified_name == "deep::over") CHECK_FALSE(sym.is_documented);
  }

  // A name that resolves to nothing must not leave a comment row behind:
  // comments.symbol_usr is a foreign key onto symbols.usr.
  for (const auto& c : m.comments) {
    bool has_symbol = false;
    for (const auto& sym : m.symbols) {
      if (sym.usr == c.symbol_usr) has_symbol = true;
    }
    CHECK(has_symbol);
  }
}

TEST_CASE("an entity's own comment beats a structural block naming it",
          "[comments]") {
  auto m = parse_fixture("structural.hpp");
  const auto* owned = find(m, "Owned");
  REQUIRE(owned != nullptr);
  REQUIRE(owned->is_documented);

  int rows = 0;
  for (const auto& c : m.comments) {
    if (c.symbol_usr == owned->usr) {
      ++rows;
      CHECK(c.text.find("Its own comment") != std::string::npos);
    }
  }
  CHECK(rows == 1);  // and no duplicate comment_fields either
  auto fs = fields_of(m, owned->usr);
  int briefs = 0;
  for (const auto& f : fs) {
    if (f.name == "brief") ++briefs;
  }
  CHECK(briefs <= 1);
}

TEST_CASE("a single-name command does not swallow the prose after it",
          "[comments]") {
  // `\relates X` names one entity; the paragraphs below it document the
  // function itself. Routing the whole block as the command's argument loses
  // the function's description entirely.
  auto m = parse_fixture("structural.hpp");
  const auto* fn = find(m, "stream_widget");
  REQUIRE(fn != nullptr);
  auto fs = fields_of(m, fn->usr);

  const Field* rel = field(fs, "relates");
  REQUIRE(rel != nullptr);
  CHECK(rel->value == "Widget");

  const Field* brief = field(fs, "brief");
  REQUIRE(brief != nullptr);
  CHECK(brief->value.find("Streams a widget") != std::string::npos);
  // The entity the command named must not leak into the prose.
  CHECK(brief->value.find("Widget") == std::string::npos);
}

TEST_CASE("doxygen parser covers the common commands", "[comments]") {
  auto m = parse_fixture("doxygen.hpp");
  const auto* divide = find(m, "doc::divide");
  REQUIRE(divide != nullptr);
  auto fs = fields_of(m, divide->usr);
  REQUIRE_FALSE(fs.empty());

  const Field* brief = field(fs, "brief");
  REQUIRE(brief != nullptr);
  CHECK(brief->value.find("quotient") != std::string::npos);

  const Field* detail = field(fs, "detail");
  REQUIRE(detail != nullptr);
  CHECK(detail->value.find("integer division") != std::string::npos);

  const Field* num = field(fs, "param", "numerator");
  REQUIRE(num != nullptr);
  CHECK(num->value.find("divide") != std::string::npos);

  const Field* den = field(fs, "param", "denominator");
  REQUIRE(den != nullptr);

  const Field* ret = field(fs, "returns");
  REQUIRE(ret != nullptr);
  CHECK(ret->value.find("quotient") != std::string::npos);

  const Field* retval = field(fs, "retval", "0");
  REQUIRE(retval != nullptr);
  CHECK(retval->value.find("numerator is zero") != std::string::npos);

  const Field* thr = field(fs, "throws", "std::domain_error");
  REQUIRE(thr != nullptr);

  CHECK(field(fs, "note") != nullptr);
  CHECK(field(fs, "warning") != nullptr);

  const Field* since = field(fs, "since");
  REQUIRE(since != nullptr);
  CHECK(since->value == "1.2");

  CHECK(field(fs, "see") != nullptr);

  // Unknown command lands under its own name (the "custom" bucket).
  const Field* author = field(fs, "author");
  REQUIRE(author != nullptr);
  CHECK(author->value == "Ada");
}

TEST_CASE("doxygen parser handles /// brief and tparam", "[comments]") {
  auto m = parse_fixture("doxygen.hpp");
  const auto* mul = find(m, "doc::multiply");
  REQUIRE(mul != nullptr);
  auto fs = fields_of(m, mul->usr);

  const Field* brief = field(fs, "brief");
  REQUIRE(brief != nullptr);
  CHECK(brief->value == "Multiplies two values.");

  const Field* tp = field(fs, "tparam", "T");
  REQUIRE(tp != nullptr);
  CHECK(tp->value.find("arithmetic") != std::string::npos);
}

TEST_CASE("doxygen parser captures deprecated", "[comments]") {
  auto m = parse_fixture("doxygen.hpp");
  const auto* od = find(m, "doc::old_divide");
  REQUIRE(od != nullptr);
  auto fs = fields_of(m, od->usr);
  const Field* dep = field(fs, "deprecated");
  REQUIRE(dep != nullptr);
  CHECK(dep->value.find("divide") != std::string::npos);
}

TEST_CASE("doxygen parser preserves verbatim block text", "[comments]") {
  auto m = parse_fixture("doxygen.hpp");
  const auto* sq = find(m, "doc::square");
  REQUIRE(sq != nullptr);
  auto fs = fields_of(m, sq->usr);

  // The @code ... @endcode body must survive as detail text rather than being
  // dropped when libclang hands back VerbatimBlockLine children.
  bool found_code = false;
  for (const auto& f : fs) {
    if (f.name == "detail" && f.value.find("square(3)") != std::string::npos) {
      found_code = true;
    }
  }
  CHECK(found_code);
}

TEST_CASE("parsed comments store a format and JSON projection", "[comments]") {
  auto m = parse_fixture("doxygen.hpp");
  const auto* divide = find(m, "doc::divide");
  REQUIRE(divide != nullptr);

  bool found = false;
  for (const auto& c : m.comments) {
    if (c.symbol_usr == divide->usr) {
      found = true;
      CHECK(c.format == "doxygen");
      CHECK(c.fields_json.find("\"brief\"") != std::string::npos);
      CHECK(c.fields_json.find("quotient") != std::string::npos);
    }
  }
  CHECK(found);
}

#else  // !CLANGQUILL_HAVE_LIBCLANG

TEST_CASE("comment parser tests skipped without libclang", "[comments][!mayfail]") {
  SUCCEED("built without libclang");
}

#endif

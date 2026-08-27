#include <catch2/catch_test_macros.hpp>

#include <string>
#include <vector>

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

// The template parameter of @p owner at @p index, or nullptr.
const model::TemplateParameter* tparam(const model::ParsedModule& m,
                                       const model::Symbol& owner, int index) {
  for (const auto& tp : m.template_parameters) {
    if (tp.owner_usr == owner.usr && tp.index == index) return &tp;
  }
  return nullptr;
}

}  // namespace

TEST_CASE("template head survives a nested-template default", "[templates]") {
  auto m = parse_fixture("nested_templates.hpp");

  const auto* holder = find(m, "nested::Holder");
  REQUIRE(holder != nullptr);
  CHECK(holder->kind == model::SymbolKind::ClassTemplate);
  // The head closes with `>>`, which a raw lex reports as one token; before it
  // was split the depth never returned to 0 and the whole head was dropped.
  CHECK(holder->signature.find("template<") != std::string::npos);
  CHECK(holder->signature.find("Fallback") != std::string::npos);

  const auto* t = tparam(m, *holder, 0);
  REQUIRE(t != nullptr);
  CHECK(t->name == "T");
  CHECK(t->default_repr.empty());

  const auto* fallback = tparam(m, *holder, 1);
  REQUIRE(fallback != nullptr);
  CHECK(fallback->name == "Fallback");
  CHECK(fallback->default_repr.find("Pair") != std::string::npos);
}

TEST_CASE("a shift in a non-type default is not an argument list",
          "[templates]") {
  auto m = parse_fixture("nested_templates.hpp");

  const auto* shifted = find(m, "nested::Shifted");
  REQUIRE(shifted != nullptr);
  CHECK(shifted->signature.find("template<") != std::string::npos);

  const auto* bits = tparam(m, *shifted, 1);
  REQUIRE(bits != nullptr);
  CHECK(bits->name == "Bits");
  CHECK(bits->kind == model::TemplateParameter::Kind::NonType);
  CHECK(bits->default_repr.find("<<") != std::string::npos);
  CHECK(bits->default_repr.find("4") != std::string::npos);
}

#endif  // CLANGQUILL_HAVE_LIBCLANG

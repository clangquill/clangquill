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

namespace {

// The specialization of `spec::Traits` whose display name carries @p args
// (every specialization shares the primary's qualified name).
const model::Symbol* traits_with(const model::ParsedModule& m,
                                 const std::string& args) {
  for (const auto& s : m.symbols) {
    if (s.spelling == "Traits" && s.display_name == "Traits" + args) return &s;
  }
  return nullptr;
}

}  // namespace

TEST_CASE("a full specialization is a class template, not a plain struct",
          "[templates]") {
  auto m = parse_fixture("specializations.hpp");

  const auto* full = traits_with(m, "<int, void>");
  REQUIRE(full != nullptr);
  // libclang reports it as a StructDecl; without the specialization check it
  // was a Struct named `spec::Traits`, indistinguishable from the primary.
  CHECK(full->kind == model::SymbolKind::ClassTemplate);
  CHECK(full->signature == "template<>");

  const auto* primary = traits_with(m, "<T, Tag>");
  REQUIRE(primary != nullptr);
  CHECK(primary->kind == model::SymbolKind::ClassTemplate);
  CHECK(primary->usr != full->usr);
  CHECK(primary->signature.find("typename T") != std::string::npos);

  const auto* partial = traits_with(m, "<U *, void>");
  REQUIRE(partial != nullptr);
  CHECK(partial->kind == model::SymbolKind::ClassTemplate);
  CHECK(partial->signature == "template<typename U>");
}

TEST_CASE("an explicit instantiation declares nothing to document",
          "[templates]") {
  auto m = parse_fixture("specializations.hpp");

  // `template struct Traits<char, void>;` writes no head of its own: it asks
  // for code for the primary template, which is already documented, so it must
  // not add a second row under the primary's name.
  CHECK(traits_with(m, "<char, void>") == nullptr);

  int traits_rows = 0;
  for (const auto& s : m.symbols) {
    if (s.qualified_name == "spec::Traits") ++traits_rows;
  }
  CHECK(traits_rows == 3);  // primary, full specialization, partial
}

namespace {

// The `vars::is_foo_v` whose display name carries @p args ("" for the primary).
const model::Symbol* is_foo_v(const model::ParsedModule& m,
                              const std::string& args) {
  for (const auto& s : m.symbols) {
    if (s.spelling == "is_foo_v" && s.display_name == "is_foo_v" + args) {
      return &s;
    }
  }
  return nullptr;
}

}  // namespace

TEST_CASE("a variable template is recorded as a variable with a head",
          "[templates]") {
  auto m = parse_fixture("variable_templates.hpp");

  // libclang reports it as CXCursor_UnexposedDecl, which the visitor skipped
  // without descending: the symbol was absent from the IR entirely.
  const auto* primary = is_foo_v(m, "");
  REQUIRE(primary != nullptr);
  CHECK(primary->kind == model::SymbolKind::Variable);
  CHECK(primary->signature == "template<typename T>");
  CHECK(primary->type_repr == "inline constexpr bool");
  CHECK(primary->is_documented);
  CHECK(primary->qualified_name == "vars::is_foo_v");

  // A type that closes two argument lists at once is recovered whole.
  const model::Symbol* pair = nullptr;
  for (const auto& s : m.symbols) {
    if (s.spelling == "empty_pair") pair = &s;
  }
  REQUIRE(pair != nullptr);
  CHECK(pair->type_repr == "inline constexpr Pair<T, Pair<int, int>>");
}

TEST_CASE("a specialized variable template keeps its arguments", "[templates]") {
  auto m = parse_fixture("variable_templates.hpp");

  const auto* primary = is_foo_v(m, "");
  const auto* spec = is_foo_v(m, "<int>");
  REQUIRE(primary != nullptr);
  REQUIRE(spec != nullptr);
  // Both spell `is_foo_v` and share a qualified name; the display name is what
  // keeps them apart in the generated docs, as it does for class templates.
  CHECK(spec->usr != primary->usr);
  CHECK(spec->kind == model::SymbolKind::Variable);
  CHECK(spec->signature == "template<>");
}

TEST_CASE("unexposed declarations that document nothing stay out",
          "[templates]") {
  auto m = parse_fixture("variable_templates.hpp");

  for (const auto& s : m.symbols) {
    // A deduction guide is spelled `<deduction guide for Wrapper>` -- neither a
    // C++ name nor something the C++ domain can render -- in both the plain and
    // the templated form (the latter is exposed, as a FunctionTemplate).
    CHECK(s.spelling.rfind("<deduction guide", 0) != 0);
    // A namespace alias and a using-declaration name an entity documented where
    // it was declared; they introduce none of their own.
    CHECK(s.spelling != "shorthand");
    CHECK(s.qualified_name != "vars::Impl");
  }
}

#endif  // CLANGQUILL_HAVE_LIBCLANG

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

model::ParsedModule parse_m7() {
  parser::ParseOptions opts;
  parser::Parser p(opts);
  model::ParsedModule mod;
  p.parse_file(std::string(CLANGQUILL_FIXTURE_DIR) + "/m7.hpp", mod);
  return mod;
}

const model::Symbol* find(const model::ParsedModule& m, const std::string& qn) {
  for (const auto& s : m.symbols) {
    if (s.qualified_name == qn) return &s;
  }
  return nullptr;
}

}  // namespace

TEST_CASE("parser maps concepts and macros to new kinds", "[m7]") {
  auto m = parse_m7();

  const auto* addable = find(m, "m7::Addable");
  REQUIRE(addable != nullptr);
  CHECK(addable->kind == model::SymbolKind::Concept);
  CHECK(addable->signature.find("template<") != std::string::npos);

  const auto* pi = find(m, "CQ_PI");
  REQUIRE(pi != nullptr);
  CHECK(pi->kind == model::SymbolKind::Macro);
  CHECK(pi->signature == "CQ_PI");

  const auto* max_macro = find(m, "CQ_MAX");
  REQUIRE(max_macro != nullptr);
  CHECK(max_macro->kind == model::SymbolKind::Macro);
  CHECK(max_macro->signature == "CQ_MAX(a, b)");
}

TEST_CASE("parser extracts template parameters with defaults", "[m7]") {
  auto m = parse_m7();
  const auto* buffer = find(m, "m7::Buffer");
  REQUIRE(buffer != nullptr);
  CHECK(buffer->kind == model::SymbolKind::ClassTemplate);
  CHECK(buffer->signature.find("template<") != std::string::npos);

  int type_params = 0;
  int nontype_params = 0;
  std::string n_default;
  for (const auto& tp : m.template_parameters) {
    if (tp.owner_usr != buffer->usr) continue;
    if (tp.kind == model::TemplateParameter::Kind::Type) {
      ++type_params;
      CHECK(tp.name == "T");
    } else if (tp.kind == model::TemplateParameter::Kind::NonType) {
      ++nontype_params;
      CHECK(tp.name == "N");
      n_default = tp.default_repr;
    }
  }
  CHECK(type_params == 1);
  CHECK(nontype_params == 1);
  CHECK(n_default == "4");  // recovered from declaration tokens
}

TEST_CASE("parser records friend relationships", "[m7]") {
  auto m = parse_m7();
  const auto* vec = find(m, "m7::Vec");
  REQUIRE(vec != nullptr);

  bool friend_fn = false;
  bool friend_class = false;
  for (const auto& r : m.references) {
    if (r.from_usr != vec->usr || r.kind != model::RefKind::Friend) continue;
    if (r.to_spelling.find("reset") != std::string::npos) friend_fn = true;
    if (r.to_spelling.find("Inspector") != std::string::npos) friend_class = true;
  }
  CHECK(friend_fn);
  CHECK(friend_class);
}

TEST_CASE("parser gives hidden friends a symbol", "[m7]") {
  auto m = parse_m7();
  const auto* eq = find(m, "m7::operator==");
  REQUIRE(eq != nullptr);
  CHECK(eq->kind == model::SymbolKind::Function);
  CHECK(eq->is_documented);

  const auto* ns = find(m, "m7");
  REQUIRE(ns != nullptr);
  CHECK(eq->parent_usr == ns->usr);

  bool has_comment = false;
  for (const auto& c : m.comments) {
    if (c.symbol_usr == eq->usr &&
        c.text.find("Compares two vectors") != std::string::npos) {
      has_comment = true;
    }
  }
  CHECK(has_comment);
}

TEST_CASE("parser captures operator overloads", "[m7]") {
  auto m = parse_m7();
  const auto* plus = find(m, "m7::operator+");
  REQUIRE(plus != nullptr);
  CHECK(plus->kind == model::SymbolKind::Function);

  const auto* subscript = find(m, "m7::Vec::operator[]");
  REQUIRE(subscript != nullptr);
  CHECK(subscript->kind == model::SymbolKind::Method);
}

TEST_CASE("parser captures conversion operators", "[m7]") {
  auto m = parse_m7();
  const auto* conv = find(m, "m7::Vec::operator bool");
  REQUIRE(conv != nullptr);
  CHECK(conv->kind == model::SymbolKind::Method);
}

TEST_CASE("parser assembles Doxygen groups and members", "[m7]") {
  auto m = parse_m7();

  const model::Group* math = nullptr;
  for (const auto& g : m.groups) {
    if (g.id == "math") math = &g;
  }
  REQUIRE(math != nullptr);
  CHECK(math->title == "Math utilities");
  CHECK(math->brief.find("arithmetic") != std::string::npos);

  const auto* add = find(m, "m7::add");
  REQUIRE(add != nullptr);
  bool add_in_math = false;
  for (const auto& member : m.group_members) {
    if (member.group_id == "math" && member.member_usr == add->usr) {
      add_in_math = true;
    }
  }
  CHECK(add_in_math);
}

TEST_CASE("parser extracts parameters and references for function templates",
         "[m7]") {
  auto m = parse_m7();

  const auto* max_value = find(m, "m7::max_value");
  REQUIRE(max_value != nullptr);
  CHECK(max_value->kind == model::SymbolKind::FunctionTemplate);

  std::vector<model::FunctionParameter> mv_params;
  for (const auto& p : m.parameters) {
    if (p.function_usr == max_value->usr) mv_params.push_back(p);
  }
  REQUIRE(mv_params.size() == 2);
  CHECK(mv_params[0].name == "a");
  CHECK(mv_params[0].index == 0);
  CHECK(mv_params[1].name == "b");
  CHECK(mv_params[1].index == 1);

  int mv_param_refs = 0;
  for (const auto& r : m.references) {
    if (r.from_usr == max_value->usr && r.kind == model::RefKind::ParamType) {
      ++mv_param_refs;
    }
  }
  CHECK(mv_param_refs == 2);

  const auto* make_vec = find(m, "m7::make_vec");
  REQUIRE(make_vec != nullptr);
  CHECK(make_vec->kind == model::SymbolKind::FunctionTemplate);

  std::vector<model::FunctionParameter> mkv_params;
  for (const auto& p : m.parameters) {
    if (p.function_usr == make_vec->usr) mkv_params.push_back(p);
  }
  REQUIRE(mkv_params.size() == 2);
  CHECK(mkv_params[0].name == "x");
  CHECK(mkv_params[1].name == "y");

  bool found_return_ref = false;
  for (const auto& r : m.references) {
    if (r.from_usr == make_vec->usr && r.kind == model::RefKind::ReturnType) {
      found_return_ref = true;
      CHECK(r.to_spelling.find("Vec") != std::string::npos);
      CHECK(r.is_resolved);
    }
  }
  CHECK(found_return_ref);
}

#else  // !CLANGQUILL_HAVE_LIBCLANG

TEST_CASE("m7 parser tests skipped without libclang", "[m7][!mayfail]") {
  SUCCEED("built without libclang");
}

#endif

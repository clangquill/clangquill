#include <catch2/catch_test_macros.hpp>

#include <filesystem>
#include <fstream>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "model/module.hpp"

#if defined(CLANGQUILL_HAVE_LIBCLANG)
#include "parser/parser.hpp"
#endif

// Generative round-trip coverage for the token-heuristic layer in
// src/cpp/parser/cursor_utils.cpp (issue #310). Rather than hand-writing one
// fixture per shape, this file *builds* a large, varied source file out of
// small combinatorial families -- nested template-id depth, shift and
// parenthesized non-type-parameter defaults, brace-inits, a fold expression,
// concept-constrained parameters -- and checks one invariant on every
// generated declaration: the recovered default/head text must be either
// exactly right or empty. A truncated-but-nonempty answer (the class of bug
// this suite exists to catch -- see the fix for the `(N > 2)`-style default
// that used to stop at the `>`) fails loudly instead of passing quietly.
//
// Every case is built the same way: as a list of source *tokens*, joined with
// single spaces both for the declaration text embedded in the synthesized
// file and for the expected recovered text. append_token() in
// cursor_utils.cpp joins the same way, so this makes exact-match assertions
// mechanical instead of hand-transcribed (and therefore trustworthy at a few
// hundred cases, not just a handful).

using namespace clangquill;

#if defined(CLANGQUILL_HAVE_LIBCLANG)

namespace {

std::filesystem::path unique_temp_dir(const std::string& label) {
  namespace fs = std::filesystem;
  std::random_device entropy;
  return fs::temp_directory_path() / (label + "-" + std::to_string(entropy()));
}

std::string join(const std::vector<std::string>& toks) {
  std::string out;
  for (const auto& t : toks) {
    if (!out.empty()) out += ' ';
    out += t;
  }
  return out;
}

const model::Symbol* find_symbol(const model::ParsedModule& m,
                                 const std::string& spelling) {
  for (const auto& s : m.symbols) {
    if (s.spelling == spelling) return &s;
  }
  return nullptr;
}

const model::TemplateParameter* tparam(const model::ParsedModule& m,
                                       const std::string& owner_usr,
                                       int index) {
  for (const auto& tp : m.template_parameters) {
    if (tp.owner_usr == owner_usr && tp.index == index) return &tp;
  }
  return nullptr;
}

const model::FunctionParameter* fparam(const model::ParsedModule& m,
                                       const std::string& function_usr,
                                       int index) {
  for (const auto& p : m.parameters) {
    if (p.function_usr == function_usr && p.index == index) return &p;
  }
  return nullptr;
}

// One generated non-type-template-parameter default: `owner` is the unique
// class-template name; the default is the last parameter in its head.
struct HeadDefaultCase {
  std::string owner;
  std::string expected_default;
  std::string label;
};

// One generated `template<...>` head, checked as a whole (no per-parameter
// default): used for shapes template_head() must reconstruct correctly but
// that param_default()/defaults_out don't apply to -- a trailing `requires`
// clause, a constrained parameter with explicit template arguments.
struct HeadOnlyCase {
  std::string owner;
  std::string expected_head;
  std::string label;
};

// One generated function-parameter default.
struct ParamCase {
  std::string func;
  std::string expected_default;
  std::string label;
};

// One generated macro signature: what macro_signature() must recover for the
// #define with the given name (an object-like macro expects its bare name).
struct MacroCase {
  std::string name;
  std::string expected_signature;
  std::string label;
};

struct Generated {
  std::ostringstream source;
  std::vector<HeadDefaultCase> head_defaults;
  std::vector<HeadOnlyCase> heads;
  std::vector<ParamCase> params;
  std::vector<MacroCase> macros;
  int uid = 0;
  std::string next_name(const std::string& prefix) {
    return prefix + std::to_string(uid++);
  }
};

// `Box<Box<...<int>...>>` at nesting depth `d`, as the default of a class
// template's sole parameter. `tight` writes the closing `>`s contiguously
// (as real code does), exercising clang_tokenize()'s raw-lex merge into `>>`
// tokens; the non-tight form writes each `>` separately, so the same shape is
// also checked without ever relying on that merge.
void add_nested_depth(Generated& g, int d, bool tight) {
  std::string owner = g.next_name("NestDepth");
  std::vector<std::string> expected_toks;
  for (int i = 0; i < d; ++i) {
    expected_toks.push_back("Box");
    expected_toks.push_back("<");
  }
  expected_toks.push_back("int");
  for (int i = 0; i < d; ++i) expected_toks.push_back(">");

  g.source << "template <typename Fallback = ";
  for (int i = 0; i < d; ++i) g.source << "Box<";
  g.source << "int";
  if (tight) {
    for (int i = 0; i < d; ++i) g.source << ">";
    g.source << ">";  // closes the template head itself
  } else {
    for (int i = 0; i < d; ++i) g.source << " >";
    g.source << " >";
  }
  g.source << "\nstruct " << owner << " { Fallback v; };\n\n";

  g.head_defaults.push_back({owner, join(expected_toks),
                             "nested depth " + std::to_string(d) +
                                 (tight ? " (tight)" : " (spaced)")});
}

// `1 << k` / `1 >> k`: a shift must not be read as opening/closing an
// argument list. `<<` never collides with angle-bracket syntax and is
// written bare, matching real code and the existing nested_templates.hpp
// fixture. `>>` does collide -- per [temp.names] a bare, unparenthesized
// `1 >> 4` does not compile at all even with only one list open (the `>>`
// still splits, closing that list early and leaving a stray `>` the grammar
// has no use for right there; see angle_closes()) -- so real code always
// parenthesizes it, which is what's generated here too.
void add_shift(Generated& g, const std::string& op, int k) {
  std::string owner = g.next_name("Shift");
  bool needs_parens = (op == ">>");
  std::vector<std::string> toks{"1", op, std::to_string(k)};
  std::vector<std::string> written = toks;
  if (needs_parens) {
    written.insert(written.begin(), "(");
    written.push_back(")");
  }
  g.source << "template <int Bits = " << join(written) << ">\n"
           << "struct " << owner << " { int v = Bits; };\n\n";
  g.head_defaults.push_back(
      {owner, join(written), "shift " + op + " " + std::to_string(k)});
}

// `(N OP k)`: a comparison inside parens must not close the head at its `>`.
void add_paren_compare(Generated& g, const std::string& op, int k) {
  std::string owner = g.next_name("ParenCmp");
  std::vector<std::string> toks{"(", "N", op, std::to_string(k), ")"};
  g.source << "template <int N, bool B = " << join(toks) << ">\n"
           << "struct " << owner << " { bool v = B; };\n\n";
  g.head_defaults.push_back(
      {owner, join(toks), "paren compare N " + op + " " + std::to_string(k)});
}

// A handful of hand-picked compound shapes: double parens, an inner shift,
// and the case this suite exists for -- a nested template-id and a comma
// *inside* parens, which must neither be misread as closing the head nor as
// separating the next parameter.
void add_compound(Generated& g) {
  struct Compound {
    std::vector<std::string> toks;
    std::string leading;  // extra leading template parameters, or ""
    std::string label;
  };
  const std::vector<Compound> compounds = {
      {{"(", "N", ">", "1", ")", "&&", "(", "N", "<", "10", ")"}, "",
       "AND of two paren-compares"},
      {{"(", "(", "N", ">", "1", ")", "&&", "(", "N", "<", "10", ")", ")"}, "",
       "double-parenthesized AND"},
      {{"(", "N", ">>", "1", ")"}, "", "shift inside parens"},
      {{"(", "(", "N", ">>", "1", ")", ">", "0", ")"}, "",
       "nested parens with an inner shift"},
      {{"!", "(", "N", ">", "5", ")"}, "", "negated paren-compare"},
      {{"(", "Box", "<", "T", ">", "{", "}", ",", "N", ">", "2", ")"},
       "typename T, ", "nested angle-id and a comma inside parens"},
  };
  for (const auto& c : compounds) {
    std::string owner = g.next_name("Compound");
    g.source << "template <" << c.leading << "int N, bool B = " << join(c.toks)
             << ">\n"
             << "struct " << owner << " { bool v = B; };\n\n";
    g.head_defaults.push_back({owner, join(c.toks), c.label});
  }
}

// `std::enable_if_t<(sizeof(int) OP k), int> flag = k`: the parameter's own
// *type* contains a parenthesized comparison before the `=` that starts the
// default. param_default()'s pre-'=' scan has to walk past it without either
// mistaking the inner `>` for closing enable_if_t's argument list too early,
// or missing the real top-level `=` that follows.
void add_enable_if_param(Generated& g, const std::string& op, int k) {
  std::string func = g.next_name("enable_if_case_");
  g.source << "template <typename T>\n"
           << "void " << func << "(std::enable_if_t<(sizeof(T) " << op << " "
           << k << "), int> flag = " << k << ");\n\n";
  g.params.push_back(
      {func, std::to_string(k), "enable_if_t sizeof(T) " + op + " " +
                                     std::to_string(k)});
}

// `pick(1, 2, ..., n)`: commas inside a default's own call arguments are not
// the concern of param_default() (a parameter cursor's tokens already stop at
// the parameter boundary), but this still exercises the nested-call shape end
// to end.
void add_call_default(Generated& g, int argc) {
  std::string func = g.next_name("call_case_");
  std::vector<std::string> toks{"pick", "("};
  for (int i = 1; i <= argc; ++i) {
    if (i > 1) toks.push_back(",");
    toks.push_back(std::to_string(i));
  }
  toks.push_back(")");
  g.source << "void " << func << "(int x = " << join(toks) << ");\n\n";
  g.params.push_back({func, join(toks), "call default, " + std::to_string(argc) +
                                            " args"});
}

// A string literal containing angle brackets must come back whole: the raw
// lexer sees a string literal as a single token, so the characters inside it
// were never at risk of being misread as angle-bracket syntax -- this locks
// that in as a regression test rather than an assumption.
void add_string_literal_param(Generated& g, const std::string& literal_body) {
  std::string func = g.next_name("string_case_");
  std::string lit = "\"" + literal_body + "\"";
  g.source << "void " << func << "(const char* label = " << lit << ");\n\n";
  g.params.push_back({func, lit, "string literal " + lit});
}

// #define NAME(params) body: macro_signature()'s parameter-list recovery.
// This family exists for issue #334, which audited the one token-heuristic
// function #310 hadn't covered: the only text its paren matching spans is the
// parameter list itself, where the grammar allows nothing but identifiers,
// `,` and `...` -- no brackets, no braces, no angle brackets, and therefore
// nothing for in_group()/angle_closes() to do. What these cases pin down
// instead is the boundary with the replacement body that follows: its own
// parens, commas and string literals must never be read as parameters.
void add_macro(Generated& g, const std::string& params, const std::string& body,
               const std::string& label) {
  std::string name = g.next_name("CQ_GEN_MACRO_");
  g.source << "#define " << name;
  std::string expected = name;
  if (!params.empty()) {
    // No space: a space before `(` would make the macro object-like.
    g.source << "(" << params << ")";
    expected += "(" + params + ")";
  }
  g.source << body << "\n";
  g.macros.push_back({name, expected, label});
}

}  // namespace

TEST_CASE("token heuristics round-trip generatively across libclang's raw lex",
         "[templates][cursor_utils][generative]") {
  Generated g;

  g.source << "#pragma once\n#include <type_traits>\n#include <cstddef>\n\n"
           << "template <typename T>\nstruct Box { T v; };\n\n"
           << "template <typename... Args>\nint pick(Args...);\n\n";

  for (int d = 1; d <= 8; ++d) add_nested_depth(g, d, /*tight=*/true);
  for (int d = 1; d <= 5; ++d) add_nested_depth(g, d, /*tight=*/false);

  for (int k = 1; k <= 10; ++k) {
    add_shift(g, "<<", k);
    add_shift(g, ">>", k);
  }

  for (const std::string& op : {"<", "<=", ">", ">=", "==", "!="}) {
    for (int k = 1; k <= 6; ++k) add_paren_compare(g, op, k);
  }
  add_compound(g);

  for (const std::string& op : {"<", "<=", ">", ">=", "==", "!="}) {
    for (int k = 1; k <= 4; ++k) add_enable_if_param(g, op, k);
  }

  for (int argc = 1; argc <= 5; ++argc) add_call_default(g, argc);

  for (const std::string& body :
       {"<tag>", "<a><b>", "a<b>c", "<<deep>>", "x < y", "a > b && c < d"}) {
    add_string_literal_param(g, body);
  }

  // A pack expansion in a default: `sizeof...(Ts)`.
  g.source << "template <typename... Ts>\n"
           << "void variadic_case(int flag = sizeof...(Ts));\n\n";
  g.params.push_back(
      {"variadic_case", "sizeof ... ( Ts )", "pack expansion default"});

  // Concepts as constraints. A trailing `requires` clause is not part of the
  // parameter list template_head() reconstructs -- it must not appear in, or
  // corrupt, the recovered head.
  g.source
      << "template <typename T>\nconcept Addable = requires(T a, T b) { a + b; };\n\n"
      << "template <typename T, typename U>\nconcept ConvertibleTo2 = "
         "std::is_convertible_v<T, U>;\n\n"
      << "template <typename T>\nrequires Addable<T>\nstruct RequiresTail { T v; };\n\n"
      << "template <Addable T>\nstruct ConstrainedSimple { T v; };\n\n"
      << "template <ConvertibleTo2<int> T>\nstruct ConstrainedWithArg { T v; };\n\n";
  g.heads.push_back(
      {"RequiresTail", "template<typename T>", "trailing requires clause"});
  g.heads.push_back(
      {"ConstrainedSimple", "template<Addable T>", "concept constraint, no args"});
  g.heads.push_back({"ConstrainedWithArg", "template<ConvertibleTo2 < int > T>",
                     "concept constraint with explicit template argument"});

  // macro_signature()'s parameter-list recovery (issue #334; see add_macro).
  add_macro(g, "", " 1", "object-like macro");
  add_macro(g, "a", " (a)", "single parameter");
  add_macro(g, "a, b", " ((a) > (b) ? (a) : (b))", "two parameters, paren body");
  add_macro(g, "", "", "object-like macro, empty body");
  add_macro(g, "fmt, ...", " log(fmt, __VA_ARGS__)", "variadic macro");
  add_macro(g, "x", " ((x) \",)\")", "paren and comma inside a string literal");
  add_macro(g, "a, b", "", "function-like macro, empty body");

  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-token-heuristics-generative");
  std::filesystem::create_directories(dir);
  const fs::path file = dir / "generated.hpp";
  {
    std::ofstream out(file);
    out << g.source.str();
  }

  parser::ParseOptions opts;
  parser::Parser p(opts);
  model::ParsedModule m;
  bool ok = p.parse_file(file.string(), m);
  std::filesystem::remove_all(dir);

  REQUIRE(ok);
  CHECK(m.diagnostics.empty());
  INFO("generated " << g.head_defaults.size() << " head-default cases, "
                    << g.heads.size() << " head-only cases, "
                    << g.params.size() << " function-parameter cases, "
                    << g.macros.size() << " macro cases");

  for (const auto& c : g.head_defaults) {
    INFO(c.label << " (owner " << c.owner << ")");
    const auto* owner = find_symbol(m, c.owner);
    REQUIRE(owner != nullptr);
    // Every generated head-default case has its default on the *last*
    // parameter of a single- or two-parameter head.
    const model::TemplateParameter* last = nullptr;
    for (int idx = 0; idx < 4; ++idx) {
      if (const auto* tp = tparam(m, owner->usr, idx)) last = tp;
    }
    REQUIRE(last != nullptr);
    // The invariant this whole file exists to check: never a truncated,
    // nonempty answer. A generated case is always well-formed, so an empty
    // recovery here would itself be the bug (unlike a real-world "documented
    // bail" shape, which this suite doesn't synthesize).
    CHECK(last->default_repr == c.expected_default);
  }

  for (const auto& c : g.heads) {
    INFO(c.label << " (owner " << c.owner << ")");
    const auto* owner = find_symbol(m, c.owner);
    REQUIRE(owner != nullptr);
    CHECK(owner->signature == c.expected_head);
  }

  for (const auto& c : g.params) {
    INFO(c.label << " (function " << c.func << ")");
    const auto* fn = find_symbol(m, c.func);
    REQUIRE(fn != nullptr);
    const auto* p0 = fparam(m, fn->usr, 0);
    REQUIRE(p0 != nullptr);
    CHECK(p0->default_value == c.expected_default);
  }

  for (const auto& c : g.macros) {
    INFO(c.label << " (macro " << c.name << ")");
    const auto* macro = find_symbol(m, c.name);
    REQUIRE(macro != nullptr);
    REQUIRE(macro->kind == model::SymbolKind::Macro);
    CHECK(macro->signature == c.expected_signature);
  }
}

#endif  // CLANGQUILL_HAVE_LIBCLANG

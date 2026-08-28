#include <catch2/catch_test_macros.hpp>

#include <set>
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

TEST_CASE("a group command takes its line, not the paragraph below it",
          "[comments]") {
  // `\ingroup a b` names two groups. Treating it as a paragraph command swept
  // the prose in too, and since group ids are split on whitespace every word
  // became a group — 408 of them on Eigen, and a page each.
  auto m = parse_fixture("structural.hpp");
  const auto* fn = find(m, "grouped_helper");
  REQUIRE(fn != nullptr);

  std::set<std::string> ids;
  for (const auto& gm : m.group_members) {
    if (gm.member_usr == fn->usr) ids.insert(gm.group_id);
  }
  CHECK(ids == std::set<std::string>{"shapes", "widgets"});

  // Nothing from the prose may have become a group anywhere in the module.
  for (const auto& g : m.groups) {
    CHECK(g.id.find(' ') == std::string::npos);
    CHECK(g.id != "sentence");
    CHECK(g.id != "documents");
  }

  // fields_of returns by value: field() must be called on a named vector, not
  // on that temporary directly, or the pointer it returns dangles the moment
  // this statement ends (#266 -- the empty/garbage brief only ever showed up
  // downstream of exactly this pattern).
  auto fs = fields_of(m, fn->usr);
  const Field* brief = field(fs, "brief");
  REQUIRE(brief != nullptr);
  CAPTURE(brief->value);
  CHECK(brief->value.find("documents the") != std::string::npos);
}

TEST_CASE("a no-argument command leaves the prose to the entity", "[comments]") {
  auto m = parse_fixture("structural.hpp");
  // The prose has to survive as renderable text. Whether it lands in brief or
  // in detail is a presentation nuance; being eaten by the marker is the bug.
  for (const auto* qn : {"internal_helper", "marker_helper"}) {
    const auto* fn = find(m, qn);
    REQUIRE(fn != nullptr);
    auto fs = fields_of(m, fn->usr);
    const Field* brief = field(fs, "brief");
    const Field* detail = field(fs, "detail");
    const bool renders = (brief != nullptr && !brief->value.empty()) ||
                         (detail != nullptr && !detail->value.empty());
    CHECK(renders);
    // ... and the marker itself must not have taken it.
    const Field* marker = field(fs, qn == std::string("marker_helper") ? "li" : "internal");
    if (marker != nullptr) CHECK(marker->value.empty());
  }
  // Both paragraphs survive, not just the first.
  const auto* marker = find(m, "marker_helper");
  REQUIRE(marker != nullptr);
  CHECK(field(fields_of(m, marker->usr), "detail") != nullptr);
}

TEST_CASE("a marker does not own the rest of its line", "[comments]") {
  // Eigen writes `\internal \ingroup enums` and `\internal \class Foo`. If the
  // marker swallows its line, the brief becomes a literal "\ingroup enums" and
  // the group membership is silently dropped.
  auto m = parse_fixture("structural.hpp");

  const auto* fn = find(m, "internal_grouped");
  REQUIRE(fn != nullptr);
  auto fs = fields_of(m, fn->usr);
  const Field* group = field(fs, "ingroup");
  REQUIRE(group != nullptr);
  CHECK(group->value == "shapes");
  bool joined = false;
  for (const auto& gm : m.group_members) {
    if (gm.member_usr == fn->usr && gm.group_id == "shapes") joined = true;
  }
  CHECK(joined);
  const Field* brief = field(fs, "brief");
  REQUIRE(brief != nullptr);
  CHECK(brief->value.find("still the entity") != std::string::npos);
  CHECK(brief->value.find('\\') == std::string::npos);

  // ... and the rescan chains into a single-name command.
  const auto* chained = find(m, "Chained");
  REQUIRE(chained != nullptr);
  CHECK(chained->is_documented);
}

TEST_CASE("a blank line ends a paragraph command", "[comments]") {
  // A `takes_paragraph` command runs to the next blank line. Letting it run on
  // put a symbol's whole detailed description inside its one-line brief, and
  // folded the prose after a `\param` into that parameter's description.
  auto m = parse_fixture("structural.hpp");
  const auto* fn = find(m, "paragraph_helper");
  REQUIRE(fn != nullptr);
  auto fs = fields_of(m, fn->usr);

  const Field* brief = field(fs, "brief");
  REQUIRE(brief != nullptr);
  CHECK(brief->value == "A blank line ends the brief.");

  const Field* param = field(fs, "param", "a");
  REQUIRE(param != nullptr);
  CHECK(param->value == "the input value");

  // Both paragraphs below a blank line belong to the entity.
  bool detailed = false, closing = false;
  for (const auto& f : fs) {
    if (f.name != "detail") continue;
    if (f.value.find("detailed description") != std::string::npos) detailed = true;
    if (f.value.find("closing paragraph") != std::string::npos) closing = true;
  }
  CHECK(detailed);
  CHECK(closing);
}

TEST_CASE("a param direction survives both parse paths", "[comments]") {
  // `comment_fields` carries the direction in the arg column, in the bracketed
  // form Doxygen writes it. The raw path used to take `param[out]` as the whole
  // command name, so the entry never reached `params` at all; the parsed path
  // dropped the direction silently.
  auto check = [](const std::vector<Field>& fs) {
    REQUIRE(field(fs, "param", "[out] result") != nullptr);
    CHECK(field(fs, "param", "[out] result")->value ==
          "where the answer is written");
    CHECK(field(fs, "param", "[in] value") != nullptr);
    CHECK(field(fs, "param", "[in,out] scratch") != nullptr);
    // An undirected parameter is spelled exactly as before.
    const Field* plain = field(fs, "param", "plain");
    REQUIRE(plain != nullptr);
    CHECK(plain->value == "a parameter with no direction attribute");
  };

  auto parsed = parse_fixture("doxygen.hpp");
  const auto* fill = find(parsed, "doc::fill");
  REQUIRE(fill != nullptr);
  check(fields_of(parsed, fill->usr));

  // `\ingroup` forces the raw path for this one.
  auto raw = parse_fixture("structural.hpp");
  const auto* directed = find(raw, "directed_helper");
  REQUIRE(directed != nullptr);
  check(fields_of(raw, directed->usr));
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

TEST_CASE("a verbatim block keeps its lines, language and place",
          "[comments]") {
  // Collapsing the block through normalize_ws turned every code example into
  // one line of mangled prose. Since the output is Markdown, its newlines and
  // relative indentation are load-bearing.
  auto expect_block = [](const std::vector<Field>& fs, const std::string& fence,
                         const std::string& indented) {
    const Field* block = nullptr;
    int prose_after = 0;
    for (const auto& f : fs) {
      if (f.name != "detail") continue;
      if (block == nullptr && f.value.rfind(fence, 0) == 0) block = &f;
      else if (block != nullptr &&
               f.value.find("stays after it") != std::string::npos) {
        ++prose_after;
      }
    }
    REQUIRE(block != nullptr);
    // Line structure survives, and the marker indent is removed while the
    // example's own indentation is kept.
    CHECK(block->value.find('\n') != std::string::npos);
    CHECK(block->value.find(indented) != std::string::npos);
    CHECK(block->value.substr(block->value.size() - 3) == "```");
    // ... and the block stays where it was written, before the closing prose.
    CHECK(prose_after == 1);
  };

  // Parsed path: `@code` with no attribute in a C++ header.
  auto parsed = parse_fixture("doxygen.hpp");
  const auto* sq = find(parsed, "doc::square");
  REQUIRE(sq != nullptr);
  expect_block(fields_of(parsed, sq->usr), "```cpp\n", "\n  return y;");

  // Raw path (forced by `\ingroup`): `\code{.py}` carries its language.
  auto raw = parse_fixture("structural.hpp");
  const auto* coded = find(raw, "coded_helper");
  REQUIRE(coded != nullptr);
  expect_block(fields_of(raw, coded->usr), "```py\n", "\n    print(y)");
}

TEST_CASE("inline markup and HTML reach the reader", "[comments]") {
  // Inline commands used to survive as literal backslash text (and a `\ref` at
  // the start of a wrapped line was mistaken for a block command, swallowing
  // the rest of the paragraph); HTML tags were deleted outright, taking their
  // list and emphasis structure with them.
  auto prose_of = [](const std::vector<Field>& fs) {
    std::string all;
    for (const auto& f : fs) {
      if (f.name == "brief" || f.name == "detail") all += f.value + '\n';
    }
    return all;
  };
  auto check_markup = [](const std::string& prose) {
    CHECK(prose.find("**bold**") != std::string::npos);
    CHECK(prose.find("*italic*") != std::string::npos);
    CHECK(prose.find("`code`") != std::string::npos);
    CHECK(prose.find("<b>tags</b>") != std::string::npos);
    CHECK(prose.find("<li>list items") != std::string::npos);
    // No inline command may reach the output as literal backslash text.
    CHECK(prose.find("\\b ") == std::string::npos);
    CHECK(prose.find("\\c ") == std::string::npos);
  };

  auto parsed = parse_fixture("doxygen.hpp");
  const auto* emphasize = find(parsed, "doc::emphasize");
  REQUIRE(emphasize != nullptr);
  std::string parsed_prose = prose_of(fields_of(parsed, emphasize->usr));
  check_markup(parsed_prose);
  // `\ref target "a title"` becomes a role carrying that title.
  CHECK(parsed_prose.find("{cpp:any}`the divide function <divide>`") !=
        std::string::npos);

  auto raw = parse_fixture("structural.hpp");
  const auto* helper = find(raw, "inline_helper");
  REQUIRE(helper != nullptr);
  auto fs = fields_of(raw, helper->usr);
  check_markup(prose_of(fs));

  // A wrapped line beginning with `\ref` is prose, not a block command: the
  // sentence stays whole and nothing lands in custom["ref"].
  const Field* brief = field(fs, "brief");
  REQUIRE(brief != nullptr);
  CHECK(brief->value ==
        "A wrapped sentence about {cpp:any}`Widget` stays one sentence.");
  CHECK(field(fs, "ref") == nullptr);

  // Punctuation closing the clause is not part of the decorated word. Carrying
  // a `)` into a role makes it an "Unparseable C++ cross-reference", which a
  // warnings-as-errors docs build turns into a hard failure -- and a target
  // that names no C++ entity degrades to a code span for the same reason.
  std::string prose = prose_of(fs);
  CHECK(prose.find("(see {cpp:any}`Widget`)") != std::string::npos);
  CHECK(prose.find("`x`:") != std::string::npos);
  CHECK(prose.find("`some-page`") != std::string::npos);
  CHECK(prose.find("{cpp:any}`some-page`") == std::string::npos);
}

TEST_CASE("a parsed comment stores its format alongside its fields",
          "[comments]") {
  auto m = parse_fixture("doxygen.hpp");
  const auto* divide = find(m, "doc::divide");
  REQUIRE(divide != nullptr);

  bool found = false;
  for (const auto& c : m.comments) {
    if (c.symbol_usr == divide->usr) {
      found = true;
      CHECK(c.format == "doxygen");
    }
  }
  CHECK(found);

  // The structured parse lives in `comment_fields` and nowhere else; the row
  // above carries only the verbatim text and the dialect it was parsed as.
  auto fs = fields_of(m, divide->usr);
  REQUIRE(field(fs, "brief") != nullptr);
  const Field* returns = field(fs, "returns");
  REQUIRE(returns != nullptr);
  CHECK(returns->value.find("quotient") != std::string::npos);
}

#else  // !CLANGQUILL_HAVE_LIBCLANG

TEST_CASE("comment parser tests skipped without libclang", "[comments][!mayfail]") {
  SUCCEED("built without libclang");
}

#endif

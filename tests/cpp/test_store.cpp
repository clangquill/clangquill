#include <catch2/catch_test_macros.hpp>

#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <random>
#include <stdexcept>
#include <string>

#include "store/sqlite_store.hpp"

using namespace clangquill;

namespace {

// A unique path in the platform's temp directory. std::filesystem rather than
// mkstemp(3), which lives in <unistd.h> and does not exist under MSVC (nor
// does /tmp). CTest runs each Catch2 case in its own process, so the name has
// to be unique across processes and not merely within one; sqlite creates the
// file on open, so it does not need to exist up front.
std::string temp_db_path() {
  namespace fs = std::filesystem;
  std::random_device entropy;
  for (int attempt = 0; attempt < 64; ++attempt) {
    const fs::path candidate =
        fs::temp_directory_path() /
        ("clangquill_test_" + std::to_string(entropy()) + ".sqlite");
    if (!fs::exists(candidate)) return candidate.string();
  }
  throw std::runtime_error("could not find an unused temporary database path");
}

model::ParsedModule make_module() {
  model::ParsedModule m;

  model::SourceFile f;
  f.path = "/tmp/example.hpp";
  f.sha256 = std::string(64, 'a');
  f.size_bytes = 123;
  m.files.push_back(f);

  model::Symbol s;
  s.usr = "c:@S@Widget";
  s.kind = model::SymbolKind::Class;
  s.spelling = "Widget";
  s.qualified_name = "Widget";
  s.display_name = "Widget";
  s.is_definition = true;
  s.is_documented = true;
  s.content_hash = "deadbeef";
  s.location.file_path = "/tmp/example.hpp";
  s.location.line = 10;
  s.location.column = 7;
  m.symbols.push_back(s);

  // Owning symbols referenced by the parameter/enumerator rows below, so the
  // foreign keys are satisfied (mirrors what the parser always produces).
  model::Symbol method;
  method.usr = "c:@S@Widget@F@resize";
  method.kind = model::SymbolKind::Method;
  method.spelling = "resize";
  method.qualified_name = "Widget::resize";
  method.display_name = "resize(int)";
  m.symbols.push_back(method);

  model::Symbol enm;
  enm.usr = "c:@E@Color";
  enm.kind = model::SymbolKind::Enum;
  enm.spelling = "Color";
  enm.qualified_name = "Color";
  enm.display_name = "Color";
  m.symbols.push_back(enm);

  model::FunctionParameter p;
  p.function_usr = "c:@S@Widget@F@resize";
  p.index = 0;
  p.name = "size";
  p.type_repr = "int";
  m.parameters.push_back(p);

  model::Reference r;
  r.from_usr = "c:@S@Widget";
  r.kind = model::RefKind::BaseClass;
  r.to_usr = "c:@S@Base";
  r.to_spelling = "Base";
  r.is_resolved = true;
  r.access = model::AccessKind::Public;
  m.references.push_back(r);

  model::Enumerator e;
  e.usr = "c:@E@Color@Red";
  e.enum_usr = "c:@E@Color";
  e.name = "Red";
  e.value = 0;
  m.enumerators.push_back(e);

  model::RawComment c;
  c.symbol_usr = "c:@S@Widget";
  c.text = "/// A widget.";
  m.comments.push_back(c);

  return m;
}

}  // namespace

TEST_CASE("SqliteStore write/read round-trips the IR", "[store]") {
  std::string path = temp_db_path();
  model::ParsedModule original = make_module();

  {
    store::SqliteStore writer(path);
    writer.write(original, store::Meta::current());
  }

  store::SqliteStore reader(path);
  model::ParsedModule got = reader.read();

  REQUIRE(got.files.size() == 1);
  CHECK(got.files[0].path == "/tmp/example.hpp");
  CHECK(got.files[0].sha256 == std::string(64, 'a'));

  REQUIRE(got.symbols.size() == 3);
  const model::Symbol* widget = nullptr;
  for (const auto& s : got.symbols) {
    if (s.usr == "c:@S@Widget") widget = &s;
  }
  REQUIRE(widget != nullptr);
  CHECK(widget->kind == model::SymbolKind::Class);
  CHECK(widget->is_definition);
  CHECK(widget->is_documented);
  CHECK(widget->location.file_path == "/tmp/example.hpp");
  CHECK(widget->location.line == 10);

  REQUIRE(got.parameters.size() == 1);
  CHECK(got.parameters[0].name == "size");

  REQUIRE(got.references.size() == 1);
  CHECK(got.references[0].to_spelling == "Base");
  CHECK(got.references[0].is_resolved);
  CHECK(got.references[0].access == model::AccessKind::Public);

  REQUIRE(got.enumerators.size() == 1);
  CHECK(got.enumerators[0].name == "Red");

  REQUIRE(got.comments.size() == 1);
  CHECK(got.comments[0].text == "/// A widget.");

  std::remove(path.c_str());
}

TEST_CASE("SqliteStore write_tus replaces only the re-parsed file's rows",
          "[store]") {
  // Two files in the IR: one to re-parse, one that must survive untouched.
  model::ParsedModule original;

  model::SourceFile a;
  a.path = "/tmp/a.hpp";
  a.sha256 = std::string(64, 'a');
  a.size_bytes = 1;
  original.files.push_back(a);

  model::SourceFile b;
  b.path = "/tmp/b.hpp";
  b.sha256 = std::string(64, 'b');
  b.size_bytes = 2;
  original.files.push_back(b);

  model::Symbol af;
  af.usr = "c:@F@a_old";
  af.kind = model::SymbolKind::Function;
  af.spelling = "a_old";
  af.qualified_name = "a_old";
  af.display_name = "a_old()";
  af.location.file_path = "/tmp/a.hpp";
  original.symbols.push_back(af);

  model::FunctionParameter ap;
  ap.function_usr = "c:@F@a_old";
  ap.index = 0;
  ap.name = "x";
  ap.type_repr = "int";
  original.parameters.push_back(ap);

  model::Symbol bf;
  bf.usr = "c:@F@b_keep";
  bf.kind = model::SymbolKind::Function;
  bf.spelling = "b_keep";
  bf.qualified_name = "b_keep";
  bf.display_name = "b_keep()";
  bf.location.file_path = "/tmp/b.hpp";
  original.symbols.push_back(bf);

  std::string path = temp_db_path();
  {
    store::SqliteStore writer(path);
    writer.write(original, store::Meta::current());
  }

  // Re-parse a.hpp: its old symbol is dropped and a new one takes its place,
  // with a refreshed file hash. b.hpp appears in the module's *file* list —
  // exactly what happens when the re-parsed unit #includes another input — and
  // its rows must survive because it is not in the replaced set.
  model::ParsedModule reparse;
  model::SourceFile a2;
  a2.path = "/tmp/a.hpp";
  a2.sha256 = std::string(64, 'c');
  a2.size_bytes = 9;
  reparse.files.push_back(a2);

  model::SourceFile b2;
  b2.path = "/tmp/b.hpp";
  b2.sha256 = std::string(64, 'b');
  b2.size_bytes = 2;
  reparse.files.push_back(b2);

  model::Symbol an;
  an.usr = "c:@F@a_new";
  an.kind = model::SymbolKind::Function;
  an.spelling = "a_new";
  an.qualified_name = "a_new";
  an.display_name = "a_new()";
  an.location.file_path = "/tmp/a.hpp";
  reparse.symbols.push_back(an);

  {
    store::SqliteStore writer(path);
    REQUIRE_NOTHROW(
        writer.write_tus(reparse, store::Meta::current(), {"/tmp/a.hpp"}));
  }

  store::SqliteStore reader(path);
  model::ParsedModule got = reader.read();

  // a.hpp's symbol was replaced; its stale parameter row cascaded away.
  bool has_a_old = false;
  bool has_a_new = false;
  bool has_b = false;
  for (const auto& s : got.symbols) {
    if (s.usr == "c:@F@a_old") has_a_old = true;
    if (s.usr == "c:@F@a_new") has_a_new = true;
    if (s.usr == "c:@F@b_keep") has_b = true;
  }
  CHECK_FALSE(has_a_old);
  CHECK(has_a_new);
  CHECK(has_b);  // the untouched file's symbol survived
  CHECK(got.parameters.empty());

  // a.hpp kept its id but refreshed its hash; b.hpp is unchanged.
  REQUIRE(got.files.size() == 2);
  for (const auto& f : got.files) {
    if (f.path == "/tmp/a.hpp") {
      CHECK(f.sha256 == std::string(64, 'c'));
      CHECK(f.size_bytes == 9);
    }
    if (f.path == "/tmp/b.hpp") CHECK(f.sha256 == std::string(64, 'b'));
  }

  std::remove(path.c_str());
}

TEST_CASE("SqliteStore tolerates a symbol seen in multiple translation units",
          "[store]") {
  model::ParsedModule m;

  model::Symbol fn;
  fn.usr = "c:@F@clamp";
  fn.kind = model::SymbolKind::Function;
  fn.spelling = "clamp";
  fn.qualified_name = "clamp";
  fn.display_name = "clamp(int)";
  m.symbols.push_back(fn);

  model::Symbol tmpl;
  tmpl.usr = "c:@FT@>1#Tidentity";
  tmpl.kind = model::SymbolKind::Function;
  tmpl.spelling = "identity";
  tmpl.qualified_name = "identity";
  tmpl.display_name = "identity";
  m.symbols.push_back(tmpl);

  // Same symbol emitted twice -> parameter rows collide on (usr, idx).
  for (int seen = 0; seen < 2; ++seen) {
    model::FunctionParameter p;
    p.function_usr = "c:@F@clamp";
    p.index = 0;
    p.name = "value";
    p.type_repr = "int";
    m.parameters.push_back(p);

    model::TemplateParameter tp;
    tp.owner_usr = "c:@FT@>1#Tidentity";
    tp.index = 0;
    tp.kind = model::TemplateParameter::Kind::Type;
    tp.name = "T";
    m.template_parameters.push_back(tp);
  }

  std::string path = temp_db_path();
  {
    store::SqliteStore writer(path);
    REQUIRE_NOTHROW(writer.write(m, store::Meta::current()));
  }

  store::SqliteStore reader(path);
  model::ParsedModule got = reader.read();

  REQUIRE(got.parameters.size() == 1);
  CHECK(got.parameters[0].name == "value");
  REQUIRE(got.template_parameters.size() == 1);
  CHECK(got.template_parameters[0].name == "T");

  std::remove(path.c_str());
}

#include <catch2/catch_test_macros.hpp>

#include <cstdio>
#include <cstdlib>
#include <string>
#include <unistd.h>

#include "store/sqlite_store.hpp"

using namespace clangquill;

namespace {

std::string temp_db_path() {
  char tmpl[] = "/tmp/clangquill_test_XXXXXX";
  int fd = mkstemp(tmpl);
  if (fd != -1) close(fd);
  return std::string(tmpl);
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

// A minimal function symbol anchored to `file`, for the group tests below.
model::Symbol group_symbol(const std::string& usr, const std::string& file) {
  model::Symbol s;
  s.usr = usr;
  s.kind = model::SymbolKind::Function;
  s.spelling = usr;
  s.qualified_name = usr;
  s.display_name = usr;
  s.location.file_path = file;
  return s;
}

// A `\defgroup`-backed group row: a real title plus prose.
model::Group titled_group() {
  model::Group g;
  g.id = "geometry";
  g.title = "Geometry helpers";
  g.brief = "Points and vectors.";
  g.detail = "The long version.";
  return g;
}

// The placeholder `ensure_group` emits for an `\ingroup` reference whose
// `\defgroup` block was not in any file this parse read: title == id, and no
// brief, detail or parent.
model::Group stub_group() {
  model::Group g;
  g.id = "geometry";
  g.title = "geometry";
  return g;
}

model::GroupMember membership(const std::string& usr) {
  model::GroupMember m;
  m.group_id = "geometry";
  m.member_usr = usr;
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

TEST_CASE("SqliteStore write_tus keeps other units' group memberships",
          "[store]") {
  // Two files contribute a member to the same group, and the `\defgroup`
  // block itself lives in a third place the incremental re-parse never reads.
  model::ParsedModule original;

  model::SourceFile a;
  a.path = "/tmp/ga.hpp";
  a.sha256 = std::string(64, 'a');
  original.files.push_back(a);

  model::SourceFile b;
  b.path = "/tmp/gb.hpp";
  b.sha256 = std::string(64, 'b');
  original.files.push_back(b);

  original.symbols.push_back(group_symbol("c:@F@ga", "/tmp/ga.hpp"));
  original.symbols.push_back(group_symbol("c:@F@gb", "/tmp/gb.hpp"));
  original.groups.push_back(titled_group());
  original.group_members.push_back(membership("c:@F@ga"));
  original.group_members.push_back(membership("c:@F@gb"));

  std::string path = temp_db_path();
  {
    store::SqliteStore writer(path);
    writer.write(original, store::Meta::current());
  }

  // Re-parse ga.hpp alone. Its symbol only *references* the group, so the
  // module carries a stub row — which must neither cascade gb.hpp's membership
  // away nor overwrite the captured title and prose.
  model::ParsedModule reparse;
  model::SourceFile a2;
  a2.path = "/tmp/ga.hpp";
  a2.sha256 = std::string(64, 'c');
  reparse.files.push_back(a2);
  reparse.symbols.push_back(group_symbol("c:@F@ga", "/tmp/ga.hpp"));
  reparse.groups.push_back(stub_group());
  reparse.group_members.push_back(membership("c:@F@ga"));

  {
    store::SqliteStore writer(path);
    REQUIRE_NOTHROW(
        writer.write_tus(reparse, store::Meta::current(), {"/tmp/ga.hpp"}));
  }

  store::SqliteStore reader(path);
  model::ParsedModule got = reader.read();

  bool has_ga = false;
  bool has_gb = false;
  for (const auto& m : got.group_members) {
    if (m.member_usr == "c:@F@ga") has_ga = true;
    if (m.member_usr == "c:@F@gb") has_gb = true;
  }
  CHECK(has_ga);  // re-inserted by this write
  CHECK(has_gb);  // contributed by a unit this write never touched
  CHECK(got.group_members.size() == 2);

  REQUIRE(got.groups.size() == 1);
  CHECK(got.groups[0].title == "Geometry helpers");
  CHECK(got.groups[0].brief == "Points and vectors.");
  CHECK(got.groups[0].detail == "The long version.");

  std::remove(path.c_str());
}

TEST_CASE("SqliteStore never downgrades a group row to a stub", "[store]") {
  // Both orders have to survive: within one write the module's group list is
  // the concatenation of per-batch lists, so a stub can precede or follow the
  // titled row depending on which batch parsed the `\defgroup` block.
  for (bool stub_first : {true, false}) {
    INFO("stub_first = " << stub_first);

    model::ParsedModule m;
    model::SourceFile f;
    f.path = "/tmp/gc.hpp";
    f.sha256 = std::string(64, 'a');
    m.files.push_back(f);
    m.symbols.push_back(group_symbol("c:@F@gc", "/tmp/gc.hpp"));
    if (stub_first) {
      m.groups.push_back(stub_group());
      m.groups.push_back(titled_group());
    } else {
      m.groups.push_back(titled_group());
      m.groups.push_back(stub_group());
    }
    m.group_members.push_back(membership("c:@F@gc"));

    std::string path = temp_db_path();
    {
      store::SqliteStore writer(path);
      REQUIRE_NOTHROW(writer.write(m, store::Meta::current()));
    }

    {
      store::SqliteStore reader(path);
      model::ParsedModule got = reader.read();

      REQUIRE(got.groups.size() == 1);
      CHECK(got.groups[0].title == "Geometry helpers");
      CHECK(got.groups[0].brief == "Points and vectors.");
      REQUIRE(got.group_members.size() == 1);
      CHECK(got.group_members[0].member_usr == "c:@F@gc");
    }

    std::remove(path.c_str());
  }
}

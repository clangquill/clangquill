#include <catch2/catch_test_macros.hpp>

#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <random>
#include <stdexcept>
#include <string>

#include "store/schema.hpp"
#include "store/sqlite_raii.hpp"
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

  model::CommentField field;
  field.symbol_usr = "c:@S@Widget";
  field.name = "brief";
  field.value = "A widget.";
  m.comment_fields.push_back(field);

  model::Group group;
  group.id = "widgets";
  group.title = "Widgets";
  group.brief = "Widget types.";
  group.detail = "The long version.";
  m.groups.push_back(group);

  model::GroupMember member;
  member.group_id = "widgets";
  member.member_usr = "c:@S@Widget";
  m.group_members.push_back(member);

  // Not part of the on-disk schema (no diagnostics table): write()/read() must
  // silently drop these rather than throw or otherwise choke on them, since
  // the CLI always shares one ParsedModule between the store and its own
  // --diagnostics-log writer.
  model::Diagnostic diag;
  diag.severity = model::kSeverityWarning;
  diag.text = "widget is on its way out";
  diag.file = "/tmp/example.hpp";
  diag.line = 1;
  m.diagnostics.push_back(diag);

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

// A `\defgroup`-backed group row: a real title plus prose, and the definition
// flag only a `\defgroup` block sets.
model::Group titled_group() {
  model::Group g;
  g.id = "geometry";
  g.title = "Geometry helpers";
  g.brief = "Points and vectors.";
  g.detail = "The long version.";
  g.is_definition = true;
  return g;
}

// What an `\addtogroup geom` block with prose but no title of its own parses
// to: title == id like a stub, but carrying a brief — so the row is neither a
// definition nor recognisable as empty.
model::Group addtogroup_group() {
  model::Group g;
  g.id = "geometry";
  g.title = "geometry";
  g.brief = "Prose from an addtogroup block.";
  g.detail = "More of it.";
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

  REQUIRE(got.comment_fields.size() == 1);
  CHECK(got.comment_fields[0].symbol_usr == "c:@S@Widget");
  CHECK(got.comment_fields[0].name == "brief");
  CHECK(got.comment_fields[0].value == "A widget.");

  REQUIRE(got.groups.size() == 1);
  CHECK(got.groups[0].id == "widgets");
  CHECK(got.groups[0].title == "Widgets");
  CHECK(got.groups[0].brief == "Widget types.");
  CHECK(got.groups[0].detail == "The long version.");

  REQUIRE(got.group_members.size() == 1);
  CHECK(got.group_members[0].group_id == "widgets");
  CHECK(got.group_members[0].member_usr == "c:@S@Widget");

  // Diagnostics are ephemeral parse output, not IR: the schema has no table
  // for them, so a round trip must drop them rather than error out.
  CHECK(got.diagnostics.empty());

  std::remove(path.c_str());
}

TEST_CASE("SqliteStore write against a non-empty DB replaces prior contents",
          "[store]") {
  std::string path = temp_db_path();

  {
    store::SqliteStore writer(path);
    REQUIRE_NOTHROW(writer.write(make_module(), store::Meta::current()));
  }

  // Re-running the full write against the same path must neither throw a
  // UNIQUE-constraint error on repeated paths/usrs nor leave stale rows from
  // the first parse sitting next to the second's.
  model::ParsedModule second;
  model::SourceFile f;
  f.path = "/tmp/other.hpp";
  f.sha256 = std::string(64, 'b');
  f.size_bytes = 42;
  second.files.push_back(f);

  model::Symbol s;
  s.usr = "c:@F@only_in_second";
  s.kind = model::SymbolKind::Function;
  s.spelling = "only_in_second";
  s.qualified_name = "only_in_second";
  s.display_name = "only_in_second()";
  s.location.file_path = "/tmp/other.hpp";
  second.symbols.push_back(s);

  {
    store::SqliteStore writer(path);
    REQUIRE_NOTHROW(writer.write(second, store::Meta::current()));
  }

  store::SqliteStore reader(path);
  model::ParsedModule got = reader.read();

  REQUIRE(got.files.size() == 1);
  CHECK(got.files[0].path == "/tmp/other.hpp");

  REQUIRE(got.symbols.size() == 1);
  CHECK(got.symbols[0].usr == "c:@F@only_in_second");
  CHECK(got.parameters.empty());
  CHECK(got.references.empty());
  CHECK(got.enumerators.empty());
  CHECK(got.comments.empty());

  std::remove(path.c_str());
}

TEST_CASE("successive write_part calls accumulate one batch at a time",
          "[store]") {
  // A full parse streams its batches into the store as they are parsed, so
  // repeated writes have to add up — and a header two batches both saw must
  // keep the one `files` row its symbols on either side are anchored to.
  std::string path = temp_db_path();
  model::SourceFile shared;
  shared.path = "/tmp/shared.hpp";
  shared.sha256 = std::string(64, 'c');
  shared.size_bytes = 3;

  model::ParsedModule first;
  first.files.push_back(shared);
  first.symbols.push_back(group_symbol("c:@F@one", shared.path));

  model::ParsedModule second;
  second.files.push_back(shared);
  second.symbols.push_back(group_symbol("c:@F@two", shared.path));

  {
    store::SqliteStore writer(path);
    writer.write_part(first, store::Meta::current());
    writer.write_part(second, store::Meta::current());
  }

  store::SqliteStore reader(path);
  model::ParsedModule got = reader.read();

  CHECK(got.files.size() == 1);
  REQUIRE(got.symbols.size() == 2);
  for (const auto& s : got.symbols) {
    CHECK(s.location.file_path == shared.path);
  }

  std::remove(path.c_str());
}

TEST_CASE("clear before a streamed parse drops the previous one", "[store]") {
  // write_part accumulates, so a streamed full parse gets the replacing
  // semantics of write by clearing once before it hands over the first batch.
  std::string path = temp_db_path();
  model::SourceFile stale;
  stale.path = "/tmp/stale.hpp";
  stale.sha256 = std::string(64, 'd');
  stale.size_bytes = 5;

  model::ParsedModule previous;
  previous.files.push_back(stale);
  previous.symbols.push_back(group_symbol("c:@F@gone", stale.path));

  model::SourceFile fresh;
  fresh.path = "/tmp/fresh.hpp";
  fresh.sha256 = std::string(64, 'e');
  fresh.size_bytes = 7;

  model::ParsedModule batch;
  batch.files.push_back(fresh);
  batch.symbols.push_back(group_symbol("c:@F@kept", fresh.path));

  {
    store::SqliteStore writer(path);
    writer.write(previous, store::Meta::current());
  }
  {
    store::SqliteStore writer(path);
    writer.clear();
    writer.write_part(batch, store::Meta::current());
  }

  store::SqliteStore reader(path);
  model::ParsedModule got = reader.read();

  REQUIRE(got.files.size() == 1);
  CHECK(got.files[0].path == fresh.path);
  REQUIRE(got.symbols.size() == 1);
  CHECK(got.symbols[0].usr == "c:@F@kept");

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

TEST_CASE("SqliteStore never lets an \\addtogroup block outrank a definition",
          "[store]") {
  // An `\addtogroup` block with prose parses to title == id plus a non-empty
  // brief, so the old whole-row guard ("not a bare stub") waved it through and
  // let it overwrite the `\defgroup` block's title *and* prose. Order must not
  // decide the outcome: within one write the module's group list is the
  // concatenation of the per-batch lists.
  for (bool addtogroup_first : {true, false}) {
    INFO("addtogroup_first = " << addtogroup_first);

    model::ParsedModule m;
    model::SourceFile f;
    f.path = "/tmp/gd.hpp";
    f.sha256 = std::string(64, 'a');
    m.files.push_back(f);
    m.symbols.push_back(group_symbol("c:@F@gd", "/tmp/gd.hpp"));
    if (addtogroup_first) {
      m.groups.push_back(addtogroup_group());
      m.groups.push_back(titled_group());
    } else {
      m.groups.push_back(titled_group());
      m.groups.push_back(addtogroup_group());
    }
    m.group_members.push_back(membership("c:@F@gd"));

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
      CHECK(got.groups[0].detail == "The long version.");
      CHECK(got.groups[0].is_definition);
    }

    std::remove(path.c_str());
  }
}

TEST_CASE("SqliteStore keeps a definition when only the addtogroup file is "
          "re-parsed",
          "[store]") {
  // The incremental mirror of the case above: the `\defgroup` block is in a
  // file this write never reads, so the module carries only the `\addtogroup`
  // row for the group.
  model::ParsedModule original;

  model::SourceFile a;
  a.path = "/tmp/ge.hpp";
  a.sha256 = std::string(64, 'a');
  original.files.push_back(a);

  original.symbols.push_back(group_symbol("c:@F@ge", "/tmp/ge.hpp"));
  original.groups.push_back(titled_group());
  original.group_members.push_back(membership("c:@F@ge"));

  std::string path = temp_db_path();
  {
    store::SqliteStore writer(path);
    writer.write(original, store::Meta::current());
  }

  model::ParsedModule reparse;
  model::SourceFile a2;
  a2.path = "/tmp/ge.hpp";
  a2.sha256 = std::string(64, 'b');
  reparse.files.push_back(a2);
  reparse.symbols.push_back(group_symbol("c:@F@ge", "/tmp/ge.hpp"));
  reparse.groups.push_back(addtogroup_group());
  reparse.group_members.push_back(membership("c:@F@ge"));

  {
    store::SqliteStore writer(path);
    REQUIRE_NOTHROW(
        writer.write_tus(reparse, store::Meta::current(), {"/tmp/ge.hpp"}));
  }

  store::SqliteStore reader(path);
  model::ParsedModule got = reader.read();

  REQUIRE(got.groups.size() == 1);
  CHECK(got.groups[0].title == "Geometry helpers");
  CHECK(got.groups[0].brief == "Points and vectors.");
  CHECK(got.groups[0].detail == "The long version.");

  std::remove(path.c_str());
}

TEST_CASE("SqliteStore lets a non-definition fill what a definition leaves "
          "empty",
          "[store]") {
  // Refusing the downgrade is not the same as freezing the row: Doxygen merges
  // `\addtogroup` documentation into the group, so a field the `\defgroup`
  // block never filled is still the addtogroup block's to contribute.
  model::ParsedModule m;
  model::SourceFile f;
  f.path = "/tmp/gf.hpp";
  f.sha256 = std::string(64, 'a');
  m.files.push_back(f);
  m.symbols.push_back(group_symbol("c:@F@gf", "/tmp/gf.hpp"));

  model::Group titled_only;
  titled_only.id = "geometry";
  titled_only.title = "Geometry helpers";
  titled_only.is_definition = true;
  m.groups.push_back(titled_only);
  m.groups.push_back(addtogroup_group());
  m.group_members.push_back(membership("c:@F@gf"));

  std::string path = temp_db_path();
  {
    store::SqliteStore writer(path);
    REQUIRE_NOTHROW(writer.write(m, store::Meta::current()));
  }

  store::SqliteStore reader(path);
  model::ParsedModule got = reader.read();

  REQUIRE(got.groups.size() == 1);
  CHECK(got.groups[0].title == "Geometry helpers");
  CHECK(got.groups[0].brief == "Prose from an addtogroup block.");
  CHECK(got.groups[0].detail == "More of it.");
  CHECK(got.groups[0].is_definition);

  std::remove(path.c_str());
}

TEST_CASE("SqliteStore still updates a group from its own re-parsed defgroup",
          "[store]") {
  // The guard must not outlaw the ordinary edit: re-parsing the file that
  // carries the `\defgroup` block has to land the block's new title and prose,
  // including a field the edit emptied.
  model::ParsedModule original;
  model::SourceFile a;
  a.path = "/tmp/gg.hpp";
  a.sha256 = std::string(64, 'a');
  original.files.push_back(a);
  original.symbols.push_back(group_symbol("c:@F@gg", "/tmp/gg.hpp"));
  original.groups.push_back(titled_group());
  original.group_members.push_back(membership("c:@F@gg"));

  std::string path = temp_db_path();
  {
    store::SqliteStore writer(path);
    writer.write(original, store::Meta::current());
  }

  model::ParsedModule reparse;
  model::SourceFile a2;
  a2.path = "/tmp/gg.hpp";
  a2.sha256 = std::string(64, 'b');
  reparse.files.push_back(a2);
  reparse.symbols.push_back(group_symbol("c:@F@gg", "/tmp/gg.hpp"));
  model::Group edited;
  edited.id = "geometry";
  edited.title = "Geometry, renamed";
  edited.brief = "Now about points only.";
  edited.is_definition = true;
  reparse.groups.push_back(edited);
  reparse.group_members.push_back(membership("c:@F@gg"));

  {
    store::SqliteStore writer(path);
    REQUIRE_NOTHROW(
        writer.write_tus(reparse, store::Meta::current(), {"/tmp/gg.hpp"}));
  }

  store::SqliteStore reader(path);
  model::ParsedModule got = reader.read();

  REQUIRE(got.groups.size() == 1);
  CHECK(got.groups[0].title == "Geometry, renamed");
  CHECK(got.groups[0].brief == "Now about points only.");
  CHECK(got.groups[0].detail.empty());

  std::remove(path.c_str());
}

TEST_CASE("SqliteStore write_tus drops dependencies that left the closure",
          "[store]") {
  // ha.hpp is an input; hdep.hpp is a header it #includes (and carries symbols
  // for, as it would when both sit in the same umbrella batch).
  model::ParsedModule original;

  model::SourceFile a;
  a.path = "/tmp/ha.hpp";
  a.sha256 = std::string(64, 'a');
  original.files.push_back(a);

  model::SourceFile dep;
  dep.path = "/tmp/hdep.hpp";
  dep.sha256 = std::string(64, 'd');
  original.files.push_back(dep);

  model::SourceFile other;
  other.path = "/tmp/hother.hpp";
  other.sha256 = std::string(64, 'o');
  original.files.push_back(other);

  original.symbols.push_back(group_symbol("c:@F@ha", "/tmp/ha.hpp"));
  original.symbols.push_back(group_symbol("c:@F@hdep", "/tmp/hdep.hpp"));
  original.symbols.push_back(group_symbol("c:@F@hother", "/tmp/hother.hpp"));

  std::string path = temp_db_path();
  {
    store::SqliteStore writer(path);
    writer.write(original, store::Meta::current());
  }

  // ha.hpp is re-parsed and no longer includes hdep.hpp. The caller knows
  // hdep.hpp was reached only through ha.hpp, so it is offered as a candidate;
  // hother.hpp belongs to a unit this write does not touch and is not offered.
  model::ParsedModule reparse;
  model::SourceFile a2;
  a2.path = "/tmp/ha.hpp";
  a2.sha256 = std::string(64, 'c');
  reparse.files.push_back(a2);
  reparse.symbols.push_back(group_symbol("c:@F@ha", "/tmp/ha.hpp"));

  {
    store::SqliteStore writer(path);
    REQUIRE_NOTHROW(writer.write_tus(reparse, store::Meta::current(),
                                     {"/tmp/ha.hpp"},
                                     {"/tmp/ha.hpp", "/tmp/hdep.hpp"}));
  }

  store::SqliteStore reader(path);
  model::ParsedModule got = reader.read();

  bool has_a = false;
  bool has_dep = false;
  bool has_other = false;
  for (const auto& f : got.files) {
    if (f.path == "/tmp/ha.hpp") has_a = true;
    if (f.path == "/tmp/hdep.hpp") has_dep = true;
    if (f.path == "/tmp/hother.hpp") has_other = true;
  }
  CHECK(has_a);          // still reached by the fresh parse
  CHECK_FALSE(has_dep);  // dropped out of every closure -> gone
  CHECK(has_other);      // never offered; belongs to another unit

  bool sym_a = false;
  bool sym_dep = false;
  bool sym_other = false;
  for (const auto& s : got.symbols) {
    if (s.usr == "c:@F@ha") sym_a = true;
    if (s.usr == "c:@F@hdep") sym_dep = true;
    if (s.usr == "c:@F@hother") sym_other = true;
  }
  CHECK(sym_a);
  CHECK_FALSE(sym_dep);
  CHECK(sym_other);

  std::remove(path.c_str());
}

TEST_CASE("SqliteStore::write refreshes a stale schema version in meta",
          "[store]") {
  // put_meta uses INSERT OR REPLACE, but nothing pinned that a later write
  // actually overwrites an older value rather than leaving it behind -- which
  // is exactly what happens when clangquill is upgraded and rerun against a
  // database an older, incompatible build wrote. The Python Store rejects a
  // stale schema_version on open (test_store_open_rejects_incompatible_schema_version);
  // this pins the C++ side that has to produce the *current* value in the
  // first place.
  std::string path = temp_db_path();
  {
    store::SqliteStore writer(path);
    store::Meta stale;
    stale.schema_version = store::kSchemaVersion - 1;
    stale.core_version = "0.0.0-stale";
    stale.libclang_version = "stale-libclang";
    writer.write(make_module(), stale);
  }
  {
    // write() assumes an empty `files` table (insert_files, not upsert_files),
    // so a second write against the same database goes through write_tus --
    // the incremental path a real upgrade-and-rerun actually takes.
    store::SqliteStore writer(path);
    writer.write_tus(make_module(), store::Meta::current(), {"/tmp/example.hpp"});
  }

  store::Db db(path);
  store::Stmt stmt(db, "SELECT value FROM meta WHERE key = ?;");
  auto meta_value = [&](const char* key) {
    stmt.reset();
    stmt.bind(1, key);
    REQUIRE(stmt.step());
    return stmt.column_text(0);
  };
  const store::Meta current = store::Meta::current();
  CHECK(meta_value("schema_version") == std::to_string(store::kSchemaVersion));
  CHECK(meta_value("core_version") == current.core_version);
  CHECK(meta_value("libclang_version") == current.libclang_version);

  std::remove(path.c_str());
}

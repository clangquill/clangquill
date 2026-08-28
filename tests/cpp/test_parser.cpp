#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <random>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "hash/content_hash.hpp"
#include "model/module.hpp"

#if defined(CLANGQUILL_HAVE_LIBCLANG)
#include "parser/cursor_utils.hpp"
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

// A directory unique to this call, under the system temp dir. ctest runs each
// TEST_CASE as its own process (catch_discover_tests), so a fixed name races
// under `ctest -j`, and a failed assertion that skips a test's own cleanup
// leaks state into whatever runs next under the same name. A random suffix
// keeps every call -- concurrent or left behind by a crashed run -- out of
// every other call's way.
std::filesystem::path unique_temp_dir(const std::string& label) {
  namespace fs = std::filesystem;
  std::random_device entropy;
  return fs::temp_directory_path() / (label + "-" + std::to_string(entropy()));
}

const model::Symbol* find(const model::ParsedModule& m, const std::string& qn) {
  for (const auto& s : m.symbols) {
    if (s.qualified_name == qn) return &s;
  }
  return nullptr;
}

}  // namespace

TEST_CASE("parser extracts symbols and hierarchy", "[parser]") {
  auto m = parse_fixture("shapes.hpp");

  const auto* ns = find(m, "geo");
  REQUIRE(ns != nullptr);
  CHECK(ns->kind == model::SymbolKind::Namespace);

  const auto* circle = find(m, "geo::Circle");
  REQUIRE(circle != nullptr);
  CHECK(circle->kind == model::SymbolKind::Class);
  CHECK(circle->is_definition);
  CHECK(circle->parent_usr == ns->usr);

  const auto* area = find(m, "geo::Shape::area");
  REQUIRE(area != nullptr);
  const auto* shape = find(m, "geo::Shape");
  REQUIRE(shape != nullptr);
  CHECK(area->parent_usr == shape->usr);
}

TEST_CASE("anonymous-namespace contents are not published as API", "[parser]") {
  // An anonymous namespace has internal linkage: what it holds belongs to one
  // translation unit and cannot be named, called or linked against from
  // anywhere else. Extracting it published internals -- and, because the
  // anonymous scope has no spelling to contribute, published them under the
  // *enclosing* namespace's name, indistinguishable from real API. Doxygen
  // hides them by default (EXTRACT_ANON_NSPACES = NO); so do we.
  auto m = parse_fixture("anonymous_ns.hpp");

  CHECK(find(m, "demo::visible") != nullptr);
  CHECK(find(m, "demo::hidden_helper") == nullptr);
  CHECK(find(m, "demo::kHiddenLimit") == nullptr);
  CHECK(find(m, "demo::HiddenTag") == nullptr);
  // Nothing from inside the scope, at any depth: the walk does not descend.
  for (const auto& sym : m.symbols) {
    CHECK(sym.qualified_name.find("Hidden") == std::string::npos);
    CHECK(sym.qualified_name.find("hidden") == std::string::npos);
  }
}

TEST_CASE("opting in names the anonymous namespace a symbol came from",
          "[parser]") {
  // With the opt-in the internals are documented -- but qualified by the scope
  // they actually live in, spelled the way the Sphinx C++ domain spells an
  // anonymous entity, so they are never mistaken for members of the enclosing
  // namespace and the declaration the generator emits still parses.
  parser::ParseOptions opts;
  opts.extract_anonymous_namespaces = true;
  model::ParsedModule m;
  REQUIRE(parser::Parser(opts).parse_file(
      std::string(CLANGQUILL_FIXTURE_DIR) + "/anonymous_ns.hpp", m));

  CHECK(find(m, "demo::visible") != nullptr);
  CHECK(find(m, "demo::@anonymous::hidden_helper") != nullptr);
  CHECK(find(m, "demo::@anonymous::kHiddenLimit") != nullptr);
  CHECK(find(m, "demo::@anonymous::HiddenTag") != nullptr);
  CHECK(find(m, "demo::@anonymous::HiddenTag::hidden_field") != nullptr);
  // Never at the enclosing scope, which is where they used to land.
  CHECK(find(m, "demo::hidden_helper") == nullptr);
  CHECK(find(m, "demo::HiddenTag") == nullptr);
}

TEST_CASE("a named namespace inside an anonymous one is internal too",
          "[parser]") {
  // The skip is of the whole subtree, so a named namespace nested inside the
  // anonymous one -- whose own spelling would otherwise carry it back into the
  // output -- goes with it. At file scope there is no enclosing namespace to
  // be mistaken for, and eliding the scope published such a helper as a
  // top-level entity of the library.
  auto m = parse_fixture("anonymous_ns.hpp");

  CHECK(find(m, "demo::inner") == nullptr);
  CHECK(find(m, "demo::inner::nested_helper") == nullptr);
  CHECK(find(m, "file_scope_helper") == nullptr);
}

TEST_CASE("the anonymous scope is named at every depth it appears", "[parser]") {
  parser::ParseOptions opts;
  opts.extract_anonymous_namespaces = true;
  model::ParsedModule m;
  REQUIRE(parser::Parser(opts).parse_file(
      std::string(CLANGQUILL_FIXTURE_DIR) + "/anonymous_ns.hpp", m));

  // A named namespace nested inside the anonymous one keeps its own segment
  // behind the scope's, rather than reattaching its members to `demo`.
  CHECK(find(m, "demo::@anonymous::inner::nested_helper") != nullptr);
  CHECK(find(m, "demo::inner::nested_helper") == nullptr);

  // At file scope the scope is the whole qualification: a bare
  // `file_scope_helper` would read as a top-level entity of the library.
  CHECK(find(m, "@anonymous::file_scope_helper") != nullptr);
  CHECK(find(m, "file_scope_helper") == nullptr);
}

TEST_CASE("parser resolves base-class references", "[parser]") {
  auto m = parse_fixture("shapes.hpp");
  const auto* circle = find(m, "geo::Circle");
  REQUIRE(circle != nullptr);

  bool found_base = false;
  for (const auto& r : m.references) {
    if (r.from_usr == circle->usr && r.kind == model::RefKind::BaseClass) {
      found_base = true;
      CHECK(r.to_spelling.find("Shape") != std::string::npos);
      CHECK(r.is_resolved);
    }
  }
  CHECK(found_base);
}

TEST_CASE("parser stores raw comments verbatim", "[parser]") {
  auto m = parse_fixture("shapes.hpp");
  const auto* circle = find(m, "geo::Circle");
  REQUIRE(circle != nullptr);
  CHECK(circle->is_documented);

  bool found = false;
  for (const auto& c : m.comments) {
    if (c.symbol_usr == circle->usr) {
      found = true;
      CHECK(c.text.find("@param r") != std::string::npos);
      CHECK(c.text.find("/**") != std::string::npos);
    }
  }
  CHECK(found);
}

TEST_CASE("parser keeps undocumented symbols", "[parser]") {
  auto m = parse_fixture("undocumented.hpp");

  const auto* undoc = find(m, "undocumented_function");
  REQUIRE(undoc != nullptr);
  CHECK_FALSE(undoc->is_documented);

  const auto* doc = find(m, "documented_function");
  REQUIRE(doc != nullptr);
  CHECK(doc->is_documented);

  // The undocumented symbol must not have a comment row.
  for (const auto& c : m.comments) {
    CHECK(c.symbol_usr != undoc->usr);
  }
}

TEST_CASE("parser reads enumerators with values", "[parser]") {
  auto m = parse_fixture("enums.hpp");
  REQUIRE(m.enumerators.size() >= 7);

  auto value_of = [&](const std::string& name) -> long long {
    for (const auto& e : m.enumerators) {
      if (e.name == name) return e.value;
    }
    return -999;
  };
  CHECK(value_of("Red") == 0);
  CHECK(value_of("Green") == 5);
  CHECK(value_of("Blue") == 6);
}

TEST_CASE("enum signedness looks through typedef sugar", "[parser]") {
  // `enum class Mask : u64` -- the underlying type arrives as CXType_Typedef,
  // not as a builtin unsigned kind, so reading the kind without canonicalizing
  // it took the enum for a signed one and stored All as -1.
  auto m = parse_fixture("enums.hpp");

  const model::Enumerator* all = nullptr;
  for (const auto& e : m.enumerators) {
    if (e.name == "All") all = &e;
  }
  REQUIRE(all != nullptr);
  CHECK_FALSE(all->value_is_signed);
  CHECK(static_cast<std::uint64_t>(all->value) == 0xFFFFFFFFFFFFFFFFULL);
}

TEST_CASE("an enum records its fixed underlying type", "[parser]") {
  auto m = parse_fixture("enums.hpp");

  auto integer_type =
      [&](const std::string& enum_name) -> const model::Reference* {
    const auto* sym = find(m, enum_name);
    if (sym == nullptr) return nullptr;
    for (const auto& r : m.references) {
      if (r.from_usr == sym->usr && r.kind == model::RefKind::EnumIntegerType) {
        return &r;
      }
    }
    return nullptr;
  };

  // The edge carries the type as written, not its canonical spelling.
  const auto* mask = integer_type("Mask");
  REQUIRE(mask != nullptr);
  CHECK(mask->to_spelling == "u64");

  const auto* level = integer_type("Level");
  REQUIRE(level != nullptr);
  CHECK(level->to_spelling == "unsigned char");

  // An enum that fixes no underlying type says nothing about one: the type
  // libclang reports there is the implementation's choice, not the header's.
  CHECK(integer_type("Color") == nullptr);
  CHECK(integer_type("Direction") == nullptr);
}

TEST_CASE("declarations inside extern \"C\" reach the IR", "[parser]") {
  // extern "C" { ... } is CXCursor_LinkageSpec, which map_kind has no case for
  // and which visit() used to prune -- along with everything declared inside
  // it -- because it is not a scope kind that drives explicit recursion.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-linkage-spec-test");
  fs::remove_all(dir);
  fs::create_directories(dir);
  const fs::path header = dir / "capi.hpp";
  std::ofstream(header) << "extern \"C\" {\n"
                        << "/// Adds two numbers.\n"
                        << "int c_add(int a, int b);\n"
                        << "typedef struct c_point { int x; int y; } c_point;\n"
                        << "}\n"
                        << "extern \"C\" int c_single(int x);\n";

  parser::ParseOptions opts;
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file(header.string(), mod));

  const auto* add = find(mod, "c_add");
  REQUIRE(add != nullptr);
  CHECK(add->kind == model::SymbolKind::Function);
  CHECK(add->is_documented);
  CHECK(add->parent_usr.empty());  // declared at namespace scope, not nested

  CHECK(find(mod, "c_point") != nullptr);
  CHECK(find(mod, "c_single") != nullptr);

  fs::remove_all(dir);
}

TEST_CASE("parser populates content and file hashes", "[parser]") {
  auto m = parse_fixture("shapes.hpp");
  REQUIRE(m.files.size() == 1);
  CHECK(m.files[0].sha256.size() == 64);

  for (const auto& s : m.symbols) {
    CHECK_FALSE(s.content_hash.empty());
  }
}

TEST_CASE("content_hash is deterministic across parses", "[parser]") {
  auto a = parse_fixture("shapes.hpp");
  auto b = parse_fixture("shapes.hpp");

  auto hash_of = [](const model::ParsedModule& m, const std::string& qn) {
    for (const auto& s : m.symbols) {
      if (s.qualified_name == qn) return s.content_hash;
    }
    return std::string{};
  };
  CHECK(hash_of(a, "geo::Circle") == hash_of(b, "geo::Circle"));
  CHECK_FALSE(hash_of(a, "geo::Circle").empty());
}

TEST_CASE("file hash cache serves repeats and notices edits", "[parser]") {
  // record_file keeps a process-wide (mtime, size) -> digest cache so a header
  // shared by many umbrella batches is read and hashed once per run. The cache
  // must replay identical rows for an unchanged file and re-hash an edited one.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-hash-cache-test");
  fs::create_directories(dir);
  const fs::path header = dir / "cached.hpp";
  {
    std::ofstream out(header);
    out << "/// v1\ninline int cached_value() { return 1; }\n";
  }

  auto file_row = [&](const model::ParsedModule& m) -> const model::SourceFile* {
    for (const auto& f : m.files) {
      if (f.path.find("cached.hpp") != std::string::npos) return &f;
    }
    return nullptr;
  };

  parser::ParseOptions opts;
  model::ParsedModule first;
  REQUIRE(parser::Parser(opts).parse_file(header.string(), first));
  const auto* row1 = file_row(first);
  REQUIRE(row1 != nullptr);
  CHECK(row1->sha256.size() == 64);

  // A separate parse of the unchanged file (fresh Parser and module, so the
  // per-module dedup set cannot help) replays the identical row.
  model::ParsedModule second;
  REQUIRE(parser::Parser(opts).parse_file(header.string(), second));
  const auto* row2 = file_row(second);
  REQUIRE(row2 != nullptr);
  CHECK(row2->sha256 == row1->sha256);
  CHECK(row2->size_bytes == row1->size_bytes);

  // Editing the file (different size, so the stat validation must miss even on
  // filesystems with coarse mtimes) yields a fresh digest, not the cached one.
  {
    std::ofstream out(header);
    out << "/// v2, edited\ninline int cached_value() { return 22; }\n";
  }
  model::ParsedModule third;
  REQUIRE(parser::Parser(opts).parse_file(header.string(), third));
  const auto* row3 = file_row(third);
  REQUIRE(row3 != nullptr);
  CHECK(row3->sha256 != row1->sha256);
  CHECK(row3->size_bytes != row1->size_bytes);

  fs::remove_all(dir);
}

TEST_CASE("normalized_path collapses a symlink to its target's own path",
          "[parser]") {
  // The OS-level canonicalization normalized_path performs (issue #329) must
  // resolve a symlink to the path its target actually lives at, not just
  // normalize the symlink's own spelling -- the same guarantee that lets two
  // #include spellings of one physical header (reached through different
  // search-path symlinks, or, on a case-insensitive filesystem, differently
  // cased) collapse to a single tracked path rather than fragmenting the
  // files/inputs cache table and, in the docs, splitting a header's symbols
  // across two "files". Symlinks are portable enough to test without a
  // platform guard, unlike a real case-insensitive filesystem; a Windows-only
  // test for that side of the fix lives in test_compile_db.cpp.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-normalized-path-test");
  fs::create_directories(dir);
  const fs::path real = dir / "real.hpp";
  {
    std::ofstream out(real);
    out << "// content\n";
  }
  const fs::path link = dir / "link.hpp";
  std::error_code ec;
  fs::create_symlink(real, link, ec);
  // Symlink creation can fail without privilege (notably on Windows without
  // Developer Mode or an elevated token); skip only the comparison that
  // depends on it rather than failing the whole test over an environment gap
  // unrelated to what's under test.
  if (!ec) {
    CHECK(parser::normalized_path(real.string()) ==
         parser::normalized_path(link.string()));
  }
  // Either way, a real file's normalized path must still resolve to it.
  CHECK(fs::equivalent(parser::normalized_path(real.string()), real));

  fs::remove_all(dir);
}

namespace {

std::vector<std::string> all_inputs() {
  const std::string dir = CLANGQUILL_FIXTURE_DIR;
  return {dir + "/shapes.hpp", dir + "/enums.hpp", dir + "/undocumented.hpp",
          dir + "/doxygen.hpp", dir + "/m7.hpp"};
}

// USR set is the stable identity of a parse, independent of row order.
std::set<std::string> symbol_usrs(const model::ParsedModule& m) {
  std::set<std::string> usrs;
  for (const auto& s : m.symbols) usrs.insert(s.usr);
  return usrs;
}

// A pair of headers that is *not* self-contained: order_b.hpp spells its own
// type name with a macro order_a.hpp defines and never includes it. Kept out of
// all_inputs() on purpose -- these two are the counterexample to "umbrella
// batching extracts the same symbols as per-file parsing", so folding them into
// that fixture set would legitimately break it.
std::vector<std::string> order_pair(bool a_first) {
  const std::string dir = CLANGQUILL_FIXTURE_DIR;
  const std::string a = dir + "/order_a.hpp";
  const std::string b = dir + "/order_b.hpp";
  return a_first ? std::vector<std::string>{a, b} : std::vector<std::string>{b, a};
}

std::set<std::string> file_paths(const model::ParsedModule& m) {
  std::set<std::string> paths;
  for (const auto& f : m.files) paths.insert(f.path);
  return paths;
}

std::vector<std::string> diagnostic_texts(const model::ParsedModule& m) {
  std::vector<std::string> texts;
  for (const auto& d : m.diagnostics) texts.push_back(d.text);
  std::sort(texts.begin(), texts.end());
  return texts;
}

}  // namespace

TEST_CASE("parse_files merges every input into one module", "[parser]") {
  parser::ParseOptions opts;
  opts.jobs = 4;
  auto merged = parser::parse_files(all_inputs(), opts);

  // Symbols from each separate input are present in the combined module.
  CHECK(find(merged, "geo::Circle") != nullptr);  // shapes.hpp
  CHECK(merged.enumerators.size() >= 7);           // enums.hpp

  // Each fixture's main file is recorded exactly once, and paths are unique.
  std::set<std::string> paths;
  for (const auto& f : merged.files) {
    CHECK(paths.insert(f.path).second);  // no duplicate file rows after merge
  }
  CHECK(paths.size() >= all_inputs().size());
}

TEST_CASE("parse_files is deterministic regardless of job count", "[parser]") {
  auto inputs = all_inputs();

  parser::ParseOptions serial;
  serial.jobs = 1;
  parser::ParseOptions parallel;
  parallel.jobs = 4;

  auto a = parser::parse_files(inputs, serial);
  auto b = parser::parse_files(inputs, parallel);

  CHECK(symbol_usrs(a) == symbol_usrs(b));
  CHECK(a.symbols.size() == b.symbols.size());
  CHECK(a.references.size() == b.references.size());
  CHECK(a.files.size() == b.files.size());

  // Merge order follows the parser's canonical order, not thread completion
  // order: the file rows land in a stable sequence whether parsed serially or
  // concurrently.
  std::vector<std::string> pa;
  std::vector<std::string> pb;
  for (const auto& f : a.files) pa.push_back(f.path);
  for (const auto& f : b.files) pb.push_back(f.path);
  CHECK(pa == pb);
}

TEST_CASE("a streamed parse hands over the IR the merge would have held",
          "[parser]") {
  // With a sink the batches are handed over one at a time instead of piling up
  // in one module, so nothing may go missing on the way.
  auto inputs = all_inputs();
  parser::ParseOptions opts;
  opts.jobs = 4;
  opts.tu_batch = 1;

  std::vector<model::ParsedModule> streamed;
  const auto rest = parser::parse_files(
      inputs, opts, nullptr, nullptr,
      [&](model::ParsedModule&& part) { streamed.push_back(std::move(part)); });

  std::set<std::string> usrs;
  std::set<std::string> paths;
  std::size_t references = 0;
  for (const auto& part : streamed) {
    for (const auto& s : part.symbols) usrs.insert(s.usr);
    for (const auto& f : part.files) paths.insert(f.path);
    references += part.references.size();
    // Diagnostics are deduplicated across batches, so they stay behind.
    CHECK(part.diagnostics.empty());
  }

  const auto merged = parser::parse_files(inputs, opts);
  CHECK(usrs == symbol_usrs(merged));
  CHECK(paths == file_paths(merged));
  CHECK(references == merged.references.size());
  CHECK(rest.symbols.empty());
  CHECK(diagnostic_texts(rest) == diagnostic_texts(merged));
}

TEST_CASE("a streamed parse arrives in the same order at any job count",
          "[parser]") {
  // The batches reach the sink in canonical order rather than in the order the
  // threads happen to finish, so the rows a caller writes land in a sequence
  // that does not depend on the job count.
  auto inputs = all_inputs();

  auto stream = [&inputs](int jobs) {
    parser::ParseOptions opts;
    opts.jobs = jobs;
    opts.tu_batch = 1;
    std::vector<std::vector<std::string>> per_part;
    parser::parse_files(inputs, opts, nullptr, nullptr,
                        [&](model::ParsedModule&& part) {
                          std::vector<std::string> usrs;
                          for (const auto& s : part.symbols) usrs.push_back(s.usr);
                          per_part.push_back(std::move(usrs));
                        });
    return per_part;
  };

  CHECK(stream(1) == stream(4));
}

TEST_CASE("an exception from the sink leaves the parse", "[parser]") {
  // The workers have to be joined before it does, so a throwing sink must
  // neither deadlock nor take the process down with a live thread.
  auto inputs = all_inputs();
  parser::ParseOptions opts;
  opts.jobs = 4;
  opts.tu_batch = 1;

  CHECK_THROWS_AS(parser::parse_files(inputs, opts, nullptr, nullptr,
                                      [](model::ParsedModule&&) {
                                        throw std::runtime_error("no room");
                                      }),
                  std::runtime_error);
}

TEST_CASE("umbrella batching extracts the same symbols as per-file parsing",
          "[parser]") {
  auto inputs = all_inputs();

  parser::ParseOptions isolated;
  isolated.tu_batch = 1;
  parser::ParseOptions batched;
  batched.tu_batch = static_cast<int>(inputs.size());

  auto a = parser::parse_files(inputs, isolated);
  auto b = parser::parse_files(inputs, batched);

  CHECK(symbol_usrs(a) == symbol_usrs(b));

  std::set<std::string> fa;
  std::set<std::string> fb;
  for (const auto& f : a.files) fa.insert(f.path);
  for (const auto& f : b.files) fb.insert(f.path);
  CHECK(fa == fb);
}

TEST_CASE("parse_files does not depend on input order", "[parser]") {
  // The IR is a function of the input *set*. order_b.hpp only parses into the
  // name `Tagged` once order_a.hpp has been seen, so before inputs were sorted
  // into a canonical order this pair extracted different symbols depending on
  // which spelling came first on the command line.
  parser::ParseOptions opts;
  opts.tu_batch = 2;

  auto a = parser::parse_files(order_pair(true), opts);
  auto b = parser::parse_files(order_pair(false), opts);

  CHECK(symbol_usrs(a) == symbol_usrs(b));
  CHECK(a.symbols.size() == b.symbols.size());
  CHECK(file_paths(a) == file_paths(b));
  CHECK(diagnostic_texts(a) == diagnostic_texts(b));
}

TEST_CASE("a repeated input is included once per umbrella", "[parser]") {
  // Canonical ordering puts duplicate spellings next to each other, so an
  // umbrella would `#include` them back to back. Everything a caller passes
  // twice must still be parsed exactly once.
  parser::ParseOptions opts;  // default batch: one umbrella for all three

  auto once = parser::parse_files(order_pair(true), opts);

  auto inputs = order_pair(false);
  inputs.push_back(inputs.front());  // {b, a, b}
  auto twice = parser::parse_files(inputs, opts);

  CHECK(symbol_usrs(twice) == symbol_usrs(once));
  CHECK(twice.symbols.size() == once.symbols.size());
  CHECK(file_paths(twice) == file_paths(once));
}

TEST_CASE("per-member sinks follow the caller's order", "[parser]") {
  // Inputs are parsed in canonical order, but tu_files/tu_parsed are indexed by
  // the position the caller used -- bindings/module.cpp relies on that.
  parser::ParseOptions opts;
  opts.tu_batch = 2;
  std::vector<std::vector<std::string>> tu_files;
  std::vector<bool> tu_ok;
  parser::parse_files(order_pair(false), opts, &tu_files, &tu_ok);

  REQUIRE(tu_files.size() == 2);
  REQUIRE(tu_ok.size() == 2);
  CHECK(tu_files[0].front().find("order_b.hpp") != std::string::npos);
  CHECK(tu_files[1].front().find("order_a.hpp") != std::string::npos);
}

TEST_CASE("an undocumented forward declaration does not displace the definition",
          "[parser]") {
  // fwd_decl.hpp names Opaque only through `std::unique_ptr<class Opaque>`, the
  // same elaborated-type-specifier the parser's own headers use for CompileDb.
  // That declaration carries no documentation and is not the definition, so it
  // must not produce a row: symbols collide on the USR primary key, and the
  // store's INSERT OR REPLACE would let whichever file is written last win —
  // handing the definition's identity to a pointer member's type name.
  const std::string dir = CLANGQUILL_FIXTURE_DIR;
  std::vector<std::string> inputs{dir + "/fwd_decl.hpp", dir + "/fwd_def.hpp"};

  parser::ParseOptions isolated;
  isolated.tu_batch = 1;  // no umbrella, so nothing hides the collision
  auto m = parser::parse_files(inputs, isolated);

  int rows = 0;
  const model::Symbol* opaque = nullptr;
  for (const auto& sym : m.symbols) {
    if (sym.qualified_name == "Opaque") {
      ++rows;
      opaque = &sym;
    }
  }
  REQUIRE(rows == 1);
  REQUIRE(opaque != nullptr);
  CHECK(opaque->is_definition);
  CHECK(opaque->is_documented);
  CHECK(opaque->location.file_path.find("fwd_def.hpp") != std::string::npos);
  // Namespace scope, not nested under Owner: the surviving row is the
  // definition's, so it keeps the definition's parent.
  CHECK(opaque->parent_usr.empty());
}

TEST_CASE("a documented forward declaration is still extracted", "[parser]") {
  // The skip is for declarations that carry nothing. Documenting an opaque type
  // where it is declared is a deliberate thing to write, so it must survive.
  const std::string dir = CLANGQUILL_FIXTURE_DIR;
  // Written into the shared fixture directory rather than a private temp
  // dir, since parse_files resolves relative includes against it -- so the
  // name needs its own per-call uniqueness to stay race-free under `ctest -j`.
  std::random_device entropy;
  const std::string header =
      dir + "/.fwd_documented_" + std::to_string(entropy()) + ".hpp";
  {
    std::ofstream out(header);
    out << "#pragma once\n/// An opaque handle, documented where it is declared.\nclass Handle;\n";
  }

  parser::ParseOptions opts;
  auto m = parser::parse_files({header}, opts);
  std::filesystem::remove(header);

  const auto* handle = find(m, "Handle");
  REQUIRE(handle != nullptr);
  CHECK(handle->is_documented);
  CHECK_FALSE(handle->is_definition);
}

TEST_CASE("a documented forward declaration spanning lines is extracted",
          "[parser]") {
  // Whether the comment belongs to this declaration is decided by where it
  // sits, and a declaration begins at its extent -- not at the line its entity
  // is named on, which a template head or a wrapped declarator puts below the
  // comment. Measuring from the name line called these comments somebody
  // else's and dropped both declarations.
  const std::string dir = CLANGQUILL_FIXTURE_DIR;
  const std::string header = dir + "/.fwd_multiline.hpp";
  {
    std::ofstream out(header);
    out << "#pragma once\n"
        << "/// An opaque handle type.\n"
        << "template <typename T>\n"
        << "class Holder;\n"
        << "\n"
        << "/// An opaque status enum.\n"
        << "enum class\n"
        << "    Status : int;\n";
  }

  parser::ParseOptions opts;
  auto m = parser::parse_files({header}, opts);
  std::filesystem::remove(header);

  const auto* holder = find(m, "Holder");
  REQUIRE(holder != nullptr);
  CHECK(holder->is_documented);
  CHECK_FALSE(holder->is_definition);

  const auto* status = find(m, "Status");
  REQUIRE(status != nullptr);
  CHECK(status->is_documented);
  CHECK_FALSE(status->is_definition);
}

TEST_CASE("only a doc comment documents a macro", "[parser]") {
  // libclang attaches no comment to a macro, so the token pre-scan recovers the
  // block written above the `#define`. It used to hand over *any* comment
  // block: a TODO, a commented-out line or a license header above a macro was
  // published as its documentation and marked it is_documented.
  const std::string dir = CLANGQUILL_FIXTURE_DIR;
  const std::string header = dir + "/.macro_docs.hpp";
  {
    std::ofstream out(header);
    out << "#pragma once\n"
        << "// TODO: rethink this\n"
        << "#define CQ_PLAIN 1\n"
        << "\n"
        << "/// Documented across a blank line.\n"
        << "\n"
        << "#define CQ_GAPPED 2\n"
        << "\n"
        << "/// Documented right above.\n"
        << "#define CQ_FIRST 3  // a trailing remark\n"
        << "#define CQ_SECOND 4\n"
        << "\n"
        << "/* not documentation, directly above a doc block */\n"
        << "/// Documented despite the plain block sitting right above.\n"
        << "#define CQ_ADJACENT_BLOCK 5\n";
  }

  parser::ParseOptions opts;
  auto m = parser::parse_files({header}, opts);
  std::filesystem::remove(header);

  auto comment_of = [&](const std::string& name) -> std::string {
    const auto* sym = find(m, name);
    if (sym == nullptr) return "<missing>";
    for (const auto& c : m.comments) {
      if (c.symbol_usr == sym->usr) return c.text;
    }
    return {};
  };

  // A plain comment is not documentation, whatever it sits above.
  const auto* plain = find(m, "CQ_PLAIN");
  REQUIRE(plain != nullptr);
  CHECK_FALSE(plain->is_documented);
  CHECK(comment_of("CQ_PLAIN").empty());

  // Doxygen attaches a doc block across the blank lines below it.
  const auto* gapped = find(m, "CQ_GAPPED");
  REQUIRE(gapped != nullptr);
  CHECK(gapped->is_documented);
  CHECK(comment_of("CQ_GAPPED").find("across a blank line") !=
        std::string::npos);

  // A comment trailing the `#define` is not part of the block above it: when
  // it was merged in, the block's key moved one line down -- off CQ_FIRST and
  // onto CQ_SECOND, which nobody documented.
  const auto* first = find(m, "CQ_FIRST");
  REQUIRE(first != nullptr);
  CHECK(first->is_documented);
  CHECK(comment_of("CQ_FIRST").find("right above") != std::string::npos);

  const auto* second = find(m, "CQ_SECOND");
  REQUIRE(second != nullptr);
  CHECK_FALSE(second->is_documented);
  CHECK(comment_of("CQ_SECOND").empty());

  // Regression for #303: a non-doc `/* */` sitting on the line directly above
  // a `///` block used to merge into it (both are comment tokens on
  // consecutive first-of-line positions), and the merged block's leading `/*`
  // made the whole thing read as non-doc -- losing the `///` documentation
  // unless a blank line separated the two.
  const auto* adjacent = find(m, "CQ_ADJACENT_BLOCK");
  REQUIRE(adjacent != nullptr);
  CHECK(adjacent->is_documented);
  CHECK(comment_of("CQ_ADJACENT_BLOCK").find("despite the plain block") !=
        std::string::npos);
}

TEST_CASE("umbrella batching attributes dependencies per member exactly",
          "[parser]") {
  // m7.hpp is self-contained while shapes.hpp has no includes: inside one
  // umbrella TU each member's dependency closure must stay its own (built from
  // the preprocessing record, so even guard-skipped includes attribute).
  const std::string dir = CLANGQUILL_FIXTURE_DIR;
  std::vector<std::string> inputs{dir + "/shapes.hpp", dir + "/enums.hpp"};

  parser::ParseOptions batched;
  batched.tu_batch = 2;
  std::vector<std::vector<std::string>> tu_files;
  std::vector<bool> tu_ok;
  parser::parse_files(inputs, batched, &tu_files, &tu_ok);

  REQUIRE(tu_files.size() == 2);
  REQUIRE(tu_ok == std::vector<bool>{true, true});
  // Each member's closure starts with (a spelling of) itself and never lists
  // the sibling input.
  for (std::size_t i = 0; i < inputs.size(); ++i) {
    REQUIRE_FALSE(tu_files[i].empty());
    CHECK(tu_files[i].front().find(i == 0 ? "shapes.hpp" : "enums.hpp") !=
          std::string::npos);
    for (const auto& dep : tu_files[i]) {
      CHECK(dep.find(i == 0 ? "enums.hpp" : "shapes.hpp") == std::string::npos);
    }
  }
}

TEST_CASE("a header with no entry of its own gets a sibling .cpp's flags",
          "[parser]") {
  // compile_commands.json only ever lists translation units (.cpp), never the
  // headers they include. A header with no entry of its own still picks up its
  // sibling .cpp's flags (e.g. the -I it needs to resolve an include) rather
  // than falling through to the parser's bare defaults -- libclang wraps the
  // database in an interpolating one that answers for unlisted files. Pinned
  // here because output silently degrades if that ever stops happening.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-sibling-cc-test");
  fs::remove_all(dir);
  fs::create_directories(dir / "extra");

  std::ofstream(dir / "extra" / "dep.hpp") << "inline int dep_value() { return 7; }\n";
  std::ofstream(dir / "widget.hpp")
      << "#include \"dep.hpp\"\ninline int widget_value() { return dep_value(); }\n";
  std::ofstream(dir / "widget.cpp") << "#include \"widget.hpp\"\n";

  {
    // generic_string() throughout: JSON has no \\U escape, so a native Windows
    // path would make the database unparseable ("Unrecognized escape code").
    // Forward slashes are valid JSON and clang accepts them on Windows too.
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"" << (dir / "widget.cpp").generic_string()
       << "\", \"arguments\": [\"c++\", \"-I" << (dir / "extra").generic_string()
       << "\", \"-c\", \"" << (dir / "widget.cpp").generic_string() << "\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file((dir / "widget.hpp").string(), mod));
  CHECK(find(mod, "widget_value") != nullptr);
  // Borrowed flags, so exactly one record saying so -- and nothing worse.
  REQUIRE(mod.diagnostics.size() == 1);
  CHECK(mod.diagnostics[0].severity == model::kSeverityWarning);
  CHECK(mod.diagnostics[0].text.find("no compilation database entry") !=
        std::string::npos);

  fs::remove_all(dir);
}

TEST_CASE("headers sharing one compile-database command share an umbrella TU",
          "[parser]") {
  // A configured compilation database used to force one translation unit per
  // input outright, which meant the Sphinx front end -- which requires a
  // database -- could never use umbrella batching at all (#214). Headers that
  // borrow the same entry are handed the same flags, so they can share a unit,
  // and now do.
  //
  // Observed through the preprocessor: `b_uses.hpp` declares something only
  // when the macro `a_defs.hpp` defines is already in scope, which is true
  // exactly when the two were parsed as one unit. Inputs are parsed in
  // canonical (path-lexicographic) order, so a_defs.hpp comes first whatever
  // order this test names them in.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-cc-batching-test");
  fs::remove_all(dir);
  fs::create_directories(dir);

  std::ofstream(dir / "a_defs.hpp")
      << "#define CQ_SHARED_UNIT 1\n/// From a.\ninline int from_a() { return 1; }\n";
  std::ofstream(dir / "b_uses.hpp")
      << "#ifdef CQ_SHARED_UNIT\n/// From b.\ninline int from_b_batched() { return 2; }\n#endif\n";
  std::ofstream(dir / "target.cpp") << "int target();\n";

  {
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.string()
       << "\", \"file\": \"" << (dir / "target.cpp").string()
       << "\", \"arguments\": [\"c++\", \"-std=c++20\", \"-c\", \""
       << (dir / "target.cpp").string() << "\"]}]";
  }

  const std::vector<std::string> inputs{(dir / "b_uses.hpp").string(),
                                        (dir / "a_defs.hpp").string()};
  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();

  const auto batched = parser::parse_files(inputs, opts);
  CHECK(find(batched, "from_a") != nullptr);
  CHECK(find(batched, "from_b_batched") != nullptr);

  // tu_batch = 1 still means exact per-file isolation, database or not: it is
  // the documented way to ask for it, and benchmarks/verify.py compares the two
  // to prove batching changes nothing else.
  parser::ParseOptions isolated = opts;
  isolated.tu_batch = 1;
  const auto separate = parser::parse_files(inputs, isolated);
  CHECK(find(separate, "from_a") != nullptr);
  CHECK(find(separate, "from_b_batched") == nullptr);

  fs::remove_all(dir);
}

TEST_CASE("headers with different compile-database commands are not batched together",
          "[parser]") {
  // Grouping by flag set is what keeps batching honest under a database: two
  // targets' headers must not land in one unit, or one target's -D would decide
  // what the other's headers declare -- and only one of the two commands could
  // be handed to libclang in the first place.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-cc-groups-test");
  fs::remove_all(dir);
  fs::create_directories(dir);

  std::ofstream(dir / "alpha.hpp")
      << "#define CQ_LEAKED 1\n#ifdef CQ_ALPHA\n"
         "/// Alpha.\ninline int alpha_flag() { return 1; }\n#endif\n";
  std::ofstream(dir / "beta.hpp")
      << "#ifdef CQ_BETA\n/// Beta.\ninline int beta_flag() { return 2; }\n#endif\n"
         "#ifdef CQ_LEAKED\n/// Leaked.\ninline int leaked_from_alpha() { return 3; }\n#endif\n";

  // Each header is listed with its own -D, so the two commands genuinely differ
  // rather than both being interpolated from one entry.
  {
    // generic_string() throughout: JSON has no \U escape, so a native Windows
    // path would make the database unparseable ("Unrecognized escape code").
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.generic_string() << "\", \"file\": \""
       << (dir / "alpha.hpp").generic_string()
       << "\", \"arguments\": [\"c++\", \"-std=c++20\", \"-DCQ_ALPHA\", \"-c\", \""
       << (dir / "alpha.hpp").generic_string() << "\"]},"
       << "{\"directory\": \"" << dir.generic_string() << "\", \"file\": \""
       << (dir / "beta.hpp").generic_string()
       << "\", \"arguments\": [\"c++\", \"-std=c++20\", \"-DCQ_BETA\", \"-c\", \""
       << (dir / "beta.hpp").generic_string() << "\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  const auto m = parser::parse_files(
      {(dir / "alpha.hpp").string(), (dir / "beta.hpp").string()}, opts);

  // Each header was parsed under its own target's define ...
  CHECK(find(m, "alpha_flag") != nullptr);
  CHECK(find(m, "beta_flag") != nullptr);
  // ... and neither inherited the other's preprocessor state.
  CHECK(find(m, "leaked_from_alpha") == nullptr);
  // Both are listed, so nothing borrowed another file's command either.
  for (const auto& d : m.diagnostics) {
    CHECK(d.text.find("no compilation database entry") == std::string::npos);
  }

  fs::remove_all(dir);
}

TEST_CASE(
    "compile-database entries differing only in -o still share an umbrella batch",
    "[parser]") {
  // The grouping key (Parser::compile_flag_keys) compares what CompileDb::
  // args_for answers, which already excludes -o and the other file-writing
  // flags (writes_a_file, exercised directly by the "never writes the files
  // it names" test above) -- so two headers, each with its own database
  // entry, whose commands differ only in their object-file path are exactly
  // equal-flags and must still land in one umbrella translation unit. #214's
  // sibling test above pins the no-entry/borrowed-command case; this pins the
  // two-own-entries case the mechanism was introduced for.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-cc-o-only-diff-test");
  fs::remove_all(dir);
  fs::create_directories(dir);

  std::ofstream(dir / "a_defs.hpp")
      << "#define CQ_SHARED_UNIT 1\n/// From a.\ninline int from_a() { return 1; }\n";
  std::ofstream(dir / "b_uses.hpp")
      << "#ifdef CQ_SHARED_UNIT\n/// From b.\ninline int from_b_batched() { return 2; }\n#endif\n";

  {
    // generic_string(): see the sibling batching test above.
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.generic_string() << "\", \"file\": \""
       << (dir / "a_defs.hpp").generic_string()
       << "\", \"arguments\": [\"c++\", \"-std=c++20\", \"-c\", \""
       << (dir / "a_defs.hpp").generic_string() << "\", \"-o\", \""
       << (dir / "a_defs.hpp.o").generic_string() << "\"]},"
       << "{\"directory\": \"" << dir.generic_string() << "\", \"file\": \""
       << (dir / "b_uses.hpp").generic_string()
       << "\", \"arguments\": [\"c++\", \"-std=c++20\", \"-c\", \""
       << (dir / "b_uses.hpp").generic_string() << "\", \"-o\", \""
       << (dir / "b_uses.hpp.o").generic_string() << "\"]}]";
  }

  const std::vector<std::string> inputs{(dir / "b_uses.hpp").string(),
                                        (dir / "a_defs.hpp").string()};
  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();

  const auto batched = parser::parse_files(inputs, opts);
  // Visible only if both headers landed in the same translation unit.
  CHECK(find(batched, "from_a") != nullptr);
  CHECK(find(batched, "from_b_batched") != nullptr);

  fs::remove_all(dir);
}

TEST_CASE("every batched member that borrowed its flags is reported", "[parser]") {
  // The batch's command is looked up once, for its first member -- but the
  // batch documents all of them, and each one borrowed a command describing a
  // different file. Reporting only the member the lookup went through would
  // silently drop that caveat for every other header in the batch, which
  // warnings-as-errors builds rely on seeing.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-cc-borrowed-batch-test");
  fs::remove_all(dir);
  fs::create_directories(dir);

  std::ofstream(dir / "one.hpp") << "/// One.\ninline int one_value() { return 1; }\n";
  std::ofstream(dir / "two.hpp") << "/// Two.\ninline int two_value() { return 2; }\n";
  std::ofstream(dir / "target.cpp") << "int target();\n";
  {
    // generic_string(): see the sibling batching test above.
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"" << (dir / "target.cpp").generic_string()
       << "\", \"arguments\": [\"c++\", \"-std=c++20\", \"-c\", \""
       << (dir / "target.cpp").generic_string() << "\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  const auto m = parser::parse_files(
      {(dir / "one.hpp").string(), (dir / "two.hpp").string()}, opts);

  REQUIRE(find(m, "one_value") != nullptr);
  REQUIRE(find(m, "two_value") != nullptr);

  std::vector<std::string> borrowed;
  for (const auto& d : m.diagnostics) {
    if (d.text.find("no compilation database entry") != std::string::npos) {
      borrowed.push_back(d.file);
      CHECK(d.severity == model::kSeverityWarning);
    }
  }
  std::sort(borrowed.begin(), borrowed.end());
  CHECK(borrowed == std::vector<std::string>{(dir / "one.hpp").string(),
                                             (dir / "two.hpp").string()});

  fs::remove_all(dir);
}

TEST_CASE("an unused link-only flag is not reported even under the "
          "project's own -Werror",
          "[parser]") {
  // A real compile_commands.json entry routinely carries link-stage flags
  // (here -fuse-ld=lld) that a parse-only libclang invocation never reaches,
  // so clang reports them unused ([-Wunused-command-line-argument]). That's
  // ordinarily below the error threshold and silently dropped, but a project
  // built with -Werror promotes it to an error -- it should still never
  // surface, since it says nothing about the source being parsed.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-werror-cc-test");
  fs::remove_all(dir);
  fs::create_directories(dir);

  std::ofstream(dir / "widget.cpp") << "inline int widget_value() { return 3; }\n";

  {
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"" << (dir / "widget.cpp").generic_string()
       << "\", \"arguments\": [\"c++\", \"-Werror\", \"-fuse-ld=lld\", \"-c\", \""
       << (dir / "widget.cpp").generic_string() << "\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file((dir / "widget.cpp").string(), mod));
  CHECK(mod.diagnostics.empty());
  CHECK(find(mod, "widget_value") != nullptr);

  fs::remove_all(dir);
}

TEST_CASE("a differently spelled source path is still dropped from the args",
          "[parser]") {
  // A generated compile_commands.json may spell its file relative to the
  // entry's `directory`, or with unresolved `..` segments, while we look the
  // entry up by resolved path. Leaving that token in the argument list hands
  // libclang two input files and the translation unit fails outright.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-cc-spelling-test");
  fs::remove_all(dir);
  fs::create_directories(dir / "sub");
  std::ofstream(dir / "sub" / "widget.hpp")
      << "inline int widget_value() { return 3; }\n";

  {
    // "file" is relative to "directory"; the argument list spells the same file
    // a third way, with a `..` hop through the parent.
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"sub/widget.hpp\", \"arguments\": [\"c++\", "
          "\"-std=c++20\", \"-xc++\", \"-c\", \""
       << (dir / "sub" / ".." / "sub" / "widget.hpp").generic_string() << "\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file((dir / "sub" / "widget.hpp").string(),
                                          mod));
  CHECK(mod.diagnostics.empty());
  CHECK(find(mod, "widget_value") != nullptr);

  fs::remove_all(dir);
}

TEST_CASE("a '--' separator in a database entry does not sink the parse",
          "[parser]") {
  // CMake's header-set verification targets are written as
  // `c++ … -c -x c++-header … -- <header>`. Once the source operand is dropped
  // the separator has nothing left to separate, but everything appended after
  // it — this parser's `-xc++`, and libclang's own `-fsyntax-only` — is read by
  // the driver as a file name instead of a flag. That leaves no compiler job,
  // so libclang builds no translation unit at all and the header silently
  // documents nothing.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-cc-separator");
  fs::remove_all(dir);
  fs::create_directories(dir);
  std::ofstream(dir / "widget.hpp")
      << "#pragma once\ninline int widget_value() { return 4; }\n";

  {
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"widget.hpp\", \"arguments\": [\"c++\", "
          "\"-Wall\", \"-Werror\", \"-c\", \"-x\", \"c++-header\", "
          "\"-std=gnu++20\", \"--\", \"widget.hpp\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  opts.capture_all_diagnostics = true;
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file((dir / "widget.hpp").string(), mod));
  // Clean, not merely non-fatal: the entry's own `-x c++-header` survives, so
  // the `#pragma once` above is not reported as being in a main file — which
  // this entry's -Werror would turn into an error on a perfectly good header.
  CHECK(mod.diagnostics.empty());
  CHECK(find(mod, "widget_value") != nullptr);

  fs::remove_all(dir);
}

TEST_CASE("an entry's own -x survives, and is supplied when it has none",
          "[parser]") {
  // `-x` applies to the inputs after it and libclang appends the source last,
  // so anything this parser appends wins over the entry. A database that has
  // stated the language knows better than the fallback does; one that has not
  // still needs it, or a header without a .cpp extension parses as C.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-cc-language");
  fs::remove_all(dir);
  fs::create_directories(dir);
  // Valid C++, invalid C: it only parses if the language is right.
  std::ofstream(dir / "plain.h")
      << "namespace demo { inline int plain_value() { return 5; } }\n";

  {
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"plain.h\", \"arguments\": [\"c++\", "
          "\"-std=c++20\", \"-c\", \"plain.h\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  opts.capture_all_diagnostics = true;
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file((dir / "plain.h").string(), mod));
  CHECK(find(mod, "demo::plain_value") != nullptr);
  for (const auto& d : mod.diagnostics) {
    CHECK(d.severity < model::kSeverityError);
  }

  fs::remove_all(dir);
}

TEST_CASE("flags that describe another file are reported", "[parser]") {
  // A header-only library parses with its tests' flags, which is the right
  // answer far more often than not -- but it is still a guess, and this project
  // reports a guess rather than let it produce plausible, wrong documentation
  // in silence.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-cc-borrow-report");
  fs::remove_all(dir);
  fs::create_directories(dir / "include" / "geo");
  fs::create_directories(dir / "tests");
  std::ofstream(dir / "include" / "geo" / "api.hpp")
      << "#pragma once\nnamespace geo {\n#ifdef GEO_FEATURE\n"
         "inline int api_value() { return 7; }\n#endif\n}\n";
  std::ofstream(dir / "tests" / "test_geo.cpp") << "int main() { return 0; }\n";

  {
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"tests/test_geo.cpp\", \"arguments\": [\"c++\", "
          "\"-std=c++20\", \"-DGEO_FEATURE=1\", \"-c\", "
          "\"tests/test_geo.cpp\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  const fs::path header = dir / "include" / "geo" / "api.hpp";
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file(header.string(), mod));

  // The borrowed -D really did apply.
  CHECK(find(mod, "geo::api_value") != nullptr);

  REQUIRE(mod.diagnostics.size() == 1);
  const model::Diagnostic& note = mod.diagnostics[0];
  CHECK(note.severity == model::kSeverityWarning);
  CHECK(note.depth == 0);
  CHECK(note.file == header.string());
  CHECK(note.text.find("no compilation database entry") != std::string::npos);
  CHECK(note.text.find(header.string()) != std::string::npos);

  fs::remove_all(dir);
}

TEST_CASE("a file the database really lists is not reported as borrowed",
          "[parser]") {
  // The report has to distinguish an entry from an interpolated one, including
  // when the database spells its file differently from the path we look up.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-cc-borrow-exact");
  fs::remove_all(dir);
  fs::create_directories(dir / "sub");
  std::ofstream(dir / "sub" / "widget.hpp")
      << "#pragma once\ninline int widget_value() { return 3; }\n";
  std::ofstream(dir / "other.cpp") << "int other() { return 1; }\n";

  {
    // "file" is relative to "directory" while the lookup is by resolved path.
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"sub/widget.hpp\", \"arguments\": [\"c++\", "
          "\"-std=c++20\", \"-c\", \"sub/widget.hpp\"]},"
       << "{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"other.cpp\", \"arguments\": [\"c++\", "
          "\"-std=c++20\", \"-c\", \"other.cpp\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  opts.capture_all_diagnostics = true;
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file((dir / "sub" / "widget.hpp").string(),
                                          mod));

  CHECK(find(mod, "widget_value") != nullptr);
  CHECK(mod.diagnostics.empty());

  fs::remove_all(dir);
}

TEST_CASE("an entry's paths resolve against its own directory, not ours",
          "[parser]") {
  // A compile_commands.json entry may spell its flags relative to its own
  // `directory` -- the format allows it and `-Iinclude` is a common way to
  // write one. A build system runs the command from there; libclang does not
  // chdir, so without replaying the working directory clang resolves the
  // include against whatever directory the docs build runs in. The header then
  // parses "successfully" with its dependency missing, and the declarations
  // that needed it quietly vanish from the output.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-cc-workdir");
  fs::remove_all(dir);
  fs::create_directories(dir / "include" / "geo" / "detail");
  fs::create_directories(dir / "src");
  std::ofstream(dir / "include" / "geo" / "detail" / "traits.hpp")
      << "#pragma once\nnamespace geo::detail { using scalar_t = double; }\n";
  // Angle include: only findable through the entry's -I.
  std::ofstream(dir / "include" / "geo" / "mesh.hpp")
      << "#pragma once\n#include <geo/detail/traits.hpp>\n"
         "namespace geo { struct Mesh { detail::scalar_t x; }; }\n";
  std::ofstream(dir / "src" / "main.cpp") << "int main() { return 0; }\n";

  {
    // Every path relative to "directory", -I included. The test process runs
    // somewhere else entirely, which is the whole point.
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"src/main.cpp\", \"arguments\": [\"c++\", "
          "\"-std=c++20\", \"-Iinclude\", \"-c\", \"src/main.cpp\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  opts.capture_all_diagnostics = true;
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file(
      (dir / "include" / "geo" / "mesh.hpp").string(), mod));

  CHECK(find(mod, "geo::Mesh") != nullptr);
  CHECK(find(mod, "geo::Mesh::x") != nullptr);
  for (const auto& d : mod.diagnostics) {
    CHECK(d.severity < model::kSeverityError);
  }

  fs::remove_all(dir);
}

TEST_CASE("a header borrowing another file's flags is parsed as a header",
          "[parser]") {
  // The common shape, and the one that used to warn on every file: the header
  // has no entry, so libclang interpolates the nearest .cpp's command -- which
  // names no language, leaving clangquill to supply one. Under plain `c++` the
  // header's own `#pragma once` is in the main file, which clang reports and a
  // project's -Werror turns into an error on a header that compiles cleanly.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-cc-header-lang");
  fs::remove_all(dir);
  fs::create_directories(dir / "include");
  fs::create_directories(dir / "tests");
  std::ofstream(dir / "include" / "widget.hpp")
      << "#pragma once\ninline int widget_value() { return 4; }\n";
  std::ofstream(dir / "tests" / "test_widget.cpp") << "int main() { return 0; }\n";

  {
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"tests/test_widget.cpp\", \"arguments\": [\"c++\", "
          "\"-Wall\", \"-Werror\", \"-std=c++20\", \"-c\", "
          "\"tests/test_widget.cpp\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  opts.capture_all_diagnostics = true;
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file(
      (dir / "include" / "widget.hpp").string(), mod));

  CHECK(find(mod, "widget_value") != nullptr);
  for (const auto& d : mod.diagnostics) {
    CHECK(d.text.find("pragma-once-outside-header") == std::string::npos);
  }

  fs::remove_all(dir);
}

TEST_CASE("a header is parsed as a header under the fallback flags too",
          "[parser]") {
  // Same reasoning with no database at all: `#pragma once` is how headers are
  // written, so the -std/-I/-D path must not report it either. An
  // extension-less input counts as a header as well -- that spelling belongs to
  // the standard library and its imitators, never to a translation unit.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-header-lang-default");
  fs::remove_all(dir);
  fs::create_directories(dir);
  std::ofstream(dir / "widget.hpp")
      << "#pragma once\ninline int widget_value() { return 4; }\n";
  std::ofstream(dir / "vector_like")
      << "#pragma once\nnamespace demo { inline int extensionless() { return 5; } }\n";

  parser::ParseOptions opts;
  opts.capture_all_diagnostics = true;
  for (const std::string& input : {"widget.hpp", "vector_like"}) {
    model::ParsedModule mod;
    REQUIRE(parser::Parser(opts).parse_file((dir / input).string(), mod));
    for (const auto& d : mod.diagnostics) {
      CHECK(d.text.find("pragma-once-outside-header") == std::string::npos);
    }
  }

  fs::remove_all(dir);
}

TEST_CASE("an uppercase header extension is parsed as a header", "[parser]") {
  // `.H` is a real, if old-school, C++ header spelling. Matching the extension
  // list case-sensitively made such a header miss header mode entirely, so its
  // own `#pragma once` was reported as being in a main file -- the failure the
  // whole `-xc++-header` dance exists to avoid.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-header-lang-upper");
  fs::remove_all(dir);
  fs::create_directories(dir);
  std::ofstream(dir / "widget.H")
      << "#pragma once\nnamespace demo { inline int upper_h() { return 6; } }\n";
  std::ofstream(dir / "gadget.HPP")
      << "#pragma once\nnamespace demo { inline int upper_hpp() { return 7; } }\n";
  std::ofstream(dir / "thing.Hxx")
      << "#pragma once\nnamespace demo { inline int mixed_hxx() { return 8; } }\n";

  parser::ParseOptions opts;
  opts.capture_all_diagnostics = true;
  for (const std::string& input : {"widget.H", "gadget.HPP", "thing.Hxx"}) {
    model::ParsedModule mod;
    REQUIRE(parser::Parser(opts).parse_file((dir / input).string(), mod));
    for (const auto& d : mod.diagnostics) {
      CHECK(d.text.find("pragma-once-outside-header") == std::string::npos);
    }
  }

  fs::remove_all(dir);
}

TEST_CASE("replaying a database entry never writes the files it names",
          "[parser]") {
  // A real entry names an object file, a make-style dependency list and often
  // a serialized diagnostics file. libclang appends -fsyntax-only, so none of
  // those outputs belongs to this run -- but clang still writes the dependency
  // list and the diagnostics file when asked, into the user's tree.
  //
  // It is worse than one stray file. The database libclang hands back
  // interpolates: a header with no entry of its own gets the nearest entry's
  // command with only the filename substituted, so every documented header
  // inherits the *same* -MF path, and batches parse concurrently.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-cc-no-outputs");
  fs::remove_all(dir);
  fs::create_directories(dir);
  std::ofstream(dir / "widget.hpp") << "inline int widget_value() { return 3; }\n";

  {
    std::ofstream cc(dir / "compile_commands.json");
    // Absolute output paths, because clang resolves a relative one against the
    // *process* working directory -- libclang never chdir's into the entry's
    // `directory` -- which for a real build is the Sphinx srcdir.
    cc << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"widget.hpp\", \"arguments\": [\"c++\", "
          "\"-std=c++20\", \"-MD\", \"-MF\", \"" << (dir / "widget.d").generic_string()
       << "\", \"-MT\", \"widget.o\", \"--serialize-diagnostics\", \""
       << (dir / "widget.dia").generic_string() << "\", \"-o\", \""
       << (dir / "widget.o").generic_string() << "\", \"-c\", \"widget.hpp\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  opts.capture_all_diagnostics = true;
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file((dir / "widget.hpp").string(), mod));

  // The rest of the command line still applied.
  CHECK(find(mod, "widget_value") != nullptr);
  CHECK_FALSE(fs::exists(dir / "widget.d"));
  CHECK_FALSE(fs::exists(dir / "widget.dia"));
  CHECK_FALSE(fs::exists(dir / "widget.o"));

  fs::remove_all(dir);
}

TEST_CASE("an interpolated entry's outputs are not written either", "[parser]") {
  // The case that actually bites: the header has no entry at all, so the
  // command -- output paths included -- is borrowed wholesale from a file it
  // has nothing to do with.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-cc-interp-outputs");
  fs::remove_all(dir);
  fs::create_directories(dir / "include");
  fs::create_directories(dir / "tests");
  std::ofstream(dir / "include" / "widget.hpp")
      << "inline int widget_value() { return 3; }\n";
  std::ofstream(dir / "tests" / "test_widget.cpp") << "int main() { return 0; }\n";

  {
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"tests/test_widget.cpp\", \"arguments\": [\"c++\", "
          "\"-std=c++20\", \"-MD\", \"-MF\", \"" << (dir / "test_widget.d").generic_string()
       << "\", \"-o\", \"" << (dir / "test_widget.o").generic_string()
       << "\", \"-c\", \"tests/test_widget.cpp\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file(
      (dir / "include" / "widget.hpp").string(), mod));

  CHECK(find(mod, "widget_value") != nullptr);
  CHECK_FALSE(fs::exists(dir / "test_widget.d"));
  CHECK_FALSE(fs::exists(dir / "test_widget.o"));

  fs::remove_all(dir);
}

TEST_CASE("an unloadable compile database is reported with the path searched",
          "[parser]") {
  // libclang reports a database it cannot open exactly like "no entry for this
  // file" -- by returning no flags -- so the fallback to -std/-I/-D would
  // otherwise kick in silently and yield plausible but wrong output.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-missing-cc-test");
  fs::remove_all(dir);
  fs::create_directories(dir);
  std::ofstream(dir / "widget.hpp") << "inline int widget_value() { return 1; }\n";

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();  // holds no compile_commands.json
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file((dir / "widget.hpp").string(), mod));

  REQUIRE(mod.diagnostics.size() == 1);
  CHECK(mod.diagnostics[0].severity == model::kSeverityError);
  CHECK(mod.diagnostics[0].text.find("could not load a compilation database") !=
        std::string::npos);
  CHECK(mod.diagnostics[0].text.find(
            (dir / "compile_commands.json").string()) != std::string::npos);
  // The parse still succeeds on the fallback flags.
  CHECK(find(mod, "widget_value") != nullptr);

  fs::remove_all(dir);
}

namespace {

// Writes `contents` into a fresh scratch directory and returns the file path.
std::filesystem::path write_scratch(const std::string& dir_name,
                                    const std::string& file_name,
                                    const std::string& contents) {
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir(dir_name);
  fs::create_directories(dir);
  const fs::path file = dir / file_name;
  std::ofstream(file) << contents;
  return file;
}

// A warning clang always emits under the default flags (-W#warnings is on by
// default, and unlike the unused-* families it does not depend on function
// bodies, which the parser skips).
constexpr const char* kWarningSource =
    "#warning \"widget is on its way out\"\n"
    "inline int widget_value() { return 2; }\n";

// A redefinition, which libclang reports as an error carrying a
// "previous definition is here" note.
constexpr const char* kErrorWithNoteSource =
    "struct Widget { int a; };\n"
    "struct Widget { int b; };\n";

int count_severity(const model::ParsedModule& m, int severity) {
  int n = 0;
  for (const auto& d : m.diagnostics) {
    if (d.severity == severity) ++n;
  }
  return n;
}

}  // namespace

TEST_CASE("warnings are dropped unless full capture is requested", "[parser]") {
  const auto file = write_scratch("clangquill-diag-default", "widget.hpp",
                                  kWarningSource);

  parser::ParseOptions opts;  // capture_all_diagnostics defaults to false
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file(file.string(), mod));

  // Nothing below error severity survives, so the console stream stays quiet.
  for (const auto& d : mod.diagnostics) {
    CHECK(d.severity >= model::kSeverityError);
  }
  CHECK(count_severity(mod, model::kSeverityWarning) == 0);

  std::filesystem::remove_all(file.parent_path());
}

TEST_CASE("capture_all_diagnostics keeps warnings with their location",
          "[parser]") {
  const auto file =
      write_scratch("clangquill-diag-all", "widget.hpp", kWarningSource);

  parser::ParseOptions opts;
  opts.capture_all_diagnostics = true;
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file(file.string(), mod));

  REQUIRE(count_severity(mod, model::kSeverityWarning) >= 1);
  const model::Diagnostic* warning = nullptr;
  for (const auto& d : mod.diagnostics) {
    if (d.severity == model::kSeverityWarning) {
      warning = &d;
      break;
    }
  }
  REQUIRE(warning != nullptr);
  CHECK(warning->text.find("widget is on its way out") != std::string::npos);
  CHECK(warning->file == file.string());
  CHECK(warning->line == 1);
  CHECK(warning->column > 0);
  CHECK(warning->depth == 0);

  std::filesystem::remove_all(file.parent_path());
}

TEST_CASE(
    "capture_all_diagnostics still drops -Wunused-command-line-argument",
    "[parser]") {
  // A parse-only invocation never reaches the link job that a flag like
  // -fuse-ld=lld governs, so it's always reported "unused" -- a fact about
  // clangquill's own argument replay, not the source. That must stay
  // filtered even when the caller asked to see every diagnostic.
  const auto file = write_scratch("clangquill-diag-unused-all", "widget.hpp",
                                  "inline int widget_value() { return 3; }\n");

  parser::ParseOptions opts;
  opts.capture_all_diagnostics = true;
  opts.extra_args.push_back("-fuse-ld=lld");
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file(file.string(), mod));

  for (const auto& d : mod.diagnostics) {
    CHECK(d.text.find("fuse-ld") == std::string::npos);
  }

  std::filesystem::remove_all(file.parent_path());
}

TEST_CASE("attached notes are captured one level below their parent",
          "[parser]") {
  const auto file = write_scratch("clangquill-diag-notes", "widget.hpp",
                                  kErrorWithNoteSource);

  parser::ParseOptions opts;
  opts.capture_all_diagnostics = true;
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file(file.string(), mod));

  // The redefinition error, immediately followed by its "previous definition
  // is here" note at depth 1 — the explanatory half that plain error-only
  // capture throws away.
  std::size_t error_at = mod.diagnostics.size();
  for (std::size_t i = 0; i < mod.diagnostics.size(); ++i) {
    if (mod.diagnostics[i].severity >= model::kSeverityError &&
        mod.diagnostics[i].depth == 0) {
      error_at = i;
      break;
    }
  }
  REQUIRE(error_at + 1 < mod.diagnostics.size());
  CHECK(mod.diagnostics[error_at].text.find("redefinition") !=
        std::string::npos);
  CHECK(mod.diagnostics[error_at + 1].depth == 1);
  CHECK(mod.diagnostics[error_at + 1].severity == model::kSeverityNote);
  CHECK(mod.diagnostics[error_at + 1].text.find("previous definition") !=
        std::string::npos);

  std::filesystem::remove_all(file.parent_path());
}

TEST_CASE("notes are dropped when full capture is off", "[parser]") {
  const auto file = write_scratch("clangquill-diag-nonotes", "widget.hpp",
                                  kErrorWithNoteSource);

  parser::ParseOptions opts;
  model::ParsedModule mod;
  parser::Parser(opts).parse_file(file.string(), mod);

  REQUIRE_FALSE(mod.diagnostics.empty());
  for (const auto& d : mod.diagnostics) {
    CHECK(d.depth == 0);
    CHECK(d.severity >= model::kSeverityError);
  }

  std::filesystem::remove_all(file.parent_path());
}

TEST_CASE("an error's location is captured even when full capture is off",
          "[parser]") {
  // A caller that persists an error-severity diagnostic across runs (e.g. to
  // know which file it belongs to once that file leaves the build) needs its
  // location even without capture_all_diagnostics — dropping notes is not the
  // same as dropping where the surviving diagnostic itself occurred.
  const auto file = write_scratch("clangquill-diag-location", "widget.hpp",
                                  kErrorWithNoteSource);

  parser::ParseOptions opts;  // capture_all_diagnostics defaults to false
  model::ParsedModule mod;
  parser::Parser(opts).parse_file(file.string(), mod);

  REQUIRE_FALSE(mod.diagnostics.empty());
  const auto& d = mod.diagnostics.front();
  CHECK(d.file == file.string());
  CHECK(d.line == 2);
  CHECK(d.column > 0);

  std::filesystem::remove_all(file.parent_path());
}

TEST_CASE("a diagnostic shared by several batches is merged once", "[parser]") {
  // Two inputs in separate umbrella batches both pull in the same bad header.
  // Without dedup its error — and its note — would be reported once per batch.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-diag-dedup");
  fs::remove_all(dir);
  fs::create_directories(dir);
  std::ofstream(dir / "shared.hpp") << "#pragma once\n" << kErrorWithNoteSource;
  std::ofstream(dir / "a.hpp")
      << "#include \"shared.hpp\"\ninline int a_value() { return 1; }\n";
  std::ofstream(dir / "b.hpp")
      << "#include \"shared.hpp\"\ninline int b_value() { return 2; }\n";

  parser::ParseOptions opts;
  opts.capture_all_diagnostics = true;
  opts.tu_batch = 1;  // one batch per input, so the header is parsed twice
  opts.jobs = 1;
  model::ParsedModule mod = parser::parse_files(
      {(dir / "a.hpp").string(), (dir / "b.hpp").string()}, opts);

  int redefinitions = 0;
  int previous_definitions = 0;
  int include_stacks = 0;
  for (const auto& d : mod.diagnostics) {
    if (d.text.find("redefinition") != std::string::npos) ++redefinitions;
    if (d.text.find("previous definition") != std::string::npos) {
      ++previous_definitions;
    }
    if (d.text.find("in file included from") != std::string::npos) {
      ++include_stacks;
    }
  }
  CHECK(redefinitions == 1);
  // The note survived alongside the parent it explains rather than being
  // orphaned or dropped with the duplicate.
  CHECK(previous_definitions == 1);
  // Exactly one include stack survives, from whichever batch got there first.
  // libclang names the *including* TU in that note, so the two groups differ
  // by construction — which is why merge_diagnostics keys on the parent alone.
  // Widening the key to the whole note chain would make this 2 and defeat the
  // dedup entirely.
  CHECK(include_stacks == 1);

  // Streaming the batches out one at a time does not weaken the dedup: the
  // sink sees no diagnostics and parse_files still returns the single copy.
  model::ParsedModule streamed = parser::parse_files(
      {(dir / "a.hpp").string(), (dir / "b.hpp").string()}, opts, nullptr,
      nullptr,
      [](model::ParsedModule&& part) { CHECK(part.diagnostics.empty()); });
  CHECK(diagnostic_texts(streamed) == diagnostic_texts(mod));

  fs::remove_all(dir);
}

TEST_CASE("the compile-database failure is reported once per parser",
          "[parser]") {
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-missing-cc-once");
  fs::remove_all(dir);
  fs::create_directories(dir);
  std::ofstream(dir / "a.hpp") << "inline int a_value() { return 1; }\n";
  std::ofstream(dir / "b.hpp") << "inline int b_value() { return 2; }\n";

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  parser::Parser parser(opts);
  model::ParsedModule mod;
  REQUIRE(parser.parse_file((dir / "a.hpp").string(), mod));
  REQUIRE(parser.parse_file((dir / "b.hpp").string(), mod));

  CHECK(mod.diagnostics.size() == 1);

  fs::remove_all(dir);
}

namespace {

// The whole `failed to parse` group: the record itself plus every note nested
// under it, joined so a test can assert on the report as a reader sees it.
// Renders `arg` the way the report's copy-pasteable command tail does: an
// argument carrying anything a shell would act on — a backslash, as every
// absolute path on Windows does, or a `$` — comes back single-quoted (see
// shell_quote in parser.cpp).
std::string as_logged(const std::string& arg) {
  static const std::string kUnquotedChars = "@%+=:,./-_";
  bool needs_quotes = arg.empty();
  for (char c : arg) {
    if (std::isalnum(static_cast<unsigned char>(c)) == 0 &&
        kUnquotedChars.find(c) == std::string::npos) {
      needs_quotes = true;
      break;
    }
  }
  if (!needs_quotes) return arg;
  std::string quoted = "'";
  for (char c : arg) {
    if (c == '\'') {
      quoted += "'\\''";
    } else {
      quoted += c;
    }
  }
  return quoted + "'";
}

std::string failure_report(const model::ParsedModule& m) {
  std::string report;
  bool inside = false;
  for (const auto& d : m.diagnostics) {
    if (d.depth == 0) inside = d.text.find("failed to parse") != std::string::npos;
    if (inside) report += d.text + "\n";
  }
  return report;
}

}  // namespace

TEST_CASE("a missing input reports why libclang refused it", "[parser]") {
  // libclang returns no translation unit — and therefore no diagnostics at all
  // — for an input the driver cannot open, so a bare "failed to parse" line was
  // everything the log ever carried for exactly this case.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-parse-fail-missing");
  fs::remove_all(dir);
  fs::create_directories(dir);

  parser::ParseOptions opts;
  opts.capture_all_diagnostics = true;
  opts.include_dirs.push_back((dir / "include").string());
  model::ParsedModule mod;
  CHECK_FALSE(parser::Parser(opts).parse_file((dir / "gone.hpp").string(), mod));

  const std::string report = failure_report(mod);
  CHECK(report.find("failed to parse: " + (dir / "gone.hpp").string()) !=
        std::string::npos);
  CHECK(report.find("CXError_Failure") != std::string::npos);
  CHECK(report.find("the input file does not exist") != std::string::npos);
  // The exact argv is in the log, so a wrong include path or standard is
  // visible without reproducing the run.
  CHECK(report.find(as_logged("-I" + (dir / "include").string())) !=
        std::string::npos);
  CHECK(report.find("-xc++") != std::string::npos);

  // Every explanatory record hangs off the failure, so error-only consumers
  // (the console stream) still see one line per failed input.
  int top_level = 0;
  for (const auto& d : mod.diagnostics) {
    if (d.depth == 0) ++top_level;
    if (d.depth > 0) CHECK(d.severity == model::kSeverityNote);
  }
  CHECK(top_level == 1);

  fs::remove_all(dir);
}

TEST_CASE("the logged command tail survives a paste into a shell", "[parser]") {
  // The report advertises the argv as something a reader can paste back, so it
  // has to be quoted the way a POSIX shell reads it. Double quotes are not
  // enough: `$`, a backtick and `!` keep their meaning inside them, so a
  // define carrying a command substitution used to come back as a command the
  // paste would *run*. Single quotes leave nothing special, with `'` spliced.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-parse-fail-quoting");
  fs::remove_all(dir);
  fs::create_directories(dir);

  const std::string substitution = "-DGREETING=$(id)";
  const std::string backticks = "-DSTAMP=`date`";
  const std::string apostrophe = "-DNAME=it's";

  parser::ParseOptions opts;
  opts.capture_all_diagnostics = true;
  opts.extra_args = {substitution, backticks, apostrophe};
  model::ParsedModule mod;
  CHECK_FALSE(parser::Parser(opts).parse_file((dir / "gone.hpp").string(), mod));

  const std::string report = failure_report(mod);
  CHECK(report.find("'" + substitution + "'") != std::string::npos);
  CHECK(report.find("'" + backticks + "'") != std::string::npos);
  CHECK(report.find("'-DNAME=it'\\''s'") != std::string::npos);
  // Nothing survives outside quotes: a bare `$(` or backtick in the tail is
  // exactly the paste hazard this guards against.
  CHECK(report.find("\"" + substitution) == std::string::npos);
  CHECK(report.find("\"" + backticks) == std::string::npos);

  // Ordinary flags stay unquoted, so the common tail reads as it always did.
  CHECK(report.find("-std=c++20") != std::string::npos);
  CHECK(report.find("'-std=c++20'") == std::string::npos);

  fs::remove_all(dir);
}

TEST_CASE("a database entry with a second input is named, and the file is "
          "re-parsed for its real diagnostics",
          "[parser]") {
  // libclang creates no translation unit when the command carries more than one
  // input — the classic way a compile_commands.json entry breaks a parse. The
  // report has to say that, and then get clang's actual complaints about the
  // source into the log by parsing it under flags known to be well formed.
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-parse-fail-argv");
  fs::remove_all(dir);
  fs::create_directories(dir);
  std::ofstream(dir / "widget.hpp") << kErrorWithNoteSource;
  std::ofstream(dir / "other.cpp") << "int other() { return 1; }\n";
  {
    std::ofstream db(dir / "compile_commands.json");
    db << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"widget.hpp\", \"arguments\": [\"clang++\", "
          "\"-std=c++20\", \"-DWIDGET_FROM_DB=1\", \"-c\", \"widget.hpp\", \""
       << (dir / "other.cpp").generic_string() << "\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  opts.capture_all_diagnostics = true;
  model::ParsedModule mod;
  CHECK_FALSE(parser::Parser(opts).parse_file((dir / "widget.hpp").string(), mod));

  const std::string report = failure_report(mod);
  CHECK(report.find("names a second input file") != std::string::npos);
  CHECK(report.find((dir / "other.cpp").generic_string()) != std::string::npos);
  // The flags are quoted verbatim, and attributed to the database rather than
  // to clangquill's own -std/-I/-D options.
  CHECK(report.find("from the compilation database") != std::string::npos);
  CHECK(report.find("-DWIDGET_FROM_DB=1") != std::string::npos);
  // The recovery parse: libclang's own error about this file, which the failed
  // parse discarded, is now in the log.
  CHECK(report.find("re-parsed with") != std::string::npos);
  CHECK(report.find("redefinition") != std::string::npos);
  CHECK(report.find("previous definition") != std::string::npos);

  // The recovered diagnostics nest under the note that introduces them, so the
  // log shows they came from a different command line than the project's.
  const model::Diagnostic* redefinition = nullptr;
  for (const auto& d : mod.diagnostics) {
    if (d.text.find("redefinition") != std::string::npos) redefinition = &d;
  }
  REQUIRE(redefinition != nullptr);
  CHECK(redefinition->depth == 2);

  fs::remove_all(dir);
}

TEST_CASE("a file that parses alone blames the compile flags", "[parser]") {
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-parse-fail-clean");
  fs::remove_all(dir);
  fs::create_directories(dir);
  std::ofstream(dir / "widget.hpp") << "inline int widget_value() { return 1; }\n";
  std::ofstream(dir / "other.cpp") << "int other() { return 1; }\n";
  {
    std::ofstream db(dir / "compile_commands.json");
    db << "[{\"directory\": \"" << dir.generic_string()
       << "\", \"file\": \"widget.hpp\", \"arguments\": [\"clang++\", "
          "\"-std=c++20\", \"-c\", \"widget.hpp\", \""
       << (dir / "other.cpp").generic_string() << "\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  opts.capture_all_diagnostics = true;
  model::ParsedModule mod;
  CHECK_FALSE(parser::Parser(opts).parse_file((dir / "widget.hpp").string(), mod));

  CHECK(failure_report(mod).find("the compile flags are what libclang "
                                 "rejected") != std::string::npos);

  fs::remove_all(dir);
}

TEST_CASE("a batch member libclang never opened is reported", "[parser]") {
  // Umbrella batching left this case silent: the input produced no symbols and
  // no message, which reads as "nothing to document" rather than "never
  // parsed".
  namespace fs = std::filesystem;
  const fs::path dir = unique_temp_dir("clangquill-batch-unopened");
  fs::remove_all(dir);
  fs::create_directories(dir);
  std::ofstream(dir / "a.hpp") << "inline int a_value() { return 1; }\n";

  parser::ParseOptions opts;
  opts.capture_all_diagnostics = true;
  opts.jobs = 1;
  std::vector<bool> ok;
  model::ParsedModule mod = parser::parse_files(
      {(dir / "a.hpp").string(), (dir / "gone.hpp").string()}, opts, nullptr,
      &ok);

  REQUIRE(ok.size() == 2);
  CHECK(ok[0]);
  CHECK_FALSE(ok[1]);
  const std::string report = failure_report(mod);
  CHECK(report.find("failed to parse: " + (dir / "gone.hpp").string()) !=
        std::string::npos);
  CHECK(report.find("never opened this file") != std::string::npos);
  CHECK(report.find("the input file does not exist") != std::string::npos);
  // The good member still parsed.
  CHECK(find(mod, "a_value") != nullptr);

  fs::remove_all(dir);
}

TEST_CASE("parser records parameter default arguments", "[parser]") {
  auto m = parse_fixture("param_defaults.hpp");

  const auto* draw = find(m, "defaults::draw");
  REQUIRE(draw != nullptr);

  std::vector<model::FunctionParameter> params;
  for (const auto& p : m.parameters) {
    if (p.function_usr == draw->usr) params.push_back(p);
  }
  REQUIRE(params.size() == 3);
  CHECK(params[0].default_value == "80");
  CHECK(params[1].default_value == "\"shape\"");
  // The type of this one closes two argument lists with a single `>>` token,
  // which used to hide the `=` that follows it.
  CHECK(params[2].name == "bounds");
  CHECK_FALSE(params[2].default_value.empty());

  const auto* resize = find(m, "defaults::Widget::resize");
  REQUIRE(resize != nullptr);
  for (const auto& p : m.parameters) {
    if (p.function_usr != resize->usr) continue;
    CHECK(p.default_value == (p.index == 1 ? "24" : ""));
  }
}

TEST_CASE("parameter defaults reach the content hash", "[parser]") {
  auto m = parse_fixture("param_defaults.hpp");
  const auto* value_or = find(m, "defaults::value_or");
  REQUIRE(value_or != nullptr);

  // A function template's parameters arrive as child cursors rather than
  // through clang_Cursor_getNumArguments; both paths must carry the default.
  bool found = false;
  for (const auto& p : m.parameters) {
    if (p.function_usr == value_or->usr && p.index == 1) {
      found = true;
      CHECK(p.default_value.find("T") != std::string::npos);
    }
  }
  CHECK(found);

  // The hash folds in the parameters, so editing only a default value
  // invalidates the cached page -- which it could not do while every
  // default_value was empty.
  std::vector<model::FunctionParameter> params;
  for (const auto& p : m.parameters) {
    if (p.function_usr == value_or->usr) params.push_back(p);
  }
  const std::string with_default = hash::content_hash(*value_or, params, "");
  params[1].default_value = "T{42}";
  CHECK(hash::content_hash(*value_or, params, "") != with_default);
}

#else  // !CLANGQUILL_HAVE_LIBCLANG

TEST_CASE("parser tests skipped without libclang", "[parser][!mayfail]") {
  SUCCEED("built without libclang");
}

#endif

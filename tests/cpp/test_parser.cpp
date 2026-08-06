#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <set>
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
  const fs::path dir = fs::temp_directory_path() / "clangquill-hash-cache-test";
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

  // Merge order follows input order, not thread completion order: the file
  // rows land in a stable sequence whether parsed serially or concurrently.
  std::vector<std::string> pa;
  std::vector<std::string> pb;
  for (const auto& f : a.files) pa.push_back(f.path);
  for (const auto& f : b.files) pb.push_back(f.path);
  CHECK(pa == pb);
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

TEST_CASE("compile_commands_dir falls back to the sibling .cpp for a header",
          "[parser]") {
  // compile_commands.json only ever lists translation units (.cpp), never the
  // headers they include. A header with no entry of its own should still pick
  // up its sibling .cpp's flags (e.g. the -I it needs to resolve an include),
  // rather than falling through to the parser's bare defaults.
  namespace fs = std::filesystem;
  const fs::path dir =
      fs::temp_directory_path() / "clangquill-sibling-cc-test";
  fs::remove_all(dir);
  fs::create_directories(dir / "extra");

  std::ofstream(dir / "extra" / "dep.hpp") << "inline int dep_value() { return 7; }\n";
  std::ofstream(dir / "widget.hpp")
      << "#include \"dep.hpp\"\ninline int widget_value() { return dep_value(); }\n";
  std::ofstream(dir / "widget.cpp") << "#include \"widget.hpp\"\n";

  {
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.string()
       << "\", \"file\": \"" << (dir / "widget.cpp").string()
       << "\", \"arguments\": [\"c++\", \"-I" << (dir / "extra").string()
       << "\", \"-c\", \"" << (dir / "widget.cpp").string() << "\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  model::ParsedModule mod;
  REQUIRE(parser::Parser(opts).parse_file((dir / "widget.hpp").string(), mod));
  CHECK(mod.diagnostics.empty());
  CHECK(find(mod, "widget_value") != nullptr);

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
  const fs::path dir = fs::temp_directory_path() / "clangquill-werror-cc-test";
  fs::remove_all(dir);
  fs::create_directories(dir);

  std::ofstream(dir / "widget.cpp") << "inline int widget_value() { return 3; }\n";

  {
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.string()
       << "\", \"file\": \"" << (dir / "widget.cpp").string()
       << "\", \"arguments\": [\"c++\", \"-Werror\", \"-fuse-ld=lld\", \"-c\", \""
       << (dir / "widget.cpp").string() << "\"]}]";
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
  const fs::path dir = fs::temp_directory_path() / "clangquill-cc-spelling-test";
  fs::remove_all(dir);
  fs::create_directories(dir / "sub");
  std::ofstream(dir / "sub" / "widget.hpp")
      << "inline int widget_value() { return 3; }\n";

  {
    // "file" is relative to "directory"; the argument list spells the same file
    // a third way, with a `..` hop through the parent.
    std::ofstream cc(dir / "compile_commands.json");
    cc << "[{\"directory\": \"" << dir.string()
       << "\", \"file\": \"sub/widget.hpp\", \"arguments\": [\"c++\", "
          "\"-std=c++20\", \"-xc++\", \"-c\", \""
       << (dir / "sub" / ".." / "sub" / "widget.hpp").string() << "\"]}]";
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

TEST_CASE("an unloadable compile database is reported with the path searched",
          "[parser]") {
  // libclang reports a database it cannot open exactly like "no entry for this
  // file" -- by returning no flags -- so the fallback to -std/-I/-D would
  // otherwise kick in silently and yield plausible but wrong output.
  namespace fs = std::filesystem;
  const fs::path dir = fs::temp_directory_path() / "clangquill-missing-cc-test";
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
  const fs::path dir = fs::temp_directory_path() / dir_name;
  fs::remove_all(dir);
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

TEST_CASE("a diagnostic shared by several batches is merged once", "[parser]") {
  // Two inputs in separate umbrella batches both pull in the same bad header.
  // Without dedup its error — and its note — would be reported once per batch.
  namespace fs = std::filesystem;
  const fs::path dir = fs::temp_directory_path() / "clangquill-diag-dedup";
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

  fs::remove_all(dir);
}

TEST_CASE("the compile-database failure is reported once per parser",
          "[parser]") {
  namespace fs = std::filesystem;
  const fs::path dir = fs::temp_directory_path() / "clangquill-missing-cc-once";
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
  const fs::path dir = fs::temp_directory_path() / "clangquill-parse-fail-missing";
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
  CHECK(report.find("-I" + (dir / "include").string()) != std::string::npos);
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

TEST_CASE("a database entry with a second input is named, and the file is "
          "re-parsed for its real diagnostics",
          "[parser]") {
  // libclang creates no translation unit when the command carries more than one
  // input — the classic way a compile_commands.json entry breaks a parse. The
  // report has to say that, and then get clang's actual complaints about the
  // source into the log by parsing it under flags known to be well formed.
  namespace fs = std::filesystem;
  const fs::path dir = fs::temp_directory_path() / "clangquill-parse-fail-argv";
  fs::remove_all(dir);
  fs::create_directories(dir);
  std::ofstream(dir / "widget.hpp") << kErrorWithNoteSource;
  std::ofstream(dir / "other.cpp") << "int other() { return 1; }\n";
  {
    std::ofstream db(dir / "compile_commands.json");
    db << "[{\"directory\": \"" << dir.string()
       << "\", \"file\": \"widget.hpp\", \"arguments\": [\"clang++\", "
          "\"-std=c++20\", \"-DWIDGET_FROM_DB=1\", \"-c\", \"widget.hpp\", \""
       << (dir / "other.cpp").string() << "\"]}]";
  }

  parser::ParseOptions opts;
  opts.compile_commands_dir = dir.string();
  opts.capture_all_diagnostics = true;
  model::ParsedModule mod;
  CHECK_FALSE(parser::Parser(opts).parse_file((dir / "widget.hpp").string(), mod));

  const std::string report = failure_report(mod);
  CHECK(report.find("names a second input file") != std::string::npos);
  CHECK(report.find((dir / "other.cpp").string()) != std::string::npos);
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
  const fs::path dir = fs::temp_directory_path() / "clangquill-parse-fail-clean";
  fs::remove_all(dir);
  fs::create_directories(dir);
  std::ofstream(dir / "widget.hpp") << "inline int widget_value() { return 1; }\n";
  std::ofstream(dir / "other.cpp") << "int other() { return 1; }\n";
  {
    std::ofstream db(dir / "compile_commands.json");
    db << "[{\"directory\": \"" << dir.string()
       << "\", \"file\": \"widget.hpp\", \"arguments\": [\"clang++\", "
          "\"-std=c++20\", \"-c\", \"widget.hpp\", \""
       << (dir / "other.cpp").string() << "\"]}]";
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
  const fs::path dir = fs::temp_directory_path() / "clangquill-batch-unopened";
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

#else  // !CLANGQUILL_HAVE_LIBCLANG

TEST_CASE("parser tests skipped without libclang", "[parser][!mayfail]") {
  SUCCEED("built without libclang");
}

#endif

// nanobind entry point for the clangquill C++ core.
//
// Exposes the libclang-backed parser (parse_to_sqlite) and small probes used by
// tests. Reads of the SQLite artifact happen in Python via stdlib sqlite3.

#include <nanobind/nanobind.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <unordered_map>

#include "core/version.hpp"
#include "model/diagnostic.hpp"
#include "store/schema.hpp"

#if defined(CLANGQUILL_HAVE_LIBCLANG)
#include <clang-c/Index.h>

#include "parser/parser.hpp"
#include "store/sqlite_store.hpp"
#endif

namespace nb = nanobind;

namespace {

bool have_libclang() {
#if defined(CLANGQUILL_HAVE_LIBCLANG)
  return true;
#else
  return false;
#endif
}

std::string libclang_version() {
#if defined(CLANGQUILL_HAVE_LIBCLANG)
  CXString s = clang_getClangVersion();
  const char* cstr = clang_getCString(s);
  std::string out = cstr ? cstr : "";
  clang_disposeString(s);
  return out;
#else
  return {};
#endif
}

// nanobind-friendly mirror of parser::ParseOptions.
struct PyParseOptions {
  std::string std_flag = "c++20";
  std::vector<std::string> include_dirs;
  std::vector<std::string> defines;
  std::vector<std::string> extra_args;
  std::optional<std::string> compile_commands_dir;
  bool keep_going = true;
  bool capture_all_diagnostics = false;
  bool extract_anonymous_namespaces = false;
  int jobs = 0;
  int tu_batch = 0;
};

struct ParseResult {
  int symbol_count = 0;
  int reference_count = 0;
  int file_count = 0;
  // Error-severity messages only, without their attached notes: the console
  // stream both front ends print. Independent of capture_all_diagnostics, so
  // enabling full capture never changes what a build prints.
  std::vector<std::string> diagnostics;
  // Everything that was captured, in parse order, notes flattened behind their
  // parent via Diagnostic::depth. Errors-only unless the parse asked for more.
  std::vector<clangquill::model::Diagnostic> diagnostic_records;

  // The set of files each input translation unit pulled in (the input itself
  // plus every transitive `#include`), in interned form: input `tu_inputs[i]`
  // depends on `tu_dep_paths[j]` for every j in `tu_dep_ids[i]`. Lets the
  // Python cache attribute each dependency to the input that needs it, so a
  // header edit re-parses only the translation units that include it.
  //
  // Interned rather than one string list per input: inputs sharing a header
  // (which, for the STL and a project's own core headers, means nearly every
  // input) would otherwise send that path across the binding once per input --
  // inputs x closure strings, millions of them on a large project, all
  // materialised as distinct Python objects. Here each distinct path crosses
  // once and the per-input lists are plain integers.
  std::vector<std::string> tu_inputs;
  std::vector<std::string> tu_dep_paths;
  std::vector<std::vector<std::int32_t>> tu_dep_ids;
};

#if defined(CLANGQUILL_HAVE_LIBCLANG)
clangquill::parser::ParseOptions to_core_options(const PyParseOptions& opt) {
  clangquill::parser::ParseOptions po;
  po.std_flag = opt.std_flag;
  po.include_dirs = opt.include_dirs;
  po.defines = opt.defines;
  po.extra_args = opt.extra_args;
  po.compile_commands_dir = opt.compile_commands_dir;
  po.keep_going = opt.keep_going;
  po.capture_all_diagnostics = opt.capture_all_diagnostics;
  po.extract_anonymous_namespaces = opt.extract_anonymous_namespaces;
  po.jobs = opt.jobs;
  po.tu_batch = opt.tu_batch;
  return po;
}

ParseResult result_from_module(const clangquill::model::ParsedModule& mod) {
  ParseResult res;
  res.symbol_count = static_cast<int>(mod.symbols.size());
  res.reference_count = static_cast<int>(mod.references.size());
  res.file_count = static_cast<int>(mod.files.size());
  res.diagnostic_records = mod.diagnostics;
  for (const auto& d : mod.diagnostics) {
    if (d.depth == 0 && d.severity >= clangquill::model::kSeverityError) {
      res.diagnostics.push_back(d.text);
    }
  }
  return res;
}
#endif

#if defined(CLANGQUILL_HAVE_LIBCLANG)
// Shared implementation of the parse entry points. `replace_only` selects the
// incremental write path: only the given inputs' rows are replaced inside an
// existing IR (every input must parse, or the whole call fails before any
// write), while a full parse rebuilds the database and tolerates per-input
// failures as diagnostics.
ParseResult parse_inputs(const std::vector<std::string>& inputs,
                         const std::string& db_path, const PyParseOptions& opt,
                         bool replace_only,
                         const std::vector<std::string>& dropped_candidates) {
  // Parse all inputs (batched into umbrella TUs and parallel, honouring
  // opt.jobs/opt.tu_batch) while capturing each translation unit's file set so
  // the cache can attribute every dependency to the input that pulled it in.
  std::vector<std::vector<std::string>> tu_files;
  std::vector<bool> tu_ok;
  clangquill::model::ParsedModule mod = clangquill::parser::parse_files(
      inputs, to_core_options(opt), &tu_files, &tu_ok);

  if (replace_only) {
    // Bail before writing on any hard parse failure: otherwise that file's
    // existing rows would be deleted and replaced with nothing, wiping good
    // documentation.
    for (std::size_t i = 0; i < inputs.size(); ++i) {
      if (!tu_ok[i]) {
        throw std::runtime_error("failed to parse translation unit: " +
                                 inputs[i]);
      }
    }
  }

  clangquill::store::SqliteStore store(db_path);
  if (replace_only) {
    store.write_tus(mod, clangquill::store::Meta::current(), inputs,
                    dropped_candidates);
  } else {
    store.write(mod, clangquill::store::Meta::current());
  }

  ParseResult res = result_from_module(mod);
  res.tu_inputs = inputs;
  res.tu_dep_ids.resize(inputs.size());
  // Shared closures are interned here rather than copied per input: one map
  // entry (and one Python string, later) per distinct path.
  std::unordered_map<std::string, std::int32_t> dep_ids;
  for (std::size_t i = 0; i < inputs.size(); ++i) {
    auto& ids = res.tu_dep_ids[i];
    ids.reserve(tu_files[i].size());
    for (const auto& file : tu_files[i]) {
      auto [it, inserted] = dep_ids.emplace(
          file, static_cast<std::int32_t>(res.tu_dep_paths.size()));
      if (inserted) res.tu_dep_paths.push_back(file);
      ids.push_back(it->second);
    }
  }
  return res;
}
#endif

ParseResult parse_to_sqlite(const std::vector<std::string>& inputs,
                            const std::string& db_path,
                            const PyParseOptions& opt) {
#if !defined(CLANGQUILL_HAVE_LIBCLANG)
  (void)inputs;
  (void)db_path;
  (void)opt;
  throw std::runtime_error(
      "clangquill._core was built without libclang; cannot parse");
#else
  return parse_inputs(inputs, db_path, opt, /*replace_only=*/false, {});
#endif
}

// Re-parses the given inputs into an existing IR, replacing only those
// translation units' rows in one transaction. The caller picks which inputs are
// stale (via the cache) and runs this once for the whole stale set instead of
// rebuilding the whole module.
//
// `dropped_candidates` names the files the previous parse attributed only to
// these inputs; any the re-parse no longer reaches is removed from the IR, so a
// header that falls out of an include closure does not linger until the next
// full rebuild.
ParseResult parse_tus_to_sqlite(
    const std::vector<std::string>& inputs, const std::string& db_path,
    const PyParseOptions& opt,
    const std::vector<std::string>& dropped_candidates) {
#if !defined(CLANGQUILL_HAVE_LIBCLANG)
  (void)inputs;
  (void)db_path;
  (void)opt;
  (void)dropped_candidates;
  throw std::runtime_error(
      "clangquill._core was built without libclang; cannot parse");
#else
  return parse_inputs(inputs, db_path, opt, /*replace_only=*/true,
                      dropped_candidates);
#endif
}

// Single-input convenience form of parse_tus_to_sqlite.
ParseResult parse_tu_to_sqlite(
    const std::string& input, const std::string& db_path,
    const PyParseOptions& opt,
    const std::vector<std::string>& dropped_candidates) {
  return parse_tus_to_sqlite({input}, db_path, opt, dropped_candidates);
}

}  // namespace

NB_MODULE(_core, m) {
  m.doc() = "clangquill C++ core (libclang-backed API extraction)";
  m.attr("__core_version__") = clangquill::core_version();
  m.attr("SCHEMA_VERSION") = clangquill::store::kSchemaVersion;
  m.def("have_libclang", &have_libclang,
        "Whether the core was built against libclang.");
  m.def("libclang_version", &libclang_version,
        "libclang version string, or '' when built without libclang.");

  nb::class_<PyParseOptions>(m, "ParseOptions")
      .def(nb::init<>())
      .def_rw("std_flag", &PyParseOptions::std_flag)
      .def_rw("include_dirs", &PyParseOptions::include_dirs)
      .def_rw("defines", &PyParseOptions::defines)
      .def_rw("extra_args", &PyParseOptions::extra_args)
      .def_rw("compile_commands_dir", &PyParseOptions::compile_commands_dir)
      .def_rw("keep_going", &PyParseOptions::keep_going)
      .def_rw("capture_all_diagnostics",
              &PyParseOptions::capture_all_diagnostics)
      .def_rw("extract_anonymous_namespaces",
              &PyParseOptions::extract_anonymous_namespaces)
      .def_rw("jobs", &PyParseOptions::jobs)
      .def_rw("tu_batch", &PyParseOptions::tu_batch);

  nb::class_<clangquill::model::Diagnostic>(m, "Diagnostic")
      .def_ro("severity", &clangquill::model::Diagnostic::severity)
      .def_ro("depth", &clangquill::model::Diagnostic::depth)
      .def_ro("text", &clangquill::model::Diagnostic::text)
      .def_ro("file", &clangquill::model::Diagnostic::file)
      .def_ro("line", &clangquill::model::Diagnostic::line)
      .def_ro("column", &clangquill::model::Diagnostic::column);

  // Every access to one of the vector attributes below converts the whole C++
  // vector into a fresh Python list -- there is no view type, so `res.diagnostics`
  // twice is two copies. Callers read each attribute once and bind the result.
  nb::class_<ParseResult>(m, "ParseResult")
      .def_ro("symbol_count", &ParseResult::symbol_count)
      .def_ro("reference_count", &ParseResult::reference_count)
      .def_ro("file_count", &ParseResult::file_count)
      .def_ro("diagnostics", &ParseResult::diagnostics)
      .def_ro("diagnostic_records", &ParseResult::diagnostic_records)
      .def_ro("tu_inputs", &ParseResult::tu_inputs)
      .def_ro("tu_dep_paths", &ParseResult::tu_dep_paths)
      .def_ro("tu_dep_ids", &ParseResult::tu_dep_ids);

  // The three parse entry points run for minutes on a large project and touch
  // no Python objects between argument conversion and return, so each releases
  // the GIL for the duration of the call. Without this the calling interpreter
  // thread holds the GIL throughout: Ctrl-C is not serviced until the parse
  // returns, and no other Python thread (progress reporting, Sphinx's parallel
  // machinery) can run. nanobind destroys the guard before converting the
  // result, so the ParseResult is built back under the GIL as usual.
  m.def("parse_to_sqlite", &parse_to_sqlite, nb::arg("inputs"),
        nb::arg("db_path"), nb::arg("options") = PyParseOptions{},
        nb::call_guard<nb::gil_scoped_release>(),
        "Parse C++ inputs and write the IR into a SQLite DB at db_path.");

  m.def("parse_tus_to_sqlite", &parse_tus_to_sqlite, nb::arg("inputs"),
        nb::arg("db_path"), nb::arg("options") = PyParseOptions{},
        nb::arg("dropped_candidates") = std::vector<std::string>{},
        nb::call_guard<nb::gil_scoped_release>(),
        "Re-parse the given inputs into an existing SQLite IR (in parallel, in "
        "one transaction), replacing only those translation units' rows. "
        "dropped_candidates names files the previous parse attributed only to "
        "these inputs; those this parse no longer reaches are removed.");

  m.def("parse_tu_to_sqlite", &parse_tu_to_sqlite, nb::arg("input"),
        nb::arg("db_path"), nb::arg("options") = PyParseOptions{},
        nb::arg("dropped_candidates") = std::vector<std::string>{},
        nb::call_guard<nb::gil_scoped_release>(),
        "Re-parse one input into an existing SQLite IR, replacing only that "
        "translation unit's rows.");
}

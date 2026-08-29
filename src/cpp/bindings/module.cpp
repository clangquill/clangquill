// nanobind entry point for the clangquill C++ core.
//
// Exposes the libclang-backed parser (parse_to_sqlite) and small probes used by
// tests. Reads of the SQLite artifact happen in Python via stdlib sqlite3.

#include <nanobind/nanobind.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/pair.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/tuple.h>
#include <nanobind/stl/vector.h>

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "comment/doxygen_raw.hpp"
#include "comment/fields.hpp"
#include "core/version.hpp"
#include "hash/content_hash.hpp"
#include "model/diagnostic.hpp"
#include "model/enum_names.hpp"
#include "model/parameters.hpp"
#include "model/reference.hpp"
#include "model/symbol.hpp"
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

// One `comment_fields` row as it crosses into Python: (name, arg, value). The
// ordinal is implicit in the list order, and the USR is the caller's, so
// neither is carried.
using CommentFieldRow = std::tuple<std::string, std::string, std::string>;

std::vector<CommentFieldRow> rows_of(
    const std::vector<clangquill::model::CommentField>& fields) {
  std::vector<CommentFieldRow> rows;
  rows.reserve(fields.size());
  for (const auto& f : fields) rows.emplace_back(f.name, f.arg, f.value);
  return rows;
}

std::vector<clangquill::model::CommentField> fields_of(
    const std::vector<CommentFieldRow>& rows) {
  std::vector<clangquill::model::CommentField> fields;
  fields.reserve(rows.size());
  int ordinal = 0;
  for (const auto& [name, arg, value] : rows) {
    clangquill::model::CommentField f;
    f.name = name;
    f.arg = arg;
    f.value = value;
    f.ordinal = ordinal++;
    fields.push_back(std::move(f));
  }
  return fields;
}

// Parses a raw Doxygen comment and hands back the flattened rows the IR would
// have persisted. Python rebuilds its CommentModel from exactly the same rows
// it gets out of SQLite, so `doxygen_parse` and a stored comment travel the
// same decoder.
std::vector<CommentFieldRow> parse_doxygen_comment(const std::string& raw) {
  return rows_of(clangquill::comment::to_comment_fields(
      "", clangquill::comment::doxygen_parse_raw(raw)));
}

// Decodes rows into a CommentModel and re-encodes them. A test hook: feeding it
// any row list must return that list unchanged, which is what pins the Python
// decoder against the C++ one without binding CommentModel itself.
std::vector<CommentFieldRow> comment_fields_roundtrip(
    const std::vector<CommentFieldRow>& rows) {
  return rows_of(clangquill::comment::to_comment_fields(
      "", clangquill::comment::from_comment_fields(fields_of(rows))));
}

nb::dict enum_dict(const clangquill::model::EnumEntry* entries, std::size_t n) {
  nb::dict d;
  for (std::size_t i = 0; i < n; ++i) d[entries[i].name] = entries[i].value;
  return d;
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

void collect_diagnostics(ParseResult& res,
                         const clangquill::model::ParsedModule& mod) {
  res.diagnostic_records = mod.diagnostics;
  for (const auto& d : mod.diagnostics) {
    if (d.depth == 0 && d.severity >= clangquill::model::kSeverityError) {
      res.diagnostics.push_back(d.text);
    }
  }
}

ParseResult result_from_module(const clangquill::model::ParsedModule& mod) {
  ParseResult res;
  res.symbol_count = static_cast<int>(mod.symbols.size());
  res.reference_count = static_cast<int>(mod.references.size());
  res.file_count = static_cast<int>(mod.files.size());
  collect_diagnostics(res, mod);
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
  ParseResult res;

  if (replace_only) {
    // The incremental path needs the whole module in hand before it writes: it
    // decides which existing rows to delete from the complete set of files the
    // re-parse reached. It is a handful of translation units, so holding their
    // IR costs little.
    clangquill::model::ParsedModule mod = clangquill::parser::parse_files(
        inputs, to_core_options(opt), &tu_files, &tu_ok);
    // Bail before writing on any hard parse failure: otherwise that file's
    // existing rows would be deleted and replaced with nothing, wiping good
    // documentation.
    for (std::size_t i = 0; i < inputs.size(); ++i) {
      if (!tu_ok[i]) {
        throw std::runtime_error("failed to parse translation unit: " +
                                 inputs[i]);
      }
    }
    clangquill::store::SqliteStore store(db_path);
    store.write_tus(mod, clangquill::store::Meta::current(), inputs,
                    dropped_candidates);
    res = result_from_module(mod);
  } else {
    // A full parse streams instead: every batch is written as it is parsed, so
    // peak memory is a couple of batches rather than the whole project's IR,
    // and the database I/O overlaps with the parsing of the batches behind it.
    // The batches arrive in canonical order on this thread, so the rows land in
    // the same sequence whatever the job count, and the counts below add up the
    // same totals the merged module used to report.
    //
    // write_streamed_full_parse builds the stream into a temporary database and
    // only replaces db_path once every batch has landed -- this binding may be
    // pointed at a database an earlier parse filled (#203), and a hard parse
    // failure or a killed process partway through the stream must leave that
    // existing IR untouched rather than a mix of old and partial rows (#317).
    const clangquill::store::Meta meta = clangquill::store::Meta::current();
    clangquill::model::ParsedModule diagnostics_only;
    clangquill::store::write_streamed_full_parse(
        db_path, meta, [&](const clangquill::store::BatchSink& write_batch) {
          std::unordered_set<std::string> files_seen;
          auto sink = [&](clangquill::model::ParsedModule&& part) {
            res.symbol_count += static_cast<int>(part.symbols.size());
            res.reference_count += static_cast<int>(part.references.size());
            // Counted here rather than from the rows: batches re-parse the
            // shared `#include` closure, and the file count has always been
            // the distinct paths across the whole parse.
            for (const auto& file : part.files) files_seen.insert(file.path);
            write_batch(std::move(part));
          };
          diagnostics_only = clangquill::parser::parse_files(
              inputs, to_core_options(opt), &tu_files, &tu_ok, sink);
          res.file_count = static_cast<int>(files_seen.size());
        });
    collect_diagnostics(res, diagnostics_only);
  }

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

  // Everything from here to the parse entry points is available in the stub
  // backend too: it comes from the libclang-free half of the core. That is the
  // point -- the Python side derives its enums, its schema and its comment
  // routing from these rather than transcribing them, so the definitions cannot
  // drift, and the drift tests that remain are not vacuous against a wheel.
  m.attr("SCHEMA_DDL") = clangquill::store::kSchemaDDL;
  m.attr("SYMBOL_KINDS") =
      enum_dict(clangquill::model::kSymbolKinds,
                std::size(clangquill::model::kSymbolKinds));
  m.attr("ACCESS_KINDS") =
      enum_dict(clangquill::model::kAccessKinds,
                std::size(clangquill::model::kAccessKinds));
  m.attr("STORAGE_KINDS") =
      enum_dict(clangquill::model::kStorageKinds,
                std::size(clangquill::model::kStorageKinds));
  m.attr("REF_KINDS") = enum_dict(clangquill::model::kRefKinds,
                                  std::size(clangquill::model::kRefKinds));
  m.attr("TEMPLATE_PARAM_KINDS") =
      enum_dict(clangquill::model::TemplateParameter::kKinds,
                std::size(clangquill::model::TemplateParameter::kKinds));
  m.attr("CONTENT_HASH_FIELDS") =
      nb::tuple(nb::cast(clangquill::hash::content_hash_symbol_fields()));

  {
    // row name -> (CommentModel attribute, shape). `clangquill.comments` builds
    // its field routing from this instead of repeating the encoder's table.
    nb::dict fields;
    for (const auto& info : clangquill::comment::comment_field_table()) {
      fields[info.row_name] = nb::make_tuple(info.member, info.shape);
    }
    m.attr("COMMENT_FIELDS") = fields;
  }

  m.def("parse_doxygen_comment", &parse_doxygen_comment, nb::arg("raw"),
        "Parse a raw Doxygen comment into (name, arg, value) comment_fields "
        "rows, in ordinal order.");
  m.def("split_param_arg", &clangquill::comment::split_param_arg,
        nb::arg("arg"),
        "Split a comment_fields arg into (parameter name, direction).");
  m.def("comment_fields_roundtrip", &comment_fields_roundtrip, nb::arg("rows"),
        "Decode rows into a CommentModel and re-encode them; returns the rows "
        "unchanged. A test hook for pinning the Python decoder.");

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

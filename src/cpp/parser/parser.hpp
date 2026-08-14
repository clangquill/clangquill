#pragma once

#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "model/module.hpp"

/**
 * @file
 * @brief Translation-unit driver that turns C++ sources into the IR model.
 */

namespace clangquill::parser {

/// @brief Options controlling how a translation unit is parsed.
///
/// Mirrors the binding layer's `ParseOptions`.
struct ParseOptions {
  std::string std_flag = "c++20";  ///< C++ standard, passed as `-std=<flag>`.
  std::vector<std::string> include_dirs;  ///< `-I` include directories.
  std::vector<std::string> defines;       ///< `-D` preprocessor definitions.
  std::vector<std::string> extra_args;    ///< Extra compiler arguments appended verbatim.
  std::optional<std::string> compile_commands_dir;  ///< Directory holding a compile_commands.json.
  bool keep_going = true;  ///< Continue past recoverable parse errors.
  /// Capture every libclang diagnostic — warnings, remarks and each
  /// diagnostic's attached `note:` chain — instead of errors only. Off by
  /// default: umbrella batching re-parses the shared `#include` closure once
  /// per batch, so a warning in a common header is re-reported per batch and
  /// the extra volume is only worth paying for when someone asked to see it.
  bool capture_all_diagnostics = false;
  int jobs = 0;  ///< Parse threads; `<= 0` means auto (hardware concurrency).
  /// Inputs grouped into one umbrella translation unit. Grouping amortises the
  /// dominant parse cost — re-lexing the shared `#include` closure — across the
  /// batch. `0` selects a default batch size; `1` parses every input as its own
  /// translation unit. Forced to `1` when `compile_commands_dir` is set, since
  /// per-file compile flags cannot be merged into one unit.
  int tu_batch = 0;
};

/// @brief Drives libclang over one translation unit at a time.
///
/// Appends extracted IR into a ParsedModule and owns a reusable CXIndex.
class Parser {
 public:
  /// @brief Constructs a parser with the given options.
  /// @param options Parse configuration applied to every file.
  explicit Parser(ParseOptions options);
  ~Parser();
  Parser(const Parser&) = delete;
  Parser& operator=(const Parser&) = delete;

  /// @brief Parses one input file, appending its IR into @p out.
  /// @param path Path of the translation unit to parse.
  /// @param out Module that extracted rows are appended to.
  /// @param tu_files Optional sink for this translation unit's full file set
  ///        (the main file plus every transitively `#include`d file). Unlike
  ///        @p out.files — which is deduplicated across every TU parsed into the
  ///        module — this captures exactly what *this* TU pulled in, so a caller
  ///        can attribute each dependency to the input that requires it.
  /// @return `false` on hard failure (the translation unit could not be
  ///         created). The failure is appended to @p out as an error carrying a
  ///         `note:` chain that explains it — see `report_parse_failure()`
  ///         below (private: Doxygen does not extract it here).
  bool parse_file(const std::string& path, model::ParsedModule& out,
                  std::vector<std::string>* tu_files = nullptr);

  /// @brief Parses a batch of inputs as one umbrella translation unit.
  ///
  /// A synthetic in-memory main file `#include`s every member, so the shared
  /// transitive include closure is lexed and parsed once for the whole batch
  /// instead of once per input. Only declarations physically located in the
  /// member files are extracted, so the result matches per-file parsing for
  /// self-contained headers. A batch of one delegates to @ref parse_file. If
  /// the umbrella itself cannot be created, every member is re-parsed
  /// individually as a fallback.
  ///
  /// @param paths The batch members, in input order.
  /// @param out Module that extracted rows are appended to.
  /// @param member_files Optional sink (sized to @p paths) receiving each
  ///        member's file set — the member plus its transitive `#include`s,
  ///        recovered exactly from the preprocessing record even when an
  ///        include was guard-skipped because a sibling pulled it in first.
  /// @param member_ok Optional sink (sized to @p paths) flagging members whose
  ///        translation unit (or umbrella inclusion) hard-failed as `false`.
  /// @return `false` when any member hard-failed.
  bool parse_batch(const std::vector<std::string>& paths,
                   model::ParsedModule& out,
                   std::vector<std::vector<std::string>>* member_files = nullptr,
                   std::vector<bool>* member_ok = nullptr);

 private:
  // Compiler arguments for @p path: the compilation database entry when there
  // is one, else the configured -std/-I/-D fallback. Sets `*from_compile_db`
  // (when given) to which of the two it was, so a failure can name the source
  // of the flags it is blaming.
  std::vector<std::string> build_args(const std::string& path,
                                      bool* from_compile_db = nullptr) const;

  // The -std/-I/-D fallback arguments, independent of any database entry.
  std::vector<std::string> default_args() const;

  // Appends the "failed to parse" record for @p path to @p out, followed by
  // the `note:` chain diagnosing it.
  //
  // libclang hands back no CXTranslationUnit when it refuses to create one,
  // and the driver's own diagnostics die with the half-built AST unit — the C
  // API offers no way to reach them. So the notes reconstruct the diagnosis
  // from what is knowable here (the error code, whether the input is readable,
  // the exact argv, whether it names a second input) and, when the flags came
  // from a compilation database, from a re-parse under @ref default_args that
  // recovers the compiler's real complaints about the file.
  //
  // @param path Input whose translation unit could not be created.
  // @param error_code The `CXErrorCode` returned, as an int (this header stays
  //        clang-free).
  // @param args Arguments that were handed to libclang.
  // @param args_from_compile_db Whether @p args came from the database.
  // @param out Module the records are appended to.
  void report_parse_failure(const std::string& path, int error_code,
                            const std::vector<std::string>& args,
                            bool args_from_compile_db,
                            model::ParsedModule& out);

  ParseOptions options_;
  void* index_ = nullptr;  // CXIndex (opaque here to keep the header clang-free)
  // Lazily-loaded compile_commands.json reader, shared across this parser's
  // translation units (mutable: caching it does not change observable state).
  mutable std::unique_ptr<class CompileDb> compile_db_;
  // Set once the load above has been attempted and failed, so the "database
  // could not be loaded" diagnostic is reported once per parser rather than
  // once per translation unit.
  mutable bool compile_db_failed_ = false;
  mutable bool compile_db_reported_ = false;

  // Appends the one-shot compile-database failure diagnostic to @p out, naming
  // the path that was searched. No-op when the database loaded (or none was
  // configured), and after the first report.
  void report_compile_db_failure(model::ParsedModule& out) const;
};

/// @brief Parses every input file and merges the per-batch IR into one module.
///
/// Inputs are grouped into batches of `options.tu_batch` (see ParseOptions) and
/// each batch is parsed as one umbrella translation unit, so the shared
/// `#include` closure is parsed once per batch rather than once per input.
/// Batches are parsed concurrently across up to `min(batches, effective_jobs)`
/// threads, each owning its own `Parser`/`CXIndex` (libclang indices must not
/// be shared between threads, but one per thread is safe). Batch composition
/// depends only on the input order — never on the job count — and results merge
/// back in batch order, so the output is deterministic regardless of how many
/// threads ran. `options.jobs <= 0` selects the hardware concurrency.
///
/// @param inputs Translation units to parse, in the order they should merge.
/// @param options Parse configuration applied to every file.
/// @param tu_files Optional sink, sized to and indexed by @p inputs, receiving
///        each input's file set (the input plus every transitive `#include`).
///        Lets a caller attribute every dependency to the input that pulled it
///        in for per-TU incremental re-parses.
/// @param tu_parsed Optional sink, sized to and indexed by @p inputs, flagging
///        inputs whose translation unit hard-failed as `false`.
/// @return The merged IR for all inputs.
model::ParsedModule parse_files(const std::vector<std::string>& inputs,
                                const ParseOptions& options,
                                std::vector<std::vector<std::string>>* tu_files = nullptr,
                                std::vector<bool>* tu_parsed = nullptr);

}  // namespace clangquill::parser

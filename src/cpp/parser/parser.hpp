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
  /// Extract the contents of anonymous namespaces. Off by default, matching
  /// Doxygen's `EXTRACT_ANON_NSPACES = NO`: an anonymous namespace has
  /// internal linkage, so what it holds is one translation unit's
  /// implementation detail rather than API anyone can name. When on, its
  /// contents carry `@anonymous` in their qualified name instead of appearing
  /// under the enclosing namespace's.
  bool extract_anonymous_namespaces = false;
  int jobs = 0;  ///< Parse threads; `<= 0` means auto (hardware concurrency).
  /// Inputs grouped into one umbrella translation unit. Grouping amortises the
  /// dominant parse cost — re-lexing the shared `#include` closure — across the
  /// batch. `0` selects a default batch size; `1` parses every input as its own
  /// translation unit. With `compile_commands_dir` set this is an upper bound
  /// rather than the batch size: a batch's members have to share one compiler
  /// command, so inputs are first grouped by the flags the database answers
  /// with (see @ref parse_files) and an input whose flags are unique ends up
  /// alone. Batch *composition* is fixed by the input set, so this value still
  /// changes what a header that is not self-contained sees; `1` is the setting
  /// that removes that effect entirely.
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
  /// A synthetic in-memory main file `#include`s every member once, so the
  /// shared transitive include closure is lexed and parsed once for the whole
  /// batch instead of once per input. Only declarations physically located in
  /// the member files are extracted, so the result matches per-file parsing for
  /// self-contained headers. A header that is *not* self-contained sees the
  /// preprocessor state its predecessors left behind, so it still depends on
  /// batch composition — which is why @ref parse_files fixes that composition
  /// from the input set alone, and why `tu_batch = 1` remains the way to ask
  /// for exact per-file isolation. A batch of one delegates to @ref parse_file.
  /// If the umbrella itself cannot be created, every member is re-parsed
  /// individually as a fallback.
  ///
  /// One translation unit gets one command line, looked up for the first
  /// member, so the members must be ones a compilation database answers for
  /// identically — which is what @ref parse_files groups them by. Every member
  /// that borrowed its flags is still reported individually.
  ///
  /// @param paths The batch members, in the order they should be included.
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
  // of the flags it is blaming. @p main_file, when given, is the file libclang
  // will actually be handed -- the synthetic umbrella for a batch -- and
  // decides the appended `-x` language; it defaults to @p path.
  std::vector<std::string> build_args(const std::string& path,
                                      bool* from_compile_db = nullptr,
                                      const std::string* main_file = nullptr) const;

  // The -std/-I/-D fallback arguments for @p path, independent of any database
  // entry. Only the trailing `-x` depends on the path: a header is parsed as
  // `c++-header` so its own `#pragma once` is not reported as being in a main
  // file.
  std::vector<std::string> default_args(const std::string& path) const;

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
  // Set by build_args when the database answered for a file it does not list,
  // and drained by report_borrowed_flags into the module the file is parsed
  // into. Stashed rather than returned because build_args yields only
  // arguments and has no module to write to -- the same reason for the flags
  // above.
  mutable std::optional<model::Diagnostic> borrowed_note_;

  // Appends the one-shot compile-database failure diagnostic to @p out, naming
  // the path that was searched. No-op when the database loaded (or none was
  // configured), and after the first report.
  void report_compile_db_failure(model::ParsedModule& out) const;

  // Appends the "these flags describe another file" diagnostic for the input
  // build_args was last called for, and clears it. No-op when the database
  // listed that input itself.
  void report_borrowed_flags(model::ParsedModule& out) const;

  // Appends that same diagnostic for every distinct member of an umbrella
  // batch whose flags were borrowed, and discards any note a preceding
  // build_args left behind.
  //
  // A batch is parsed under one member's command, so build_args only ever
  // classifies that one member -- but the batch documents all of them, and a
  // header whose flags describe another file is exactly as much of a guess
  // whether or not it happened to be the member the lookup went through.
  //
  // @param paths The batch members, in the caller's own spelling (which the
  //        diagnostic names).
  // @param abs The same members, absolute (which deduplicates them, since the
  //        umbrella includes a repeated member once).
  // @param out Module the diagnostics are appended to.
  void report_member_borrowed_flags(const std::vector<std::string>& paths,
                                    const std::vector<std::string>& abs,
                                    model::ParsedModule& out) const;
};

/// @brief Parses every input file and merges the per-batch IR into one module.
///
/// Inputs are grouped into batches of `options.tu_batch` (see ParseOptions) and
/// each batch is parsed as one umbrella translation unit, so the shared
/// `#include` closure is parsed once per batch rather than once per input.
/// When `options.compile_commands_dir` is set the batches are additionally cut
/// along compiler commands: inputs are grouped by the (normalised) command the
/// database answers with, and only inputs that agree on it share a unit — so a
/// project whose headers borrow a handful of per-target flag sets, which is
/// what CMake generates, still gets umbrella batching, while an input with
/// genuinely unique flags is parsed on its own.
/// Batches are parsed concurrently across up to `min(batches, effective_jobs)`
/// threads, each owning its own `Parser`/`CXIndex` (libclang indices must not
/// be shared between threads, but one per thread is safe). Inputs are parsed in
/// a canonical order (absolute, lexically-normalised, lexicographic) rather
/// than the order given, so batch composition — and with it the merged IR and
/// the diagnostics — is a function of the input *set*, never of the sequence a
/// caller happened to pass nor of the job count. Results merge back in that
/// canonical order. `options.jobs <= 0` selects the hardware concurrency.
///
/// @param inputs Translation units to parse. Their order does not affect the
///        result; it only fixes the indexing of @p tu_files and @p tu_parsed.
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

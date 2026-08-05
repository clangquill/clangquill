#pragma once

#include <string>

/**
 * @file
 * @brief One diagnostic reported while parsing a translation unit.
 */

namespace clangquill::model {

/// @brief Severity levels, mirroring `CXDiagnosticSeverity`.
///
/// Kept as plain ints so this header — and everything downstream of it —
/// stays clang-free.
inline constexpr int kSeverityIgnored = 0;
inline constexpr int kSeverityNote = 1;
inline constexpr int kSeverityWarning = 2;
inline constexpr int kSeverityError = 3;
inline constexpr int kSeverityFatal = 4;

/// @brief A single parse diagnostic, flattened out of libclang's tree.
///
/// libclang nests explanatory `note:` diagnostics under the diagnostic they
/// belong to. Those children are stored here as ordinary records that follow
/// their parent, tagged with a @ref depth one greater — flat storage keeps
/// merging and dedup across umbrella batches a simple linear walk, and the
/// parent/child relation is still recoverable from the ordering.
struct Diagnostic {
  int severity = kSeverityError;  ///< One of the `kSeverity*` levels above.
  /// Nesting level: 0 for a top-level diagnostic, `n > 0` for a note attached
  /// to the nearest preceding record of depth `n - 1`.
  int depth = 0;
  /// Formatted message as libclang renders it, already carrying
  /// `file:line:col`, the severity word and the `[-Wflag]` suffix.
  std::string text;
  std::string file;  ///< Presumed file of the location; empty when there is none.
  int line = 0;      ///< Presumed line, or 0 when the diagnostic has no location.
  int column = 0;    ///< Presumed column, or 0 when the diagnostic has no location.
};

}  // namespace clangquill::model

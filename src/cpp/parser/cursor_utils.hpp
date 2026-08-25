#pragma once

#include <clang-c/Index.h>

#include <array>
#include <optional>
#include <set>
#include <string>
#include <unordered_set>
#include <vector>

#include "model/symbol.hpp"

/**
 * @file
 * @brief Small helpers over libclang cursors: strings, names, and signatures.
 */

namespace clangquill::parser {

/// @brief RAII wrapper that guarantees `clang_disposeString`.
class ScopedCXString {
 public:
  /// @brief Takes ownership of @p s.
  /// @param s The libclang string to dispose on destruction.
  explicit ScopedCXString(CXString s) : s_(s) {}
  ~ScopedCXString() { clang_disposeString(s_); }
  ScopedCXString(const ScopedCXString&) = delete;
  ScopedCXString& operator=(const ScopedCXString&) = delete;

  /// @brief Returns the wrapped string as a `std::string`.
  /// @return The C string, or "" when null.
  std::string str() const {
    const char* c = clang_getCString(s_);
    return c ? c : "";
  }

 private:
  CXString s_;
};

/// @brief Converts a `CXString` to `std::string`, disposing the original.
/// @param s The libclang string to convert (consumed).
/// @return Its text, or "" when null.
inline std::string to_string(CXString s) { return ScopedCXString(s).str(); }

/// @brief Returns a cursor's spelling (unqualified name).
/// @param c The cursor to inspect.
/// @return The spelling text.
inline std::string spelling(CXCursor c) {
  return to_string(clang_getCursorSpelling(c));
}
/// @brief Returns a cursor's display name (includes parameters for overloads).
/// @param c The cursor to inspect.
/// @return The display-name text.
inline std::string display_name(CXCursor c) {
  return to_string(clang_getCursorDisplayName(c));
}
/// @brief Returns a cursor's USR (unified symbol resolution string).
/// @param c The cursor to inspect.
/// @return The USR text.
inline std::string usr(CXCursor c) {
  return to_string(clang_getCursorUSR(c));
}

/// @brief Maps a libclang cursor kind to our SymbolKind.
/// @param kind The libclang cursor kind.
/// @return The mapped kind, or `Unknown` if not a documented entity in M2.
model::SymbolKind map_kind(CXCursorKind kind);

/// @brief Returns the USR of the canonical cursor.
///
/// Collapses forward declarations and definitions to a single identity.
/// @param c The cursor to canonicalize.
/// @return The canonical USR.
std::string canonical_usr(CXCursor c);

/// @brief Builds a qualified name by walking semantic parents (e.g. `geo::Circle`).
/// @param c The cursor to name.
/// @return The fully qualified name.
std::string qualified_name(CXCursor c);

/// @brief Pretty-prints a declaration with terse output (no body).
/// @param c The cursor to print.
/// @return The signature text; empty on failure.
std::string pretty_signature(CXCursor c);

/// @brief Reconstructs a macro's declaration text.
///
/// Yields `"NAME"` for object-like macros and `"NAME(a, b)"` for function-like
/// macros (recovered by tokenizing the extent, since libclang exposes no
/// macro-parameter API). Falls back to the spelling.
/// @param c The macro-definition cursor.
/// @return The reconstructed macro signature.
std::string macro_signature(CXCursor c);

/// @brief Builds the leading `template<...>` clause for a template/concept owner.
///
/// Reconstructed from the declaration tokens (libclang exposes no
/// default-argument API).
/// @param owner The template or concept cursor.
/// @param defaults_out When non-null, filled with the per-parameter default
///   text (text after a top-level `=`), one entry per template parameter in
///   declaration order.
/// @return The `template<...>` head, or "" when the owner has no template head.
std::string template_head(CXCursor owner, std::vector<std::string>* defaults_out);

/// @brief Recovers the default-argument text of a function parameter cursor.
///
/// Scans its tokens for a top-level `=`.
/// @param param The parameter cursor.
/// @return The default text, or "" when there is none.
std::string param_default(CXCursor param);

/// @brief Maps a cursor's C++ access specifier to AccessKind.
/// @param c The cursor to inspect.
/// @return The mapped access level.
model::AccessKind map_access(CXCursor c);

/// @brief Maps a cursor's storage class to StorageKind.
/// @param c The cursor to inspect.
/// @return The mapped storage class.
model::StorageKind map_storage(CXCursor c);

/// @brief Tests whether a cursor's location is in the given main file.
/// @param c The cursor to test.
/// @param main_file The main file path to compare against.
/// @return `true` when @p c is declared in @p main_file.
bool in_file(CXCursor c, const std::string& main_file);

/// @brief A file's identity on disk, independent of how it was spelled.
///
/// libclang names a file by the path it was *requested* with, so one file
/// reached two ways — `./Eigen/src/Core/Matrix.h` through an `-include`
/// prologue's own relative includes, and the absolute path an umbrella
/// translation unit includes it by — answers `clang_getFileName` with two
/// different strings. Comparing those strings silently attributes nothing.
using FileIdSet = std::set<std::array<unsigned long long, 3>>;

/// @brief Returns @p path as a normalized absolute path.
///
/// libclang reports a file by the spelling it was reached with, so the same
/// header can arrive as `./Eigen/src/Core/Matrix.h` and as an absolute path
/// within one run. Recording either verbatim puts two rows in the IR for one
/// file and — for the relative one — a path that means something different to
/// whoever reopens the IR from another directory. Absolute paths are returned
/// unchanged, so the common case costs one check.
std::string normalized_path(const std::string& path);

/// @brief Returns @p file's identity, or `std::nullopt` if libclang has none.
std::optional<std::array<unsigned long long, 3>> file_identity(CXFile file);

/// @brief Tests whether a cursor's location is in one of the given files.
/// @param c The cursor to test.
/// @param main_files Accepted file path spellings.
/// @param main_ids Accepted file identities — the reliable test; @p main_files
///        remains as a fallback for a spelling libclang could not resolve.
/// @param trust_main_file Whether the TU's main file is accepted regardless of
///        path spelling (`false` for synthetic umbrella main files).
/// @return `true` when @p c is declared in one of the accepted files.
bool in_file(CXCursor c, const std::unordered_set<std::string>& main_files,
             const FileIdSet& main_ids, bool trust_main_file);

}  // namespace clangquill::parser

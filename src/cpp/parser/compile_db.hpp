#pragma once

#include <optional>
#include <string>
#include <unordered_set>
#include <vector>

/**
 * @file
 * @brief Reader for per-file flags from a compile_commands.json directory.
 */

namespace clangquill::parser {

/// @brief Looks up per-file compile arguments from a compile_commands.json directory.
///
/// Implemented with libclang's CXCompilationDatabase so we don't hand-parse JSON
/// for this purpose.
class CompileDb {
 public:
  CompileDb() = default;
  ~CompileDb();
  CompileDb(const CompileDb&) = delete;
  CompileDb& operator=(const CompileDb&) = delete;

  /// @brief Loads compile_commands.json from @p dir.
  /// @param dir Directory containing a compile_commands.json.
  /// @return `false` if the database is not available.
  bool load(const std::string& dir);

  /// @brief Whether a database is currently loaded.
  /// @return `true` once @ref load has succeeded.
  bool loaded() const { return db_ != nullptr; }

  /// @brief Returns the compile args for @p path.
  ///
  /// Excludes the compiler `argv[0]`, the source file itself, and everything
  /// whose only effect would be to write a file.
  ///
  /// Note that this answers for *any* path, listed or not: libclang wraps the
  /// database in an interpolating one, which serves an unlisted file the
  /// nearest listed file's command with the filename substituted. Use
  /// @ref lists_file to tell the two apart.
  ///
  /// @param path The source file to look up.
  /// @return The argument list, or empty if there is no entry.
  std::vector<std::string> args_for(const std::string& path) const;

  /// @brief Whether the database really has an entry for @p path.
  ///
  /// `false` means @ref args_for answered from a *different* file's command --
  /// a good guess for a header, but a guess, and worth telling the user about.
  /// There is no libclang call for this, so it is decided by looking @p path up
  /// in the database's own file list, which is enumerated (and canonicalized)
  /// once on first use.
  ///
  /// @param path The file to look for.
  /// @return `true` when the database lists @p path itself.
  bool lists_file(const std::string& path) const;

 private:
  void* db_ = nullptr;  // CXCompilationDatabase
  // Canonical paths of every file the database lists, built on first use of
  // lists_file (mutable: caching does not change observable state). Enumerating
  // the database is only worth paying for once something asks.
  mutable bool files_built_ = false;
  mutable std::unordered_set<std::string> files_;
};

}  // namespace clangquill::parser

#include "parser/compile_db.hpp"

#include <clang-c/CXCompilationDatabase.h>

#include <filesystem>
#include <system_error>

#include "parser/cursor_utils.hpp"

namespace clangquill::parser {
namespace {

/// @brief Whether @p arg names the same file as @p path.
///
/// A compile_commands.json is free to spell its source file differently from
/// the path we look it up with -- relative to the entry's `directory`, or with
/// unresolved `..` segments -- so a plain string comparison is not enough.
/// Missing this leaves the source path in the argument list, and libclang then
/// sees two input files and fails to create the translation unit at all.
bool names_same_file(const std::filesystem::path& dir, const std::string& arg,
                     const std::string& path) {
  if (arg == path) return true;
  // Flags never name the source file; skipping them also keeps `-I../include`
  // and friends out of the (comparatively costly) filesystem resolution.
  if (arg.empty() || arg.front() == '-') return false;
  std::filesystem::path candidate(arg);
  if (candidate.is_relative() && !dir.empty()) candidate = dir / candidate;
  std::error_code arg_ec;
  std::error_code path_ec;
  const std::filesystem::path resolved_arg =
      std::filesystem::weakly_canonical(candidate, arg_ec);
  const std::filesystem::path resolved_path =
      std::filesystem::weakly_canonical(std::filesystem::path(path), path_ec);
  if (arg_ec || path_ec) return false;
  return resolved_arg == resolved_path;
}

}  // namespace

CompileDb::~CompileDb() {
  if (db_) {
    clang_CompilationDatabase_dispose(
        static_cast<CXCompilationDatabase>(db_));
  }
}

bool CompileDb::load(const std::string& dir) {
  if (db_) {
    clang_CompilationDatabase_dispose(
        static_cast<CXCompilationDatabase>(db_));
    db_ = nullptr;
  }
  CXCompilationDatabase_Error err = CXCompilationDatabase_NoError;
  CXCompilationDatabase db =
      clang_CompilationDatabase_fromDirectory(dir.c_str(), &err);
  if (err != CXCompilationDatabase_NoError) {
    if (db) clang_CompilationDatabase_dispose(db);
    return false;
  }
  db_ = db;
  return true;
}

std::vector<std::string> CompileDb::args_for(const std::string& path) const {
  std::vector<std::string> args;
  if (!db_) return args;

  CXCompileCommands cmds = clang_CompilationDatabase_getCompileCommands(
      static_cast<CXCompilationDatabase>(db_), path.c_str());
  if (!cmds) return args;

  unsigned n = clang_CompileCommands_getSize(cmds);
  if (n > 0) {
    CXCompileCommand cmd = clang_CompileCommands_getCommand(cmds, 0);
    unsigned argc = clang_CompileCommand_getNumArgs(cmd);
    const std::filesystem::path dir(
        to_string(clang_CompileCommand_getDirectory(cmd)));
    // Skip argv[0] (the compiler) and drop any token naming the source file
    // itself, however the database spells it; libclang adds the file back.
    for (unsigned i = 1; i < argc; ++i) {
      std::string a = to_string(clang_CompileCommand_getArg(cmd, i));
      if (names_same_file(dir, a, path)) continue;
      args.push_back(std::move(a));
    }
  }
  clang_CompileCommands_dispose(cmds);
  return args;
}

}  // namespace clangquill::parser

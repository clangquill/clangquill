#include "parser/compile_db.hpp"

#include <clang-c/CXCompilationDatabase.h>

#include <algorithm>
#include <array>
#include <filesystem>
#include <string_view>
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

/// @brief Whether @p arg starts with @p prefix.
bool starts_with(const std::string& arg, std::string_view prefix) {
  return arg.compare(0, prefix.size(), prefix) == 0;
}

/// @brief Whether @p arg only exists to make the compiler write a file.
///
/// A parse is not a build. libclang appends `-fsyntax-only`, so the object
/// file, the make-style dependency list and the serialized diagnostics an entry
/// asks for describe outputs this run has no business producing -- clang
/// reports them as -Wunused-command-line-argument, which
/// `is_unused_argument_diagnostic` already suppresses.
///
/// Replaying them is not merely redundant, it writes into the user's tree. The
/// database libclang hands back interpolates: a file with no entry of its own
/// gets the nearest entry's command with only the *filename* substituted, so
/// `-MF build/foo.d` survives into the command for every documented header at
/// once. Batches parse concurrently (see parse_files), so those all race to
/// write one path -- and a path spelled relatively lands in the *process*
/// working directory, since libclang never chdir's into the entry's
/// `directory`: for a Sphinx build that is the srcdir, next to the sources.
/// Documenting a project must not touch its build, let alone its source tree.
///
/// @param arg The argument to classify.
/// @param takes_value Set when the argument's value is the *next* token, which
///        has to be dropped with it.
/// @return `true` when @p arg must not be replayed.
bool writes_a_file(const std::string& arg, bool* takes_value) {
  *takes_value = false;
  // `-M`/`-MM` write the dependency list to stdout; the rest of the family
  // either redirect it or adjust its shape, and mean nothing without it.
  static constexpr std::array<std::string_view, 6> kStandalone = {
      "-M", "-MM", "-MD", "-MMD", "-MG", "-MP"};
  static constexpr std::array<std::string_view, 6> kWithValue = {
      "-o", "-MF", "-MT", "-MQ", "-MJ", "--serialize-diagnostics"};
  // Joined spellings of the same flags, e.g. `-ofoo.o`, `-MFdeps.d`.
  static constexpr std::array<std::string_view, 5> kJoined = {"-o", "-MF", "-MT",
                                                              "-MQ", "-MJ"};

  if (std::find(kStandalone.begin(), kStandalone.end(), arg) !=
      kStandalone.end()) {
    return true;
  }
  if (std::find(kWithValue.begin(), kWithValue.end(), arg) != kWithValue.end()) {
    *takes_value = true;
    return true;
  }
  if (starts_with(arg, "--serialize-diagnostics=")) return true;
  // The ObjC ARC migrator is the one flag family spelled `-o...` that is not an
  // output path.
  if (starts_with(arg, "-objcmt")) return false;
  return std::any_of(kJoined.begin(), kJoined.end(),
                     [&arg](std::string_view prefix) {
                       return arg.size() > prefix.size() &&
                              starts_with(arg, prefix);
                     });
}

}  // namespace

CompileDb::~CompileDb() {
  if (db_) {
    clang_CompilationDatabase_dispose(
        static_cast<CXCompilationDatabase>(db_));
  }
}

bool CompileDb::load(const std::string& dir) {
  // The cache below describes the database being replaced, so it has to go
  // with it -- otherwise lists_file would answer for the old one and the
  // borrowed-flags warning would be reported (or suppressed) for the wrong
  // files.
  files_.clear();
  files_built_ = false;
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
    // Replay the entry's working directory. A compile_commands.json entry may
    // spell every path in it relative to its own `directory` -- the format
    // explicitly allows it, and `-Iinclude` is a common way to write one --
    // but a build system runs the command from there while libclang does not
    // chdir anywhere. Without this, clang resolves those against whatever
    // directory the docs build happens to run in and the include is simply not
    // found, which for a header means its declarations quietly go missing.
    //
    // Prepended, so an entry carrying its own -working-directory still wins:
    // clang takes the last such option.
    if (!dir.empty()) {
      std::error_code ec;
      const std::filesystem::path resolved =
          dir.is_absolute() ? dir : std::filesystem::absolute(dir, ec);
      args.push_back("-working-directory=" +
                     (ec ? dir : resolved).string());
    }
    // Skip argv[0] (the compiler) and drop any token naming the source file
    // itself, however the database spells it; libclang adds the file back.
    for (unsigned i = 1; i < argc; ++i) {
      std::string a = to_string(clang_CompileCommand_getArg(cmd, i));
      if (names_same_file(dir, a, path)) continue;
      bool takes_value = false;
      if (writes_a_file(a, &takes_value)) {
        if (takes_value) ++i;  // Drop the path along with the flag.
        continue;
      }
      // Drop the `--` separator too. Past it the driver reads every token as a
      // file name, and both libclang and this parser append arguments after
      // whatever the database supplied -- libclang's own `-fsyntax-only` among
      // them. A surviving separator therefore turns those into input files
      // ("error: no such file or directory: '-fsyntax-only'"), the driver
      // produces no compiler job, and clang_parseTranslationUnit2 returns
      // CXError_ASTReadError with no translation unit and no diagnostics.
      //
      // Not a corner case: CMake writes exactly this shape for the header-set
      // verification targets it generates (`-c -x c++-header … -- <header>`),
      // so the entries most likely to belong to a documented header are the
      // ones that hit it. The operand the separator protected is the source
      // file, which is dropped above and re-supplied by libclang, so nothing
      // is left for it to separate.
      if (a == "--") continue;
      args.push_back(std::move(a));
    }
  }
  clang_CompileCommands_dispose(cmds);
  return args;
}

bool CompileDb::lists_file(const std::string& path) const {
  if (!db_) return false;
  if (!files_built_) {
    files_built_ = true;
    CXCompileCommands all = clang_CompilationDatabase_getAllCompileCommands(
        static_cast<CXCompilationDatabase>(db_));
    if (all) {
      const unsigned n = clang_CompileCommands_getSize(all);
      files_.reserve(n);
      for (unsigned i = 0; i < n; ++i) {
        CXCompileCommand cmd = clang_CompileCommands_getCommand(all, i);
        // Canonicalized against the entry's own `directory`, because an entry
        // is free to spell its file relatively while we are asked about a
        // resolved path -- the same mismatch names_same_file exists for.
        const std::filesystem::path dir(
            to_string(clang_CompileCommand_getDirectory(cmd)));
        std::filesystem::path file(
            to_string(clang_CompileCommand_getFilename(cmd)));
        if (file.is_relative() && !dir.empty()) file = dir / file;
        std::error_code ec;
        const std::filesystem::path resolved =
            std::filesystem::weakly_canonical(file, ec);
        files_.insert(ec ? file.lexically_normal().string() : resolved.string());
      }
      clang_CompileCommands_dispose(all);
    }
  }
  if (files_.count(path) != 0) return true;
  std::error_code ec;
  const std::filesystem::path resolved =
      std::filesystem::weakly_canonical(std::filesystem::path(path), ec);
  if (ec) return false;
  return files_.count(resolved.string()) != 0;
}

}  // namespace clangquill::parser

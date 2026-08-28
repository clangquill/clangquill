#include <catch2/catch_test_macros.hpp>

#if defined(CLANGQUILL_HAVE_LIBCLANG) && defined(_WIN32)

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <random>
#include <string>
#include <vector>

#include "parser/compile_db.hpp"

using namespace clangquill;

namespace {

// See test_parser.cpp's helper of the same name: a directory unique to this
// call, so a crashed prior run or a concurrent `ctest -j` cannot collide.
std::filesystem::path unique_temp_dir(const std::string& label) {
  namespace fs = std::filesystem;
  std::random_device entropy;
  return fs::temp_directory_path() / (label + "-" + std::to_string(entropy()));
}

std::string uppercased(std::string s) {
  std::transform(s.begin(), s.end(), s.begin(),
                 [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
  return s;
}

}  // namespace

// Windows-only: `C:\`-style drive letters and `\` separators are not paths at
// all to `std::filesystem::path` on POSIX (a leading `C:\` parses as one
// relative filename component with no root), and every POSIX filesystem this
// project ships on is case-sensitive, so there is no case-insensitivity for
// these sites to get wrong there. This is exactly the "Windows semantics" gap
// issue 313 asks for: NTFS is case-insensitive but case-preserving, so a
// compile_commands.json entry spelling a file differently than the path this
// project looks it up with must still be recognized as the same file.
TEST_CASE("CompileDb recognizes a source file spelled with different case",
         "[compile_db][windows]") {
  const std::filesystem::path dir = unique_temp_dir("clangquill-compiledb");
  std::filesystem::create_directories(dir);
  const std::filesystem::path source = dir / "main.cpp";
  {
    std::ofstream out(source);
    out << "int main() { return 0; }\n";
  }

  // `directory` and `file` are spelled exactly as they exist on disk, so
  // libclang's own database lookup for `source` finds this entry outright --
  // what is under test is `CompileDb`'s own handling of the *argument list*,
  // not libclang's (separately fuzzy) unlisted-file interpolation. The source
  // file argument itself is spelled in a different case and with a mixed `/`
  // separator, the way a build system on a case-insensitive volume might
  // reasonably emit it. Backslashes in `dir` (native on Windows) need
  // escaping to make valid JSON.
  {
    std::string dir_json;
    for (char c : dir.string()) {
      if (c == '\\' || c == '"') dir_json += '\\';
      dir_json += c;
    }
    std::ofstream json(dir / "compile_commands.json", std::ios::trunc);
    json << "[\n"
        << "  {\n"
        << "    \"directory\": \"" << dir_json << "\",\n"
        << "    \"file\": \"main.cpp\",\n"
        << "    \"arguments\": [\"clang++\", \"-c\", \"-std=c++17\", \"./MAIN.CPP\"]\n"
        << "  }\n"
        << "]\n";
  }

  parser::CompileDb db;
  REQUIRE(db.load(dir.string()));

  const std::string canonical_path = source.string();
  const std::vector<std::string> args = db.args_for(canonical_path);
  // The differently-cased, differently-separated source argument must be
  // recognized as `source` and dropped -- surviving here is exactly the bug:
  // libclang would see two input files (the re-supplied main file plus this
  // stale token) and fail to create the translation unit.
  for (const auto& a : args) {
    INFO("argument: " << a);
    REQUIRE(a.find("MAIN.CPP") == std::string::npos);
  }

  // `lists_file` must also recognize the entry regardless of how the *query*
  // path is cased.
  REQUIRE(db.lists_file(canonical_path));
  REQUIRE(db.lists_file(uppercased(canonical_path)));

  std::filesystem::remove_all(dir);
}

#endif  // defined(CLANGQUILL_HAVE_LIBCLANG) && defined(_WIN32)

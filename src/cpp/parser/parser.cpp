#include "parser/parser.hpp"

#include <clang-c/Index.h>

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <exception>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <mutex>
#include <sstream>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "hash/sha256.hpp"
#include "parser/ast_visitor.hpp"
#include "parser/compile_db.hpp"
#include "parser/cursor_utils.hpp"

namespace clangquill::parser {
namespace {

CXIndex as_index(void* p) { return static_cast<CXIndex>(p); }

// Inputs grouped per umbrella translation unit when ParseOptions::tu_batch is
// 0 (auto). Fixed — independent of the job count — so the batch composition,
// and with it the extracted IR, is identical no matter how many threads run.
// 64 measured 2-3x faster cold than 16 on abseil/eigen and beat a shared
// common-header PCH at every parallelism level (#90); going wider kept paying
// on abseil but regressed eigen and starves small projects of parallel
// batches.
constexpr std::size_t kDefaultTuBatch = 64;

// RAII guard so the translation unit is disposed on every exit path,
// including exceptions thrown while collecting diagnostics or visiting.
struct TuGuard {
  CXTranslationUnit tu;
  ~TuGuard() {
    if (tu) clang_disposeTranslationUnit(tu);
  }
};

// One remembered digest of the process-wide content-hash cache below, valid
// while the file's (mtime, size) stat is unchanged.
struct CachedFileHash {
  std::filesystem::file_time_type mtime;
  std::uintmax_t size = 0;
  std::string sha256;
};

// Reads a file and appends a SourceFile row (path, sha256, size). `seen` keys
// the rows already present in the module so a file shared by many inclusions
// is read and hashed once per module.
//
// `seen` cannot deduplicate across the umbrella batches (each worker builds
// its own module), so a header shared by every batch — the common prelude of
// the whole project — was read and SHA-256-hashed once per batch: on a cold
// abseil parse Sha256 alone was ~10 % of all instructions, more than any
// single libclang function. A process-wide `path -> (mtime, size, digest)`
// cache makes that one read+hash per process instead. The (mtime, size)
// validation mirrors the fast-path the Python-side build cache already trusts
// for exactly these files, so an edited file (changed stat) is re-hashed while
// a long-lived process (the Sphinx extension) reuses digests across builds.
void record_file(const std::string& path, model::ParsedModule& out,
                 std::unordered_set<std::string>& seen) {
  if (!seen.insert(path).second) return;

  static std::mutex cache_mutex;
  static std::unordered_map<std::string, CachedFileHash> cache;

  std::error_code ec;
  const std::filesystem::file_time_type mtime =
      std::filesystem::last_write_time(path, ec);
  std::uintmax_t stat_size = 0;
  if (!ec) stat_size = std::filesystem::file_size(path, ec);
  const bool stat_ok = !ec;

  if (stat_ok) {
    std::lock_guard<std::mutex> lock(cache_mutex);
    auto it = cache.find(path);
    if (it != cache.end() && it->second.mtime == mtime &&
        it->second.size == stat_size) {
      model::SourceFile file;
      file.path = path;
      file.sha256 = it->second.sha256;
      file.size_bytes = static_cast<std::int64_t>(it->second.size);
      out.files.push_back(std::move(file));
      return;
    }
  }

  std::ifstream in(path, std::ios::binary);
  if (!in) return;
  std::ostringstream ss;
  ss << in.rdbuf();
  std::string contents = ss.str();

  model::SourceFile file;
  file.path = path;
  file.sha256 = hash::sha256_hex(contents);
  file.size_bytes = static_cast<std::int64_t>(contents.size());
  // Only remember the digest when the bytes read agree with the stat taken
  // before the read; a file racing a concurrent edit is simply not cached and
  // will be read again next time.
  if (stat_ok && static_cast<std::uintmax_t>(contents.size()) == stat_size) {
    std::lock_guard<std::mutex> lock(cache_mutex);
    cache[path] = CachedFileHash{mtime, stat_size, file.sha256};
  }
  out.files.push_back(std::move(file));
}

// Seeds the dedup set from rows already in the module, so repeated parse calls
// against the same module never duplicate file rows.
std::unordered_set<std::string> seen_from(const model::ParsedModule& out) {
  std::unordered_set<std::string> seen;
  seen.reserve(out.files.size());
  for (const auto& f : out.files) seen.insert(f.path);
  return seen;
}

// Threaded through the inclusion visitor: the module the file rows land in, the
// dedup set for those rows, an optional per-TU sink so a dependency can be
// attributed to the input TU that pulled it in (single-file TUs only), and the
// synthetic umbrella path to skip in batch mode.
struct InclusionCtx {
  model::ParsedModule* out;
  std::unordered_set<std::string>* seen;
  std::vector<std::string>* tu_files;  // may be null
  const std::string* skip_path;        // may be null
};

// Appends `path` to a per-TU file list, skipping duplicates so a header included
// many times within one TU is listed once.
void note_tu_file(std::vector<std::string>* tu_files, const std::string& path) {
  if (tu_files == nullptr) return;
  for (const auto& f : *tu_files) {
    if (f == path) return;
  }
  tu_files->push_back(path);
}

// libclang inclusion visitor: records every file pulled into the translation
// unit (the main file plus everything it transitively `#include`s) so the M6
// cache can invalidate a re-parse when any tracked dependency changes.
void record_inclusion(CXFile included_file, CXSourceLocation* /*stack*/,
                      unsigned /*len*/, CXClientData data) {
  auto& ctx = *static_cast<InclusionCtx*>(data);
  CXString name = clang_getFileName(included_file);
  const char* cstr = clang_getCString(name);
  if (cstr != nullptr && cstr[0] != '\0' &&
      (ctx.skip_path == nullptr || *ctx.skip_path != cstr)) {
    record_file(cstr, *ctx.out, *ctx.seen);
    note_tu_file(ctx.tu_files, cstr);
  }
  clang_disposeString(name);
}

// Returns `path` as a normalized absolute path (best effort: the original
// spelling when the filesystem refuses to resolve it).
std::string absolute_path(const std::string& path) {
  std::error_code ec;
  std::filesystem::path abs = std::filesystem::absolute(path, ec);
  if (ec) return path;
  return abs.lexically_normal().string();
}

// The `includer -> included` file edges of a translation unit, recovered from
// the detailed preprocessing record. Unlike clang_getInclusions — which reports
// each file only when it is actually *entered* — the record keeps an
// InclusionDirective for every `#include` in the sources, including ones
// guard-skipped because a sibling already pulled the file in. That makes the
// per-member dependency closure of an umbrella TU exact.
std::unordered_map<std::string, std::vector<std::string>> include_edges(
    CXTranslationUnit tu) {
  std::unordered_map<std::string, std::vector<std::string>> edges;
  clang_visitChildren(
      clang_getTranslationUnitCursor(tu),
      [](CXCursor c, CXCursor, CXClientData data) {
        if (clang_getCursorKind(c) != CXCursor_InclusionDirective) {
          return CXChildVisit_Continue;
        }
        CXFile included = clang_getIncludedFile(c);
        if (included == nullptr) return CXChildVisit_Continue;  // unresolved
        CXFile from = nullptr;
        unsigned line = 0, col = 0, off = 0;
        clang_getFileLocation(clang_getCursorLocation(c), &from, &line, &col,
                              &off);
        if (from == nullptr) return CXChildVisit_Continue;
        auto& e = *static_cast<
            std::unordered_map<std::string, std::vector<std::string>>*>(data);
        e[to_string(clang_getFileName(from))].push_back(
            to_string(clang_getFileName(included)));
        return CXChildVisit_Continue;
      },
      &edges);
  return edges;
}

// Breadth-first transitive closure of `root` over `edges`, root first.
std::vector<std::string> include_closure(
    const std::unordered_map<std::string, std::vector<std::string>>& edges,
    const std::string& root) {
  std::vector<std::string> result{root};
  std::unordered_set<std::string> visited{root};
  for (std::size_t i = 0; i < result.size(); ++i) {
    auto it = edges.find(result[i]);
    if (it == edges.end()) continue;
    for (const auto& next : it->second) {
      if (visited.insert(next).second) result.push_back(next);
    }
  }
  return result;
}

}  // namespace

Parser::Parser(ParseOptions options) : options_(std::move(options)) {
  index_ = clang_createIndex(/*excludeDeclarationsFromPCH=*/0,
                             /*displayDiagnostics=*/0);
}

Parser::~Parser() {
  if (index_) clang_disposeIndex(as_index(index_));
}

std::vector<std::string> Parser::build_args(const std::string& path) const {
  std::vector<std::string> args;

  if (options_.compile_commands_dir) {
    // Loaded once per Parser, not once per file: the database does not change
    // mid-run and reloading it for every translation unit is pure overhead.
    if (compile_db_ == nullptr) {
      compile_db_ = std::make_unique<CompileDb>();
      compile_db_failed_ = !compile_db_->load(*options_.compile_commands_dir);
    }
    if (compile_db_->loaded()) {
      auto from_db = compile_db_->args_for(path);
      if (from_db.empty()) {
        // Headers are rarely their own compile_commands.json entry. Fall back
        // to the same-directory .cpp of the same name, e.g. foo.hpp ->
        // foo.cpp, which usually shares the header's include dirs/defines.
        std::filesystem::path sibling =
            std::filesystem::path(path).replace_extension(".cpp");
        from_db = compile_db_->args_for(sibling.string());
      }
      args.insert(args.end(), from_db.begin(), from_db.end());
    }
  }

  if (args.empty()) {
    args.push_back("-std=" + options_.std_flag);
    for (const auto& inc : options_.include_dirs) args.push_back("-I" + inc);
    for (const auto& def : options_.defines) args.push_back("-D" + def);
    for (const auto& extra : options_.extra_args) args.push_back(extra);
  }

  // Parse headers as C++ even without a .cpp extension.
  args.push_back("-xc++");
  return args;
}

void Parser::report_compile_db_failure(model::ParsedModule& out) const {
  if (!compile_db_failed_ || compile_db_reported_) return;
  compile_db_reported_ = true;
  // libclang reports an unloadable database the same way it reports "no entry
  // for this file" — by handing back no flags — so without this the run would
  // quietly fall back to the -std/-I/-D defaults and produce plausible but
  // wrong output. Name the directory that was searched so the misconfiguration
  // is obvious.
  const std::filesystem::path dir(*options_.compile_commands_dir);
  out.diagnostics.push_back(model::Diagnostic{
      .text = "could not load a compilation database from '" + dir.string() +
              "'; looked for '" + (dir / "compile_commands.json").string() +
              "'. Falling back to -std/-I/-D flags."});
}

namespace {

// Deepest note chain followed; libclang nests at most a couple of levels, so
// this only guards against a pathological (or malicious) diagnostic tree.
constexpr int kMaxDiagnosticDepth = 8;

// Appends `d` to `out.diagnostics` at nesting level `depth`, then recurses
// into its attached `note:` diagnostics.
void collect_one(CXDiagnostic d, int depth, model::ParsedModule& out) {
  model::Diagnostic record;
  record.severity = static_cast<int>(clang_getDiagnosticSeverity(d));
  record.depth = depth;
  // The default display options already include file:line:col, the severity
  // word and CXDiagnostic_DisplayOption (the `[-Wunused-variable]` suffix), so
  // the formatted text stands on its own in a log.
  record.text = to_string(
      clang_formatDiagnostic(d, clang_defaultDiagnosticDisplayOptions()));

  // Presumed rather than spelling location, so a `#line` directive maps the
  // diagnostic back to the file the author would recognise.
  CXString filename{};
  unsigned line = 0;
  unsigned column = 0;
  clang_getPresumedLocation(clang_getDiagnosticLocation(d), &filename, &line,
                            &column);
  record.file = to_string(filename);
  record.line = static_cast<int>(line);
  record.column = static_cast<int>(column);
  out.diagnostics.push_back(std::move(record));

  if (depth >= kMaxDiagnosticDepth) return;
  // The child set is owned by `d` — it must NOT be disposed, only the
  // individual diagnostics taken out of it.
  CXDiagnosticSet children = clang_getChildDiagnostics(d);
  unsigned n = clang_getNumDiagnosticsInSet(children);
  for (unsigned i = 0; i < n; ++i) {
    CXDiagnostic child = clang_getDiagnosticInSet(children, i);
    collect_one(child, depth + 1, out);
    clang_disposeDiagnostic(child);
  }
}

// Collects `tu`'s diagnostics into `out.diagnostics`: errors only, or every
// severity plus each diagnostic's attached notes when `all` is set.
void collect_diagnostics(CXTranslationUnit tu, model::ParsedModule& out,
                         bool all) {
  unsigned n = clang_getNumDiagnostics(tu);
  for (unsigned i = 0; i < n; ++i) {
    CXDiagnostic d = clang_getDiagnostic(tu, i);
    if (all) {
      collect_one(d, 0, out);
    } else if (clang_getDiagnosticSeverity(d) >= CXDiagnostic_Error) {
      // Deliberately flat: without `all`, notes are dropped and only the
      // top-level message is kept, exactly as before this option existed.
      out.diagnostics.push_back(model::Diagnostic{
          .severity = static_cast<int>(clang_getDiagnosticSeverity(d)),
          .text = to_string(clang_formatDiagnostic(
              d, clang_defaultDiagnosticDisplayOptions()))});
    }
    clang_disposeDiagnostic(d);
  }
}

}  // namespace

bool Parser::parse_file(const std::string& path, model::ParsedModule& out,
                        std::vector<std::string>* tu_files) {
  std::vector<std::string> args = build_args(path);
  report_compile_db_failure(out);
  std::vector<const char*> argv;
  argv.reserve(args.size());
  for (const auto& a : args) argv.push_back(a.c_str());

  unsigned flags = CXTranslationUnit_SkipFunctionBodies |
                   CXTranslationUnit_DetailedPreprocessingRecord;
  if (options_.keep_going) flags |= CXTranslationUnit_KeepGoing;

  CXTranslationUnit tu = nullptr;
  CXErrorCode rc = clang_parseTranslationUnit2(
      as_index(index_), path.c_str(), argv.data(),
      static_cast<int>(argv.size()), nullptr, 0, flags, &tu);
  if (rc != CXError_Success || tu == nullptr) {
    out.diagnostics.push_back(
        model::Diagnostic{.text = "failed to parse: " + path});
    if (tu) clang_disposeTranslationUnit(tu);
    return false;
  }
  TuGuard guard{tu};

  collect_diagnostics(tu, out, options_.capture_all_diagnostics);

  std::unordered_set<std::string> seen = seen_from(out);
  record_file(path, out, seen);
  note_tu_file(tu_files, path);
  // Track transitive #include dependencies so a header edit invalidates the
  // cached parse for every translation unit that pulled it in.
  InclusionCtx ctx{&out, &seen, tu_files, nullptr};
  clang_getInclusions(tu, record_inclusion, &ctx);
  visit_translation_unit(clang_getTranslationUnitCursor(tu), path, out);

  return true;
}

bool Parser::parse_batch(const std::vector<std::string>& paths,
                         model::ParsedModule& out,
                         std::vector<std::vector<std::string>>* member_files,
                         std::vector<bool>* member_ok) {
  if (paths.empty()) return true;
  if (paths.size() == 1) {
    bool ok = parse_file(paths[0], out,
                         member_files != nullptr ? &(*member_files)[0] : nullptr);
    if (member_ok != nullptr) (*member_ok)[0] = ok;
    return ok;
  }

  // The umbrella includes every member by absolute path so resolution is
  // independent of the (synthetic) main file's location.
  std::vector<std::string> abs;
  abs.reserve(paths.size());
  for (const auto& p : paths) abs.push_back(absolute_path(p));

  std::string umbrella =
      (std::filesystem::path(abs.front()).parent_path() /
       ".clangquill-umbrella.cpp")
          .string();
  std::string contents;
  for (const auto& p : abs) contents += "#include \"" + p + "\"\n";

  std::vector<std::string> args = build_args(abs.front());
  report_compile_db_failure(out);
  std::vector<const char*> argv;
  argv.reserve(args.size());
  for (const auto& a : args) argv.push_back(a.c_str());

  unsigned flags = CXTranslationUnit_SkipFunctionBodies |
                   CXTranslationUnit_DetailedPreprocessingRecord;
  if (options_.keep_going) flags |= CXTranslationUnit_KeepGoing;

  CXUnsavedFile unsaved{umbrella.c_str(), contents.c_str(),
                        static_cast<unsigned long>(contents.size())};
  CXTranslationUnit tu = nullptr;
  CXErrorCode rc = clang_parseTranslationUnit2(
      as_index(index_), umbrella.c_str(), argv.data(),
      static_cast<int>(argv.size()), &unsaved, 1, flags, &tu);
  if (rc != CXError_Success || tu == nullptr) {
    if (tu) clang_disposeTranslationUnit(tu);
    // The umbrella itself could not be created (should be rare): fall back to
    // exact per-file parses so a pathological batch never costs symbols.
    bool all_ok = true;
    for (std::size_t i = 0; i < paths.size(); ++i) {
      bool ok = parse_file(paths[i], out,
                           member_files != nullptr ? &(*member_files)[i] : nullptr);
      if (member_ok != nullptr) (*member_ok)[i] = ok;
      all_ok = all_ok && ok;
    }
    return all_ok;
  }
  TuGuard guard{tu};

  collect_diagnostics(tu, out, options_.capture_all_diagnostics);

  // Record every file the batch pulled in, minus the synthetic umbrella.
  std::unordered_set<std::string> seen = seen_from(out);
  InclusionCtx ctx{&out, &seen, nullptr, &umbrella};
  clang_getInclusions(tu, record_inclusion, &ctx);

  // Extract only declarations physically located in the member files. Both the
  // caller's spelling and the absolute one are accepted, matching however
  // libclang names the entered file.
  std::vector<std::string> mains;
  mains.reserve(paths.size() * 2);
  mains.insert(mains.end(), paths.begin(), paths.end());
  mains.insert(mains.end(), abs.begin(), abs.end());
  visit_translation_unit(clang_getTranslationUnitCursor(tu), mains,
                         /*trust_main_file=*/false, out);

  bool all_ok = true;
  if (member_files != nullptr || member_ok != nullptr) {
    auto edges = include_edges(tu);
    for (std::size_t i = 0; i < paths.size(); ++i) {
      // A member libclang never opened (missing file, broken include) parsed
      // nothing; report it like a per-file hard failure.
      CXFile file = clang_getFile(tu, abs[i].c_str());
      bool entered = file != nullptr;
      if (member_ok != nullptr) (*member_ok)[i] = entered;
      all_ok = all_ok && entered;
      if (member_files != nullptr && entered) {
        (*member_files)[i] =
            include_closure(edges, to_string(clang_getFileName(file)));
      }
    }
  }
  return all_ok;
}

namespace {

// Move-appends every element of `src` onto the end of `dst`.
template <typename T>
void append(std::vector<T>& dst, std::vector<T>& src) {
  dst.insert(dst.end(), std::make_move_iterator(src.begin()),
             std::make_move_iterator(src.end()));
}

// Merges `part`'s diagnostics into `out`, skipping any whose top-level message
// was already merged from an earlier batch.
//
// Every batch re-parses the shared `#include` closure, so one bad header is
// reported once per batch that reaches it — noise that grows with the input
// count. The formatted text already embeds `file:line:col`, so equal
// (severity, text) means genuinely the same diagnostic. Notes travel with their
// parent: a group is taken or skipped whole, because dropping a duplicated
// parent while keeping its `note:` children would leave them orphaned.
//
// One accepted collision: the synthetic umbrella main file is named
// `.clangquill-umbrella.cpp` in the same directory for every batch rooted
// there, so two genuinely distinct diagnostics reported *at the umbrella
// itself* with identical text and line would merge into one. Only `#include`
// resolution failures are attributed to the umbrella, so that is a fair trade
// for dropping the per-batch repetition.
void merge_diagnostics(model::ParsedModule& out, model::ParsedModule& part,
                       std::unordered_set<std::string>& seen) {
  for (std::size_t i = 0; i < part.diagnostics.size();) {
    auto& parent = part.diagnostics[i];
    std::size_t end = i + 1;
    while (end < part.diagnostics.size() && part.diagnostics[end].depth > 0) {
      ++end;
    }
    if (seen.insert(std::to_string(parent.severity) + '\0' + parent.text)
            .second) {
      for (std::size_t j = i; j < end; ++j) {
        out.diagnostics.push_back(std::move(part.diagnostics[j]));
      }
    }
    i = end;
  }
}

// Merges `part` into `out` in place, deduplicating source files by path
// (`files.path` is UNIQUE in the schema) and diagnostics by message. All other
// rows are concatenated: each translation unit only emits symbols/references
// physically located in its own member files, so distinct batches never
// collide, and symbol-keyed tables use INSERT OR REPLACE on write to absorb any
// genuine cross-file duplicates.
void merge_into(model::ParsedModule& out, model::ParsedModule& part,
                std::unordered_set<std::string>& seen_files,
                std::unordered_set<std::string>& seen_diagnostics) {
  for (auto& f : part.files) {
    if (seen_files.insert(f.path).second) out.files.push_back(std::move(f));
  }
  append(out.symbols, part.symbols);
  append(out.parameters, part.parameters);
  append(out.template_parameters, part.template_parameters);
  append(out.enumerators, part.enumerators);
  append(out.references, part.references);
  append(out.comments, part.comments);
  append(out.comment_fields, part.comment_fields);
  append(out.groups, part.groups);
  append(out.group_members, part.group_members);
  merge_diagnostics(out, part, seen_diagnostics);
}

}  // namespace

model::ParsedModule parse_files(const std::vector<std::string>& inputs,
                                const ParseOptions& options,
                                std::vector<std::vector<std::string>>* tu_files,
                                std::vector<bool>* tu_parsed) {
  if (tu_files != nullptr) tu_files->assign(inputs.size(), {});
  if (tu_parsed != nullptr) tu_parsed->assign(inputs.size(), false);

  std::size_t batch_size;
  if (options.compile_commands_dir) {
    batch_size = 1;  // per-file compile flags cannot share one TU
  } else if (options.tu_batch > 0) {
    batch_size = static_cast<std::size_t>(options.tu_batch);
  } else {
    batch_size = kDefaultTuBatch;
  }
  const std::size_t num_batches =
      inputs.empty() ? 0 : (inputs.size() + batch_size - 1) / batch_size;

  // One result slot per batch keeps the merge deterministic (input order)
  // regardless of which thread parses which batch or in what order it finishes.
  std::vector<model::ParsedModule> parts(num_batches);
  // Per-batch success flags, flattened into `tu_parsed` only after the workers
  // join: writing worker results straight into a shared std::vector<bool> would
  // race, since its bit-packed elements can share a word across batches.
  std::vector<std::vector<bool>> ok_parts(num_batches);

  unsigned effective_jobs = options.jobs > 0
                                ? static_cast<unsigned>(options.jobs)
                                : std::thread::hardware_concurrency();
  if (effective_jobs == 0) effective_jobs = 1;
  effective_jobs =
      std::min<unsigned>(effective_jobs, static_cast<unsigned>(num_batches));

  // Each worker owns its own Parser (hence its own CXIndex) and pulls the next
  // unclaimed batch until the queue drains.
  std::atomic<std::size_t> next{0};
  auto worker = [&]() {
    Parser parser(options);
    std::size_t b;
    while ((b = next.fetch_add(1)) < num_batches) {
      const std::size_t begin = b * batch_size;
      const std::size_t end = std::min(begin + batch_size, inputs.size());
      std::vector<std::string> members(inputs.begin() + begin,
                                       inputs.begin() + end);
      // Parse into a local module so a mid-parse exception cannot leave
      // half-built rows in the slot: only a clean parse is published, and an
      // exception escaping a worker thread (which would otherwise call
      // std::terminate) is contained as a diagnostic (parse errors are already
      // reported this way) so the run carries on with the next batch.
      try {
        model::ParsedModule part;
        std::vector<std::vector<std::string>> member_files(members.size());
        std::vector<bool> member_ok(members.size(), false);
        parser.parse_batch(members, part,
                           tu_files != nullptr ? &member_files : nullptr,
                           tu_parsed != nullptr ? &member_ok : nullptr);
        // Each thread writes only its own batch's slots — distinct objects in
        // the shared outer vectors — so this needs no synchronisation. The
        // success flags stay per-batch (ok_parts) until the join, because
        // bit-packed vector<bool> elements are not distinct objects.
        for (std::size_t i = 0; i < members.size(); ++i) {
          if (tu_files != nullptr) (*tu_files)[begin + i] = std::move(member_files[i]);
        }
        ok_parts[b] = std::move(member_ok);
        parts[b] = std::move(part);
      } catch (const std::exception& e) {
        parts[b] = model::ParsedModule{};
        parts[b].diagnostics.push_back(model::Diagnostic{
            .text = "exception parsing batch of " + inputs[begin] + ": " +
                    e.what()});
      } catch (...) {
        parts[b] = model::ParsedModule{};
        parts[b].diagnostics.push_back(model::Diagnostic{
            .text = "unknown exception parsing batch of " + inputs[begin]});
      }
    }
  };

  if (effective_jobs <= 1) {
    worker();  // Avoid spawning a thread for the trivial single-job case.
  } else {
    std::vector<std::thread> threads;
    threads.reserve(effective_jobs);
    // Destroying a joinable std::thread calls std::terminate, so if launching
    // one throws (e.g. the OS refuses a new thread) join the ones already
    // started before letting the exception propagate.
    try {
      for (unsigned t = 0; t < effective_jobs; ++t) threads.emplace_back(worker);
    } catch (...) {
      for (auto& t : threads) {
        if (t.joinable()) t.join();
      }
      throw;
    }
    for (auto& t : threads) t.join();
  }

  if (tu_parsed != nullptr) {
    // A batch that died with an exception leaves its ok_parts slot empty, so
    // its inputs keep their initial `false`.
    for (std::size_t b = 0; b < num_batches; ++b) {
      const std::size_t begin = b * batch_size;
      for (std::size_t i = 0; i < ok_parts[b].size(); ++i) {
        (*tu_parsed)[begin + i] = ok_parts[b][i];
      }
    }
  }

  model::ParsedModule merged;
  std::unordered_set<std::string> seen_files;
  std::unordered_set<std::string> seen_diagnostics;
  for (auto& part : parts) {
    merge_into(merged, part, seen_files, seen_diagnostics);
  }
  return merged;
}

}  // namespace clangquill::parser

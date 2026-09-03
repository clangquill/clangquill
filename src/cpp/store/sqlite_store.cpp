#include "store/sqlite_store.hpp"

#include <filesystem>
#include <random>
#include <stdexcept>
#include <system_error>
#include <unordered_map>
#include <unordered_set>

#include "core/version.hpp"
#include "store/schema.hpp"

#if defined(CLANGQUILL_HAVE_LIBCLANG)
#include <clang-c/Index.h>
#endif

namespace clangquill::store {

namespace {

// The prefix every unique_sibling_path() candidate for `target` starts with,
// so reclaim_stale_temp_siblings() can recognise one left behind by an
// earlier run.
std::string temp_sibling_prefix(const std::filesystem::path& target) {
  return target.filename().string() + ".tmp";
}

// A path next to `target` that does not currently exist, so the temporary
// database @ref write_streamed_full_parse builds can later be renamed onto
// `target` atomically (same directory => same filesystem, which a rename
// across filesystems cannot do).
std::filesystem::path unique_sibling_path(const std::filesystem::path& target) {
  namespace fs = std::filesystem;
  const fs::path dir = target.has_parent_path() ? target.parent_path() : fs::path(".");
  const std::string prefix = temp_sibling_prefix(target);
  std::random_device entropy;
  for (int attempt = 0; attempt < 64; ++attempt) {
    const fs::path candidate = dir / (prefix + std::to_string(entropy()));
    std::error_code ec;
    if (!fs::exists(candidate, ec)) return candidate;
  }
  throw std::runtime_error("could not find an unused temporary path next to " +
                           target.string());
}

// Best-effort removal of `path`'s SQLite sidecar files: `-wal`/`-shm` (WAL
// mode) and `-journal` (rollback mode, in case something other than this
// store ever wrote `path` directly). Safe to call whether or not any exist.
//
// Deleting a `-journal` is only safe when `path`'s *main* file is about to be
// discarded wholesale, as it always is at every call site below -- a live
// journal is what lets SQLite roll an interrupted writer's main file back to
// consistency, which matters only for a main file that is still in use.
void remove_stale_sidecars(const std::filesystem::path& path) {
  for (const char* suffix : {"-wal", "-shm", "-journal"}) {
    std::error_code ec;
    std::filesystem::remove(path.string() + suffix, ec);
  }
}

// Removes leftover temp-staging siblings of `target` matching the
// unique_sibling_path() naming scheme: `<target filename>.tmp` followed by
// nothing but digits. A process killed mid-parse never reaches the
// catch-block cleanup in write_streamed_full_parse (SIGKILL doesn't unwind
// the stack), so without this every interrupted run leaves another randomly
// named file behind that nothing else ever reclaims. Best-effort: a listing
// or removal failure is ignored, since this is opportunistic cleanup that the
// parse about to run never depends on -- and only files matching the exact
// naming scheme are touched, never anything merely sharing the prefix.
void reclaim_stale_temp_siblings(const std::filesystem::path& target) {
  namespace fs = std::filesystem;
  const fs::path dir = target.has_parent_path() ? target.parent_path() : fs::path(".");
  const std::string prefix = temp_sibling_prefix(target);
  std::error_code dir_ec;
  for (const auto& entry : fs::directory_iterator(dir, dir_ec)) {
    const std::string name = entry.path().filename().string();
    if (name.rfind(prefix, 0) != 0) continue;
    const std::string suffix = name.substr(prefix.size());
    if (suffix.empty() || suffix.find_first_not_of("0123456789") != std::string::npos) {
      continue;
    }
    std::error_code rm_ec;
    fs::remove(entry.path(), rm_ec);
    remove_stale_sidecars(entry.path());
  }
}

}  // namespace

Meta Meta::current() {
  Meta m;
  m.schema_version = kSchemaVersion;
  m.core_version = clangquill::core_version();
#if defined(CLANGQUILL_HAVE_LIBCLANG)
  CXString s = clang_getClangVersion();
  const char* c = clang_getCString(s);
  m.libclang_version = c ? c : "";
  clang_disposeString(s);
#endif
  return m;
}

SqliteStore::SqliteStore(const std::string& path) : db_(path) {
  db_.exec(kSchemaDDL);
}

void SqliteStore::write(const model::ParsedModule& module, const Meta& meta) {
  Transaction tx(db_);
  put_meta(meta);
  clear_all();
  // Every upsert is an insert on the table clear_all just emptied; the shared
  // path is what keeps this and write_part from drifting on how a file is
  // written.
  FileIds file_ids = upsert_files(module);
  insert_rows(module, file_ids);
  tx.commit();
}

void SqliteStore::write_part(const model::ParsedModule& module,
                             const Meta& meta) {
  Transaction tx(db_);
  put_meta(meta);
  // Upsert rather than insert: a batch re-parses the shared `#include` closure
  // its siblings also saw, so a header an earlier batch already wrote has to
  // keep the id that batch's symbols reference.
  FileIds file_ids = upsert_files(module);
  insert_rows(module, file_ids);
  tx.commit();
}

void SqliteStore::checkpoint_and_truncate_wal() {
  db_.exec("PRAGMA wal_checkpoint(TRUNCATE);");
}

void SqliteStore::clear_all() {
  // Children before parents, spelled out explicitly rather than relying on
  // `ON DELETE CASCADE`: symbols' cascades cover function_parameters,
  // template_parameters, enumerators, references_, comments and
  // comment_fields, and groups' cascade covers group_members — but files has
  // no cascade from symbols (a file row is never dropped on the ordinary
  // paths), so symbols must go before files regardless.
  db_.exec("DELETE FROM group_members;");
  db_.exec("DELETE FROM groups;");
  db_.exec("DELETE FROM comment_fields;");
  db_.exec("DELETE FROM comments;");
  db_.exec("DELETE FROM references_;");
  db_.exec("DELETE FROM enumerators;");
  db_.exec("DELETE FROM template_parameters;");
  db_.exec("DELETE FROM function_parameters;");
  db_.exec("DELETE FROM symbols;");
  db_.exec("DELETE FROM files;");
}

void SqliteStore::write_tus(
    const model::ParsedModule& module, const Meta& meta,
    const std::vector<std::string>& replaced_files,
    const std::vector<std::string>& dropped_candidates) {
  Transaction tx(db_);
  put_meta(meta);
  // Upsert so files shared with other TUs keep their id (and the symbols other
  // TUs anchored to them survive); a changed file simply refreshes its hash.
  FileIds file_ids = upsert_files(module);
  // Delete only the rows of the re-parsed inputs, never of files that merely
  // appear in the module because a re-parsed unit #includes them — those may be
  // other inputs whose symbols are not part of this partial module at all.
  delete_files_rows(replaced_ids(module, replaced_files, file_ids));
  // Nothing above ever removes a `files` row, so a header that fell out of a
  // re-parsed unit's include closure would otherwise linger — with its symbols
  // — until the next full rebuild. The caller names the files only the replaced
  // units used to reach; the ones this parse no longer reaches go now.
  drop_vanished_files(dropped_candidates, file_ids);
  insert_rows(module, file_ids);
  tx.commit();
}

SqliteStore::FileIds SqliteStore::replaced_ids(
    const model::ParsedModule& module,
    const std::vector<std::string>& replaced_files, const FileIds& known) {
  FileIds doomed;
  Stmt lookup(db_, "SELECT id FROM files WHERE path = ?;");
  auto add = [&](const std::string& path) {
    if (path.empty() || doomed.count(path) != 0) return;
    if (auto it = known.find(path); it != known.end()) {
      doomed.emplace(path, it->second);
      return;
    }
    // A spelling the fresh module does not carry (e.g. libclang named the file
    // differently last time): resolve it from the DB so its stale rows still
    // get replaced rather than lingering next to the new ones.
    lookup.reset();
    lookup.bind(1, path);
    if (lookup.step()) doomed.emplace(path, lookup.column_int64(0));
  };
  for (const auto& path : replaced_files) add(path);
  // The fresh symbols' anchor files are replaced too, covering any difference
  // between the caller's input spelling and libclang's name for the same file.
  for (const auto& sym : module.symbols) add(sym.location.file_path);
  return doomed;
}

void SqliteStore::put_meta(const Meta& meta) {
  Stmt m(db_, "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?);");
  auto put = [&](std::string_view k, std::string_view v) {
    m.reset();
    m.bind(1, k);
    m.bind(2, v);
    m.step();
  };
  put("schema_version", std::to_string(meta.schema_version));
  put("core_version", meta.core_version);
  put("libclang_version", meta.libclang_version);
}

SqliteStore::FileIds SqliteStore::upsert_files(
    const model::ParsedModule& module) {
  FileIds file_ids;
  // RETURNING hands back the row id whether the path was inserted or updated, so
  // a file shared with an earlier TU keeps the id its symbols already reference.
  Stmt f(db_,
         "INSERT INTO files(path, sha256, size_bytes) VALUES(?, ?, ?) "
         "ON CONFLICT(path) DO UPDATE SET sha256 = excluded.sha256, "
         "size_bytes = excluded.size_bytes RETURNING id;");
  for (const auto& file : module.files) {
    f.reset();
    f.bind(1, file.path);
    f.bind(2, file.sha256);
    f.bind(3, file.size_bytes);
    f.step();
    file_ids[file.path] = f.column_int64(0);
  }
  return file_ids;
}

void SqliteStore::delete_files_rows(const FileIds& file_ids) {
  // group_members has no cascade onto its member symbol (member_usr is plain
  // text so cross-TU/unresolved members stay first class), so clear those rows
  // explicitly before the symbol delete cascades the rest away.
  Stmt dgm(db_,
           "DELETE FROM group_members WHERE member_usr IN "
           "(SELECT usr FROM symbols WHERE file_id = ?);");
  Stmt ds(db_, "DELETE FROM symbols WHERE file_id = ?;");
  for (const auto& [path, id] : file_ids) {
    dgm.reset();
    dgm.bind(1, id);
    dgm.step();
    ds.reset();
    ds.bind(1, id);
    ds.step();
  }
}

void SqliteStore::drop_vanished_files(
    const std::vector<std::string>& candidates, const FileIds& fresh) {
  FileIds doomed;
  Stmt lookup(db_, "SELECT id FROM files WHERE path = ?;");
  for (const auto& path : candidates) {
    // Still reached by this parse, already handled, or never in the DB: keep.
    if (path.empty() || fresh.count(path) != 0 || doomed.count(path) != 0) {
      continue;
    }
    lookup.reset();
    lookup.bind(1, path);
    if (lookup.step()) doomed.emplace(path, lookup.column_int64(0));
  }
  if (doomed.empty()) return;
  // `symbols.file_id` has no ON DELETE CASCADE (deliberately: a file row is
  // never dropped on the ordinary paths), so its rows have to go first or the
  // foreign key would reject the delete.
  delete_files_rows(doomed);
  Stmt df(db_, "DELETE FROM files WHERE id = ?;");
  for (const auto& [path, id] : doomed) {
    df.reset();
    df.bind(1, id);
    df.step();
  }
}

void SqliteStore::insert_rows(const model::ParsedModule& module,
                              const FileIds& file_ids) {
  {
    // Upsert, never INSERT OR REPLACE. REPLACE is delete+insert, and under
    // `PRAGMA foreign_keys=ON` deleting a `symbols` row cascades onto
    // `function_parameters`, `template_parameters`, `enumerators`,
    // `references_`, `comments` and `comment_fields` — so a later batch
    // merely re-declaring a USR an earlier batch already wrote would wipe
    // that earlier batch's child rows for it, and they only come back if the
    // later batch's TU happened to produce equivalent ones (issue #316).
    // `write`'s single transaction dodges this by inserting every symbol
    // before any child row, so a duplicate's REPLACE always fires against an
    // empty child set; `write_part` commits one transaction per batch and
    // has no such ordering to lean on.
    //
    // The DO UPDATE also picks a winner instead of plain last-write-wins,
    // mirroring the in-TU dedup in ast_visitor.cpp (`is_def` supersedes a
    // prior forward declaration but a forward declaration never supersedes a
    // definition already on record): `is_definition` and `is_documented` are
    // monotonic (once true, always true — a later batch's narrower view of
    // the same USR should never un-flag either), and every other column
    // keeps the existing definition's data rather than being overwritten by
    // a later non-definition. Two definitions of the same USR (unusual) or
    // two plain declarations both take the latest, same as before.
    Stmt s(db_,
           "INSERT INTO symbols(usr, parent_usr, kind, spelling, "
           "qualified_name, display_name, signature, type_repr, access, "
           "storage, is_definition, is_documented, content_hash, file_id, "
           "line, col) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
           "ON CONFLICT(usr) DO UPDATE SET "
           "parent_usr = CASE WHEN symbols.is_definition = 1 AND "
           "excluded.is_definition = 0 THEN symbols.parent_usr "
           "ELSE excluded.parent_usr END, "
           "kind = CASE WHEN symbols.is_definition = 1 AND "
           "excluded.is_definition = 0 THEN symbols.kind "
           "ELSE excluded.kind END, "
           "spelling = CASE WHEN symbols.is_definition = 1 AND "
           "excluded.is_definition = 0 THEN symbols.spelling "
           "ELSE excluded.spelling END, "
           "qualified_name = CASE WHEN symbols.is_definition = 1 AND "
           "excluded.is_definition = 0 THEN symbols.qualified_name "
           "ELSE excluded.qualified_name END, "
           "display_name = CASE WHEN symbols.is_definition = 1 AND "
           "excluded.is_definition = 0 THEN symbols.display_name "
           "ELSE excluded.display_name END, "
           "signature = CASE WHEN symbols.is_definition = 1 AND "
           "excluded.is_definition = 0 THEN symbols.signature "
           "ELSE excluded.signature END, "
           "type_repr = CASE WHEN symbols.is_definition = 1 AND "
           "excluded.is_definition = 0 THEN symbols.type_repr "
           "ELSE excluded.type_repr END, "
           "access = CASE WHEN symbols.is_definition = 1 AND "
           "excluded.is_definition = 0 THEN symbols.access "
           "ELSE excluded.access END, "
           "storage = CASE WHEN symbols.is_definition = 1 AND "
           "excluded.is_definition = 0 THEN symbols.storage "
           "ELSE excluded.storage END, "
           "is_definition = MAX(symbols.is_definition, "
           "excluded.is_definition), "
           "is_documented = MAX(symbols.is_documented, "
           "excluded.is_documented), "
           "content_hash = CASE WHEN symbols.is_definition = 1 AND "
           "excluded.is_definition = 0 THEN symbols.content_hash "
           "ELSE excluded.content_hash END, "
           "file_id = CASE WHEN symbols.is_definition = 1 AND "
           "excluded.is_definition = 0 THEN symbols.file_id "
           "ELSE excluded.file_id END, "
           "line = CASE WHEN symbols.is_definition = 1 AND "
           "excluded.is_definition = 0 THEN symbols.line "
           "ELSE excluded.line END, "
           "col = CASE WHEN symbols.is_definition = 1 AND "
           "excluded.is_definition = 0 THEN symbols.col "
           "ELSE excluded.col END;");
    for (const auto& sym : module.symbols) {
      s.reset();
      s.bind(1, sym.usr);
      if (sym.parent_usr.empty()) {
        s.bind_null(2);
      } else {
        s.bind(2, sym.parent_usr);
      }
      s.bind(3, static_cast<int>(sym.kind));
      s.bind(4, sym.spelling);
      s.bind(5, sym.qualified_name);
      s.bind(6, sym.display_name);
      s.bind(7, sym.signature);
      s.bind(8, sym.type_repr);
      s.bind(9, static_cast<int>(sym.access));
      s.bind(10, static_cast<int>(sym.storage));
      s.bind(11, sym.is_definition ? 1 : 0);
      s.bind(12, sym.is_documented ? 1 : 0);
      s.bind(13, sym.content_hash);
      auto it = file_ids.find(sym.location.file_path);
      if (it == file_ids.end()) {
        s.bind_null(14);
      } else {
        s.bind(14, it->second);
      }
      s.bind(15, static_cast<int>(sym.location.line));
      s.bind(16, static_cast<int>(sym.location.column));
      s.step();
    }
  }

  {
    Stmt p(db_,
           "INSERT OR REPLACE INTO function_parameters(function_usr, idx, name, "
           "type_repr, default_value) VALUES(?,?,?,?,?);");
    for (const auto& param : module.parameters) {
      p.reset();
      p.bind(1, param.function_usr);
      p.bind(2, param.index);
      p.bind(3, param.name);
      p.bind(4, param.type_repr);
      p.bind(5, param.default_value);
      p.step();
    }
  }

  {
    Stmt t(db_,
           "INSERT OR REPLACE INTO template_parameters(owner_usr, idx, "
           "param_kind, name, "
           "type_repr, default_repr) VALUES(?,?,?,?,?,?);");
    for (const auto& tp : module.template_parameters) {
      t.reset();
      t.bind(1, tp.owner_usr);
      t.bind(2, tp.index);
      t.bind(3, static_cast<int>(tp.kind));
      t.bind(4, tp.name);
      t.bind(5, tp.type_repr);
      t.bind(6, tp.default_repr);
      t.step();
    }
  }

  {
    Stmt e(db_,
           "INSERT OR REPLACE INTO enumerators(usr, enum_usr, name, value, "
           "value_is_signed, idx) VALUES(?,?,?,?,?,?);");
    for (const auto& en : module.enumerators) {
      e.reset();
      e.bind(1, en.usr);
      e.bind(2, en.enum_usr);
      e.bind(3, en.name);
      e.bind(4, static_cast<std::int64_t>(en.value));
      e.bind(5, en.value_is_signed ? 1 : 0);
      e.bind(6, en.index);
      e.step();
    }
  }

  {
    Stmt r(db_,
           "INSERT INTO references_(from_usr, ref_kind, to_usr, to_spelling, "
           "is_resolved, access, ordinal) VALUES(?,?,?,?,?,?,?);");
    for (const auto& ref : module.references) {
      r.reset();
      r.bind(1, ref.from_usr);
      r.bind(2, static_cast<int>(ref.kind));
      if (ref.to_usr.empty()) {
        r.bind_null(3);
      } else {
        r.bind(3, ref.to_usr);
      }
      r.bind(4, ref.to_spelling);
      r.bind(5, ref.is_resolved ? 1 : 0);
      r.bind(6, static_cast<int>(ref.access));
      r.bind(7, ref.ordinal);
      r.step();
    }
  }

  {
    Stmt c(db_,
           "INSERT OR REPLACE INTO comments(symbol_usr, raw_text, format) "
           "VALUES(?,?,?);");
    for (const auto& cm : module.comments) {
      c.reset();
      c.bind(1, cm.symbol_usr);
      c.bind(2, cm.text);
      c.bind(3, cm.format);
      c.step();
    }
  }

  {
    // Replace per symbol, never plain append. A symbol's comment_fields are one
    // set written together and carry no key of their own, so a second write
    // that documents the same USR used to leave both sets in the table -- and
    // every stage downstream reads *all* of them, so the symbol's brief and
    // prose were rendered once per write.
    //
    // Two writes documenting one USR is the norm, not a corner case:
    // clang_Cursor_getRawCommentText answers for a redeclaration with the
    // comment written on another one, so a namespace reopened in 200 headers
    // picks up the comment of whichever header documented it in every single
    // one of them. The per-file delete in write_tus() does not catch that --
    // it deletes by the symbol row's anchor file, and a namespace is anchored
    // to whichever file declared it first, which no later write replaces.
    // dune-gdt rendered the `TUPLE_TYPEDEFS_2_TUPLE` block on its `Dune`
    // namespace page 198 times that way.
    //
    // Deleting by symbol touches only the USRs this write actually carries
    // documentation for, and leaves the last writer owning both the raw
    // comment (INSERT OR REPLACE above) and the fields parsed out of it,
    // rather than pairing one write's raw text with another's fields.
    Stmt dcf(db_, "DELETE FROM comment_fields WHERE symbol_usr = ?;");
    std::unordered_set<std::string> replaced;
    for (const auto& field : module.comment_fields) {
      if (!replaced.insert(field.symbol_usr).second) continue;
      dcf.reset();
      dcf.bind(1, field.symbol_usr);
      dcf.step();
    }

    Stmt cf(db_,
            "INSERT INTO comment_fields(symbol_usr, name, arg, value, ordinal) "
            "VALUES(?,?,?,?,?);");
    for (const auto& field : module.comment_fields) {
      cf.reset();
      cf.bind(1, field.symbol_usr);
      cf.bind(2, field.name);
      cf.bind(3, field.arg);
      cf.bind(4, field.value);
      cf.bind(5, field.ordinal);
      cf.step();
    }
  }

  {
    // Upsert, never INSERT OR REPLACE. REPLACE is delete+insert, and under
    // `PRAGMA foreign_keys=ON` deleting a `groups` row fires `group_members`'
    // ON DELETE CASCADE — so merely re-inserting an existing group would wipe
    // every membership row it owns, including the ones contributed by
    // translation units this write is not touching.
    //
    // The DO UPDATE additionally refuses the *downgrade*. Three kinds of row
    // carry the same group id, and last write wins would let the weakest one
    // land last: the `\defgroup` block that defines the group, an
    // `\addtogroup` block that only adds to it, and the stub `ensure_group`
    // emits (title == id, no brief/detail/parent) for every `\ingroup`
    // reference whose defining block this parse did not read. Which one lands
    // last is decided by batch order on a full parse, and by which files an
    // incremental parse happened to re-read otherwise — so precedence has to
    // be a property of the rows, not of their arrival order.
    //
    // Hence `is_definition`, set only by `\defgroup`, and a per-field merge:
    //
    //   * definition over definition — plain last write wins, so editing a
    //     `\defgroup` block's own title or prose still reaches the database
    //     (including emptying a field);
    //   * definition over non-definition — the definition wins every field it
    //     actually fills; an `\addtogroup` block with prose keeps title == id
    //     and so used to pass the old whole-row guard and clobber the real
    //     title *and* brief (issue #301);
    //   * anything over a field the existing row leaves unset — a stub title
    //     (== id), an empty brief or detail, a null parent — fills it in, so
    //     an `\addtogroup` block still contributes what the definition omits
    //     and a stub still creates the row it references;
    //   * otherwise the stored value stands: among non-definitions the first
    //     one to fill a field keeps it.
    Stmt g(db_,
           "INSERT INTO "
           "groups(id, title, brief, detail, parent_group_id, is_definition) "
           "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
           "title = CASE"
           " WHEN excluded.is_definition = 1 AND groups.is_definition = 1"
           " THEN excluded.title"
           " WHEN excluded.is_definition = 1 AND excluded.title <> excluded.id"
           " THEN excluded.title"
           " WHEN groups.title = groups.id THEN excluded.title"
           " ELSE groups.title END, "
           "brief = CASE"
           " WHEN excluded.is_definition = 1 AND groups.is_definition = 1"
           " THEN excluded.brief"
           " WHEN excluded.is_definition = 1 AND excluded.brief <> ''"
           " THEN excluded.brief"
           " WHEN groups.brief = '' THEN excluded.brief"
           " ELSE groups.brief END, "
           "detail = CASE"
           " WHEN excluded.is_definition = 1 AND groups.is_definition = 1"
           " THEN excluded.detail"
           " WHEN excluded.is_definition = 1 AND excluded.detail <> ''"
           " THEN excluded.detail"
           " WHEN groups.detail = '' THEN excluded.detail"
           " ELSE groups.detail END, "
           "parent_group_id = CASE"
           " WHEN excluded.is_definition = 1 AND groups.is_definition = 1"
           " THEN excluded.parent_group_id"
           " WHEN excluded.is_definition = 1"
           " AND excluded.parent_group_id IS NOT NULL"
           " THEN excluded.parent_group_id"
           " WHEN groups.parent_group_id IS NULL"
           " THEN excluded.parent_group_id"
           " ELSE groups.parent_group_id END, "
           "is_definition = "
           "MAX(groups.is_definition, excluded.is_definition);");
    for (const auto& grp : module.groups) {
      g.reset();
      g.bind(1, grp.id);
      g.bind(2, grp.title);
      g.bind(3, grp.brief);
      g.bind(4, grp.detail);
      if (grp.parent_group_id.empty()) {
        g.bind_null(5);
      } else {
        g.bind(5, grp.parent_group_id);
      }
      g.bind(6, grp.is_definition ? 1 : 0);
      g.step();
    }
  }

  {
    // Replaced per member, for the reason the comment_fields delete above
    // spells out: a membership row has no key either, and it is registered
    // from the member's *own* `\ingroup`, so a symbol documented by two writes
    // contributed its membership twice and the group listed it twice. Keyed on
    // the member rather than the group so a write only clears the memberships
    // of the symbols it carries, leaving the rest of the group alone.
    Stmt dgm(db_, "DELETE FROM group_members WHERE member_usr = ?;");
    std::unordered_set<std::string> replaced;
    for (const auto& member : module.group_members) {
      if (member.member_usr.empty()) continue;
      if (!replaced.insert(member.member_usr).second) continue;
      dgm.reset();
      dgm.bind(1, member.member_usr);
      dgm.step();
    }

    Stmt gm(db_,
            "INSERT INTO group_members(group_id, member_usr, ordinal) "
            "VALUES(?,?,?);");
    for (const auto& member : module.group_members) {
      gm.reset();
      gm.bind(1, member.group_id);
      if (member.member_usr.empty()) {
        gm.bind_null(2);
      } else {
        gm.bind(2, member.member_usr);
      }
      gm.bind(3, member.ordinal);
      gm.step();
    }
  }
}

model::ParsedModule SqliteStore::read() {
  model::ParsedModule m;
  std::unordered_map<std::int64_t, std::string> file_paths;

  {
    Stmt f(db_, "SELECT id, path, sha256, size_bytes FROM files ORDER BY id;");
    while (f.step()) {
      model::SourceFile file;
      file.id = f.column_int64(0);
      file.path = f.column_text(1);
      file.sha256 = f.column_text(2);
      file.size_bytes = f.column_int64(3);
      file_paths[file.id] = file.path;
      m.files.push_back(std::move(file));
    }
  }

  {
    Stmt s(db_,
           "SELECT usr, parent_usr, kind, spelling, qualified_name, "
           "display_name, signature, type_repr, access, storage, "
           "is_definition, is_documented, content_hash, file_id, line, col "
           "FROM symbols ORDER BY usr;");
    while (s.step()) {
      model::Symbol sym;
      sym.usr = s.column_text(0);
      sym.parent_usr = s.column_text(1);
      sym.kind = static_cast<model::SymbolKind>(s.column_int64(2));
      sym.spelling = s.column_text(3);
      sym.qualified_name = s.column_text(4);
      sym.display_name = s.column_text(5);
      sym.signature = s.column_text(6);
      sym.type_repr = s.column_text(7);
      sym.access = static_cast<model::AccessKind>(s.column_int64(8));
      sym.storage = static_cast<model::StorageKind>(s.column_int64(9));
      sym.is_definition = s.column_int64(10) != 0;
      sym.is_documented = s.column_int64(11) != 0;
      sym.content_hash = s.column_text(12);
      auto it = file_paths.find(s.column_int64(13));
      if (it != file_paths.end()) sym.location.file_path = it->second;
      sym.location.line = static_cast<unsigned>(s.column_int64(14));
      sym.location.column = static_cast<unsigned>(s.column_int64(15));
      m.symbols.push_back(std::move(sym));
    }
  }

  {
    Stmt p(db_,
           "SELECT function_usr, idx, name, type_repr, default_value FROM "
           "function_parameters ORDER BY function_usr, idx;");
    while (p.step()) {
      model::FunctionParameter param;
      param.function_usr = p.column_text(0);
      param.index = static_cast<int>(p.column_int64(1));
      param.name = p.column_text(2);
      param.type_repr = p.column_text(3);
      param.default_value = p.column_text(4);
      m.parameters.push_back(std::move(param));
    }
  }

  {
    Stmt t(db_,
           "SELECT owner_usr, idx, param_kind, name, type_repr, default_repr "
           "FROM template_parameters ORDER BY owner_usr, idx;");
    while (t.step()) {
      model::TemplateParameter tp;
      tp.owner_usr = t.column_text(0);
      tp.index = static_cast<int>(t.column_int64(1));
      tp.kind = static_cast<model::TemplateParameter::Kind>(t.column_int64(2));
      tp.name = t.column_text(3);
      tp.type_repr = t.column_text(4);
      tp.default_repr = t.column_text(5);
      m.template_parameters.push_back(std::move(tp));
    }
  }

  {
    Stmt e(db_,
           "SELECT usr, enum_usr, name, value, value_is_signed, idx FROM "
           "enumerators ORDER BY enum_usr, idx;");
    while (e.step()) {
      model::Enumerator en;
      en.usr = e.column_text(0);
      en.enum_usr = e.column_text(1);
      en.name = e.column_text(2);
      en.value = e.column_int64(3);
      en.value_is_signed = e.column_int64(4) != 0;
      en.index = static_cast<int>(e.column_int64(5));
      m.enumerators.push_back(std::move(en));
    }
  }

  {
    Stmt r(db_,
           "SELECT from_usr, ref_kind, to_usr, to_spelling, is_resolved, "
           "access, ordinal FROM references_ ORDER BY from_usr, ref_kind, "
           "ordinal;");
    while (r.step()) {
      model::Reference ref;
      ref.from_usr = r.column_text(0);
      ref.kind = static_cast<model::RefKind>(r.column_int64(1));
      ref.to_usr = r.column_text(2);
      ref.to_spelling = r.column_text(3);
      ref.is_resolved = r.column_int64(4) != 0;
      ref.access = static_cast<model::AccessKind>(r.column_int64(5));
      ref.ordinal = static_cast<int>(r.column_int64(6));
      m.references.push_back(std::move(ref));
    }
  }

  {
    Stmt c(db_,
           "SELECT symbol_usr, raw_text, format FROM comments "
           "ORDER BY symbol_usr;");
    while (c.step()) {
      model::RawComment cm;
      cm.symbol_usr = c.column_text(0);
      cm.text = c.column_text(1);
      cm.format = c.column_text(2);
      m.comments.push_back(std::move(cm));
    }
  }

  {
    Stmt cf(db_,
            "SELECT symbol_usr, name, arg, value, ordinal FROM comment_fields "
            "ORDER BY symbol_usr, ordinal;");
    while (cf.step()) {
      model::CommentField field;
      field.symbol_usr = cf.column_text(0);
      field.name = cf.column_text(1);
      field.arg = cf.column_text(2);
      field.value = cf.column_text(3);
      field.ordinal = static_cast<int>(cf.column_int64(4));
      m.comment_fields.push_back(std::move(field));
    }
  }

  {
    Stmt g(db_,
           "SELECT id, title, brief, detail, parent_group_id, is_definition "
           "FROM groups "
           "ORDER BY id;");
    while (g.step()) {
      model::Group grp;
      grp.id = g.column_text(0);
      grp.title = g.column_text(1);
      grp.brief = g.column_text(2);
      grp.detail = g.column_text(3);
      grp.parent_group_id = g.column_text(4);
      grp.is_definition = g.column_int64(5) != 0;
      m.groups.push_back(std::move(grp));
    }
  }

  {
    Stmt gm(db_,
            "SELECT group_id, member_usr, ordinal FROM group_members "
            "ORDER BY group_id, ordinal;");
    while (gm.step()) {
      model::GroupMember member;
      member.group_id = gm.column_text(0);
      member.member_usr = gm.column_text(1);
      member.ordinal = static_cast<int>(gm.column_int64(2));
      m.group_members.push_back(std::move(member));
    }
  }

  return m;
}

void write_streamed_full_parse(const std::string& path, const Meta& meta,
                               const std::function<void(const BatchSink&)>& produce) {
  namespace fs = std::filesystem;
  const fs::path target(path);
  reclaim_stale_temp_siblings(target);
  const fs::path tmp = unique_sibling_path(target);
  try {
    {
      // Scoped so the temp database's connection is closed before anything
      // below touches the filesystem -- and, if `produce` throws, before the
      // temp file is removed in the catch block.
      SqliteStore staging(tmp.string());
      produce([&](model::ParsedModule&& part) { staging.write_part(part, meta); });
      // Force every committed frame out of the WAL and back into the main
      // file, and truncate the WAL to empty, before the connection closes.
      // The rename below moves only the main file -- nothing renames a
      // `-wal` alongside it -- so without this, any batch still sitting in
      // the WAL rather than the main file would be silently dropped by the
      // swap, however reliably a plain close would otherwise have cleaned
      // the WAL up.
      staging.checkpoint_and_truncate_wal();
    }
    // Ordinarily already gone -- a clean close of the sole connection above
    // gets rid of a fully-checkpointed WAL's sidecars -- but nothing renames
    // sidecars below, so any that somehow survived would otherwise be
    // orphaned under the temp name forever; this is defensive.
    remove_stale_sidecars(tmp);
    // A *stale* sidecar (or rollback journal) can survive next to `target`
    // from an earlier, abnormally terminated write -- exactly the crash this
    // whole mechanism defends against. The rename below only ever touches
    // the main file, so left in place it would sit next to the freshly
    // swapped-in database, and SQLite's WAL/journal recovery on the next
    // open could replay it, resurrecting old, unrelated rows into what is
    // supposed to be a fresh parse. Clear it before the swap so the
    // replacement really is just the fresh file, nothing riding along with
    // it -- safe here specifically because `target`'s main file is about to
    // be replaced wholesale, not read through its old journal.
    remove_stale_sidecars(target);
    std::error_code ec;
    fs::rename(tmp, target, ec);
    if (ec) {
      throw std::runtime_error("failed to move parsed database '" + tmp.string() +
                               "' into place at '" + target.string() +
                               "': " + ec.message());
    }
  } catch (...) {
    std::error_code ec;
    fs::remove(tmp, ec);
    remove_stale_sidecars(tmp);
    throw;
  }
}

}  // namespace clangquill::store

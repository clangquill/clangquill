#include "store/sqlite_store.hpp"

#include <unordered_map>

#include "core/version.hpp"
#include "store/schema.hpp"

#if defined(CLANGQUILL_HAVE_LIBCLANG)
#include <clang-c/Index.h>
#endif

namespace clangquill::store {

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
  FileIds file_ids = insert_files(module);
  insert_rows(module, file_ids);
  tx.commit();
}

void SqliteStore::clear_all() {
  // Children before parents, spelled out explicitly rather than relying on
  // `ON DELETE CASCADE`: symbols' cascades cover function_parameters,
  // template_parameters, enumerators, references_, comments, comment_fields
  // and outputs, and groups' cascade covers group_members — but files has no
  // cascade from symbols (a file row is never dropped on the ordinary
  // paths), so symbols must go before files regardless.
  db_.exec("DELETE FROM group_members;");
  db_.exec("DELETE FROM groups;");
  db_.exec("DELETE FROM outputs;");
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

SqliteStore::FileIds SqliteStore::insert_files(
    const model::ParsedModule& module) {
  FileIds file_ids;
  Stmt f(db_, "INSERT INTO files(path, sha256, size_bytes) VALUES(?, ?, ?);");
  for (const auto& file : module.files) {
    f.reset();
    f.bind(1, file.path);
    f.bind(2, file.sha256);
    f.bind(3, file.size_bytes);
    f.step();
    file_ids[file.path] = sqlite3_last_insert_rowid(db_.get());
  }
  return file_ids;
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
    Stmt s(db_,
           "INSERT OR REPLACE INTO symbols(usr, parent_usr, kind, spelling, "
           "qualified_name, display_name, signature, type_repr, access, "
           "storage, is_definition, is_documented, content_hash, file_id, "
           "line, col) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);");
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
    // The WHERE additionally refuses the *downgrade*: `ensure_group` emits a
    // stub row (title == id, no brief/detail/parent) for every `\ingroup`
    // reference whose `\defgroup` block this parse did not read, and last
    // write wins would let such a stub overwrite the real title and prose —
    // both on an incremental write whose re-parsed units only reference the
    // group, and on a full parse where batch order decides which copy lands
    // last. A stub therefore only ever creates a row, never updates one.
    Stmt g(db_,
           "INSERT INTO groups(id, title, brief, detail, parent_group_id) "
           "VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
           "title = excluded.title, brief = excluded.brief, "
           "detail = excluded.detail, "
           "parent_group_id = excluded.parent_group_id "
           "WHERE excluded.title <> excluded.id OR excluded.brief <> '' "
           "OR excluded.detail <> '' OR excluded.parent_group_id IS NOT NULL;");
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
      g.step();
    }
  }

  {
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
           "SELECT id, title, brief, detail, parent_group_id FROM groups "
           "ORDER BY id;");
    while (g.step()) {
      model::Group grp;
      grp.id = g.column_text(0);
      grp.title = g.column_text(1);
      grp.brief = g.column_text(2);
      grp.detail = g.column_text(3);
      grp.parent_group_id = g.column_text(4);
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

}  // namespace clangquill::store

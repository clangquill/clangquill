#pragma once

/**
 * @file
 * @brief Schema version and DDL for the intermediate SQLite artifact.
 */

namespace clangquill::store {

/// @brief On-disk schema version.
///
/// Bump when the DDL below changes in a backward-incompatible way.
inline constexpr int kSchemaVersion = 2;

/// @brief Full schema for the intermediate SQLite artifact.
///
/// The `references_` table is named with a trailing underscore to avoid the SQL
/// reserved word, and intentionally has no foreign key on `to_usr` so cross-TU
/// (and unresolved) references are first class.
inline constexpr const char* kSchemaDDL = R"SQL(
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  id         INTEGER PRIMARY KEY,
  path       TEXT NOT NULL UNIQUE,
  sha256     TEXT NOT NULL,
  size_bytes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
  usr            TEXT PRIMARY KEY,
  parent_usr     TEXT,
  kind           INTEGER NOT NULL,
  spelling       TEXT NOT NULL,
  qualified_name TEXT NOT NULL,
  display_name   TEXT NOT NULL,
  signature      TEXT NOT NULL DEFAULT '',
  type_repr      TEXT NOT NULL DEFAULT '',
  access         INTEGER NOT NULL DEFAULT 0,
  storage        INTEGER NOT NULL DEFAULT 0,
  is_definition  INTEGER NOT NULL DEFAULT 0,
  is_documented  INTEGER NOT NULL DEFAULT 0,
  content_hash   TEXT NOT NULL DEFAULT '',
  file_id        INTEGER REFERENCES files(id),
  line           INTEGER NOT NULL DEFAULT 0,
  col            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_symbols_parent ON symbols(parent_usr);
CREATE INDEX IF NOT EXISTS idx_symbols_kind   ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_file   ON symbols(file_id);

CREATE TABLE IF NOT EXISTS function_parameters (
  id            INTEGER PRIMARY KEY,
  function_usr  TEXT NOT NULL REFERENCES symbols(usr) ON DELETE CASCADE,
  idx           INTEGER NOT NULL,
  name          TEXT NOT NULL DEFAULT '',
  type_repr     TEXT NOT NULL DEFAULT '',
  default_value TEXT NOT NULL DEFAULT '',
  UNIQUE(function_usr, idx)
);

CREATE TABLE IF NOT EXISTS template_parameters (
  id           INTEGER PRIMARY KEY,
  owner_usr    TEXT NOT NULL REFERENCES symbols(usr) ON DELETE CASCADE,
  idx          INTEGER NOT NULL,
  param_kind   INTEGER NOT NULL,
  name         TEXT NOT NULL DEFAULT '',
  type_repr    TEXT NOT NULL DEFAULT '',
  default_repr TEXT NOT NULL DEFAULT '',
  UNIQUE(owner_usr, idx)
);

CREATE TABLE IF NOT EXISTS enumerators (
  usr             TEXT PRIMARY KEY,
  enum_usr        TEXT NOT NULL REFERENCES symbols(usr) ON DELETE CASCADE,
  name            TEXT NOT NULL,
  value           INTEGER NOT NULL,
  value_is_signed INTEGER NOT NULL DEFAULT 1,
  idx             INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_enumerators_enum ON enumerators(enum_usr);

CREATE TABLE IF NOT EXISTS references_ (
  id          INTEGER PRIMARY KEY,
  from_usr    TEXT NOT NULL REFERENCES symbols(usr) ON DELETE CASCADE,
  ref_kind    INTEGER NOT NULL,
  to_usr      TEXT,
  to_spelling TEXT NOT NULL DEFAULT '',
  is_resolved INTEGER NOT NULL DEFAULT 0,
  access      INTEGER NOT NULL DEFAULT 0,
  ordinal     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_refs_from ON references_(from_usr);
CREATE INDEX IF NOT EXISTS idx_refs_to   ON references_(to_usr);

CREATE TABLE IF NOT EXISTS comments (
  symbol_usr  TEXT PRIMARY KEY REFERENCES symbols(usr) ON DELETE CASCADE,
  raw_text    TEXT NOT NULL,
  format      TEXT NOT NULL DEFAULT 'doxygen-raw',
  fields_json TEXT
);

CREATE TABLE IF NOT EXISTS comment_fields (
  id         INTEGER PRIMARY KEY,
  symbol_usr TEXT NOT NULL REFERENCES symbols(usr) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  arg        TEXT NOT NULL DEFAULT '',
  value      TEXT NOT NULL DEFAULT '',
  ordinal    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_comment_fields_sym ON comment_fields(symbol_usr);
-- Store.related_by_name() selects every `\relates` field in the database by
-- name; without this it full-scans comment_fields (every documented symbol's
-- every field) once per build.
CREATE INDEX IF NOT EXISTS idx_comment_fields_name ON comment_fields(name);

CREATE TABLE IF NOT EXISTS outputs (
  id           INTEGER PRIMARY KEY,
  symbol_usr   TEXT REFERENCES symbols(usr) ON DELETE CASCADE,
  output_path  TEXT NOT NULL,
  content_hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS groups (
  id              TEXT PRIMARY KEY,
  title           TEXT NOT NULL DEFAULT '',
  brief           TEXT NOT NULL DEFAULT '',
  detail          TEXT NOT NULL DEFAULT '',
  parent_group_id TEXT
);

CREATE TABLE IF NOT EXISTS group_members (
  id         INTEGER PRIMARY KEY,
  group_id   TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  member_usr TEXT,
  ordinal    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id);
-- An incremental re-parse deletes the group memberships of the symbols it is
-- about to replace (`member_usr IN (...)`, once per replaced file); without
-- this each of those deletes full-scans group_members.
CREATE INDEX IF NOT EXISTS idx_group_members_member ON group_members(member_usr);
)SQL";

}  // namespace clangquill::store

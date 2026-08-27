#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

#include "model/module.hpp"
#include "store/sqlite_raii.hpp"

/**
 * @file
 * @brief SQLite-backed persistence for the parsed IR.
 */

namespace clangquill::store {

/// @brief Metadata written into the `meta` table.
struct Meta {
  int schema_version = 0;        ///< On-disk schema version.
  std::string core_version;      ///< Version of the native core that wrote the DB.
  std::string libclang_version;  ///< Version of libclang used for the parse.

  /// @brief Builds the Meta describing the current build.
  /// @return Metadata populated from the compiled-in versions.
  static Meta current();
};

/// @brief Persists a ParsedModule into the SQLite artifact and reads it back.
///
/// The production Python layer reads the DB directly via stdlib sqlite3;
/// @ref read exists mainly for round-trip testing.
class SqliteStore {
 public:
  /// @brief Opens (creating if needed) the database at @p path.
  /// @param path Filesystem path of the SQLite database.
  explicit SqliteStore(const std::string& path);

  /// @brief Writes the whole module in a single transaction.
  ///
  /// `files.id` is resolved from each symbol's location path.
  /// @param module The IR to persist.
  /// @param meta Metadata stored alongside the IR.
  void write(const model::ParsedModule& module, const Meta& meta);

  /// @brief Re-writes the re-parsed translation units' rows into an existing DB.
  ///
  /// Replaces only the IR sourced from @p replaced_files (plus any file the
  /// fresh @p module anchors a symbol to): every `symbols` row whose `file_id`
  /// belongs to one of those files (and, via the schema's `ON DELETE CASCADE`
  /// chain, that symbol's parameters, references, comments and group
  /// memberships) is deleted, then @p module is inserted afresh. Rows owned by
  /// other translation units are left untouched — including symbols of inputs
  /// that merely appear in @p module's *file* list because a re-parsed unit
  /// `#include`s them — so touching one input re-parses only that input rather
  /// than the whole module. File rows (path, hash, size) are upserted for the
  /// whole module so changed dependencies refresh their hashes.
  ///
  /// Files named in @p dropped_candidates that @p module does not carry are
  /// removed outright (rows first, then the `files` row): they belonged to a
  /// replaced unit's previous `#include` closure and nothing pulls them in any
  /// more, so without this their `files` and `symbols` rows would survive every
  /// later incremental build and only vanish on a full rebuild.
  ///
  /// @param module The freshly re-parsed IR (its files plus their symbols).
  /// @param meta Metadata refreshed alongside the IR.
  /// @param replaced_files The inputs whose rows must be replaced wholesale,
  ///        even when their re-parse produced no symbols.
  /// @param dropped_candidates Files the previous parse attributed *only* to
  ///        the replaced units. Each one the fresh @p module no longer carries
  ///        is deleted; the caller must not list a file any surviving unit
  ///        still contributes to.
  void write_tus(const model::ParsedModule& module, const Meta& meta,
                 const std::vector<std::string>& replaced_files,
                 const std::vector<std::string>& dropped_candidates = {});

  /// @brief Reconstructs a ParsedModule from the database.
  /// @return The IR read back from storage.
  model::ParsedModule read();

 private:
  /// Map from source-file path to its assigned `files.id`.
  using FileIds = std::unordered_map<std::string, std::int64_t>;

  /// Upserts the `meta` rows describing this build.
  void put_meta(const Meta& meta);
  /// Inserts @p module's files (assumes an empty `files` table) and returns ids.
  FileIds insert_files(const model::ParsedModule& module);
  /// Upserts @p module's files (insert-or-update on path) and returns their ids.
  FileIds upsert_files(const model::ParsedModule& module);
  /// Resolves the ids of the files whose rows a write_tus call must replace.
  FileIds replaced_ids(const model::ParsedModule& module,
                       const std::vector<std::string>& replaced_files,
                       const FileIds& known);
  /// Deletes every symbol (and cascaded child rows) sourced from @p file_ids.
  void delete_files_rows(const FileIds& file_ids);
  /// Drops the `files` rows (and their symbols) of @p candidates that the fresh
  /// module — whose files are @p fresh — no longer carries.
  void drop_vanished_files(const std::vector<std::string>& candidates,
                           const FileIds& fresh);
  /// Inserts all non-file IR rows (symbols, params, refs, comments, groups, …).
  void insert_rows(const model::ParsedModule& module, const FileIds& file_ids);

  Db db_;
};

}  // namespace clangquill::store

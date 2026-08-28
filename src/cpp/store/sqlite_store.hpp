#pragma once

#include <cstdint>
#include <functional>
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
  /// Clears every existing IR row first, so calling this against a
  /// non-empty database (as the exported `parse_to_sqlite` binding may) is
  /// safe: it replaces prior contents rather than throwing a UNIQUE-constraint
  /// error or leaving stale rows from an earlier parse.
  /// `files.id` is resolved from each symbol's location path.
  /// @param module The IR to persist.
  /// @param meta Metadata stored alongside the IR.
  void write(const model::ParsedModule& module, const Meta& meta);

  /// @brief Writes one batch of a streamed full parse, in its own transaction.
  ///
  /// A full parse hands its IR over batch by batch as the parser produces it,
  /// so peak memory is a couple of batches rather than the whole project's IR.
  /// Unlike @ref write these calls accumulate, which is what lets the stream
  /// build one database: files are upserted, so a header a previous batch
  /// already wrote keeps the id that batch's symbols reference, and every
  /// other table is keyed by USR and written with `INSERT OR REPLACE` or a
  /// non-destructive upsert. Nothing is ever deleted here, so this is meant to
  /// be called against a database that starts empty — see
  /// @ref write_streamed_full_parse, which drives exactly that; refreshing
  /// part of an IR in place is @ref write_tus.
  /// @param module The batch's IR.
  /// @param meta Metadata stored alongside the IR.
  void write_part(const model::ParsedModule& module, const Meta& meta);

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
  /// Deletes every row from every IR table, so a full @ref write starts from
  /// an empty database even when @ref path already held a previous parse.
  /// Assumes a transaction is already open.
  void clear_all();
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

/// @brief Receives one batch of a streamed full parse, as @ref write_part would.
using BatchSink = std::function<void(model::ParsedModule&&)>;

/// @brief Streams a full parse into `path`, replacing it only once it succeeds.
///
/// `produce` is called exactly once, and hands it a sink to invoke for every
/// batch of a streamed parse (mirroring `parser::PartSink`). Every batch lands
/// in a fresh temporary database created next to `path`, via @ref write_part —
/// `path` itself is never opened for writing. Only once `produce` returns
/// without throwing is the temporary file renamed over `path`, atomically
/// replacing whatever was there (or creating it fresh).
///
/// This is what gives a streamed full parse — which, unlike @ref write, has no
/// single transaction to roll back — the same all-or-nothing guarantee for the
/// *target path*: an exception from `produce` (a hard parse failure, a
/// constraint violation) or the process being killed mid-parse discards the
/// temporary file and leaves `path` exactly as it was, rather than holding a
/// mix of an old IR's rows and however many batches had landed (#317).
///
/// The database's own WAL-mode `-wal`/`-shm` sidecar files are handled
/// alongside the main one: any left next to `path` by an earlier, abnormally
/// terminated write are cleared before the swap (the rename above only ever
/// touches the main file, so a stale sidecar pair would otherwise sit next to
/// the freshly replaced database and could be replayed onto it by the next
/// reader), and the temporary file's own sidecars — ordinarily gone already,
/// checkpointed away by its clean close — are swept up too rather than left
/// behind under the temp name.
///
/// @param path Filesystem path of the target database.
/// @param meta Metadata stored alongside every batch.
/// @param produce Callback that drives the parse, calling the sink it is given
///        once per batch.
/// @throws Whatever `produce` throws, or std::runtime_error if the temporary
///         file cannot be created or the final rename fails.
void write_streamed_full_parse(
    const std::string& path, const Meta& meta,
    const std::function<void(const BatchSink&)>& produce);

}  // namespace clangquill::store

#pragma once

#include <sqlite3.h>

#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>

/**
 * @file
 * @brief RAII wrappers over the sqlite3 C API (connection, statement, transaction).
 */

namespace clangquill::store {

/// @brief Owning wrapper around `sqlite3*`.
///
/// Enables foreign keys and WAL on open, and closes the handle on destruction.
class Db {
 public:
  /// @brief Opens the database at @p path and applies the standard pragmas.
  /// @param path Filesystem path of the SQLite database.
  /// @throws std::runtime_error if the database cannot be opened or configured.
  explicit Db(const std::string& path) {
    if (sqlite3_open(path.c_str(), &db_) != SQLITE_OK) {
      std::string msg = db_ ? sqlite3_errmsg(db_) : "out of memory";
      sqlite3_close(db_);
      db_ = nullptr;
      throw std::runtime_error("sqlite3_open: " + msg);
    }
    // Without a busy timeout, any statement that meets another connection's
    // lock fails instantly with "database is locked" instead of waiting for
    // it to clear.
    sqlite3_busy_timeout(db_, kBusyTimeoutMs);
    // The destructor will not run if the constructor throws, so close the
    // handle ourselves on any pragma failure to avoid leaking it.
    try {
      exec("PRAGMA foreign_keys=ON;");
      exec("PRAGMA journal_mode=WAL;");
      exec("PRAGMA synchronous=NORMAL;");
    } catch (...) {
      sqlite3_close(db_);
      db_ = nullptr;
      throw;
    }
  }
  ~Db() { sqlite3_close(db_); }
  Db(const Db&) = delete;
  Db& operator=(const Db&) = delete;

  /// @brief Returns the underlying connection handle.
  /// @return The owned `sqlite3*`.
  sqlite3* get() const { return db_; }

  /// @brief Executes one or more SQL statements with no result rows.
  /// @param sql The SQL text to run.
  /// @throws std::runtime_error if execution fails.
  void exec(const char* sql) {
    char* err = nullptr;
    if (sqlite3_exec(db_, sql, nullptr, nullptr, &err) != SQLITE_OK) {
      std::string msg = err ? err : "exec failed";
      sqlite3_free(err);
      throw std::runtime_error("sqlite3_exec: " + msg);
    }
  }

 private:
  /// Milliseconds sqlite3_busy_timeout waits for a lock before giving up.
  static constexpr int kBusyTimeoutMs = 5000;

  sqlite3* db_ = nullptr;
};

/// @brief Owning wrapper around a prepared statement with typed bind/column helpers.
class Stmt {
 public:
  /// @brief Prepares @p sql against @p db.
  /// @param db The connection to prepare against.
  /// @param sql The SQL text of the statement.
  /// @throws std::runtime_error if preparation fails.
  Stmt(Db& db, std::string_view sql) : db_(db.get()) {
    if (sqlite3_prepare_v2(db_, sql.data(), static_cast<int>(sql.size()), &st_,
                           nullptr) != SQLITE_OK) {
      throw std::runtime_error(std::string("sqlite3_prepare_v2: ") +
                               sqlite3_errmsg(db_));
    }
  }
  ~Stmt() { sqlite3_finalize(st_); }
  Stmt(const Stmt&) = delete;
  Stmt& operator=(const Stmt&) = delete;

  /// @brief Binds a text value to parameter @p i (1-based).
  /// @param i 1-based parameter index.
  /// @param s The text to bind.
  void bind(int i, std::string_view s) {
    // SQLITE_TRANSIENT: sqlite copies the bytes, so the source may outlive the
    // call or not — safe regardless of the bound string's lifetime.
    check_bind(sqlite3_bind_text(st_, i, s.data(), static_cast<int>(s.size()),
                                 SQLITE_TRANSIENT));
  }
  /// @brief Binds a 64-bit integer to parameter @p i (1-based).
  /// @param i 1-based parameter index.
  /// @param v The value to bind.
  void bind(int i, std::int64_t v) { check_bind(sqlite3_bind_int64(st_, i, v)); }
  /// @brief Binds an `int` to parameter @p i (1-based).
  /// @param i 1-based parameter index.
  /// @param v The value to bind.
  void bind(int i, int v) { check_bind(sqlite3_bind_int64(st_, i, v)); }
  /// @brief Binds SQL NULL to parameter @p i (1-based).
  /// @param i 1-based parameter index.
  void bind_null(int i) { check_bind(sqlite3_bind_null(st_, i)); }

  /// @brief Advances the statement by one step.
  /// @return `true` if a row is available (`SQLITE_ROW`), `false` when done.
  /// @throws std::runtime_error on a step error.
  bool step() {
    int rc = sqlite3_step(st_);
    if (rc != SQLITE_ROW && rc != SQLITE_DONE) {
      throw std::runtime_error(std::string("sqlite3_step: ") +
                               sqlite3_errmsg(db_));
    }
    return rc == SQLITE_ROW;
  }

  /// @brief Resets the statement and clears its bindings for reuse.
  void reset() {
    sqlite3_reset(st_);
    sqlite3_clear_bindings(st_);
  }

  /// @brief Reads column @p i (0-based) of the current row as text.
  /// @param i 0-based column index.
  /// @return The column text, or "" when null.
  std::string column_text(int i) const {
    const auto* p = sqlite3_column_text(st_, i);
    return p ? reinterpret_cast<const char*>(p) : "";
  }
  /// @brief Reads column @p i (0-based) of the current row as a 64-bit integer.
  /// @param i 0-based column index.
  /// @return The column value.
  std::int64_t column_int64(int i) const { return sqlite3_column_int64(st_, i); }

 private:
  void check_bind(int rc) {
    if (rc != SQLITE_OK) {
      throw std::runtime_error(std::string("sqlite3_bind: ") +
                               sqlite3_errmsg(db_));
    }
  }

  sqlite3* db_;
  sqlite3_stmt* st_ = nullptr;
};

/// @brief RAII transaction: @ref commit on success; rolls back if destroyed uncommitted.
class Transaction {
 public:
  /// @brief Begins a write transaction on @p db.
  ///
  /// Uses `BEGIN IMMEDIATE` rather than a deferred `BEGIN` so the write lock
  /// is acquired up front — and thus waits on the busy timeout — instead of
  /// risking `SQLITE_BUSY` mid-transaction at the first write statement.
  /// @param db The connection to run the transaction on.
  explicit Transaction(Db& db) : db_(db) { db_.exec("BEGIN IMMEDIATE;"); }
  ~Transaction() {
    if (!done_) {
      try {
        db_.exec("ROLLBACK;");
      } catch (...) {  // nothing actionable during stack unwinding
      }
    }
  }
  Transaction(const Transaction&) = delete;
  Transaction& operator=(const Transaction&) = delete;

  /// @brief Commits the transaction.
  /// @throws std::runtime_error if the commit fails.
  void commit() {
    db_.exec("COMMIT;");
    done_ = true;
  }

 private:
  Db& db_;
  bool done_ = false;
};

}  // namespace clangquill::store

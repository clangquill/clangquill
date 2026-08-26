#pragma once

#include <clang-c/Index.h>

#include <string>
#include <vector>

#include "model/comment.hpp"
#include "model/comment_model.hpp"

/**
 * @file
 * @brief Pluggable comment-parser interface and IR serialization helpers.
 */

namespace clangquill::parser {

/// @brief Pluggable comment-parsing strategy.
///
/// The default implementation is DoxygenCommentParser; this interface keeps the
/// comment format swappable without touching the AST visitor or the store.
/// Implementations receive both the libclang cursor (for the parsed CXComment
/// tree) and the verbatim raw text (for command scanning that libclang does not
/// surface).
class ICommentParser {
 public:
  virtual ~ICommentParser() = default;

  /// @brief Stable identifier persisted in `comments.format` (e.g. `"doxygen"`).
  /// @return The format identifier.
  virtual std::string format() const = 0;

  /// @brief Parses a symbol's documentation comment into a structured model.
  /// @param cursor The documented cursor (source of the parsed CXComment tree).
  ///        May be a null cursor: a free-floating block opened with a structural
  ///        command (`\class Name`) documents an entity it is not attached to,
  ///        so there is no cursor to hand over. Implementations must fall back
  ///        to @p raw rather than dereference it.
  /// @param raw The verbatim comment text, markers included.
  /// @return The structured comment model.
  virtual model::CommentModel parse(CXCursor cursor,
                                    const std::string& raw) const = 0;
};

/// @brief Serializes a CommentModel into the JSON stored in `comments.fields_json`.
/// @param model The structured comment to serialize.
/// @return The JSON representation.
std::string to_fields_json(const model::CommentModel& model);

/// @brief Flattens a CommentModel into normalized `comment_fields` rows.
///
/// Order is preserved via a running ordinal so reads round-trip the model.
/// @param usr USR of the documented symbol.
/// @param model The structured comment to flatten.
/// @return The normalized comment-field rows for @p usr.
std::vector<model::CommentField> to_comment_fields(
    const std::string& usr, const model::CommentModel& model);

}  // namespace clangquill::parser

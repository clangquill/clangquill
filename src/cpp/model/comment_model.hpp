#pragma once

#include <map>
#include <string>
#include <vector>

/**
 * @file
 * @brief Format-agnostic structured representation of a documentation comment.
 */

namespace clangquill::model {

/// @brief A documented parameter or template parameter (`\@param` / `\@tparam`).
struct CommentParam {
  std::string name;         ///< The (template) parameter name.
  std::string description;  ///< Its documented description.
  /// @brief Doxygen's parameter-passing direction, without the brackets.
  ///
  /// One of `"in"`, `"out"`, `"in,out"`, or empty when the comment did not
  /// spell one out. Doxygen writes it as an attribute on the command itself
  /// (`\@param[out] result ...`), so it belongs to the entry rather than to
  /// its description.
  std::string direction;
};

/// @brief A documented named return value (`\@retval <value> <description>`).
struct CommentRetval {
  std::string value;        ///< The returned value being described.
  std::string description;  ///< What that value means.
};

/// @brief A documented thrown exception (`\@throws` / `\@throw` / `\@exception`).
struct CommentThrow {
  std::string exception;    ///< The exception type that may be thrown.
  std::string description;  ///< The condition under which it is thrown.
};

/// @brief Format-agnostic structured documentation comment.
///
/// Produced by an ICommentParser (the default being the Doxygen parser) from a
/// symbol's raw comment. Downstream code consumes this model without knowing the
/// source comment format, so the format stays swappable. Commands the model does
/// not name explicitly land in @ref custom, keyed by the command word.
struct CommentModel {
  std::string brief;                ///< One-line summary.
  std::vector<std::string> detail;  ///< Free-form paragraphs / blocks.
  std::vector<CommentParam> params;   ///< `\@param` entries.
  std::vector<CommentParam> tparams;  ///< `\@tparam` entries.
  std::string returns;              ///< `\@return` description.
  std::vector<CommentRetval> retvals;  ///< `\@retval` entries.
  std::vector<CommentThrow> throws;    ///< `\@throws` entries.
  std::vector<std::string> see;        ///< `\@see` references.
  std::vector<std::string> since;      ///< `\@since` notes.
  std::vector<std::string> deprecated; ///< `\@deprecated` notes.
  std::vector<std::string> note;       ///< `\@note` blocks.
  std::vector<std::string> warning;    ///< `\@warning` blocks.
  std::vector<std::string> pre;        ///< `\@pre` preconditions.
  std::vector<std::string> post;       ///< `\@post` postconditions.
  std::map<std::string, std::vector<std::string>> custom;  ///< Unrecognized commands, keyed by command word.

  /// @brief True when no field carries any documentation.
  /// @return `true` if every member is empty.
  bool empty() const {
    return brief.empty() && detail.empty() && params.empty() &&
           tparams.empty() && returns.empty() && retvals.empty() &&
           throws.empty() && see.empty() && since.empty() &&
           deprecated.empty() && note.empty() && warning.empty() &&
           pre.empty() && post.empty() && custom.empty();
  }
};

}  // namespace clangquill::model

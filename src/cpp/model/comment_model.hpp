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

/// @brief Every named CommentModel field, in flatten order.
///
/// `X(member, "row name")` -- the member of CommentModel, and the
/// `comment_fields.name` it is persisted under. The two differ where Doxygen's
/// command is singular but the model holds a list (`retvals` / `"retval"`).
///
/// One list, four consumers: CommentModel::empty(), to_comment_fields(),
/// from_comment_fields() and to_fields_json() all walk it, so they cannot
/// disagree about which fields exist -- and the bindings export it, so the
/// Python read side derives its routing table from it rather than repeating it.
///
/// The order is the flatten order, and flatten order is the persisted
/// `comment_fields.ordinal`. Reordering this list rewrites the IR.
///
/// `custom` is deliberately absent: it is the open bucket every *unlisted*
/// command falls into, so it has no row name of its own.
#define CLANGQUILL_COMMENT_FIELDS(X) \
  X(brief, "brief")                  \
  X(detail, "detail")                \
  X(params, "param")                 \
  X(tparams, "tparam")               \
  X(returns, "returns")              \
  X(retvals, "retval")               \
  X(throws, "throws")                \
  X(see, "see")                      \
  X(since, "since")                  \
  X(deprecated, "deprecated")        \
  X(note, "note")                    \
  X(warning, "warning")              \
  X(pre, "pre")                      \
  X(post, "post")                    \
  X(invariant, "invariant")          \
  X(todo, "todo")                    \
  X(bug, "bug")                      \
  X(author, "author")                \
  X(version, "version")              \
  X(date, "date")

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
  std::vector<std::string> note;       ///< `\@note` / `\@remark` blocks.
  std::vector<std::string> warning;    ///< `\@warning` blocks.
  std::vector<std::string> pre;        ///< `\@pre` preconditions.
  std::vector<std::string> post;       ///< `\@post` postconditions.
  std::vector<std::string> invariant;  ///< `\@invariant` blocks.
  std::vector<std::string> todo;       ///< `\@todo` items.
  std::vector<std::string> bug;        ///< `\@bug` reports.
  std::vector<std::string> author;     ///< `\@author` / `\@authors` credits.
  std::vector<std::string> version;    ///< `\@version` notes.
  std::vector<std::string> date;       ///< `\@date` notes.
  std::map<std::string, std::vector<std::string>> custom;  ///< Unrecognized commands, keyed by command word.

  /// @brief True when no field carries any documentation.
  /// @return `true` if every member is empty.
  bool empty() const {
    bool none = custom.empty();
#define CLANGQUILL_COMMENT_FIELD_EMPTY(member, row) none = none && member.empty();
    CLANGQUILL_COMMENT_FIELDS(CLANGQUILL_COMMENT_FIELD_EMPTY)
#undef CLANGQUILL_COMMENT_FIELD_EMPTY
    return none;
  }
};

}  // namespace clangquill::model

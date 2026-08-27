#pragma once

#include <string>

/**
 * @file
 * @brief Raw and lightly-normalized documentation-comment records.
 */

namespace clangquill::model {

/// @brief The verbatim documentation comment attached to a symbol.
///
/// Only the raw text and the dialect it was parsed as: the structured parse
/// lives in CommentField rows, so the comment format stays swappable and the
/// model is stored exactly once.
struct RawComment {
  std::string symbol_usr;              ///< USR of the documented symbol.
  std::string text;                    ///< Verbatim comment, markers included.
  std::string format = "doxygen-raw";  ///< Identifier of the comment dialect.
};

/// @brief A normalized projection of a single structured comment field.
///
/// For example a `\@param` entry. These rows are the persisted form of the
/// parsed CommentModel; the Python read side rebuilds the model from them.
struct CommentField {
  std::string symbol_usr;  ///< USR of the documented symbol.
  std::string name;        ///< Field name: brief / param / return / tparam / ...
  std::string arg;         ///< Argument, e.g. the parameter name.
  std::string value;       ///< The field text.
  int ordinal = 0;         ///< Position for stable ordering.
};

}  // namespace clangquill::model

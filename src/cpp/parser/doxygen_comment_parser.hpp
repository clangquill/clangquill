#pragma once

#include <clang-c/Index.h>

#include <string>

#include "model/comment_model.hpp"
#include "parser/comment_parser.hpp"

/**
 * @file
 * @brief The default Doxygen comment parser.
 */

namespace clangquill::parser {

/// @brief Default comment parser, understanding Doxygen syntax.
///
/// Walks libclang's parsed CXComment tree for structure (brief, paragraphs,
/// `\@param`/`\@tparam`, block commands) and supplements it with raw-text command
/// scanning for arguments libclang does not split out (`\@retval` / `\@throws`
/// value names). The two passes are merged into a single format-agnostic
/// CommentModel.
///
/// The raw-text scan itself is `clangquill::comment::doxygen_parse_raw`, which
/// needs no libclang and is what the Python `doxygen_parse` binds onto -- this
/// class only adds the CXComment tree on top of it.
class DoxygenCommentParser : public ICommentParser {
 public:
  /// @copydoc ICommentParser::format
  std::string format() const override { return "doxygen"; }

  /// @copydoc ICommentParser::parse
  model::CommentModel parse(CXCursor cursor,
                            const std::string& raw) const override;
};

}  // namespace clangquill::parser

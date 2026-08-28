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
class DoxygenCommentParser : public ICommentParser {
 public:
  /// @copydoc ICommentParser::format
  std::string format() const override { return "doxygen"; }

  /// @copydoc ICommentParser::parse
  model::CommentModel parse(CXCursor cursor,
                            const std::string& raw) const override;

  /// @brief Parses a comment from its raw text alone (no cursor/parsed tree).
  ///
  /// Used to recover free-floating `\defgroup` blocks that attach to no cursor,
  /// and is the entry point checked against `tests/comment_corpus/` alongside
  /// `clangquill.comments.doxygen_parse`, the independent Python implementation
  /// of the same raw-text scan (see its module docstring).
  /// @param raw The verbatim comment text.
  /// @return The structured comment model.
  static model::CommentModel parse_raw_text(const std::string& raw);
};

}  // namespace clangquill::parser

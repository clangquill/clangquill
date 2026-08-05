#pragma once

#include <string>
#include <vector>

#include "model/comment.hpp"
#include "model/diagnostic.hpp"
#include "model/enumerator.hpp"
#include "model/file.hpp"
#include "model/group.hpp"
#include "model/parameters.hpp"
#include "model/reference.hpp"
#include "model/symbol.hpp"

/**
 * @file
 * @brief The top-level container holding the IR extracted from a parse.
 */

namespace clangquill::model {

/// @brief The complete extracted IR for one or more translation units.
///
/// Owns all rows; the store writes it into SQLite and can read it back for
/// round-trip tests.
struct ParsedModule {
  std::vector<SourceFile> files;                  ///< Source files seen during the parse.
  std::vector<Symbol> symbols;                    ///< Every extracted symbol.
  std::vector<FunctionParameter> parameters;      ///< Function/method parameters.
  std::vector<TemplateParameter> template_parameters;  ///< Template parameters.
  std::vector<Enumerator> enumerators;            ///< Enumerators within enums.
  std::vector<Reference> references;              ///< Cross-reference edges.
  std::vector<RawComment> comments;               ///< Raw documentation comments.
  std::vector<CommentField> comment_fields;       ///< Normalized comment fields.
  std::vector<Group> groups;                      ///< Doxygen group definitions.
  std::vector<GroupMember> group_members;         ///< Symbol-to-group memberships.
  std::vector<Diagnostic> diagnostics;            ///< Parse diagnostics, notes flattened behind their parent.
};

}  // namespace clangquill::model

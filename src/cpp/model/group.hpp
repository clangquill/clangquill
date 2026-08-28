#pragma once

#include <string>

/**
 * @file
 * @brief Doxygen group definitions and symbol-to-group membership records.
 */

namespace clangquill::model {

/// @brief A Doxygen documentation group (`\defgroup` / `\addtogroup`).
///
/// `id` is the group token (stable identity); `parent_group_id` is set when the
/// group's own block carries an `\ingroup` to nest it under another group.
struct Group {
  std::string id;               ///< The group token; stable identity.
  std::string title;            ///< Human-readable group title.
  std::string brief;            ///< One-line group summary.
  std::string detail;           ///< Longer group description.
  std::string parent_group_id;  ///< Enclosing group token; empty for a top-level group.
  /// @brief True when this row came from a `\defgroup` block.
  ///
  /// `\addtogroup` blocks and the stub rows an `\ingroup` reference emits are
  /// *not* definitions: they may fill in what the definition leaves empty, but
  /// they never overwrite a definition's title or prose.
  bool is_definition = false;
};

/// @brief Membership of a symbol in a group.
///
/// Recorded from an `\ingroup` (or inline `\defgroup`) on the symbol's own
/// comment.
struct GroupMember {
  std::string group_id;    ///< The group the member belongs to.
  std::string member_usr;  ///< USR of the member symbol.
  int ordinal = 0;         ///< Position within the group for stable ordering.
};

}  // namespace clangquill::model

#pragma once

/**
  \defgroup prose Prose formatting

  **Bold** opens the description.

  A second paragraph, which must stay a paragraph of its own.
  It runs across two source lines.

  *Emphasis* opens a third.
*/

/**
 * \defgroup decorated Decorated block
 *
 * Star-decorated lines still lose their decoration.
 */

/// \ingroup prose
/// A symbol so the group has a member.
inline int prose_value() { return 1; }

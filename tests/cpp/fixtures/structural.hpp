#pragma once

// Every structural block lives up here, away from the entities it names, the
// way Eigen writes them. Nothing below is adjacent to a block, so anything that
// ends up documented got there by resolution rather than by libclang gluing a
// comment to whatever followed it.

/** \class Widget
  * \ingroup shapes
  * \brief A widget documented from a block that is nowhere near it.
  */

/** \struct Gadget
  * \brief A struct named by a structural block.
  */

/** \enum Colour
  * \brief An enum named by a structural block.
  */

/** \namespace deep
  * \brief A namespace named by a structural block.
  */

/** \typedef Distance
  * \brief A typedef named by a structural block.
  */

/** \fn deep::scale(int)
  * \brief A function named with a signature the resolver has to strip.
  */

/** \fn deep::over
  * \brief Ambiguous: two overloads answer to this name, so neither is taken.
  */

/** \class NoSuchThingAnywhere
  * \brief Names nothing at all; must not crash or leave a dangling row.
  */

/** \class Owned
  * \brief This must NOT replace Owned's own documentation.
  */

namespace detail {
struct Unrelated {
  int filler = 0;
};
}  // namespace detail

class Widget {
 public:
  int w = 0;
};

struct Gadget {
  int g = 0;
};

enum class Colour { Red, Green };

using Distance = int;

/// Its own comment, which must win over the structural block above.
class Owned {
 public:
  int v = 0;
};

/** \relates Widget
  *
  * Streams a widget. This prose belongs to the function, not to the command
  * above it, which names one entity and nothing more.
  *
  * \sa Widget
  */
int stream_widget(int w);

namespace deep {
int scale(int factor);
int over(int a);
int over(double a);
}  // namespace deep

/** \ingroup shapes widgets
  * A group command takes the ids on its own line; this sentence documents the
  * function and must not become a group of its own.
  */
int grouped_helper(int a);

/** \internal
  * An internal marker takes no argument, so this sentence is the entity's own
  * documentation rather than the marker's.
  */
int internal_helper(int a);

/** \li A list marker likewise takes no argument.
  *
  * And this second paragraph still belongs to the entity.
  */
int marker_helper(int a);

/** \internal \ingroup shapes
  * A marker does not own the rest of its line: the group command after it has
  * to be seen, and this sentence is still the entity's own documentation.
  */
int internal_grouped(int a);

/** \internal \class Chained
  * The rescan has to chain into a single-name command too.
  */
class Chained {
 public:
  int c = 0;
};

/** \ingroup shapes
  * \brief A blank line ends the brief.
  *
  * This paragraph is the detailed description; a paragraph command runs to the
  * next blank line, so it must not be folded into the one-line summary.
  *
  * \param a the input value
  *
  * A closing paragraph documents the function, not the parameter above it.
  */
int paragraph_helper(int a);

/** \ingroup shapes
  * \brief Directions survive the raw path.
  *
  * \param[out] result where the answer is written
  * \param[in] value the input value
  * \param[in,out] scratch reused working storage
  * \param plain a parameter with no direction attribute
  */
int directed_helper(int* result, int value, int* scratch, int plain);

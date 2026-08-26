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

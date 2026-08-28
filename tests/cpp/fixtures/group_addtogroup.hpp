#pragma once

/**
 * \addtogroup geom
 *
 * Prose from an addtogroup block, scanned before the definition.
 */

/**
 * \defgroup geom Geometry helpers
 *
 * Points and vectors.
 *
 * The long version.
 */

/**
 * \addtogroup geom
 *
 * A later addtogroup block, which must not win either.
 */

/**
 * \defgroup bare Bare definition
 */

/**
 * \addtogroup bare
 *
 * Prose only an addtogroup block supplies.
 */

/// \ingroup geom
/// A symbol so the group has a member.
inline int geom_value() { return 1; }

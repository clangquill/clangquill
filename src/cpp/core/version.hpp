#pragma once

#include <string>

/**
 * @file
 * @brief Version probe for the native C++ core.
 */

namespace clangquill {

/// @brief Version of the C++ core.
///
/// Kept separate from the Python package version (which comes from
/// setuptools_scm) so the native layer can be probed independently. It is also
/// the lever that invalidates a warm cache when the *meaning* of a parse
/// configuration changes rather than its values -- the parse fingerprint keys
/// on it -- so bump it when the same options would now produce a different
/// command line. 0.2.0: `extra_args` reaches compile-database commands too.
/// @return The core version string.
inline std::string core_version() { return "0.2.0"; }

}  // namespace clangquill

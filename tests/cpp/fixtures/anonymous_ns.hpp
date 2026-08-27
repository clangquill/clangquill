#pragma once

namespace demo {

/// Public API a caller can name.
inline int visible() { return 1; }

namespace {

/// An internal helper: internal linkage, one translation unit only.
inline int hidden_helper() { return 2; }

/// An internal constant.
constexpr int kHiddenLimit = 3;

/// An internal tag type, with a member of its own.
struct HiddenTag {
  /// A member of an internal type is internal too.
  int hidden_field = 0;
};

}  // namespace

}  // namespace demo

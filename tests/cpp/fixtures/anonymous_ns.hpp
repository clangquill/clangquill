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

namespace inner {

/// A named namespace nested inside the anonymous one is internal too.
inline int nested_helper() { return 4; }

}  // namespace inner

}  // namespace

}  // namespace demo

namespace {

/// An internal helper at file scope, enclosed by no named namespace at all.
inline int file_scope_helper() { return 5; }

}  // namespace

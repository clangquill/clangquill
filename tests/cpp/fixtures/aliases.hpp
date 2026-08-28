#pragma once

/// Every form an alias declaration takes, and every shape its target can have.
namespace al {

/// A type an alias can point at.
struct Widget {
  int w = 0;  ///< The width.
};

/// A `typedef`, the form libclang has always described.
typedef double Distance;

/// A `using` alias naming a record declared here, so the edge resolves.
using Handle = Widget;

/// A `using` alias whose target is an array type.
using Buffer = char[64];

/// A pair-like holder, so a dependent alias has something to reach through.
/// \tparam A the first element type
/// \tparam B the second element type
template <typename A, typename B>
struct Pair {
  /// The first element's type -- dependent, and named by a bare parameter.
  using first = A;
};

/// Traits of a type.
/// \tparam T the type described
template <typename T>
struct Traits {
  /// The described type: a member alias of a class template.
  using type = T;

  /// A member alias reaching through another template.
  using through = typename Pair<T, int>::first;
};

/// An alias template: the alias proper is a child of this cursor.
/// \tparam T the pointee type
template <typename T>
using Ptr = T*;

}  // namespace al

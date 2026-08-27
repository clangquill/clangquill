#pragma once

/// Variable templates and the other declarations libclang leaves unexposed.
namespace vars {

/// Whether `T` is the foo type.
/// \tparam T the type to test
template <typename T>
struct is_foo {
  /// The answer.
  static constexpr bool value = false;
};

/// Trait shorthand for is_foo.
/// \tparam T the type to test
template <typename T>
inline constexpr bool is_foo_v = is_foo<T>::value;

/// `int` is the foo type.
template <>
inline constexpr bool is_foo_v<int> = true;

/// A pair-like holder.
/// \tparam A the first element type
/// \tparam B the second element type
template <typename A, typename B>
struct Pair {
  A first;   ///< The first element.
  B second;  ///< The second element.
};

/// The empty pair, whose type closes two argument lists at once.
/// \tparam T the first element type
template <typename T>
inline constexpr Pair<T, Pair<int, int>> empty_pair{};

/// A wrapper.
/// \tparam T the wrapped type
template <typename T>
struct Wrapper {
  T value;  ///< The wrapped value.
};

/// Deduces a Wrapper from any value.
template <typename T>
Wrapper(T) -> Wrapper<T>;

/// Deduces a Wrapper of long from an int.
Wrapper(int) -> Wrapper<long>;

namespace detail {
/// An implementation type.
struct Impl {};
}  // namespace detail

/// A shorthand for the detail namespace.
namespace shorthand = detail;

/// Re-exports the implementation type.
using detail::Impl;

}  // namespace vars

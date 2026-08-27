#pragma once

/// Templates specialized in every form the parser has to tell apart.
namespace spec {

/// Traits of a type.
/// \tparam T the type described
/// \tparam Tag a disambiguating tag
template <typename T, typename Tag = void>
struct Traits {
  /// The described type.
  using type = T;

  /// Describes the type.
  static const char* describe();
};

/// Traits of `int`.
template <>
struct Traits<int, void> {
  /// The described type.
  using type = double;

  /// Describes the type.
  static const char* describe();
};

/// Traits of any pointer.
/// \tparam U the pointee type
template <typename U>
struct Traits<U*, void> {
  /// The described type.
  using type = U;
};

/// Explicitly instantiated for `char`.
template struct Traits<char, void>;

}  // namespace spec

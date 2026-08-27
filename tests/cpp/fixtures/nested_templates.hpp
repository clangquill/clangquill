#pragma once

/// Templates whose heads exercise the raw lexer's `>>` and `<<` tokens.
namespace nested {

/// A pair-like holder.
/// \tparam A the first element type
/// \tparam B the second element type
template <typename A, typename B>
struct Pair {
  A first;   ///< The first element.
  B second;  ///< The second element.
};

/// A holder whose fallback type defaults to a nested template-id.
///
/// The default closes three argument lists with two tokens (`>>` then `>`),
/// which a raw lex reports as one `>>` token.
/// \tparam T the element type
/// \tparam Fallback the type used in place of a missing element
template <typename T, typename Fallback = Pair<T, Pair<int, int>>>
class Holder {
 public:
  T value;  ///< The held element.
};

/// A holder whose capacity defaults to a shift expression.
///
/// `1 << 4` must not read as two opening argument lists.
/// \tparam T the element type
/// \tparam Bits the capacity, as a power of two
template <typename T, int Bits = 1 << 4>
struct Shifted {
  T value;  ///< The held element.
};

}  // namespace nested

#pragma once

/// \defgroup math Math utilities
///
/// Helpers for arithmetic and small numeric containers.

/// An object-like macro.
#define CQ_PI 3.14159

/// A function-like macro returning the larger of two values.
#define CQ_MAX(a, b) ((a) > (b) ? (a) : (b))

namespace m7 {

/// A fixed-capacity buffer.
/// \tparam T the element type
/// \tparam N the capacity (defaults to 4)
/// \ingroup math
template <typename T, int N = 4>
class Buffer {
 public:
  T data[N];
};

/// Returns the larger of two values.
/// \ingroup math
template <typename T>
T max_value(T a, T b) {
  return a > b ? a : b;
}

/// Types that support addition with themselves.
template <typename T>
concept Addable = requires(T a, T b) {
  a + b;
};

/// A 2D vector with operator overloads.
struct Vec {
  int x;
  int y;

  /// Indexed access to a component.
  int operator[](int i) const { return i == 0 ? x : y; }

  /// True if either component is non-zero.
  explicit operator bool() const { return x != 0 || y != 0; }

  /// Grants the free `reset` function access to internals.
  friend void reset(Vec& v);
  /// Grants a helper type access to internals.
  friend struct Inspector;

  /// Compares two vectors for equality.
  friend bool operator==(const Vec& a, const Vec& b) {
    return a.x == b.x && a.y == b.y;
  }
};

/// Adds two vectors component-wise.
Vec operator+(const Vec& a, const Vec& b);

/// Builds a vector from two components of any convertible type.
template <typename T>
Vec make_vec(T x, T y);

/// Adds two integers.
/// \ingroup math
int add(int a, int b);

}  // namespace m7

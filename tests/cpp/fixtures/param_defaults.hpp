#pragma once

/// Functions whose parameters carry default arguments.
namespace defaults {

/// A pair-like holder.
/// \tparam A the first element type
/// \tparam B the second element type
template <typename A, typename B>
struct Pair {
  A first;   ///< The first element.
  B second;  ///< The second element.
};

/// Draws a shape.
/// \param width the width in columns
/// \param label the label to print
/// \param bounds the clipping bounds
void draw(int width = 80, const char* label = "shape",
          Pair<int, Pair<int, int>> bounds = {});

/// Returns the value, or a default-constructed fallback.
/// \tparam T the value type
/// \param value the value to return
/// \param fallback the fallback value
template <typename T>
T value_or(T value, T fallback = T{});

/// A widget.
struct Widget {
  /// Resizes the widget.
  /// \param width the new width
  /// \param height the new height
  void resize(int width, int height = 24);
};

}  // namespace defaults

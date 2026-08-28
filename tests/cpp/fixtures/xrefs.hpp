#pragma once

/// Symbols whose cross-references the C++ domain has to resolve.
namespace xr {

/// A fixed-capacity buffer.
/// \tparam T the element type
/// \tparam N the capacity
template <typename T, int N = 4>
class Buffer {
 public:
  /// The stored elements.
  T data[N];

  /// The element type.
  using value_type = T;

  /// The capacity, as a value.
  static constexpr int capacity = N;

  /// Returns the first element.
  T front() const { return data[0]; }

  /// A cursor into the buffer.
  struct Cursor {
    /// The current offset.
    int offset;

    /// Advances the cursor.
    void advance() { ++offset; }
  };
};

/// Traits of a type.
/// \tparam T the described type
template <typename T>
struct Traits {
  /// The described type.
  using type = T;

  /// Describes the type.
  static const char* describe();
};

/// Traits of any pointer.
/// \tparam U the pointee type
template <typename U>
struct Traits<U*> {
  /// The described type.
  using type = U;

  /// Describes the type.
  static const char* describe();
};

/// Traits of `int`.
template <>
struct Traits<int> {
  /// The described type.
  using type = double;

  /// Describes the type.
  static const char* describe();
};

/// Whether `T` is the foo type.
/// \tparam T the type to test
template <typename T>
inline constexpr bool is_foo_v = false;

/// `int` is the foo type.
template <>
inline constexpr bool is_foo_v<int> = true;

/// Types that support addition with themselves.
template <typename T>
concept Addable = requires(T a, T b) { a + b; };

/// Colours, as a scoped enum.
enum class Colour {
  Red,   ///< The red colour.
  Green  ///< The green colour.
};

/// Colours, as an unscoped enum.
enum Plain {
  PlainRed,   ///< Unscoped red.
  PlainGreen  ///< Unscoped green.
};

/// A 2D vector.
struct Vec {
  int x;  ///< The x component.
  int y;  ///< The y component.

  /// Indexed access to a component.
  int operator[](int i) const { return i == 0 ? x : y; }

  /// Adds another vector in place.
  Vec& operator+=(const Vec& other);

  /// A tag type nested in a plain struct.
  struct Tag {
    /// The tag's identifier.
    int id;
  };
};

/// Compares two vectors for equality.
bool operator==(const Vec& a, const Vec& b);

/// Adds two vectors.
Vec operator+(const Vec& a, const Vec& b);

/// An alias for the default buffer.
using DefaultBuffer = Buffer<int, 4>;

/// Doubles one integer.
int overloaded(int a);

/// Adds two integers.
int overloaded(int a, int b);

/// Links to every cross-reference shape the C++ domain must resolve.
///
/// Class template \ref xr::Buffer, its member \ref xr::Buffer::data, its
/// method \ref xr::Buffer::front, its member alias \ref xr::Buffer::value_type,
/// its static member \ref xr::Buffer::capacity, its nested class
/// \ref xr::Buffer::Cursor, that class's member \ref xr::Buffer::Cursor::offset
/// and its method \ref xr::Buffer::Cursor::advance.
///
/// Primary template \ref xr::Traits and its member \ref xr::Traits::describe.
///
/// Variable template \ref xr::is_foo_v and concept \ref xr::Addable.
///
/// Scoped enum \ref xr::Colour with \ref xr::Colour::Red; unscoped enum
/// \ref xr::Plain with \ref xr::PlainRed.
///
/// Struct \ref xr::Vec, its field \ref xr::Vec::x, its subscript operator
/// \ref xr::Vec::operator[], its compound assignment \ref xr::Vec::operator+=,
/// its nested type \ref xr::Vec::Tag and that type's field
/// \ref xr::Vec::Tag::id.
///
/// Free operators \ref xr::operator== and \ref xr::operator+, the alias
/// \ref xr::DefaultBuffer and the overload set \ref xr::overloaded.
///
/// \see xr::Buffer::front
/// \see xr::operator==
/// \see xr::Vec::operator[]
void hub();

}  // namespace xr

namespace xr {

/// An outer class template with a class template of its own inside it.
/// \tparam T the outer element type
template <typename T>
struct Outer {
  /// The inner class template.
  /// \tparam U the inner element type
  template <typename U>
  struct Inner {
    /// A member of the inner template.
    U value;

    /// Returns the member.
    U get() const { return value; }
  };
};

/// Links to the nested-template shapes.
///
/// The outer \ref xr::Outer, the inner \ref xr::Outer::Inner, its member
/// \ref xr::Outer::Inner::value and its method \ref xr::Outer::Inner::get.
void nested_hub();

}  // namespace xr

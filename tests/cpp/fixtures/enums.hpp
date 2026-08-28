#pragma once

/// A scoped enum with explicit values.
enum class Color {
  Red,
  Green = 5,
  Blue,  // 6
};

/// An unscoped enum.
enum Direction {
  North,
  East,
  South,
  West,
};

/// A 64-bit alias standing in for `std::uint64_t` -- sugar, not a builtin kind.
using u64 = unsigned long long;

/// A scoped enum whose underlying type is written through a typedef.
enum class Mask : u64 {
  None = 0,
  All = 0xFFFFFFFFFFFFFFFF,
};

/// A scoped enum with a builtin fixed underlying type.
enum class Level : unsigned char {
  Low,
  High = 200,
};

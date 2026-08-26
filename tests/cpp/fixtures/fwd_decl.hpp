#pragma once

#include <memory>

/// Owner that only ever names Opaque through a pointer.
struct Owner {
  /// The elaborated-type-specifier below forward-declares Opaque, exactly as
  /// `std::unique_ptr<class CompileDb>` does in the parser's own headers.
  std::unique_ptr<class Opaque> held;
};

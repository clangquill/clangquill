#pragma once

// Deliberately does NOT include order_a.hpp: this header is only well-formed
// when order_a.hpp has already been parsed into the same translation unit.
// Which name this declares therefore depends on include order, and the
// divergence lands in the symbol's USR rather than only in a diagnostic.

/// A type whose very name comes from a macro defined elsewhere.
struct QUILL_ORDER_NAME {
  OrderIndex value;  ///< The tagged value.
};

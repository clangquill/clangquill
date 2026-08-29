#pragma once

/**
 * @file
 * @brief How an IR enumerator is spelled on the Python side.
 */

namespace clangquill::model {

/// @brief One enumerator, paired with the name its Python mirror uses.
///
/// The IR persists these enums as integers, so their values are part of the
/// on-disk schema and `clangquill.store` declares a matching `IntEnum`. Each
/// enum below is followed by a table of these entries, which the bindings
/// export so the Python mirror can be compared against the C++ definition
/// rather than transcribed from it.
///
/// The table spells its values as *enumerator references*, never as literals:
/// renaming an enumerator then fails to compile, and reordering cannot make a
/// row claim the wrong integer. A `static_assert` on the table's size catches
/// the one remaining mistake, an enumerator added without a row. What is left
/// hand-written is only the SCREAMING_SNAKE spelling, which is exactly what
/// `tests/test_enum_mirrors.py` compares against `IntEnum.__members__`.
struct EnumEntry {
  const char* name;  ///< SCREAMING_SNAKE spelling used by `clangquill.store`.
  int value;         ///< The integer persisted in the IR.
};

}  // namespace clangquill::model

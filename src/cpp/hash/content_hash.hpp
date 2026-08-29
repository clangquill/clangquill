#pragma once

#include <string>
#include <vector>

#include "model/parameters.hpp"
#include "model/symbol.hpp"

/**
 * @file
 * @brief Stable content hash of a symbol's documented surface.
 */

namespace clangquill::hash {

/// @brief Computes a stable hash of a symbol's documented surface.
///
/// Source location is deliberately excluded so moving a symbol within a file
/// does not invalidate the M6 cache; the raw comment text is included so doc
/// edits do change the hash.
/// @param sym The symbol whose semantic surface is hashed.
/// @param params The symbol's parameters (for functions/methods).
/// @param raw_comment The verbatim documentation comment, if any.
/// @return A hex digest covering the inputs above.
std::string content_hash(const model::Symbol& sym,
                         const std::vector<model::FunctionParameter>& params,
                         const std::string& raw_comment);

/// @brief The `Symbol` fields content_hash() folds in, in hashing order.
///
/// Read off the same table content_hash() walks, so it cannot fall out of step
/// with what is actually hashed. The bindings export it because the Python read
/// side needs the *complement*: `Generator._wide_tokens` fingerprints exactly
/// the `Symbol` fields this list leaves out, which is what lets a custom
/// template depend on them safely.
/// @return The hashed field names.
std::vector<std::string> content_hash_symbol_fields();

}  // namespace clangquill::hash

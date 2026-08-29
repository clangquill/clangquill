#pragma once

#include <string>

#include "model/comment_model.hpp"

/**
 * @file
 * @brief Doxygen comment scanning that needs nothing but the raw text.
 *
 * Split out of the libclang-backed parser so it builds into the core
 * unconditionally: the stub backend (no libclang) still parses comments, and
 * the Python `doxygen_parse` is a binding onto this entry point rather than a
 * second implementation of the same grammar.
 */

namespace clangquill::comment {

/// @brief Parses a comment from its raw text alone (no cursor/parsed tree).
///
/// The whole grammar lives here: marker stripping, paragraph breaks, command
/// routing, verbatim blocks and inline markup. The libclang-backed parser walks
/// the parsed CXComment tree for structure and falls back to this scan, so the
/// two agree by sharing the routing rather than by convention.
/// @param raw The verbatim comment text, markers included.
/// @return The structured comment model.
model::CommentModel doxygen_parse_raw(const std::string& raw);

/// @brief True when the raw comment uses a command libclang's tree mishandles.
///
/// Such comments have to take the raw path even when a parsed tree exists.
/// @param raw The verbatim comment text.
/// @return `true` when the raw scan must be preferred.
bool raw_has_unroutable_command(const std::string& raw);

}  // namespace clangquill::comment

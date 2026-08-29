#pragma once

#include <string>
#include <vector>

#include "model/comment_model.hpp"

/**
 * @file
 * @brief The scanner internals the libclang-backed comment parser shares.
 *
 * `parser/doxygen_comment_parser.cpp` walks libclang's parsed CXComment tree
 * but must route commands, normalize text and promote lead paragraphs exactly
 * as the raw scan does, or the two paths would disagree about the same comment.
 * These are the pieces both need; everything else in `doxygen_raw.cpp` is
 * private to that translation unit.
 */

namespace clangquill::comment::detail {

/// @brief Lowercases @p s in place and returns it.
/// @param s The string to fold.
/// @return The lowercased string.
std::string lower(std::string s);

/// @brief Collapses whitespace runs to single spaces and trims the ends.
/// @param s The text to normalize.
/// @return The normalized text.
std::string normalize_ws(const std::string& s);

/// @brief Rewrites Doxygen's inline commands into MyST markup.
/// @param text Already-normalized text.
/// @return The text with inline commands rendered.
std::string render_inline_markup(const std::string& text);

/// @brief The language tag of a `\@code{.py}` attribute, or "" when absent.
/// @param attr The attribute text without its braces.
/// @return The MyST language name.
std::string code_language(const std::string& attr);

/// @brief Renders a verbatim block as a fenced code block.
/// @param kind The command word (`code` or `verbatim`).
/// @param language The language tag, possibly empty.
/// @param lines The block's lines, indentation intact.
/// @return The fenced block.
std::string fenced_block(const std::string& kind, const std::string& language,
                         std::vector<std::string> lines);

/// @brief True for a group command, whose text must never reach the prose.
/// @param name The lowercased command word.
/// @return `true` for `ingroup` / `defgroup` / `addtogroup`.
bool is_group_command(const std::string& name);

/// @brief True when @p name is one of Doxygen's inline (word-decorating) commands.
/// @param name The lowercased command word.
/// @return `true` when the command decorates the next word.
bool is_inline_command(const std::string& name);

/// @brief Routes one command's text into the model.
/// @param m The model to write into.
/// @param name The lowercased command word.
/// @param text The command's normalized text.
/// @param direction A `\@param` direction attribute, or empty.
void route_command(model::CommentModel& m, const std::string& name,
                   const std::string& text, const std::string& direction = {});

/// @brief Promotes the leading free-text paragraphs into brief/detail.
/// @param m The model to write into.
/// @param lead The lead paragraphs in source order.
/// @param explicit_brief Whether the comment wrote a `\@brief`.
void apply_lead(model::CommentModel& m, const std::vector<std::string>& lead,
                bool explicit_brief);

}  // namespace clangquill::comment::detail

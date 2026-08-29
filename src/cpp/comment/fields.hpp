#pragma once

#include <string>
#include <utility>
#include <vector>

#include "model/comment.hpp"
#include "model/comment_model.hpp"

/**
 * @file
 * @brief The `comment_fields` projection and its inverse.
 *
 * A CommentModel is persisted as normalized `comment_fields` rows rather than
 * a blob, so the read side can query it. Flattening, rebuilding and the JSON
 * form all walk `CLANGQUILL_COMMENT_FIELDS`, so they cannot disagree about
 * which fields exist; comment_field_table() exports that same list, which is
 * how the Python read side derives its routing instead of repeating it.
 *
 * Free of libclang so it builds into the core unconditionally.
 */

namespace clangquill::comment {

/// @brief Encodes a parameter's name and direction into one field argument.
///
/// `comment_fields` has a single slot for a field's argument, so a directed
/// parameter carries its direction there in the bracketed form Doxygen itself
/// writes: `[out] result`. An undirected parameter is spelled as its bare name.
/// @param param The documented parameter.
/// @return The encoded `arg` value.
std::string encode_param_arg(const model::CommentParam& param);

/// @brief Splits an encoded field argument back into name and direction.
///
/// The exact inverse of encode_param_arg: `"[out] result"` becomes
/// `{"result", "out"}`. An argument whose brackets do not hold a direction
/// Doxygen defines is left alone, so a parameter genuinely named `[x]` is not
/// silently rewritten.
/// @param arg The `comment_fields.arg` value.
/// @return The parameter name and its direction.
std::pair<std::string, std::string> split_param_arg(const std::string& arg);

/// @brief Serializes a CommentModel into its canonical JSON form.
///
/// Not persisted: the IR stores the model once, as `comment_fields` rows. This
/// is the shape the shared conformance corpus (tests/comment_corpus/) spells a
/// case's expected model in.
/// @param model The structured comment to serialize.
/// @return The JSON representation.
std::string to_fields_json(const model::CommentModel& model);

/// @brief Flattens a CommentModel into normalized `comment_fields` rows.
///
/// Order is preserved via a running ordinal so reads round-trip the model.
/// @param usr USR of the documented symbol.
/// @param model The structured comment to flatten.
/// @return The normalized comment-field rows for @p usr.
std::vector<model::CommentField> to_comment_fields(
    const std::string& usr, const model::CommentModel& model);

/// @brief Rebuilds a CommentModel from its `comment_fields` rows.
///
/// The inverse of to_comment_fields, generated from the same field list so the
/// two cannot disagree. Rows must arrive in ordinal order. A field name the
/// model does not know lands in CommentModel::custom, which is what makes an
/// unrecognized Doxygen command survive the round trip.
/// @param fields The flattened rows of one symbol, in ordinal order.
/// @return The reconstructed model.
model::CommentModel from_comment_fields(
    const std::vector<model::CommentField>& fields);

/// @brief One entry of the exported comment-field table.
struct CommentFieldInfo {
  const char* row_name;   ///< The `comment_fields.name` value.
  const char* member;     ///< The CommentModel member it belongs to.
  const char* shape;      ///< "scalar", "list", "param", "retval" or "throws".
};

/// @brief Every named comment field, in flatten order.
///
/// Exported through the bindings so `clangquill.comments` derives its routing
/// table from the encoder rather than transcribing it.
/// @return The field table.
const std::vector<CommentFieldInfo>& comment_field_table();

}  // namespace clangquill::comment

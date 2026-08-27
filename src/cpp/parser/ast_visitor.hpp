#pragma once

#include <clang-c/Index.h>

#include <string>
#include <vector>

#include "model/module.hpp"

/**
 * @file
 * @brief Entry point that walks a translation unit's AST into the IR model.
 */

namespace clangquill::parser {

/// @brief Options that change what the AST walk extracts.
struct VisitOptions {
  /// @brief Extract the contents of anonymous namespaces.
  ///
  /// Off by default, matching Doxygen's `EXTRACT_ANON_NSPACES = NO`: an
  /// anonymous namespace has internal linkage, so what it holds is one
  /// translation unit's implementation detail rather than API anyone can name
  /// or link against. When on, its contents are extracted with `@anonymous`
  /// in their qualified name -- the Sphinx C++ domain's spelling for an
  /// anonymous entity -- rather than under the enclosing namespace's name.
  bool extract_anonymous_namespaces = false;
};

/// @brief Walks the translation unit rooted at @p tu_cursor into @p out.
///
/// Appends symbols, references, parameters, enumerators, comments and file rows.
/// @param tu_cursor Cursor for the translation unit to traverse.
/// @param main_file Path passed to the parser, used to filter out included declarations.
/// @param out Module that extracted rows are appended to.
/// @param options What the walk extracts (see VisitOptions).
void visit_translation_unit(CXCursor tu_cursor, const std::string& main_file,
                            model::ParsedModule& out,
                            const VisitOptions& options = {});

/// @brief Walks the translation unit, extracting from a *set* of files.
///
/// Used for umbrella translation units that `#include` several inputs: only
/// declarations physically located in one of @p main_files are extracted, and
/// each of those files is scanned for free-floating comment blocks (group
/// definitions, macro docs) exactly as a per-file parse would scan its main
/// file.
///
/// @param tu_cursor Cursor for the translation unit to traverse.
/// @param main_files Accepted file spellings; entries that name the same file
///        under different spellings are deduplicated.
/// @param trust_main_file Whether a cursor in the TU's main file is accepted
///        regardless of path spelling (`true` only when the main file is a real
///        input rather than a synthetic umbrella).
/// @param out Module that extracted rows are appended to.
/// @param options What the walk extracts (see VisitOptions).
void visit_translation_unit(CXCursor tu_cursor,
                            const std::vector<std::string>& main_files,
                            bool trust_main_file, model::ParsedModule& out,
                            const VisitOptions& options = {});

}  // namespace clangquill::parser

#pragma once

#include <string>

/**
 * @file
 * @brief Core symbol record and the enumerations describing a C++ entity.
 */

namespace clangquill::model {

/// @brief The kind of C++ entity a symbol represents.
///
/// Stored as an integer in SQLite, so the underlying values are part of the
/// on-disk schema and must stay stable.
enum class SymbolKind {
  Unknown = 0,       ///< Not one of the kinds below (or not yet classified).
  Namespace,         ///< A `namespace`.
  Class,             ///< A `class`.
  Struct,            ///< A `struct`.
  Union,             ///< A `union`.
  Function,          ///< A free (non-member) function.
  Method,            ///< A member function.
  Constructor,       ///< A constructor.
  Destructor,        ///< A destructor.
  Field,             ///< A non-static data member.
  Variable,          ///< A namespace- or file-scope variable.
  Enum,              ///< An `enum` / `enum class`.
  Enumerator,        ///< A single constant within an enum.
  Typedef,           ///< A `typedef`.
  TypeAlias,         ///< A `using` type alias.
  FunctionTemplate,  ///< A function template.
  ClassTemplate,     ///< A class template.
  Concept,           ///< A C++20 `concept`.
  Macro,             ///< A preprocessor `#define`.
};

/// @brief C++ access specifier.
///
/// `None` is used for namespace- and file-scope entities that have no access
/// level.
enum class AccessKind {
  None = 0,   ///< No access level (namespace/file scope).
  Public,     ///< `public`.
  Protected,  ///< `protected`.
  Private,    ///< `private`.
};

/// @brief Storage class of a symbol; `None` means "unspecified".
enum class StorageKind {
  None = 0,     ///< No explicit storage class.
  Static,       ///< `static`.
  Extern,       ///< `extern`.
  Register,     ///< `register`.
  ThreadLocal,  ///< `thread_local`.
  Auto,         ///< `auto` storage duration.
};

/// @brief Where a symbol is declared/defined.
///
/// `file_path` is resolved to a `files.id` at write time so the IR stays
/// serializable without pointers.
struct SourceLocation {
  std::string file_path;  ///< Absolute, normalized path; empty when unknown.
  unsigned line = 0;      ///< 1-based line number; 0 when unknown.
  unsigned column = 0;    ///< 1-based column number; 0 when unknown.
};

/// @brief A documented (or undocumented) C++ entity.
///
/// `usr` is the stable identity key (the clang USR of the canonical cursor) and
/// the primary key in SQLite.
struct Symbol {
  std::string usr;             ///< Stable identity (clang USR of the canonical cursor).
  std::string parent_usr;      ///< Enclosing namespace/record USR; empty at TU scope.
  SymbolKind kind = SymbolKind::Unknown;  ///< What kind of entity this is.
  std::string spelling;        ///< Unqualified name.
  std::string qualified_name;  ///< Fully qualified name, e.g. `geo::Circle`.
  std::string display_name;    ///< Name including parameters for overloads.
  std::string signature;       ///< Pretty-printed declaration (functions/methods).
  std::string type_repr;       ///< Spelling of the cursor's type.
  AccessKind access = AccessKind::None;    ///< Access specifier within its parent.
  StorageKind storage = StorageKind::None; ///< Storage class.
  /// True when this row is the definition, not just a declaration. A tag type
  /// whose only declaration is an undocumented forward declaration gets no row
  /// at all, so a `false` here means either a documented forward declaration or
  /// a function/variable a header declares without defining.
  bool is_definition = false;
  bool is_documented = false;  ///< True when the symbol carries a non-empty raw comment.
  std::string content_hash;    ///< Stable hash of semantic fields (feeds M6 caching).
  SourceLocation location;     ///< Declaration/definition location.
};

}  // namespace clangquill::model

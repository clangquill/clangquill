#include "parser/ast_visitor.hpp"

#include <algorithm>
#include <map>
#include <optional>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "hash/content_hash.hpp"
#include "parser/comment_parser.hpp"
#include "parser/cursor_utils.hpp"
#include "parser/doxygen_comment_parser.hpp"
#include "parser/references.hpp"

namespace clangquill::parser {
namespace {

// Comment blocks keyed by (file path, line) of the source line each block
// immediately precedes; used to attach doc comments to macros, which libclang
// does not associate itself. Keyed per file because an umbrella translation
// unit extracts from several files whose line numbers collide.
using DocAboveLine = std::map<std::pair<std::string, unsigned>, std::string>;

// A free-floating comment block that opened with a Doxygen structural command
// (`\class Select`, `\fn ns::f`, ...): documentation for an entity declared
// somewhere else rather than for whatever follows the block. Collected during
// the token pre-scan and resolved once the whole translation unit has been
// visited, since the entity it names has usually not been seen yet.
struct StructuralBlock {
  std::string file;     ///< Normalized path of the file the block was written in.
  std::string command;  ///< The command word, without its `\` or `@`.
  std::string target;   ///< The entity it names, normalized by structural_name().
  std::string raw;      ///< The whole block, verbatim.
};

struct VisitCtx {
  model::ParsedModule* mod;
  std::string parent_usr;
  const std::unordered_set<std::string>* main_files;
  bool trust_main_file;
  // De-dup of symbols/comments by USR, and parameters collected per function
  // so content_hash can include them.
  std::unordered_set<std::string>* seen_symbols;
  std::unordered_set<std::string>* documented;
  std::unordered_map<std::string, std::vector<model::FunctionParameter>>*
      params_by_func;
  std::unordered_set<std::string>* seen_groups;
  DocAboveLine* doc_above_line;
  std::vector<StructuralBlock>* structural;
  const ICommentParser* comment_parser;
  const FileIdSet* main_ids;
};

std::pair<std::string, unsigned> cursor_file_line(CXCursor c) {
  CXFile file = nullptr;
  unsigned line = 0, col = 0, off = 0;
  clang_getSpellingLocation(clang_getCursorLocation(c), &file, &line, &col,
                            &off);
  std::string path = file != nullptr ? to_string(clang_getFileName(file)) : "";
  return {std::move(path), line};
}

bool is_record(model::SymbolKind k) {
  return k == model::SymbolKind::Class || k == model::SymbolKind::Struct ||
         k == model::SymbolKind::Union || k == model::SymbolKind::ClassTemplate;
}

bool is_scope(model::SymbolKind k) {
  return k == model::SymbolKind::Namespace || is_record(k);
}

bool is_function_like(model::SymbolKind k) {
  return k == model::SymbolKind::Function || k == model::SymbolKind::Method ||
         k == model::SymbolKind::Constructor ||
         k == model::SymbolKind::Destructor ||
         k == model::SymbolKind::FunctionTemplate;
}

void fill_location(CXCursor c, model::Symbol& sym) {
  CXSourceLocation loc = clang_getCursorLocation(c);
  CXFile file;
  unsigned line = 0, column = 0, offset = 0;
  clang_getFileLocation(loc, &file, &line, &column, &offset);
  if (file != nullptr) {
    // Normalized: libclang spells a file by the path it was reached with, and a
    // location that says "./Eigen/src/Core/Matrix.h" means something different
    // to whoever reopens the IR from another directory.
    sym.location.file_path = normalized_path(to_string(clang_getFileName(file)));
  }
  sym.location.line = line;
  sym.location.column = column;
}

// Records parameter @p arg at @p index of @p usr: its FunctionParameter row
// and its ParamType reference.
void add_parameter(VisitCtx& ctx, const std::string& usr, int index,
                   CXCursor arg) {
  model::FunctionParameter p;
  p.function_usr = usr;
  p.index = index;
  p.name = spelling(arg);
  p.type_repr = to_string(clang_getTypeSpelling(clang_getCursorType(arg)));
  // Recovered from the declaration tokens: libclang exposes the default
  // argument only as a child expression cursor, with no API for its text.
  p.default_value = param_default(arg);
  (*ctx.params_by_func)[usr].push_back(p);
  ctx.mod->parameters.push_back(p);

  ctx.mod->references.push_back(make_type_ref(
      usr, model::RefKind::ParamType, clang_getCursorType(arg), index));
}

// Extracts function parameters and type references for a function-like cursor.
void extract_function_details(CXCursor c, const std::string& usr,
                              VisitCtx& ctx) {
  // clang_getCursorResultType resolves the return type directly from the
  // cursor and, unlike clang_getResultType(clang_getCursorType(c)), works for
  // CXCursor_FunctionTemplate too (a function template's clang_getCursorType
  // does not describe a function type). Skip for constructors/destructors,
  // which report void and have no meaningful return type.
  CXType result = clang_getCursorResultType(c);
  if (result.kind != CXType_Invalid && result.kind != CXType_Void) {
    ctx.mod->references.push_back(
        make_type_ref(usr, model::RefKind::ReturnType, result, -1));
  }

  int n = clang_Cursor_getNumArguments(c);
  if (n >= 0) {
    for (int i = 0; i < n; ++i) {
      add_parameter(ctx, usr, i, clang_Cursor_getArgument(c, i));
    }
    return;
  }

  // clang_Cursor_getNumArguments only answers for FunctionDecl/CXXMethod/
  // constructor/destructor cursors; it returns -1 for CXCursor_FunctionTemplate,
  // whose parameters are still exposed as CXCursor_ParmDecl children.
  struct ParamCtx {
    VisitCtx* ctx;
    const std::string* usr;
    int index;
  } pctx{&ctx, &usr, 0};

  clang_visitChildren(
      c,
      [](CXCursor child, CXCursor, CXClientData data) {
        auto& p = *static_cast<ParamCtx*>(data);
        if (clang_getCursorKind(child) != CXCursor_ParmDecl) {
          return CXChildVisit_Continue;
        }
        add_parameter(*p.ctx, *p.usr, p.index++, child);
        return CXChildVisit_Continue;
      },
      &pctx);
}

void extract_enum(CXCursor enum_cursor, const std::string& enum_usr,
                  VisitCtx& ctx);

void extract_base_classes(CXCursor record, const std::string& usr,
                          VisitCtx& ctx);

void extract_template_parameters(CXCursor c, const std::string& usr,
                                 VisitCtx& ctx);

void extract_friends(CXCursor record, const std::string& usr, VisitCtx& ctx);

void register_symbol_groups(VisitCtx& ctx, const std::string& usr,
                            const std::string& raw);

CXChildVisitResult visit(CXCursor c, CXCursor parent, CXClientData data);

// First whitespace-delimited token of a string (e.g. a group id from
// "mygroup My Title").
std::string first_token(const std::string& s) {
  std::size_t a = s.find_first_not_of(" \t\r\n");
  if (a == std::string::npos) return {};
  std::size_t b = s.find_first_of(" \t\r\n", a);
  return s.substr(a, b == std::string::npos ? std::string::npos : b - a);
}

// True for the kinds whose non-defining declaration is a forward declaration
// rather than the API a header publishes. A function or variable declared and
// not defined is exactly what a header is for; a tag declared and not defined
// carries nothing.
bool is_forward_declarable(model::SymbolKind kind) {
  return is_record(kind) || kind == model::SymbolKind::Enum;
}

// True when @p c carries a doc comment of its *own*.
//
// clang_Cursor_getRawCommentText answers for one declaration with a comment
// written on another declaration of the same entity elsewhere in the
// translation unit. That inheritance is what keeps `is_documented` true across
// an umbrella batch, and it also means the raw text cannot say which
// declaration was documented — a forward declaration reports its definition's
// comment whenever both are in the same unit. So ask where the comment sits: it
// belongs to this cursor when it ends in the same file, on or just above the
// line the declaration starts on. Trailing `///<` comments end on that line
// too.
//
// The declaration's start is the start of its *extent*, not the cursor
// location: the latter is where the entity is named, which a template head or
// an attribute puts lines below the comment --
//
//     /// An opaque handle type.
//     template <typename T>
//     class Foo;
//
// -- and measuring against the `Foo` line called that comment somebody else's,
// dropping a deliberately documented forward declaration.
bool documented_here(CXCursor c) {
  CXSourceRange range = clang_Cursor_getCommentRange(c);
  if (clang_Range_isNull(range) != 0) return false;
  CXFile comment_file = nullptr;
  CXFile cursor_file = nullptr;
  unsigned comment_end = 0, cursor_line = 0, col = 0, off = 0;
  clang_getSpellingLocation(clang_getRangeEnd(range), &comment_file, &comment_end, &col, &off);
  clang_getSpellingLocation(clang_getRangeStart(clang_getCursorExtent(c)),
                            &cursor_file, &cursor_line, &col, &off);
  if (comment_file == nullptr || clang_File_isEqual(comment_file, cursor_file) == 0) return false;
  return comment_end + 1 >= cursor_line;
}

// Records @p raw as @p usr's documentation, once per USR per translation unit.
//
// First writer wins, which is what makes an entity's own comment beat a
// free-floating structural block naming it: the whole cursor visit finishes
// before those blocks are resolved. @p c may be a null cursor, for a block that
// belongs to no cursor at all — the comment parser falls back to parsing the
// raw text in that case.
void record_comment(VisitCtx& ctx, const std::string& usr,
                    const std::string& raw, CXCursor c) {
  if (!ctx.documented->insert(usr).second) return;

  model::CommentModel parsed = ctx.comment_parser->parse(c, raw);

  model::RawComment comment;
  comment.symbol_usr = usr;
  comment.text = raw;
  comment.format = ctx.comment_parser->format();
  ctx.mod->comments.push_back(std::move(comment));

  auto fields = to_comment_fields(usr, parsed);
  ctx.mod->comment_fields.insert(ctx.mod->comment_fields.end(), fields.begin(),
                                 fields.end());

  // `\ingroup` on the comment makes the symbol a member of that group.
  register_symbol_groups(ctx, usr, raw);
}

// Fields a caller recovered itself, for a cursor libclang does not describe.
// Every member is optional: an empty one leaves the cursor's own value.
struct SymbolOverrides {
  std::string signature;
  std::string type_repr;
  std::string display_name;
};

// Records a symbol row (and its details) for a cursor, then recurses into
// scopes. Returns the symbol's USR (empty if skipped).
std::string handle_symbol(CXCursor c, model::SymbolKind kind, VisitCtx& ctx,
                          const SymbolOverrides* overrides = nullptr) {
  std::string usr = canonical_usr(c);
  if (usr.empty()) return {};  // anonymous/local entity: no stable identity

  bool is_def = clang_isCursorDefinition(c);

  // An undocumented forward declaration of a tag type carries nothing and must
  // not produce a row. `symbols.usr` is a primary key written with INSERT OR
  // REPLACE, and modules from separate translation units are concatenated
  // without a USR merge, so two rows for one tag are settled by whichever file
  // is written last — which would hand the definition's identity, location and
  // parent to something like the `class CompileDb` inside
  // `std::unique_ptr<class CompileDb>`. Skipping leaves the definition as the
  // only row, wherever it lives.
  //
  // Tag types only: for a function or a variable a non-defining declaration is
  // exactly what a header publishes, and skipping those would empty the IR.
  // A forward declaration that *is* documented still emits — documenting an
  // opaque type where it is declared is a deliberate thing to write.
  //
  // Before the comment block below, not after: `comments.symbol_usr` is a
  // foreign key onto `symbols.usr`, and a forward declaration inherits its
  // definition's comment (see `documented_here`). Recording that comment and
  // then dropping the row leaves it dangling whenever the definition itself is
  // outside the input set — which is every tag an upstream header defines.
  if (!is_def && is_forward_declarable(kind) && !documented_here(c)) return {};

  // Raw comment (verbatim). Track documented USRs so the symbol flag is set
  // even if the comment is seen on a different (re)declaration.
  std::string raw = to_string(clang_Cursor_getRawCommentText(c));
  // libclang does not attach comments to preprocessor macros; recover the doc
  // comment written immediately above the `#define` from the token scan.
  if (raw.empty() && kind == model::SymbolKind::Macro &&
      ctx.doc_above_line != nullptr) {
    auto it = ctx.doc_above_line->find(cursor_file_line(c));
    if (it != ctx.doc_above_line->end()) raw = it->second;
  }
  if (!raw.empty()) record_comment(ctx, usr, raw, c);

  // De-dup symbols: keep the first row, but let a definition supersede a prior
  // forward declaration.
  if (!ctx.seen_symbols->insert(usr).second) {
    if (is_def) {
      for (auto& existing : ctx.mod->symbols) {
        if (existing.usr == usr && !existing.is_definition) {
          existing.is_definition = true;
          existing.signature = is_function_like(kind) ? pretty_signature(c)
                                                       : existing.signature;
          break;
        }
      }
    }
    return usr;
  }

  model::Symbol sym;
  sym.usr = usr;
  sym.parent_usr = ctx.parent_usr;
  sym.kind = kind;
  sym.spelling = spelling(c);
  sym.qualified_name = qualified_name(c);
  sym.display_name = display_name(c);
  sym.type_repr = to_string(clang_getTypeSpelling(clang_getCursorType(c)));
  sym.access = map_access(c);
  sym.storage = map_storage(c);
  sym.is_definition = is_def;
  sym.is_documented = ctx.documented->count(usr) != 0;
  if (is_function_like(kind)) {
    sym.signature = pretty_signature(c);
  } else if (kind == model::SymbolKind::Macro) {
    sym.signature = macro_signature(c);
  } else if (kind == model::SymbolKind::ClassTemplate ||
             kind == model::SymbolKind::Concept) {
    // Store just the "template<...>" head; the generator joins it with the
    // qualified name (and base clause) to form the domain directive argument.
    sym.signature = template_head(c, nullptr);
  }
  if (overrides != nullptr) {
    if (!overrides->signature.empty()) sym.signature = overrides->signature;
    if (!overrides->type_repr.empty()) sym.type_repr = overrides->type_repr;
    if (!overrides->display_name.empty()) {
      sym.display_name = overrides->display_name;
    }
  }
  fill_location(c, sym);

  if (is_function_like(kind)) extract_function_details(c, usr, ctx);
  if (is_record(kind)) {
    extract_base_classes(c, usr, ctx);
    extract_friends(c, usr, ctx);
  }
  if (kind == model::SymbolKind::ClassTemplate ||
      kind == model::SymbolKind::FunctionTemplate ||
      kind == model::SymbolKind::TypeAlias ||
      kind == model::SymbolKind::Concept) {
    extract_template_parameters(c, usr, ctx);
  }
  if (kind == model::SymbolKind::Enum) extract_enum(c, usr, ctx);

  if (kind == model::SymbolKind::Typedef) {
    CXType u = clang_getTypedefDeclUnderlyingType(c);
    ctx.mod->references.push_back(
        make_type_ref(usr, model::RefKind::UnderlyingType, u, 0));
  } else if (kind == model::SymbolKind::Field ||
             kind == model::SymbolKind::Variable) {
    model::RefKind rk = kind == model::SymbolKind::Field
                            ? model::RefKind::FieldType
                            : model::RefKind::VariableType;
    // A variable template's cursor describes the template, not the declaration,
    // so it has no type of its own; a reference to nothing is only noise.
    if (CXType type = clang_getCursorType(c); type.kind != CXType_Invalid) {
      ctx.mod->references.push_back(make_type_ref(usr, rk, type, 0));
    }
  }

  ctx.mod->symbols.push_back(std::move(sym));
  return usr;
}

// True when the enum declaration spells its own underlying type
// (`enum class E : std::uint8_t`) rather than leaving it to the implementation.
//
// libclang has no query for this: clang_getEnumDeclIntegerType answers with the
// type the implementation chose for an enum that fixes none, and with `int` for
// both `enum class E` and `enum class E : int`. The `:` introducing the type is
// what tells the two apart, so the declaration head -- everything up to the
// first enumerator -- is tokenized to look for one. A qualified type name
// spells `::` as a single token, so a bare `:` there is unambiguous.
bool has_fixed_underlying_type(CXCursor enum_cursor) {
  CXSourceRange extent = clang_getCursorExtent(enum_cursor);
  if (clang_Range_isNull(extent) != 0) return false;

  // Stop at the first enumerator so a large enum body is not tokenized (and so
  // a `? :` in an enumerator's initializer cannot be mistaken for the type's).
  CXSourceLocation head_end = clang_getRangeEnd(extent);
  clang_visitChildren(
      enum_cursor,
      [](CXCursor child, CXCursor, CXClientData data) {
        if (clang_getCursorKind(child) != CXCursor_EnumConstantDecl) {
          return CXChildVisit_Continue;
        }
        *static_cast<CXSourceLocation*>(data) =
            clang_getRangeStart(clang_getCursorExtent(child));
        return CXChildVisit_Break;
      },
      &head_end);

  CXTranslationUnit tu = clang_Cursor_getTranslationUnit(enum_cursor);
  CXSourceRange head = clang_getRange(clang_getRangeStart(extent), head_end);
  CXToken* tokens = nullptr;
  unsigned count = 0;
  clang_tokenize(tu, head, &tokens, &count);
  bool fixed = false;
  for (unsigned t = 0; t < count && !fixed; ++t) {
    fixed = clang_getTokenKind(tokens[t]) == CXToken_Punctuation &&
            to_string(clang_getTokenSpelling(tu, tokens[t])) == ":";
  }
  if (tokens != nullptr) clang_disposeTokens(tu, tokens, count);
  return fixed;
}

void extract_enum(CXCursor enum_cursor, const std::string& enum_usr,
                  VisitCtx& ctx) {
  bool is_signed = true;
  CXType underlying = clang_getEnumDeclIntegerType(enum_cursor);
  // Canonicalized first: the underlying type is written through a typedef at
  // least as often as with a builtin keyword, and `std::uint64_t` arrives as
  // CXType_Typedef (or CXType_Elaborated), neither of which is a case below.
  // Reading that sugar as signed stores `Max = 0xFFFFFFFFFFFFFFFF` as -1.
  switch (clang_getCanonicalType(underlying).kind) {
    case CXType_Bool:
    case CXType_Char_U:
    case CXType_UChar:
    case CXType_Char16:
    case CXType_Char32:
    case CXType_UShort:
    case CXType_UInt:
    case CXType_ULong:
    case CXType_ULongLong:
    case CXType_UInt128:
      is_signed = false;
      break;
    default:
      break;
  }

  // The written underlying type is an edge like any other type mention, and the
  // only one RefKind::EnumIntegerType is for. It is recorded with the sugar the
  // author wrote (`std::uint8_t`, not `unsigned char`), matching every other
  // reference. Only a *fixed* type is recorded: the implementation-chosen one
  // is not something the header says.
  if (has_fixed_underlying_type(enum_cursor)) {
    ctx.mod->references.push_back(make_type_ref(
        enum_usr, model::RefKind::EnumIntegerType, underlying, 0));
  }

  struct EnumCtx {
    model::ParsedModule* mod;
    std::string enum_usr;
    bool is_signed;
    int index;
  } ectx{ctx.mod, enum_usr, is_signed, 0};

  clang_visitChildren(
      enum_cursor,
      [](CXCursor child, CXCursor, CXClientData data) {
        auto& e = *static_cast<EnumCtx*>(data);
        if (clang_getCursorKind(child) != CXCursor_EnumConstantDecl) {
          return CXChildVisit_Continue;
        }
        model::Enumerator en;
        en.usr = canonical_usr(child);
        en.enum_usr = e.enum_usr;
        en.name = spelling(child);
        en.index = e.index++;
        en.value_is_signed = e.is_signed;
        if (e.is_signed) {
          en.value = clang_getEnumConstantDeclValue(child);
        } else {
          en.value = static_cast<std::int64_t>(
              clang_getEnumConstantDeclUnsignedValue(child));
        }
        e.mod->enumerators.push_back(std::move(en));
        return CXChildVisit_Continue;
      },
      &ectx);
}

void extract_base_classes(CXCursor record, const std::string& usr,
                          VisitCtx& ctx) {
  struct BaseCtx {
    model::ParsedModule* mod;
    std::string from_usr;
    int index;
  } bctx{ctx.mod, usr, 0};

  clang_visitChildren(
      record,
      [](CXCursor child, CXCursor, CXClientData data) {
        auto& b = *static_cast<BaseCtx*>(data);
        if (clang_getCursorKind(child) != CXCursor_CXXBaseSpecifier) {
          return CXChildVisit_Continue;
        }
        model::Reference ref = make_type_ref(b.from_usr,
                                             model::RefKind::BaseClass,
                                             clang_getCursorType(child),
                                             b.index++);
        ref.access = map_access(child);
        b.mod->references.push_back(std::move(ref));
        return CXChildVisit_Continue;
      },
      &bctx);
}

void extract_template_parameters(CXCursor c, const std::string& usr,
                                 VisitCtx& ctx) {
  // Default arguments are not exposed by any libclang API; recover them from
  // the declaration tokens (aligned to template-parameter order).
  std::vector<std::string> defaults;
  template_head(c, &defaults);

  struct TpCtx {
    model::ParsedModule* mod;
    std::string owner;
    const std::vector<std::string>* defaults;
    int index;
  } tctx{ctx.mod, usr, &defaults, 0};

  clang_visitChildren(
      c,
      [](CXCursor child, CXCursor, CXClientData data) {
        auto& t = *static_cast<TpCtx*>(data);
        model::TemplateParameter::Kind kind;
        switch (clang_getCursorKind(child)) {
          case CXCursor_TemplateTypeParameter:
            kind = model::TemplateParameter::Kind::Type;
            break;
          case CXCursor_NonTypeTemplateParameter:
            kind = model::TemplateParameter::Kind::NonType;
            break;
          case CXCursor_TemplateTemplateParameter:
            kind = model::TemplateParameter::Kind::Template;
            break;
          default:
            return CXChildVisit_Continue;
        }
        model::TemplateParameter tp;
        tp.owner_usr = t.owner;
        tp.index = t.index;
        tp.kind = kind;
        tp.name = spelling(child);
        if (kind == model::TemplateParameter::Kind::NonType) {
          tp.type_repr =
              to_string(clang_getTypeSpelling(clang_getCursorType(child)));
        }
        if (t.index < static_cast<int>(t.defaults->size())) {
          tp.default_repr = (*t.defaults)[t.index];
        }
        t.mod->template_parameters.push_back(std::move(tp));
        ++t.index;
        return CXChildVisit_Continue;
      },
      &tctx);
}

// Whether @p friend_decl (a CXCursor_FriendDecl) gives its befriended
// function a body right here -- the "hidden friend" idiom -- rather than
// merely declaring it.
//
// This can't be asked of the FriendDecl's function child directly:
// translation units here are parsed with CXTranslationUnit_SkipFunctionBodies
// (a deliberate perf tradeoff, since this tool only needs signatures and
// docs), which makes libclang report a body-less clang_getCursorExtent and
// clang_isCursorDefinition()==false for every function -- defined or not.
// Raw tokenization of a range that isn't itself truncated still sees the
// body, though, so @p record_tokens holds the enclosing record's tokens
// (comments excluded, tokenized once for the whole record) as (file offset,
// spelling) pairs; the answer is just whichever one comes right after this
// FriendDecl's own (truncated) extent.
bool friend_decl_has_inline_body(
    CXCursor friend_decl,
    const std::vector<std::pair<unsigned, std::string>>& record_tokens) {
  CXFile file = nullptr;
  unsigned line = 0, col = 0, end_offset = 0;
  clang_getFileLocation(clang_getRangeEnd(clang_getCursorExtent(friend_decl)),
                        &file, &line, &col, &end_offset);
  for (const auto& [offset, spelling] : record_tokens) {
    if (offset < end_offset) continue;
    return spelling == "{";
  }
  return false;
}

void extract_friends(CXCursor record, const std::string& usr, VisitCtx& ctx) {
  CXTranslationUnit tu = clang_Cursor_getTranslationUnit(record);
  CXToken* raw_tokens = nullptr;
  unsigned raw_count = 0;
  clang_tokenize(tu, clang_getCursorExtent(record), &raw_tokens, &raw_count);
  std::vector<std::pair<unsigned, std::string>> record_tokens;
  record_tokens.reserve(raw_count);
  for (unsigned i = 0; i < raw_count; ++i) {
    if (clang_getTokenKind(raw_tokens[i]) == CXToken_Comment) continue;
    CXFile file = nullptr;
    unsigned line = 0, col = 0, offset = 0;
    clang_getFileLocation(clang_getTokenLocation(tu, raw_tokens[i]), &file,
                          &line, &col, &offset);
    record_tokens.emplace_back(
        offset, to_string(clang_getTokenSpelling(tu, raw_tokens[i])));
  }
  if (raw_tokens != nullptr) clang_disposeTokens(tu, raw_tokens, raw_count);

  struct FriendCtx {
    VisitCtx* ctx;
    const std::vector<std::pair<unsigned, std::string>>* record_tokens;
    std::string from_usr;
    int index;
  } fctx{&ctx, &record_tokens, usr, 0};

  clang_visitChildren(
      record,
      [](CXCursor child, CXCursor, CXClientData data) {
        auto& f = *static_cast<FriendCtx*>(data);
        if (clang_getCursorKind(child) != CXCursor_FriendDecl) {
          return CXChildVisit_Continue;
        }
        bool has_body = friend_decl_has_inline_body(child, *f.record_tokens);
        // The befriended entity is the FriendDecl's child: a TypeRef for a
        // friend class, or a function declaration for a friend function.
        struct Inner {
          model::Reference ref;
          bool found = false;
          bool has_body = false;
          CXCursor func_def = clang_getNullCursor();
        } inner;
        inner.has_body = has_body;
        clang_visitChildren(
            child,
            [](CXCursor gc, CXCursor, CXClientData d) {
              auto& in = *static_cast<Inner*>(d);
              CXCursorKind gk = clang_getCursorKind(gc);
              if (gk == CXCursor_TypeRef || gk == CXCursor_TemplateRef) {
                CXCursor ref = clang_getCursorReferenced(gc);
                in.ref.to_usr = canonical_usr(ref);
                std::string qn = qualified_name(ref);
                in.ref.to_spelling = qn.empty() ? spelling(gc) : qn;
                in.ref.is_resolved = !in.ref.to_usr.empty();
                in.found = true;
                return CXChildVisit_Break;
              }
              if (gk == CXCursor_FunctionDecl || gk == CXCursor_CXXMethod ||
                  gk == CXCursor_FunctionTemplate) {
                in.ref.to_usr = canonical_usr(gc);
                in.ref.to_spelling = display_name(gc);
                in.ref.is_resolved = !in.ref.to_usr.empty();
                in.found = true;
                // A hidden friend: this function is never reached by
                // ordinary top-level traversal (a FriendDecl child maps to
                // Unknown in map_kind and is pruned without descent), so it
                // gets no symbol unless we give it one here.
                if (in.has_body) in.func_def = gc;
                return CXChildVisit_Break;
              }
              return CXChildVisit_Continue;
            },
            &inner);
        if (inner.found) {
          inner.ref.from_usr = f.from_usr;
          inner.ref.kind = model::RefKind::Friend;
          inner.ref.ordinal = f.index++;
          f.ctx->mod->references.push_back(std::move(inner.ref));
        }
        if (!clang_Cursor_isNull(inner.func_def)) {
          // Attributed to the enclosing namespace scope, as Doxygen does:
          // f.ctx->parent_usr is still the record's own parent here, since
          // that's the ctx handle_symbol(record, ...) was itself called
          // with, before it descends into the record's own scope.
          model::SymbolKind fk = map_kind(clang_getCursorKind(inner.func_def));
          if (fk != model::SymbolKind::Unknown) {
            handle_symbol(inner.func_def, fk, *f.ctx);
          }
        }
        return CXChildVisit_Continue;
      },
      &fctx);
}

// Ensures a (possibly stub) group row exists for `id`, so members and pages can
// reference it even when its `\defgroup` block was not captured.
void ensure_group(VisitCtx& ctx, const std::string& id) {
  if (id.empty() || !ctx.seen_groups->insert(id).second) return;
  model::Group g;
  g.id = id;
  g.title = id;
  ctx.mod->groups.push_back(std::move(g));
}

void register_symbol_groups(VisitCtx& ctx, const std::string& usr,
                            const std::string& raw) {
  // libclang's parsed-comment tree does not surface `\ingroup`, so recover the
  // membership from a raw scan of the symbol's own comment.
  model::CommentModel cm = DoxygenCommentParser::parse_raw_text(raw);
  auto it = cm.custom.find("ingroup");
  if (it == cm.custom.end()) return;
  // `\ingroup` accepts several space-separated group ids; register each.
  for (const std::string& v : it->second) {
    std::size_t start = v.find_first_not_of(" \t\r\n");
    while (start != std::string::npos) {
      std::size_t end = v.find_first_of(" \t\r\n", start);
      std::string id =
          v.substr(start, end == std::string::npos ? end : end - start);
      ensure_group(ctx, id);
      model::GroupMember member;
      member.group_id = id;
      member.member_usr = usr;
      member.ordinal = static_cast<int>(ctx.mod->group_members.size());
      ctx.mod->group_members.push_back(std::move(member));
      start = v.find_first_not_of(" \t\r\n", end);
    }
  }
}

// Scans one raw comment for `\defgroup`/`\addtogroup` definitions. Free-floating
// group blocks attach to no cursor, so they are recovered by tokenizing the
// translation unit and feeding each comment token here. Ordinary doc comments
// (no group-definition command) produce nothing.
// The Doxygen commands that retarget a block onto an entity declared elsewhere.
// `relates` is deliberately absent: it appears inside a comment already attached
// to a free function and only *associates* it with a class, so retargeting it
// would take that function's own documentation away.
bool is_structural(const std::string& cmd) {
  return cmd == "class" || cmd == "struct" || cmd == "union" ||
         cmd == "enum" || cmd == "namespace" || cmd == "fn" || cmd == "var" ||
         cmd == "typedef";
}

// Reduces a structural command's argument to a qualified name.
//
// Doxygen accepts a whole declaration after `\fn`, so the argument can carry a
// parameter list and template arguments — `DenseBase<Derived>::minCoeff(IndexType*
// rowId) const` has to come out as `DenseBase::minCoeff` to stand a chance of
// matching a symbol's qualified_name, which is built from cursor spellings and
// so carries neither.
std::string structural_name(const std::string& rest) {
  std::string s = rest.substr(0, rest.find('('));
  std::string out;
  int depth = 0;
  for (char ch : s) {
    if (ch == '<') {
      ++depth;
    } else if (ch == '>') {
      if (depth > 0) --depth;
    } else if (depth == 0) {
      out += ch;
    }
  }
  out = first_token(out);
  if (out.rfind("::", 0) == 0) out.erase(0, 2);
  return out;
}

// Whether a symbol of kind @p k is the sort of entity @p cmd documents.
// Record kinds are pooled on purpose: Eigen writes `\class` for a `struct`
// (util/ForwardDeclarations.h) and Doxygen accepts it.
bool structural_kind_matches(const std::string& cmd, model::SymbolKind k) {
  if (cmd == "class" || cmd == "struct" || cmd == "union") return is_record(k);
  if (cmd == "fn") return is_function_like(k);
  if (cmd == "namespace") return k == model::SymbolKind::Namespace;
  if (cmd == "enum") return k == model::SymbolKind::Enum;
  if (cmd == "var") {
    return k == model::SymbolKind::Variable || k == model::SymbolKind::Field;
  }
  if (cmd == "typedef") {
    return k == model::SymbolKind::Typedef || k == model::SymbolKind::TypeAlias;
  }
  return false;
}

void scan_group_definitions(const std::string& raw, const std::string& file,
                            VisitCtx& ctx) {
  std::string line;
  model::Group* current = nullptr;
  std::size_t i = 0;
  auto clean = [](std::string s) {
    std::size_t a = s.find_first_not_of(" \t\r");
    if (a == std::string::npos) return std::string{};
    s = s.substr(a);
    std::size_t m = 0;
    while (m < s.size() &&
           (s[m] == '/' || s[m] == '*' || s[m] == '!' || s[m] == '<')) {
      ++m;
    }
    s = s.substr(m);
    // Trim trailing whitespace first so a trailing `*/` is stripped even when
    // followed by spaces or a carriage return (e.g. ` * text */ `).
    std::size_t b = s.find_last_not_of(" \t\r");
    if (b != std::string::npos) s = s.substr(0, b + 1);
    if (s.size() >= 2 && s.compare(s.size() - 2, 2, "*/") == 0) {
      s.erase(s.size() - 2);
    }
    a = s.find_first_not_of(" \t\r");
    b = s.find_last_not_of(" \t\r");
    return a == std::string::npos ? std::string{} : s.substr(a, b - a + 1);
  };

  auto handle_line = [&](const std::string& rawline) {
    std::string l = clean(rawline);
    if (!l.empty() && (l[0] == '@' || l[0] == '\\')) {
      std::size_t e = l.find_first_of(" \t", 1);
      std::string cmd = l.substr(1, (e == std::string::npos ? l.size() : e) - 1);
      std::string rest = e == std::string::npos ? std::string{} : l.substr(e + 1);
      std::size_t ra = rest.find_first_not_of(" \t");
      rest = ra == std::string::npos ? std::string{} : rest.substr(ra);
      if (cmd == "defgroup" || cmd == "addtogroup") {
        std::string id = first_token(rest);
        if (id.empty()) return;
        std::string title = rest.substr(id.size());
        std::size_t ta = title.find_first_not_of(" \t");
        title = ta == std::string::npos ? id : title.substr(ta);
        if (ctx.seen_groups->insert(id).second) {
          model::Group g;
          g.id = id;
          g.title = title;
          ctx.mod->groups.push_back(std::move(g));
          current = &ctx.mod->groups.back();
        } else {
          current = nullptr;
          for (auto& g : ctx.mod->groups) {
            if (g.id == id) {
              current = &g;
              break;
            }
          }
        }
      } else if (cmd == "ingroup" && current != nullptr) {
        current->parent_group_id = first_token(rest);
      } else if (is_structural(cmd) && ctx.structural != nullptr) {
        // The prose below belongs to the entity this block names, not to a
        // group defined earlier in the same block — clearing `current` also
        // stops it leaking into that group's description.
        current = nullptr;
        std::string target = structural_name(rest);
        if (!target.empty()) {
          ctx.structural->push_back({file, cmd, std::move(target), raw});
        }
      }
      return;
    }
    if (current != nullptr && !l.empty()) {
      if (current->brief.empty()) {
        current->brief = l;
      } else {
        if (!current->detail.empty()) current->detail += ' ';
        current->detail += l;
      }
    }
  };

  while (i <= raw.size()) {
    if (i == raw.size() || raw[i] == '\n') {
      handle_line(line);
      line.clear();
    } else {
      line.push_back(raw[i]);
    }
    ++i;
  }
}

unsigned token_line(CXTranslationUnit tu, CXToken token, bool end) {
  CXSourceRange r = clang_getTokenExtent(tu, token);
  CXSourceLocation loc = end ? clang_getRangeEnd(r) : clang_getRangeStart(r);
  unsigned line = 0, col = 0, off = 0;
  clang_getSpellingLocation(loc, nullptr, &line, &col, &off);
  return line;
}

// Where a token starts, in 1-based line and column.
struct TokenStart {
  unsigned line = 0;
  unsigned column = 0;
};

TokenStart token_start(CXTranslationUnit tu, CXToken token) {
  TokenStart pos;
  unsigned off = 0;
  CXSourceRange extent = clang_getTokenExtent(tu, token);
  clang_getSpellingLocation(clang_getRangeStart(extent), nullptr, &pos.line,
                            &pos.column, &off);
  return pos;
}

// A file's lines, for the two questions about the source text the token stream
// cannot answer: whether a comment is the first thing on its line, and which
// line below a comment block is the first that carries anything.
class LineIndex {
 public:
  explicit LineIndex(std::string_view contents) {
    std::size_t start = 0;
    for (;;) {
      std::size_t nl = contents.find('\n', start);
      lines_.push_back(contents.substr(
          start, nl == std::string_view::npos ? std::string_view::npos
                                              : nl - start));
      if (nl == std::string_view::npos) break;
      start = nl + 1;
    }
  }

  /// True when everything before @p column on @p line is whitespace.
  bool starts_line(unsigned line, unsigned column) const {
    if (column <= 1) return true;
    std::string_view text = at(line);
    const std::size_t upto = std::min<std::size_t>(column - 1, text.size());
    return is_blank(text.substr(0, upto));
  }

  /// The first line at or below @p line that is not whitespace-only.
  unsigned next_content_line(unsigned line) const {
    while (line <= lines_.size() && is_blank(at(line))) ++line;
    return line;
  }

 private:
  std::string_view at(unsigned line) const {
    return line >= 1 && line <= lines_.size() ? lines_[line - 1]
                                              : std::string_view{};
  }

  static bool is_blank(std::string_view s) {
    return s.find_first_not_of(" \t\r\f\v") == std::string_view::npos;
  }

  std::vector<std::string_view> lines_;
};

// True for a comment block Doxygen would read as documentation for whatever
// follows it: one opening with a doc marker. A plain `//` or `/* */` block --
// a `// TODO:`, a commented-out line, a license header -- documents nothing,
// and publishing it as the documentation of the `#define` below also marks
// that macro is_documented, which passes it through the documented-only filter
// and onto a rendered page.
//
// The `<` variants (`///<`, `/**<`) are deliberately not markers here: those
// document the *preceding* entity, so a trailing one must not be handed to the
// next declaration down.
bool is_doc_block(const std::string& block) {
  for (std::string_view marker : {"///", "//!", "/**", "/*!"}) {
    if (block.rfind(marker, 0) != 0) continue;
    return block.size() == marker.size() || block[marker.size()] != '<';
  }
  return false;
}

// Tokenizes one source file and feeds free-floating comment blocks to the group
// scanner so `\defgroup` definitions (and their following description lines)
// become group rows. Consecutive line comments (`///`) tokenize separately, so
// line-adjacent comment tokens are merged back into one block first.
void scan_free_comments(CXTranslationUnit tu, CXFile file, VisitCtx& ctx) {
  std::size_t size = 0;
  const char* contents = clang_getFileContents(tu, file, &size);
  if (contents == nullptr || size == 0) return;
  const LineIndex lines{std::string_view(contents, size)};
  CXSourceLocation begin = clang_getLocationForOffset(tu, file, 0);
  CXSourceLocation end =
      clang_getLocationForOffset(tu, file, static_cast<unsigned>(size));
  CXSourceRange range = clang_getRange(begin, end);
  std::string file_name = to_string(clang_getFileName(file));
  // Symbol locations are recorded normalized, so a structural block's file has
  // to be too or it can never match its target. doc_above_line keeps the raw
  // spelling: it is looked up with the same raw spelling from cursor_file_line.
  const std::string normalized_file = normalized_path(file_name);

  CXToken* tokens = nullptr;
  unsigned count = 0;
  clang_tokenize(tu, range, &tokens, &count);

  std::string block;
  unsigned last_line = 0;
  auto flush = [&]() {
    if (block.empty()) return;
    scan_group_definitions(block, normalized_file, ctx);
    // Record the block against the line it documents, for macro doc lookup --
    // the first line below it that carries anything, since Doxygen attaches a
    // block across the blank lines between it and the declaration it belongs
    // to. Only a block written as documentation is attachable at all.
    if (ctx.doc_above_line != nullptr && is_doc_block(block)) {
      const unsigned documents = lines.next_content_line(last_line + 1);
      (*ctx.doc_above_line)[{file_name, documents}] = block;
    }
    block.clear();
  };
  for (unsigned t = 0; t < count; ++t) {
    if (clang_getTokenKind(tokens[t]) != CXToken_Comment) continue;
    TokenStart start = token_start(tu, tokens[t]);
    // A block is a run of comment tokens on consecutive lines, each of them
    // the first thing on its line. A comment trailing code (`#define A 1  //
    // note`) is not a continuation of the block above: merging it moved the
    // block's key past the very line it documents.
    const bool continues = start.line <= last_line + 1 &&
                           lines.starts_line(start.line, start.column);
    if (!block.empty() && !continues) flush();
    if (!block.empty()) block += '\n';
    block += to_string(clang_getTokenSpelling(tu, tokens[t]));
    last_line = token_line(tu, tokens[t], /*end=*/true);
  }
  flush();

  if (tokens != nullptr) clang_disposeTokens(tu, tokens, count);
}

CXChildVisitResult visit(CXCursor c, CXCursor /*parent*/, CXClientData data) {
  auto& ctx = *static_cast<VisitCtx*>(data);

  if (!in_file(c, *ctx.main_files, *ctx.main_ids, ctx.trust_main_file)) {
    return CXChildVisit_Continue;
  }

  // extern "C" { ... } / extern "C" decl blocks carry no symbol kind of their
  // own and aren't a scope, so the recursion below never reaches inside them.
  // Descend transparently, under the enclosing scope's parent_usr, so the
  // declarations they wrap are still visited.
  if (clang_getCursorKind(c) == CXCursor_LinkageSpec) {
    clang_visitChildren(c, visit, &ctx);
    return CXChildVisit_Continue;
  }

  // libclang gives neither variable templates nor deduction guides a cursor
  // kind of their own: both arrive as CXCursor_UnexposedDecl, which map_kind()
  // answers Unknown and the walk below skips without descending. A variable
  // template is documented C++ -- `inline constexpr bool is_foo_v = ...` is the
  // modern trait spelling -- so it is recovered from the declaration text and
  // recorded as a variable carrying a template head.
  //
  // The other declarations behind this cursor kind stay out: a deduction guide
  // is spelled `<deduction guide for X>` and has no directive to render into,
  // and a namespace alias or using-declaration names an entity documented where
  // it was declared rather than introducing one.
  if (clang_getCursorKind(c) == CXCursor_UnexposedDecl) {
    if (std::optional<VariableTemplate> var = variable_template(c)) {
      SymbolOverrides overrides;
      overrides.signature = var->head;
      overrides.type_repr = var->type_repr;
      // A specialization shares the primary's spelling, so give it the same
      // `name<args>` display name libclang gives a class specialization: that
      // is what keeps the two apart in the generated docs.
      if (!var->spec_args.empty()) {
        overrides.display_name = spelling(c) + var->spec_args;
      }
      handle_symbol(c, model::SymbolKind::Variable, ctx, &overrides);
    }
    return CXChildVisit_Continue;
  }

  // A *templated* deduction guide is exposed, as a FunctionTemplate whose
  // spelling is that same `<deduction guide for X>`; drop it for the same
  // reason rather than emitting a symbol under a name that is not one.
  if (is_deduction_guide(c)) return CXChildVisit_Continue;

  model::SymbolKind kind = map_kind(clang_getCursorKind(c));
  if (kind == model::SymbolKind::Unknown) return CXChildVisit_Continue;

  // Specializations arrive by their tag kind, so map_kind() answers
  // Struct/Class/Union for them and they would be recorded with no template
  // head under the primary template's qualified name -- the name the primary
  // itself occupies.
  //
  // A *full* specialization (`template <> struct Traits<int>`) becomes a
  // ClassTemplate: that gives it the head (`template<>`) and, with the argument
  // list libclang puts in its display name, an identity of its own. Partial
  // specializations already arrive as their own cursor kind and land here
  // unchanged.
  //
  // An explicit instantiation (`template struct Traits<char>;`, or the `extern
  // template` form) is dropped instead: it declares no entity, it only asks for
  // code for one the primary template already documents. Recording it would add
  // a second row under that name and emit a declaration the C++ domain rejects
  // for carrying an argument list with no parameter list.
  if (is_record(kind)) {
    switch (specialization_form(c)) {
      case SpecializationForm::Explicit:
        kind = model::SymbolKind::ClassTemplate;
        break;
      case SpecializationForm::Instantiation:
        return CXChildVisit_Continue;
      case SpecializationForm::None:
        break;
    }
  }

  // Compiler builtins and command-line macros are not part of the documented
  // surface; skip them so only macros written in the sources are recorded.
  if (kind == model::SymbolKind::Macro &&
      clang_Cursor_isMacroBuiltin(c) != 0) {
    return CXChildVisit_Continue;
  }

  std::string usr = handle_symbol(c, kind, ctx);

  // Recurse into scopes with this symbol as the parent. Drive recursion
  // explicitly so children always get the correct parent_usr.
  if (is_scope(kind) && !usr.empty()) {
    VisitCtx child = ctx;
    child.parent_usr = usr;
    clang_visitChildren(c, visit, &child);
  }
  return CXChildVisit_Continue;
}

// Attaches each structural block to the entity it names.
//
// Scoped to the file the block was written in, not the translation unit. That is
// deliberate: umbrella batches are 64 inputs wide, so a module-wide lookup would
// resolve or not depending on which batch a target landed in, and never under
// `tu_batch = 1` — exactly the divergence verify.py's isolation check gates on.
// Within one file the symbol set is the same however the inputs were grouped.
// It also keeps a block and its target in one batch, which matters because
// comment_fields rows are appended rather than replaced.
//
// Must run before the finalize loop builds comment_by_usr: that map holds
// pointers into out.comments, and appending here would reallocate it.
void attach_structural_blocks(VisitCtx& ctx,
                              const std::vector<StructuralBlock>& blocks) {
  if (blocks.empty()) return;  // the common case; costs nothing

  // Spelling -> the symbols carrying it. Thrown away with the translation unit,
  // so no index and no schema change.
  std::unordered_map<std::string, std::vector<model::Symbol*>> by_spelling;
  for (auto& sym : ctx.mod->symbols) by_spelling[sym.spelling].push_back(&sym);

  for (const auto& block : blocks) {
    const std::size_t cut = block.target.rfind("::");
    const std::string leaf =
        cut == std::string::npos ? block.target : block.target.substr(cut + 2);
    auto it = by_spelling.find(leaf);
    if (it == by_spelling.end()) continue;

    model::Symbol* found = nullptr;
    bool ambiguous = false;
    for (model::Symbol* sym : it->second) {
      if (sym->location.file_path != block.file) continue;
      if (!structural_kind_matches(block.command, sym->kind)) continue;
      const bool matches =
          sym->qualified_name == block.target ||
          (sym->qualified_name.size() > block.target.size() + 2 &&
           sym->qualified_name.compare(
               sym->qualified_name.size() - block.target.size() - 2,
               block.target.size() + 2, "::" + block.target) == 0);
      if (!matches) continue;
      if (found != nullptr) {
        ambiguous = true;
        break;
      }
      found = sym;
    }
    // Nothing, or more than one candidate: leave it alone. Overloads named by a
    // bare `\fn` are the common case, and guessing between them would put the
    // documentation on the wrong one.
    if (ambiguous || found == nullptr) continue;
    record_comment(ctx, found->usr, block.raw, clang_getNullCursor());
  }
}

}  // namespace

void visit_translation_unit(CXCursor tu_cursor, const std::string& main_file,
                            model::ParsedModule& out) {
  visit_translation_unit(tu_cursor, std::vector<std::string>{main_file},
                         /*trust_main_file=*/true, out);
}

void visit_translation_unit(CXCursor tu_cursor,
                            const std::vector<std::string>& main_files,
                            bool trust_main_file, model::ParsedModule& out) {
  CXTranslationUnit tu = clang_Cursor_getTranslationUnit(tu_cursor);

  std::unordered_set<std::string> seen_symbols;
  std::unordered_set<std::string> documented;
  std::unordered_set<std::string> seen_groups;
  DocAboveLine doc_above_line;
  std::vector<StructuralBlock> structural;
  std::unordered_map<std::string, std::vector<model::FunctionParameter>>
      params_by_func;
  DoxygenCommentParser comment_parser;

  std::unordered_set<std::string> main_set(main_files.begin(),
                                           main_files.end());
  FileIdSet main_ids;

  VisitCtx ctx;
  ctx.mod = &out;
  ctx.parent_usr = "";
  ctx.main_files = &main_set;
  ctx.trust_main_file = trust_main_file;
  ctx.seen_symbols = &seen_symbols;
  ctx.documented = &documented;
  ctx.params_by_func = &params_by_func;
  ctx.seen_groups = &seen_groups;
  ctx.doc_above_line = &doc_above_line;
  ctx.structural = &structural;
  ctx.comment_parser = &comment_parser;
  ctx.main_ids = &main_ids;

  // Capture free-floating `\defgroup` blocks first so groups carry their title
  // and description before any `\ingroup` membership creates a stub for them.
  // Each accepted file is scanned once (different spellings of the same file
  // dedupe on libclang's own name for it), and that name joins the filter set
  // so cursor locations match however libclang spells the path.
  std::unordered_set<std::string> scanned;
  for (const auto& mf : main_files) {
    CXFile file = clang_getFile(tu, mf.c_str());
    if (file == nullptr) continue;
    // Identity, not the name, is what decides whether a cursor is in one of
    // these files: libclang answers clang_getFileName with the path a file was
    // *requested* by, so an umbrella including it absolutely and an `-include`
    // prologue reaching it relatively produce two names for one file.
    if (auto id = file_identity(file)) main_ids.insert(*id);
    std::string name = to_string(clang_getFileName(file));
    if (!scanned.insert(name).second) continue;
    main_set.insert(name);
    scan_free_comments(tu, file, ctx);
  }

  clang_visitChildren(tu_cursor, visit, &ctx);

  // After the visit, so every entity a block could name exists, and before the
  // finalize loop below, whose comment_by_usr holds pointers into out.comments.
  attach_structural_blocks(ctx, structural);

  // Finalize: set is_documented and content_hash now that all comments and
  // parameters have been collected. Index comments by USR first so the per
  // symbol lookup is O(1) rather than scanning all comments.
  static const std::vector<model::FunctionParameter> kNoParams;
  std::unordered_map<std::string, const std::string*> comment_by_usr;
  comment_by_usr.reserve(out.comments.size());
  for (const auto& cm : out.comments) comment_by_usr[cm.symbol_usr] = &cm.text;

  for (auto& sym : out.symbols) {
    if (documented.count(sym.usr)) sym.is_documented = true;

    std::string raw;
    if (auto cit = comment_by_usr.find(sym.usr); cit != comment_by_usr.end()) {
      raw = *cit->second;
    }
    auto it = params_by_func.find(sym.usr);
    const auto& params = it != params_by_func.end() ? it->second : kNoParams;
    sym.content_hash = hash::content_hash(sym, params, raw);
  }
}

}  // namespace clangquill::parser

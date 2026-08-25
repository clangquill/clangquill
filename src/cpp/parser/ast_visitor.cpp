#include "parser/ast_visitor.hpp"

#include <map>
#include <unordered_map>
#include <unordered_set>
#include <utility>

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
  const ICommentParser* comment_parser;
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
    sym.location.file_path = to_string(clang_getFileName(file));
  }
  sym.location.line = line;
  sym.location.column = column;
}

// Extracts function parameters and type references for a function-like cursor.
void extract_function_details(CXCursor c, const std::string& usr,
                              VisitCtx& ctx) {
  CXType fn_type = clang_getCursorType(c);
  // Return type reference (skip for constructors/destructors which have none
  // meaningful).
  CXType result = clang_getResultType(fn_type);
  if (result.kind != CXType_Invalid && result.kind != CXType_Void) {
    ctx.mod->references.push_back(
        make_type_ref(usr, model::RefKind::ReturnType, result, -1));
  }

  int n = clang_Cursor_getNumArguments(c);
  for (int i = 0; i < n; ++i) {
    CXCursor arg = clang_Cursor_getArgument(c, i);
    model::FunctionParameter p;
    p.function_usr = usr;
    p.index = i;
    p.name = spelling(arg);
    p.type_repr = to_string(clang_getTypeSpelling(clang_getCursorType(arg)));
    (*ctx.params_by_func)[usr].push_back(p);
    ctx.mod->parameters.push_back(p);

    ctx.mod->references.push_back(make_type_ref(
        usr, model::RefKind::ParamType, clang_getCursorType(arg), i));
  }
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
// line the cursor starts on. Trailing `///<` comments end on that line too.
bool documented_here(CXCursor c) {
  CXSourceRange range = clang_Cursor_getCommentRange(c);
  if (clang_Range_isNull(range) != 0) return false;
  CXFile comment_file = nullptr;
  CXFile cursor_file = nullptr;
  unsigned comment_end = 0, cursor_line = 0, col = 0, off = 0;
  clang_getSpellingLocation(clang_getRangeEnd(range), &comment_file, &comment_end, &col, &off);
  clang_getSpellingLocation(clang_getCursorLocation(c), &cursor_file, &cursor_line, &col, &off);
  if (comment_file == nullptr || clang_File_isEqual(comment_file, cursor_file) == 0) return false;
  return comment_end + 1 >= cursor_line;
}

// Records a symbol row (and its details) for a cursor, then recurses into
// scopes. Returns the symbol's USR (empty if skipped).
std::string handle_symbol(CXCursor c, model::SymbolKind kind, VisitCtx& ctx) {
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
  if (!raw.empty() && ctx.documented->insert(usr).second) {
    model::CommentModel parsed = ctx.comment_parser->parse(c, raw);

    model::RawComment comment;
    comment.symbol_usr = usr;
    comment.text = raw;
    comment.format = ctx.comment_parser->format();
    comment.fields_json = to_fields_json(parsed);
    ctx.mod->comments.push_back(std::move(comment));

    auto fields = to_comment_fields(usr, parsed);
    ctx.mod->comment_fields.insert(ctx.mod->comment_fields.end(),
                                   fields.begin(), fields.end());

    // `\ingroup` on the symbol's comment makes it a member of that group.
    register_symbol_groups(ctx, usr, raw);
  }

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
    ctx.mod->references.push_back(
        make_type_ref(usr, rk, clang_getCursorType(c), 0));
  }

  ctx.mod->symbols.push_back(std::move(sym));
  return usr;
}

void extract_enum(CXCursor enum_cursor, const std::string& enum_usr,
                  VisitCtx& ctx) {
  bool is_signed = true;
  CXType underlying = clang_getEnumDeclIntegerType(enum_cursor);
  switch (underlying.kind) {
    case CXType_UChar:
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

void extract_friends(CXCursor record, const std::string& usr, VisitCtx& ctx) {
  struct FriendCtx {
    model::ParsedModule* mod;
    std::string from_usr;
    int index;
  } fctx{ctx.mod, usr, 0};

  clang_visitChildren(
      record,
      [](CXCursor child, CXCursor, CXClientData data) {
        auto& f = *static_cast<FriendCtx*>(data);
        if (clang_getCursorKind(child) != CXCursor_FriendDecl) {
          return CXChildVisit_Continue;
        }
        // The befriended entity is the FriendDecl's child: a TypeRef for a
        // friend class, or a function declaration for a friend function.
        struct Inner {
          model::Reference ref;
          bool found = false;
        } inner;
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
                return CXChildVisit_Break;
              }
              return CXChildVisit_Continue;
            },
            &inner);
        if (inner.found) {
          inner.ref.from_usr = f.from_usr;
          inner.ref.kind = model::RefKind::Friend;
          inner.ref.ordinal = f.index++;
          f.mod->references.push_back(std::move(inner.ref));
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
void scan_group_definitions(const std::string& raw, VisitCtx& ctx) {
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

// Tokenizes one source file and feeds free-floating comment blocks to the group
// scanner so `\defgroup` definitions (and their following description lines)
// become group rows. Consecutive line comments (`///`) tokenize separately, so
// line-adjacent comment tokens are merged back into one block first.
void scan_free_comments(CXTranslationUnit tu, CXFile file, VisitCtx& ctx) {
  std::size_t size = 0;
  if (clang_getFileContents(tu, file, &size) == nullptr || size == 0) return;
  CXSourceLocation begin = clang_getLocationForOffset(tu, file, 0);
  CXSourceLocation end =
      clang_getLocationForOffset(tu, file, static_cast<unsigned>(size));
  CXSourceRange range = clang_getRange(begin, end);
  std::string file_name = to_string(clang_getFileName(file));

  CXToken* tokens = nullptr;
  unsigned count = 0;
  clang_tokenize(tu, range, &tokens, &count);

  std::string block;
  unsigned last_line = 0;
  auto flush = [&]() {
    if (block.empty()) return;
    scan_group_definitions(block, ctx);
    // Record the block against the line it precedes, for macro doc lookup.
    if (ctx.doc_above_line != nullptr) {
      (*ctx.doc_above_line)[{file_name, last_line + 1}] = block;
    }
    block.clear();
  };
  for (unsigned t = 0; t < count; ++t) {
    if (clang_getTokenKind(tokens[t]) != CXToken_Comment) continue;
    unsigned start = token_line(tu, tokens[t], /*end=*/false);
    if (!block.empty() && start > last_line + 1) flush();  // blank-line gap
    if (!block.empty()) block += '\n';
    block += to_string(clang_getTokenSpelling(tu, tokens[t]));
    last_line = token_line(tu, tokens[t], /*end=*/true);
  }
  flush();

  if (tokens != nullptr) clang_disposeTokens(tu, tokens, count);
}

CXChildVisitResult visit(CXCursor c, CXCursor /*parent*/, CXClientData data) {
  auto& ctx = *static_cast<VisitCtx*>(data);

  if (!in_file(c, *ctx.main_files, ctx.trust_main_file)) {
    return CXChildVisit_Continue;
  }

  model::SymbolKind kind = map_kind(clang_getCursorKind(c));
  if (kind == model::SymbolKind::Unknown) return CXChildVisit_Continue;

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
  std::unordered_map<std::string, std::vector<model::FunctionParameter>>
      params_by_func;
  DoxygenCommentParser comment_parser;

  std::unordered_set<std::string> main_set(main_files.begin(),
                                           main_files.end());

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
  ctx.comment_parser = &comment_parser;

  // Capture free-floating `\defgroup` blocks first so groups carry their title
  // and description before any `\ingroup` membership creates a stub for them.
  // Each accepted file is scanned once (different spellings of the same file
  // dedupe on libclang's own name for it), and that name joins the filter set
  // so cursor locations match however libclang spells the path.
  std::unordered_set<std::string> scanned;
  for (const auto& mf : main_files) {
    CXFile file = clang_getFile(tu, mf.c_str());
    if (file == nullptr) continue;
    std::string name = to_string(clang_getFileName(file));
    if (!scanned.insert(name).second) continue;
    main_set.insert(name);
    scan_free_comments(tu, file, ctx);
  }

  clang_visitChildren(tu_cursor, visit, &ctx);

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

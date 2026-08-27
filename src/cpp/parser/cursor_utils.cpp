#include "parser/cursor_utils.hpp"

#include <filesystem>
#include <system_error>
#include <vector>

namespace clangquill::parser {

model::SymbolKind map_kind(CXCursorKind kind) {
  switch (kind) {
    case CXCursor_Namespace:
      return model::SymbolKind::Namespace;
    case CXCursor_ClassDecl:
      return model::SymbolKind::Class;
    case CXCursor_StructDecl:
      return model::SymbolKind::Struct;
    case CXCursor_UnionDecl:
      return model::SymbolKind::Union;
    case CXCursor_ClassTemplate:
    case CXCursor_ClassTemplatePartialSpecialization:
      return model::SymbolKind::ClassTemplate;
    case CXCursor_FunctionDecl:
      return model::SymbolKind::Function;
    case CXCursor_FunctionTemplate:
      return model::SymbolKind::FunctionTemplate;
    case CXCursor_CXXMethod:
    case CXCursor_ConversionFunction:
      return model::SymbolKind::Method;
    case CXCursor_Constructor:
      return model::SymbolKind::Constructor;
    case CXCursor_Destructor:
      return model::SymbolKind::Destructor;
    case CXCursor_FieldDecl:
      return model::SymbolKind::Field;
    case CXCursor_VarDecl:
      return model::SymbolKind::Variable;
    case CXCursor_EnumDecl:
      return model::SymbolKind::Enum;
    case CXCursor_TypedefDecl:
      return model::SymbolKind::Typedef;
    case CXCursor_TypeAliasDecl:
    case CXCursor_TypeAliasTemplateDecl:
      return model::SymbolKind::TypeAlias;
    case CXCursor_ConceptDecl:
      return model::SymbolKind::Concept;
    case CXCursor_MacroDefinition:
      return model::SymbolKind::Macro;
    default:
      return model::SymbolKind::Unknown;
  }
}

std::string canonical_usr(CXCursor c) {
  return usr(clang_getCanonicalCursor(c));
}

std::string qualified_name(CXCursor c) {
  std::vector<std::string> parts;
  parts.push_back(spelling(c));
  CXCursor parent = clang_getCursorSemanticParent(c);
  while (!clang_Cursor_isNull(parent)) {
    CXCursorKind pk = clang_getCursorKind(parent);
    if (pk == CXCursor_TranslationUnit || pk == CXCursor_InvalidFile) break;
    std::string s = spelling(parent);
    if (!s.empty()) parts.push_back(s);
    parent = clang_getCursorSemanticParent(parent);
  }
  std::string out;
  for (auto it = parts.rbegin(); it != parts.rend(); ++it) {
    if (!out.empty()) out += "::";
    out += *it;
  }
  return out;
}

std::string pretty_signature(CXCursor c) {
  CXPrintingPolicy policy = clang_getCursorPrintingPolicy(c);
  clang_PrintingPolicy_setProperty(policy, CXPrintingPolicy_TerseOutput, 1);
  clang_PrintingPolicy_setProperty(policy, CXPrintingPolicy_PolishForDeclaration,
                                   1);
  std::string out = to_string(clang_getCursorPrettyPrinted(c, policy));
  clang_PrintingPolicy_dispose(policy);
  return out;
}

namespace {

// Collects the spellings of every token covering a cursor's extent, in order.
std::vector<std::string> cursor_tokens(CXCursor c) {
  std::vector<std::string> out;
  CXTranslationUnit tu = clang_Cursor_getTranslationUnit(c);
  CXSourceRange range = clang_getCursorExtent(c);
  CXToken* tokens = nullptr;
  unsigned count = 0;
  clang_tokenize(tu, range, &tokens, &count);
  out.reserve(count);
  for (unsigned i = 0; i < count; ++i) {
    out.push_back(to_string(clang_getTokenSpelling(tu, tokens[i])));
  }
  if (tokens != nullptr) clang_disposeTokens(tu, tokens, count);
  return out;
}

// Joins a token onto a buffer, inserting a single space unless the buffer is
// empty. Keeps reconstructed text readable without faithfully reproducing the
// original spacing (which libclang does not preserve in tokens).
void append_token(std::string& buf, const std::string& tok) {
  if (!buf.empty()) buf += ' ';
  buf += tok;
}

// How many template-argument lists @p tok closes, given @p depth already open.
//
// clang_tokenize is a raw lex, so the close of `A<B<C>>` arrives as a single
// `>>` token (and `A<B<C<D>>>` as `>>` followed by `>`), matching neither "<"
// nor ">". A `>>` closes two lists only when two are open: with a single list
// open the same spelling is the shift operator of an expression (an NTTP
// default such as `1 >> 4`), whose second `>` would have to close a list that
// was never opened. `<<` needs no mirror rule -- no declaration opens two
// argument lists with one token, so it is always a shift and stays an ordinary
// token.
int angle_closes(const std::string& tok, int depth) {
  if (tok == ">") return 1;
  if (tok == ">>" && depth >= 2) return 2;
  return 0;
}

}  // namespace

std::string macro_signature(CXCursor c) {
  std::string name = spelling(c);
  if (clang_Cursor_isMacroFunctionLike(c) == 0) return name;
  // Function-like: rebuild "NAME(a, b)" from the leading tokens up to the close
  // paren that matches the first one (the body, if any, follows and is ignored).
  std::vector<std::string> toks = cursor_tokens(c);
  std::string params;
  int depth = 0;
  bool started = false;
  for (const std::string& t : toks) {
    if (!started) {
      if (t == "(") {
        started = true;
        depth = 1;
      }
      continue;
    }
    if (t == "(") {
      ++depth;
      params += t;
    } else if (t == ")") {
      if (--depth == 0) break;
      params += t;
    } else if (t == ",") {
      params += ", ";
    } else {
      params += t;
    }
  }
  return started ? name + "(" + params + ")" : name;
}

std::string template_head(CXCursor owner,
                          std::vector<std::string>* defaults_out) {
  std::vector<std::string> toks = cursor_tokens(owner);
  std::vector<std::string> segs;      // full text per top-level parameter
  std::vector<std::string> defaults;  // default text (after top-level '=')
  std::string cur, cur_default;
  bool started = false, done = false, in_default = false;
  int depth = 0;

  auto push_seg = [&]() {
    segs.push_back(cur);
    defaults.push_back(cur_default);
    cur.clear();
    cur_default.clear();
    in_default = false;
  };

  for (const std::string& t : toks) {
    if (done) break;
    if (!started) {
      if (t == "template") started = true;
      continue;
    }
    if (depth == 0) {
      if (t == "<") {
        depth = 1;
        continue;
      }
      break;  // tokens before the '<' that are not 'template' end the head
    }
    if (t == "<") {
      ++depth;
      append_token(cur, t);
      if (in_default) append_token(cur_default, t);
    } else if (int closes = angle_closes(t, depth); closes > 0) {
      for (int i = 0; i < closes; ++i) {
        if (--depth == 0) {
          push_seg();
          done = true;
          break;
        }
        // A `>>` that closes a nested list and then continues is re-emitted as
        // the two `>` it stands for; token text carries no spacing to preserve.
        append_token(cur, ">");
        if (in_default) append_token(cur_default, ">");
      }
    } else if (t == "," && depth == 1) {
      push_seg();
    } else if (t == "=" && depth == 1) {
      append_token(cur, t);
      in_default = true;
    } else {
      append_token(cur, t);
      if (in_default) append_token(cur_default, t);
    }
  }

  if (!started || segs.empty() || (segs.size() == 1 && segs.front().empty())) {
    if (defaults_out != nullptr) defaults_out->clear();
    return "";
  }
  if (defaults_out != nullptr) *defaults_out = defaults;

  std::string head = "template<";
  for (std::size_t i = 0; i < segs.size(); ++i) {
    if (i != 0) head += ", ";
    head += segs[i];
  }
  head += '>';
  return head;
}

std::string param_default(CXCursor param) {
  std::vector<std::string> toks = cursor_tokens(param);
  std::string out;
  bool seen = false;
  int depth = 0;   // `(`, `[` and `{` groups
  int angles = 0;  // template-argument lists, counted apart from the above so
                   // the `>>` rule in angle_closes() sees the angle depth only
  for (const std::string& t : toks) {
    if (!seen) {
      if (t == "=" && depth == 0 && angles == 0) {
        seen = true;
      } else if (t == "(" || t == "[" || t == "{") {
        ++depth;
      } else if (t == ")" || t == "]" || t == "}") {
        --depth;
      } else if (t == "<") {
        ++angles;
      } else if (int closes = angle_closes(t, angles); closes > 0) {
        angles -= closes;
      }
      continue;
    }
    append_token(out, t);
  }
  return out;
}

model::AccessKind map_access(CXCursor c) {
  switch (clang_getCXXAccessSpecifier(c)) {
    case CX_CXXPublic:
      return model::AccessKind::Public;
    case CX_CXXProtected:
      return model::AccessKind::Protected;
    case CX_CXXPrivate:
      return model::AccessKind::Private;
    default:
      return model::AccessKind::None;
  }
}

model::StorageKind map_storage(CXCursor c) {
  switch (clang_Cursor_getStorageClass(c)) {
    case CX_SC_Static:
      return model::StorageKind::Static;
    case CX_SC_Extern:
      return model::StorageKind::Extern;
    case CX_SC_Register:
      return model::StorageKind::Register;
    case CX_SC_Auto:
      return model::StorageKind::Auto;
    default:
      return model::StorageKind::None;
  }
}

bool in_file(CXCursor c, const std::string& main_file) {
  std::unordered_set<std::string> mains;
  if (!main_file.empty()) mains.insert(main_file);
  return in_file(c, mains, FileIdSet{}, /*trust_main_file=*/true);
}

std::string normalized_path(const std::string& path) {
  if (path.empty()) return path;
  std::filesystem::path p(path);
  if (p.is_absolute()) return p.lexically_normal().string();
  std::error_code ec;
  std::filesystem::path abs = std::filesystem::absolute(p, ec);
  if (ec) return path;
  return abs.lexically_normal().string();
}

std::optional<std::array<unsigned long long, 3>> file_identity(CXFile file) {
  if (file == nullptr) return std::nullopt;
  CXFileUniqueID id{};
  if (clang_getFileUniqueID(file, &id) != 0) return std::nullopt;
  return std::array<unsigned long long, 3>{id.data[0], id.data[1], id.data[2]};
}

bool in_file(CXCursor c, const std::unordered_set<std::string>& main_files,
             const FileIdSet& main_ids, bool trust_main_file) {
  CXSourceLocation loc = clang_getCursorLocation(c);
  if (clang_Location_isInSystemHeader(loc)) return false;
  // Primary check: entities declared in the TU's main file. Robust against path
  // spelling differences (relative vs absolute). Skipped for umbrella TUs,
  // whose synthetic main file contains nothing user-visible.
  if (trust_main_file && clang_Location_isFromMainFile(loc)) return true;
  if (main_files.empty() && main_ids.empty()) return false;
  CXFile file;
  unsigned line = 0, column = 0, offset = 0;
  clang_getFileLocation(loc, &file, &line, &column, &offset);
  if (file == nullptr) return false;
  // Identity first: the same file reached under two spellings is one file, and
  // only this notices. The name match stays as a fallback for a spelling
  // libclang never resolved to a file of its own.
  if (auto id = file_identity(file); id && main_ids.count(*id) > 0) return true;
  return main_files.count(to_string(clang_getFileName(file))) > 0;
}

}  // namespace clangquill::parser

#include "parser/cursor_utils.hpp"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <system_error>
#include <vector>

// The token-heuristic layer's contract.
//
// libclang's structured cursor API has no accessor for a template parameter's
// default argument, a variable template's shape (it reports the whole
// declaration as one opaque CXCursor_UnexposedDecl), or a macro's parameter
// list. template_head(), param_default(), variable_template() and
// macro_signature() recover this by re-lexing the cursor's own raw tokens
// (clang_tokenize()) and walking them with a small hand-rolled parser --
// angle_closes() and in_group(), below -- rather than parsing the grammar
// properly. That makes this file the most libclang-version-sensitive code in
// the repo: it depends on clang_tokenize()'s raw-lex splitting of `>>`/`<<`
// (see angle_closes()) and, for the friend-body probe in ast_visitor.cpp
// (friend_decl_has_inline_body()), on exactly how far
// CXTranslationUnit_SkipFunctionBodies truncates a cursor's extent.
//
// What each function recovers:
//   - template_head(): the `template<...>` clause of a template/concept
//     owner, plus (via defaults_out) each parameter's default-argument text.
//     Handles arbitrary angle-bracket nesting split across `<`/`>`/`>>`
//     tokens however the raw lexer grouped them, and a default containing a
//     parenthesized/bracketed/braced subexpression -- `(N > 2)`, a fold
//     expression, a call, a brace-init -- whose own `<`, `>`, `>>`, `,` and
//     `=` are not template-argument-list syntax (in_group()). It does not
//     look past the closing `>` of the parameter list, so a trailing
//     `requires` clause is simply not part of what it returns -- neither
//     included nor a reason to fail.
//   - param_default(): a function/method parameter's default-argument text,
//     found as the first top-level `=` (not inside the parameter's own type,
//     e.g. `enable_if_t<(sizeof(int) > 2), int>`).
//   - variable_template(): a variable template's head, decl-specifiers-and-
//     type, and (on an explicit specialization) trailing argument list, none
//     of which libclang's API exposes for an unexposed declaration.
//   - specialization_form(): explicit specialization vs. explicit
//     instantiation, told apart by whether template_head() finds a head at
//     all (instantiation has none).
//
// When it bails: an owner with no `template` keyword, an unbalanced angle- or
// group-nesting, or a token shape this file's parser doesn't recognize all
// bail to "" (template_head(), param_default(), macro_signature()'s params)
// or std::nullopt (variable_template()) -- never a partial answer. A silently
// *wrong* answer -- a truncated-but-nonempty default, a head missing its last
// parameter -- is the one class of bug this layer must not have; a documented
// bail is fine. tests/cpp/test_token_heuristics_generative.cpp exercises this
// invariant generatively across nested templates, shift and parenthesized
// NTTP defaults, brace-inits, fold expressions and constrained parameters; a
// find that violates it belongs there, not just as an isolated regression
// test. Re-run that suite against each new libclang major as it's packaged.

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

namespace {

// The name a scope contributes to a qualified name.
//
// An anonymous namespace has no spelling of its own, and eliding it would name
// its contents after the *enclosing* scope -- publishing an internal-linkage
// entity as though it were ordinary API of that namespace. So the scope is
// named `@anonymous`: the Sphinx C++ domain spells an anonymous entity with a
// leading `@`, and every qualified name here ends up in a domain directive, so
// clang's own `(anonymous namespace)` would be rejected as a declaration.
// Other unnamed entities (an anonymous struct, union or enum) keep the empty
// spelling the caller already skips.
std::string name_segment(CXCursor c) {
  std::string s = spelling(c);
  if (s.empty() && clang_getCursorKind(c) == CXCursor_Namespace) {
    return "@anonymous";
  }
  return s;
}

}  // namespace

std::string qualified_name(CXCursor c) {
  std::vector<std::string> parts;
  parts.push_back(name_segment(c));
  CXCursor parent = clang_getCursorSemanticParent(c);
  while (!clang_Cursor_isNull(parent)) {
    CXCursorKind pk = clang_getCursorKind(parent);
    if (pk == CXCursor_TranslationUnit || pk == CXCursor_InvalidFile) break;
    std::string s = name_segment(parent);
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
// nor ">". Per [temp.names], an unparenthesized `>>` is *always* split into
// two `>` wherever a template-argument/parameter list could end -- it is not
// an ambiguity the compiler resolves by how many lists happen to be open.
// With two open, both close cleanly. With one, the first `>` closes it and
// the second is a leftover token the grammar has no use for right there --
// which is why a bare `1 >> 4` as an NTTP default does not compile at all
// (unlike `1 << 4`, which never collides with list-closing syntax); it needs
// parens, which route it through in_group() instead of here. So @p depth < 2
// is a shape that never reaches this function from source that actually
// compiled -- returning 0 is a safe default for the unreachable case, not a
// claim that the bare form is valid C++.
int angle_closes(const std::string& tok, int depth) {
  if (tok == ">") return 1;
  if (tok == ">>" && depth >= 2) return 2;
  return 0;
}

// Advances @p paren_depth for one token of a raw token stream -- `(`, `[` and
// `{` open a group, `)`, `]` and `}` close one (never letting it go negative,
// so a stray unmatched closer is a no-op rather than a wrong angle depth for
// everything after it) -- and reports whether @p tok must be read as plain
// text rather than as angle-bracket syntax: either it is one of those six
// bracket tokens, or a group is still open around it.
//
// Every function in this file that walks raw tokens counting `<`/`>`/`>>`
// needs this: a `>` inside a parenthesized NTTP default (`(N > 2)`), a fold
// expression (`(Ns + ... + 0)`), a call (`pick(1, 2)`) or a brace-init
// (`{1, 2}`) is never template-argument-list syntax, and a `,` there is never
// a top-level parameter separator. Skipping this check reads that `>` as
// closing a list that was never opened, or that `,` as ending a parameter
// early -- silently wrong, not a bail.
bool in_group(const std::string& tok, int& paren_depth) {
  if (tok == "(" || tok == "[" || tok == "{") {
    ++paren_depth;
    return true;
  }
  if (tok == ")" || tok == "]" || tok == "}") {
    if (paren_depth > 0) --paren_depth;
    return true;
  }
  return paren_depth > 0;
}

// Joins declaration tokens back into readable text: a space only where two
// tokens would otherwise run together into one identifier, plus one after every
// comma. libclang's tokens carry no spacing of their own, and `Pair < T , int >`
// is a poor thing to put in a rendered declaration.
std::string join_declaration(const std::vector<std::string>& toks) {
  auto is_word = [](char ch) {
    return std::isalnum(static_cast<unsigned char>(ch)) != 0 || ch == '_';
  };
  std::string out;
  for (const std::string& t : toks) {
    if (t.empty()) continue;
    if (!out.empty() && ((is_word(out.back()) && is_word(t.front())) ||
                         out.back() == ',')) {
      out += ' ';
    }
    out += t;
  }
  return out;
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
  int paren_depth = 0;  // see in_group()

  auto push_seg = [&]() {
    segs.push_back(cur);
    defaults.push_back(cur_default);
    cur.clear();
    cur_default.clear();
    in_default = false;
  };
  auto emit = [&](const std::string& t) {
    append_token(cur, t);
    if (in_default) append_token(cur_default, t);
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
    if (in_group(t, paren_depth)) {
      emit(t);
      continue;
    }
    if (t == "<") {
      ++depth;
      emit(t);
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
      emit(t);
      in_default = true;
    } else {
      emit(t);
    }
  }

  // `done` is set only when the outermost `<...>` actually closed. Without
  // it, a shape this loop doesn't fully understand (an unbalanced group, a
  // libclang tokenization this wasn't written against) would still return
  // whatever partial segments it collected -- a head silently missing its
  // last parameter, which is worse than bailing.
  if (!started || segs.empty() || !done) {
    if (defaults_out != nullptr) defaults_out->clear();
    return "";
  }
  // `template <>`: the head of a full explicit specialization. Empty is not the
  // same as absent -- it is what tells the specialization apart from the
  // primary template, and from an explicit instantiation, which writes no head
  // of its own (`template struct Traits<int>;` leaves `segs` empty above).
  if (segs.size() == 1 && segs.front().empty()) {
    if (defaults_out != nullptr) defaults_out->clear();
    return "template<>";
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

bool is_deduction_guide(CXCursor c) {
  // libclang spells a guide `<deduction guide for Wrapper>`, which is not a
  // name anything can be documented or cross-referenced under.
  return spelling(c).rfind("<deduction guide", 0) == 0;
}

std::optional<VariableTemplate> variable_template(CXCursor c) {
  if (clang_getCursorKind(c) != CXCursor_UnexposedDecl) return std::nullopt;
  const std::string name = spelling(c);
  if (name.empty() || is_deduction_guide(c)) return std::nullopt;

  VariableTemplate out;
  out.head = template_head(c, nullptr);
  if (out.head.empty()) return std::nullopt;

  // Walk past the head, then read `<decl-specifiers and type> <name>` and, on an
  // explicit specialization, the argument list that follows the name.
  const std::vector<std::string> toks = cursor_tokens(c);
  std::size_t i = 0;
  while (i < toks.size() && toks[i] != "template") ++i;
  ++i;  // the `template` keyword itself
  int depth = 0;
  int paren_depth = 0;  // see in_group()
  for (; i < toks.size(); ++i) {
    if (in_group(toks[i], paren_depth)) continue;
    if (toks[i] == "<") {
      ++depth;
      continue;
    }
    if (int closes = angle_closes(toks[i], depth); closes > 0) {
      depth -= closes;
      if (depth <= 0) {
        ++i;
        break;
      }
    }
  }

  std::vector<std::string> type_toks;
  bool named = false;
  depth = 0;
  paren_depth = 0;
  for (; i < toks.size(); ++i) {
    if (paren_depth == 0 && depth == 0 && toks[i] == name) {
      named = true;
      ++i;
      break;
    }
    if (in_group(toks[i], paren_depth)) {
      type_toks.push_back(toks[i]);
      continue;
    }
    if (toks[i] == "<") {
      ++depth;
    } else if (int closes = angle_closes(toks[i], depth); closes > 0) {
      depth -= closes;
    }
    type_toks.push_back(toks[i]);
  }
  // No declarator, or a declarator with no type before it: not the shape of a
  // variable template, so leave the cursor to whatever else may recognize it.
  if (!named || type_toks.empty()) return std::nullopt;
  out.type_repr = join_declaration(type_toks);

  if (i < toks.size() && toks[i] == "<") {
    std::vector<std::string> args{toks[i]};
    depth = 1;
    paren_depth = 0;
    for (++i; i < toks.size() && depth > 0; ++i) {
      if (in_group(toks[i], paren_depth)) {
        args.push_back(toks[i]);
        continue;
      }
      if (toks[i] == "<") {
        ++depth;
        args.push_back(toks[i]);
      } else if (int closes = angle_closes(toks[i], depth); closes > 0) {
        depth -= closes;
        args.insert(args.end(), static_cast<std::size_t>(closes), ">");
      } else {
        args.push_back(toks[i]);
      }
    }
    if (depth != 0) return std::nullopt;  // unbalanced: not understood
    out.spec_args = join_declaration(args);
  }
  return out;
}

SpecializationForm specialization_form(CXCursor c) {
  if (clang_Cursor_isNull(clang_getSpecializedCursorTemplate(c)) != 0) {
    return SpecializationForm::None;
  }
  // Both forms report a specialized template, and libclang reports both by the
  // tag's own cursor kind. Only an explicit specialization writes a
  // `template<...>` head of its own -- empty for a full one, the parameters its
  // argument list uses for a partial one -- so the head is what separates them.
  return template_head(c, nullptr).empty() ? SpecializationForm::Instantiation
                                           : SpecializationForm::Explicit;
}

std::string param_default(CXCursor param) {
  std::vector<std::string> toks = cursor_tokens(param);
  std::string out;
  bool seen = false;
  int depth = 0;   // '(', '[' and '{' groups; see in_group()
  int angles = 0;  // template-argument lists opened by the parameter's own
                   // type (e.g. `std::enable_if_t<...>`), so a top-level `=`
                   // inside them (a default nested in a default template
                   // argument) isn't mistaken for this parameter's own `=`.
  for (const std::string& t : toks) {
    if (!seen) {
      if (in_group(t, depth)) {
        // A '<'/'>' inside a group -- `enable_if_t<(sizeof(int) > 2), int>`
        // -- is part of that subexpression, never an angle-list boundary.
      } else if (t == "=" && angles == 0) {
        seen = true;
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

std::string path_lookup_key(const std::string& path) {
#if defined(_WIN32)
  std::string key = path;
  std::transform(key.begin(), key.end(), key.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return key;
#else
  return path;
#endif
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
  // libclang never resolved to a file of its own -- folded case-insensitively
  // on Windows so that fallback does not itself fail on the very spelling
  // mismatch (`Foo.h` vs `foo.h`) identity was just unable to resolve.
  if (auto id = file_identity(file); id && main_ids.count(*id) > 0) return true;
  const std::string key = path_lookup_key(to_string(clang_getFileName(file)));
  return std::any_of(main_files.begin(), main_files.end(),
                     [&key](const std::string& mf) { return path_lookup_key(mf) == key; });
}

}  // namespace clangquill::parser

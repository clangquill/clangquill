#include "parser/doxygen_comment_parser.hpp"

#include <clang-c/Documentation.h>

#include <string>
#include <utility>
#include <vector>

#include "comment/doxygen_raw.hpp"
#include "comment/doxygen_raw_detail.hpp"
#include "parser/cursor_utils.hpp"

namespace clangquill::parser {

// The parsed-tree walk and the raw scan must agree about the same comment, so
// the routing, normalization and lead promotion below are the raw scanner's
// own rather than a second copy of the same rules.
using comment::detail::apply_lead;
using comment::detail::code_language;
using comment::detail::fenced_block;
using comment::detail::is_group_command;
using comment::detail::is_inline_command;
using comment::detail::lower;
using comment::detail::normalize_ws;
using comment::detail::render_inline_markup;
using comment::detail::route_command;

namespace {

// Recursively gathers the text of a comment node (Text + inline commands).
void collect_text(CXComment c, std::string& out) {
  CXCommentKind kind = clang_Comment_getKind(c);
  if (kind == CXComment_Text) {
    out += to_string(clang_TextComment_getText(c));
    out += ' ';
    return;
  }
  if (kind == CXComment_InlineCommand) {
    std::string name =
        lower(to_string(clang_InlineCommandComment_getCommandName(c)));
    if (is_group_command(name)) return;
    unsigned n = clang_InlineCommandComment_getNumArgs(c);
    if (is_inline_command(name)) {
      // Written back in source form so text_of's single markup pass covers the
      // parsed tree and parse_raw's scan identically -- and so a `\ref X "a
      // title"`, whose title libclang leaves in the following text, is still
      // one string when the markup is applied.
      if (n == 0) return;
      out += '\\';
      out += name;
      for (unsigned i = 0; i < n; ++i) {
        out += ' ';
        out += to_string(clang_InlineCommandComment_getArgText(c, i));
      }
      out += ' ';
      return;
    }
    if (n == 0) {
      out += name;
      out += ' ';
      return;
    }
    for (unsigned i = 0; i < n; ++i) {
      out += to_string(clang_InlineCommandComment_getArgText(c, i));
      out += ' ';
    }
    return;
  }
  // libclang models HTML markup as tag nodes with no children, so anything not
  // handled here is deleted outright -- `<b>`, `<br>` and whole `<ul><li>`
  // lists vanished from the text along with the structure they carried. They
  // are passed through instead: Markdown keeps raw inline HTML, whereas
  // rewriting the tags as Markdown emphasis would have to fight the space this
  // walk puts after every text node (`** bold **` is not emphasis).
  if (kind == CXComment_HTMLStartTag) {
    out += to_string(clang_HTMLTagComment_getAsString(c));
    return;
  }
  if (kind == CXComment_HTMLEndTag) {
    // This walk puts a space after every text node, which inside a tag pair
    // would surface as `<b>bold </b>` -- and leave the parsed path disagreeing
    // with parse_raw, which sees the source spacing as written.
    if (!out.empty() && out.back() == ' ') out.pop_back();
    out += "</";
    out += to_string(clang_HTMLTagComment_getTagName(c));
    out += '>';
    return;
  }
  if (kind == CXComment_VerbatimBlockLine) {
    out += to_string(clang_VerbatimBlockLineComment_getText(c));
    out += ' ';
    return;
  }
  if (kind == CXComment_VerbatimLine) {
    out += to_string(clang_VerbatimLineComment_getText(c));
    out += ' ';
    return;
  }
  unsigned n = clang_Comment_getNumChildren(c);
  for (unsigned i = 0; i < n; ++i) collect_text(clang_Comment_getChild(c, i), out);
}

std::string text_of(CXComment c) {
  std::string s;
  collect_text(c, s);
  return render_inline_markup(normalize_ws(s));
}

// Combined argument + paragraph text of a block command. libclang declares
// arguments for some commands (so the value lands in getArgText) and leaves
// others entirely in the paragraph; concatenating both recovers the full text
// regardless of which path libclang took.
std::string block_text(CXComment bc) {
  std::string s;
  unsigned na = clang_BlockCommandComment_getNumArgs(bc);
  for (unsigned i = 0; i < na; ++i) {
    s += to_string(clang_BlockCommandComment_getArgText(bc, i));
    s += ' ';
  }
  collect_text(clang_BlockCommandComment_getParagraph(bc), s);
  return render_inline_markup(normalize_ws(s));
}

// The direction libclang parsed off a `\param`, or "" when the comment did not
// write one. libclang reports In for an unannotated parameter, so the explicit
// flag is what separates a documented direction from that default.
std::string explicit_direction(CXComment param_command) {
  if (clang_ParamCommandComment_isDirectionExplicit(param_command) == 0) {
    return {};
  }
  switch (clang_ParamCommandComment_getDirection(param_command)) {
    case CXCommentParamPassDirection_In:
      return "in";
    case CXCommentParamPassDirection_Out:
      return "out";
    case CXCommentParamPassDirection_InOut:
      return "in,out";
  }
  return {};
}

// The fenced rendering of a parsed `\code` / `\verbatim` block. libclang hands
// the body back one CXComment_VerbatimBlockLine per source line -- collapsing
// them with collect_text is what destroyed the block -- and puts a `\code{.py}`
// attribute in the first of those lines rather than in the command's arguments.
std::string verbatim_block(CXComment bc) {
  std::string kind = lower(to_string(clang_BlockCommandComment_getCommandName(bc)));
  std::vector<std::string> lines;
  unsigned n = clang_Comment_getNumChildren(bc);
  for (unsigned i = 0; i < n; ++i) {
    CXComment child = clang_Comment_getChild(bc, i);
    if (clang_Comment_getKind(child) != CXComment_VerbatimBlockLine) continue;
    lines.push_back(to_string(clang_VerbatimBlockLineComment_getText(child)));
  }
  std::string language;
  if (!lines.empty()) {
    std::string first = lines.front();
    std::size_t a = first.find_first_not_of(" \t");
    if (a != std::string::npos && first[a] == '{' && first.back() == '}') {
      language = code_language(first.substr(a + 1, first.size() - a - 2));
      lines.erase(lines.begin());
    }
  }
  return fenced_block(kind, language, std::move(lines));
}

model::CommentModel parse_parsed_comment(CXComment full) {
  model::CommentModel m;
  std::vector<std::string> lead;
  bool explicit_brief = false;

  unsigned n = clang_Comment_getNumChildren(full);
  for (unsigned i = 0; i < n; ++i) {
    CXComment child = clang_Comment_getChild(full, i);
    switch (clang_Comment_getKind(child)) {
      case CXComment_Paragraph: {
        if (clang_Comment_isWhitespace(child) != 0) break;
        std::string t = text_of(child);
        if (!t.empty()) lead.push_back(std::move(t));
        break;
      }
      case CXComment_BlockCommand: {
        std::string name = lower(to_string(clang_BlockCommandComment_getCommandName(child)));
        if (name == "brief" || name == "short") explicit_brief = true;
        route_command(m, name, block_text(child));
        break;
      }
      case CXComment_ParamCommand: {
        std::string name = to_string(clang_ParamCommandComment_getParamName(child));
        m.params.push_back(model::CommentParam{
            name, text_of(clang_BlockCommandComment_getParagraph(child)),
            explicit_direction(child)});
        break;
      }
      case CXComment_TParamCommand: {
        std::string name = to_string(clang_TParamCommandComment_getParamName(child));
        // No direction: Doxygen defines the attribute on `\param` only.
        m.tparams.push_back(model::CommentParam{
            name, text_of(clang_BlockCommandComment_getParagraph(child)), {}});
        break;
      }
      case CXComment_VerbatimBlockCommand: {
        // Appended to `lead`, not straight to `detail`: apply_lead flushes the
        // prose paragraphs afterwards, so a block written between two of them
        // would otherwise be reordered ahead of both.
        std::string t = verbatim_block(child);
        if (!t.empty()) lead.push_back(std::move(t));
        break;
      }
      case CXComment_VerbatimLine: {
        std::string t = text_of(child);
        if (!t.empty()) m.detail.push_back(std::move(t));
        break;
      }
      default:
        break;
    }
  }

  apply_lead(m, lead, explicit_brief);
  return m;
}

}  // namespace

model::CommentModel DoxygenCommentParser::parse(CXCursor cursor,
                                                const std::string& raw) const {
  // Group and structural commands confuse libclang's parsed tree; scan the raw
  // text instead. A null cursor has no parsed comment either, so a block that
  // belongs to no cursor at all takes the same path.
  if (comment::raw_has_unroutable_command(raw)) {
    return comment::doxygen_parse_raw(raw);
  }
  CXComment full = clang_Cursor_getParsedComment(cursor);
  if (clang_Comment_getKind(full) == CXComment_FullComment &&
      clang_Comment_getNumChildren(full) > 0) {
    model::CommentModel m = parse_parsed_comment(full);
    if (!m.empty()) return m;
  }
  // Fallback: libclang did not surface a structured comment (e.g. a plain `//`
  // comment). Recover what we can by scanning the raw text for commands.
  return comment::doxygen_parse_raw(raw);
}

}  // namespace clangquill::parser

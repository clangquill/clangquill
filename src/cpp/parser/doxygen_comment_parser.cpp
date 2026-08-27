#include "parser/doxygen_comment_parser.hpp"

#include <clang-c/Documentation.h>

#include <cctype>
#include <string>
#include <utility>
#include <vector>

#include "parser/cursor_utils.hpp"

namespace clangquill::parser {
namespace {

// Collapses internal whitespace runs to single spaces and trims the ends.
std::string normalize_ws(const std::string& s) {
  std::string out;
  out.reserve(s.size());
  bool pending_space = false;
  for (char ch : s) {
    if (std::isspace(static_cast<unsigned char>(ch)) != 0) {
      pending_space = !out.empty();
    } else {
      if (pending_space) out.push_back(' ');
      pending_space = false;
      out.push_back(ch);
    }
  }
  return out;
}

// Splits "name rest of text" into (name, rest); used for @retval / @throws /
// @param argument names that follow the command word.
std::pair<std::string, std::string> split_first_token(const std::string& s) {
  std::size_t i = 0;
  while (i < s.size() && std::isspace(static_cast<unsigned char>(s[i])) == 0) {
    ++i;
  }
  std::string first = s.substr(0, i);
  while (i < s.size() && std::isspace(static_cast<unsigned char>(s[i])) != 0) {
    ++i;
  }
  return {first, s.substr(i)};
}

std::string lower(std::string s) {
  for (char& ch : s)
    ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
  return s;
}

// Group commands carry cross-symbol bookkeeping (assembled separately from the
// symbol's raw comment); they must never leak into the rendered prose.
bool is_group_command(const std::string& name) {
  return name == "ingroup" || name == "defgroup" || name == "addtogroup";
}

// Recursively gathers the text of a comment node (Text + inline commands).
void collect_text(CXComment c, std::string& out) {
  CXCommentKind kind = clang_Comment_getKind(c);
  if (kind == CXComment_Text) {
    out += to_string(clang_TextComment_getText(c));
    out += ' ';
    return;
  }
  if (kind == CXComment_InlineCommand) {
    if (is_group_command(
            lower(to_string(clang_InlineCommandComment_getCommandName(c))))) {
      return;
    }
    unsigned n = clang_InlineCommandComment_getNumArgs(c);
    if (n == 0) {
      out += to_string(clang_InlineCommandComment_getCommandName(c));
      out += ' ';
    } else {
      for (unsigned i = 0; i < n; ++i) {
        out += to_string(clang_InlineCommandComment_getArgText(c, i));
        out += ' ';
      }
    }
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
  return normalize_ws(s);
}

// True when the raw comment uses a command libclang's parsed tree mishandles:
// it drops the command and leaves its argument as stray prose. Group commands
// are one family; the structural commands are the other, which libclang models
// as verbatim-line commands and spills into the detail text — so `\class Select`
// would put a bare "Select" in the description. The no-argument markers join
// them because this parser is the only one that knows their arity: whatever
// takes_line/takes_nothing/takes_single_name classify has to reach parse_raw
// to be treated that way. Such comments are parsed from the raw text instead,
// where every family routes into `custom` and the prose stays with the entity.
// Commands whose argument is a single entity name, with anything after it
// belonging to the enclosing documentation rather than to the command. Doxygen
// writes `\relates DenseBase` and then several paragraphs about the function;
// without this the whole description becomes the command's argument.
// Commands whose argument is the rest of their line. Doxygen's `\ingroup a b c`
// legitimately names several groups on one line, so the argument ends at the
// newline — treating it as a paragraph command turns the following prose into
// group names, one per word.
// True for the commands whose argument runs on across following lines until a
// blank line or the next command — `\brief`, `\param`, `\note` and the rest.
// Everything else has its argument complete on its own line.
bool takes_paragraph(const std::string& cmd);

bool takes_line(const std::string& cmd) {
  return cmd == "ingroup" || cmd == "defgroup" || cmd == "addtogroup";
}

// Commands that take no argument at all. `\internal` marks the text after it as
// internal documentation and `\code`/`\endcode`/`\li` are markers, so
// everything following them is the entity's own prose. (This only stops them
// swallowing it; rendering `\code` as a real code block is a separate matter.)
bool takes_nothing(const std::string& cmd) {
  // `\code` is NOT here: it genuinely takes a block, and treating it as a
  // marker spills the code body into the prose. What was broken is that
  // `\endcode` never ended it, so the text after the block was swallowed too.
  return cmd == "internal" || cmd == "endinternal" || cmd == "endcode" ||
         cmd == "endverbatim" || cmd == "li" || cmd == "arg";
}

bool takes_single_name(const std::string& cmd) {
  return cmd == "class" || cmd == "struct" || cmd == "union" ||
         cmd == "enum" || cmd == "namespace" || cmd == "fn" || cmd == "var" ||
         cmd == "typedef" || cmd == "relates";
}

bool takes_paragraph(const std::string& cmd) {
  return !takes_line(cmd) && !takes_nothing(cmd) && !takes_single_name(cmd);
}

bool raw_has_unroutable_command(const std::string& raw) {
  static const char* const kCmds[] = {
      "ingroup",  "defgroup", "addtogroup", "class", "struct",  "union",
      "enum",     "namespace", "fn",        "var",   "typedef", "relates",
      "internal", "li"};
  // Deliberately not `code`/`endcode`: libclang's parsed tree preserves a
  // verbatim block's line structure, which parse_raw's normalize_ws destroys.
  // A comment that also carries one of the above still takes the raw path, and
  // `endcode` is classified there so the prose after the block is not lost.
  for (std::size_t i = 0; i + 1 < raw.size(); ++i) {
    if (raw[i] != '\\' && raw[i] != '@') continue;
    for (const char* cmd : kCmds) {
      std::string c(cmd);
      if (raw.compare(i + 1, c.size(), c) != 0) continue;
      // Require a word boundary so `\defgrouping` / `\ingroup_x` do not match.
      std::size_t next = i + 1 + c.size();
      if (next >= raw.size() ||
          (std::isalnum(static_cast<unsigned char>(raw[next])) == 0 &&
           raw[next] != '_')) {
        return true;
      }
    }
  }
  return false;
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
  return normalize_ws(s);
}

// Routes one command (lowercased name, normalized text) into the model. The
// brief/detail lead paragraphs are handled by the caller; everything else flows
// through here so the CXComment and raw-scanning passes stay consistent.
void route_command(model::CommentModel& m, const std::string& name,
                   const std::string& text) {
  if (name == "brief" || name == "short") {
    if (m.brief.empty()) m.brief = text;
  } else if (name == "return" || name == "returns" || name == "result") {
    if (!m.returns.empty()) m.returns += ' ';
    m.returns += text;
  } else if (name == "param") {
    auto [n, d] = split_first_token(text);
    m.params.push_back(model::CommentParam{n, d});
  } else if (name == "tparam") {
    auto [n, d] = split_first_token(text);
    m.tparams.push_back(model::CommentParam{n, d});
  } else if (name == "retval") {
    auto [n, d] = split_first_token(text);
    m.retvals.push_back(model::CommentRetval{n, d});
  } else if (name == "throw" || name == "throws" || name == "exception") {
    auto [n, d] = split_first_token(text);
    m.throws.push_back(model::CommentThrow{n, d});
  } else if (name == "see" || name == "sa") {
    m.see.push_back(text);
  } else if (name == "since") {
    m.since.push_back(text);
  } else if (name == "deprecated") {
    m.deprecated.push_back(text);
  } else if (name == "note") {
    m.note.push_back(text);
  } else if (name == "warning" || name == "attention") {
    m.warning.push_back(text);
  } else if (name == "pre") {
    m.pre.push_back(text);
  } else if (name == "post") {
    m.post.push_back(text);
  } else {
    m.custom[name].push_back(text);
  }
}

// Promotes the leading free-text paragraphs into brief/detail. With an explicit
// @brief the lead paragraphs are all detail; otherwise the first is the brief.
void apply_lead(model::CommentModel& m, const std::vector<std::string>& lead,
                bool explicit_brief) {
  std::size_t start = 0;
  if (!explicit_brief && !lead.empty()) {
    m.brief = lead.front();
    start = 1;
  }
  for (std::size_t i = start; i < lead.size(); ++i) m.detail.push_back(lead[i]);
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
        m.params.push_back(model::CommentParam{name, text_of(clang_BlockCommandComment_getParagraph(child))});
        break;
      }
      case CXComment_TParamCommand: {
        std::string name = to_string(clang_TParamCommandComment_getParamName(child));
        m.tparams.push_back(model::CommentParam{name, text_of(clang_BlockCommandComment_getParagraph(child))});
        break;
      }
      case CXComment_VerbatimBlockCommand:
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

// Removes Doxygen/C++ comment markers, returning the documentation lines. Used
// only as a fallback when libclang produced no parsed comment.
std::vector<std::string> strip_markers(const std::string& raw) {
  std::vector<std::string> lines;
  std::string cur;
  std::size_t i = 0;
  while (i <= raw.size()) {
    if (i == raw.size() || raw[i] == '\n') {
      std::string line = cur;
      cur.clear();
      // Trim.
      std::size_t a = line.find_first_not_of(" \t\r");
      if (a == std::string::npos) {
        lines.emplace_back();
        ++i;
        continue;
      }
      std::size_t b = line.find_last_not_of(" \t\r");
      line = line.substr(a, b - a + 1);
      // Strip leading markers (post-item "<" variants checked first so the
      // trailing '<' is not left behind as content).
      auto starts_with = [&](const char* p) { return line.rfind(p, 0) == 0; };
      if (starts_with("///<") || starts_with("//!<")) line.erase(0, 4);
      else if (starts_with("/**<") || starts_with("/*!<")) line.erase(0, 4);
      else if (starts_with("/**") || starts_with("/*!")) line.erase(0, 3);
      else if (starts_with("/*")) line.erase(0, 2);
      else if (starts_with("///") || starts_with("//!")) line.erase(0, 3);
      else if (starts_with("//")) line.erase(0, 2);
      // Strip trailing */.
      if (line.size() >= 2 && line.compare(line.size() - 2, 2, "*/") == 0) {
        line.erase(line.size() - 2);
      }
      // Strip a single leading '*' (Javadoc continuation).
      std::size_t c = line.find_first_not_of(" \t");
      if (c != std::string::npos && line[c] == '*') line.erase(0, c + 1);
      // Re-trim.
      a = line.find_first_not_of(" \t");
      lines.push_back(a == std::string::npos ? std::string() : line.substr(a));
      ++i;
      continue;
    }
    cur.push_back(raw[i]);
    ++i;
  }
  return lines;
}

model::CommentModel parse_raw(const std::string& raw) {
  model::CommentModel m;
  std::vector<std::string> lead;
  bool explicit_brief = false;

  std::string cmd;          // active command (empty => lead text)
  std::string buf;          // accumulated text for the active section
  bool have_lead_para = false;

  auto flush = [&]() {
    std::string text = normalize_ws(buf);
    buf.clear();
    if (cmd.empty()) {
      if (!text.empty()) lead.push_back(text);
      have_lead_para = false;
    } else if (takes_single_name(cmd)) {
      // Only the name is the argument; the rest is the entity's own prose and
      // has to rejoin the lead text or it disappears into the command.
      auto [name, prose] = split_first_token(text);
      route_command(m, cmd, name);
      if (!prose.empty()) lead.push_back(prose);
    } else if (takes_nothing(cmd)) {
      route_command(m, cmd, "");
      if (!text.empty()) lead.push_back(text);
    } else {
      if (cmd == "brief" || cmd == "short") explicit_brief = true;
      route_command(m, cmd, text);
    }
    cmd.clear();
  };

  for (const std::string& source_line : strip_markers(raw)) {
    std::string line = source_line;
    // A no-argument command does not even own the rest of its own line: Eigen
    // writes `\internal \ingroup enums` and `\internal \class Foo`, so the
    // command after the marker has to be seen rather than swept into the prose.
    // Hence the rescan rather than a single pass per line.
    for (bool again = true; again;) {
      again = false;
      std::size_t s = line.find_first_not_of(" \t");
      bool is_command = s != std::string::npos && (line[s] == '@' || line[s] == '\\');
      if (is_command) {
        flush();
        std::size_t e = line.find_first_of(" \t", s + 1);
        cmd = lower(line.substr(s + 1, (e == std::string::npos ? line.size() : e) - s - 1));
        std::string rest = e == std::string::npos ? std::string{} : line.substr(e + 1);
        if (takes_nothing(cmd)) {
          route_command(m, cmd, "");
          cmd.clear();
          line = rest;
          // Guarded on a non-blank remainder so a marker alone on its line
          // cannot loop, nor be mistaken for a paragraph break.
          again = rest.find_first_not_of(" \t") != std::string::npos;
          continue;
        }
        buf = rest;
        if (takes_line(cmd)) flush();  // the argument ended with the line
        continue;
      }
      if (line.empty()) {
        // A blank line ends whatever section is open. Doxygen's paragraph
        // commands run only to the next blank line, so the paragraphs below one
        // document the entity rather than extending `\brief` or the last
        // `\param` -- letting them run on put a symbol's entire detailed
        // description inside its one-line summary.
        if (!cmd.empty() || have_lead_para) flush();
        continue;
      }
      // A command that does not take a paragraph already has its argument, so
      // this line starts the entity's prose rather than extending the command.
      if (!cmd.empty() && !takes_paragraph(cmd)) flush();
      if (!buf.empty()) buf += ' ';
      buf += line;
      if (cmd.empty()) have_lead_para = true;
    }
  }
  flush();

  apply_lead(m, lead, explicit_brief);
  return m;
}

}  // namespace

model::CommentModel DoxygenCommentParser::parse(CXCursor cursor,
                                                const std::string& raw) const {
  // Group and structural commands confuse libclang's parsed tree; scan the raw
  // text instead. A null cursor has no parsed comment either, so a block that
  // belongs to no cursor at all takes the same path.
  if (raw_has_unroutable_command(raw)) return parse_raw(raw);
  CXComment full = clang_Cursor_getParsedComment(cursor);
  if (clang_Comment_getKind(full) == CXComment_FullComment &&
      clang_Comment_getNumChildren(full) > 0) {
    model::CommentModel m = parse_parsed_comment(full);
    if (!m.empty()) return m;
  }
  // Fallback: libclang did not surface a structured comment (e.g. a plain `//`
  // comment). Recover what we can by scanning the raw text for commands.
  return parse_raw(raw);
}

model::CommentModel DoxygenCommentParser::parse_raw_text(
    const std::string& raw) {
  return parse_raw(raw);
}

}  // namespace clangquill::parser

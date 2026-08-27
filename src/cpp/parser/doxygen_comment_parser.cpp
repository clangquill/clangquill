#include "parser/doxygen_comment_parser.hpp"

#include <clang-c/Documentation.h>

#include <algorithm>
#include <cctype>
#include <string>
#include <string_view>
#include <tuple>
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

// The highlighting language of a `\code{...}` attribute. Doxygen writes it as a
// file extension (`{.py}`, `{.cpp}`) and spells "no highlighting" as
// `{.unparsed}`, which maps to an empty info string.
std::string code_language(const std::string& attr) {
  std::string lang = lower(attr);
  if (!lang.empty() && lang.front() == '.') lang.erase(0, 1);
  return lang == "unparsed" ? std::string{} : lang;
}

// Canonical spelling of a `\param` direction attribute, or "" when the text is
// not a direction Doxygen defines. Doxygen writes the attribute without spaces
// (`[in]`, `[out]`, `[in,out]`), which is what lets the command word be
// tokenized on whitespace at all; `inout` is accepted as the spelling libclang
// uses for the same thing.
std::string canonical_direction(const std::string& attr) {
  std::string t = lower(attr);
  if (t == "in") return "in";
  if (t == "out") return "out";
  if (t == "inout" || t == "in,out") return "in,out";
  return {};
}

// A command word and the attribute Doxygen glues onto it.
struct CommandWord {
  std::string name;       ///< The command itself, e.g. "param" or "code".
  std::string direction;  ///< `\param[in,out]` -> "in,out"; else empty.
  std::string language;   ///< `\code{.py}` -> "py"; else empty.
};

// Splits a raw command word from its attribute. Doxygen writes both the
// parameter direction and a code block's language attached to the command word
// itself, so without this the whole `param[out]` / `code{.py}` is taken as the
// command name, matches no route, and (for `\param`) the name/description split
// never happens -- the entry vanishes from `params`. Only attributes this
// parser understands are stripped, so an unknown `\foo[bar]` still reaches
// `custom` under its full spelling.
CommandWord split_command_word(const std::string& cmd) {
  if (cmd.empty()) return {cmd, {}, {}};
  char close = cmd.back();
  char open_ch = close == ']' ? '[' : (close == '}' ? '{' : '\0');
  if (open_ch == '\0') return {cmd, {}, {}};
  std::size_t open = cmd.find(open_ch);
  if (open == std::string::npos) return {cmd, {}, {}};
  std::string name = cmd.substr(0, open);
  std::string attr = cmd.substr(open + 1, cmd.size() - open - 2);
  if (close == ']' && (name == "param" || name == "tparam")) {
    std::string dir = canonical_direction(attr);
    if (!dir.empty()) return {name, dir, {}};
  } else if (close == '}' && name == "code") {
    return {name, {}, code_language(attr)};
  }
  return {cmd, {}, {}};
}

bool is_blank(const std::string& s) {
  return s.find_first_not_of(" \t") == std::string::npos;
}

// Renders a verbatim block as a MyST fenced code block, line structure intact.
//
// The output target is Markdown, where a code example's newlines and relative
// indentation are load-bearing -- running the body through normalize_ws turned
// every example into one line of mangled prose. The block is carried as a
// ready-to-emit fence in `detail` so it keeps its position among the prose
// paragraphs, which a separate field could not.
//
// @param kind The opening command, `code` or `verbatim`.
// @param language Highlighting language from a `{.py}` attribute, if any.
// @param lines The block body, one entry per source line.
std::string fenced_block(const std::string& kind, const std::string& language,
                         std::vector<std::string> lines) {
  while (!lines.empty() && is_blank(lines.front())) lines.erase(lines.begin());
  while (!lines.empty() && is_blank(lines.back())) lines.pop_back();
  if (lines.empty()) return {};

  // The comment marker (`///`, ` * `) left a common indent on every line; only
  // what each line has beyond it is the example's own structure.
  std::size_t indent = std::string::npos;
  for (const std::string& line : lines) {
    if (is_blank(line)) continue;
    indent = std::min(indent, line.find_first_not_of(" \t"));
  }
  if (indent == std::string::npos) indent = 0;

  // A fence has to be longer than any backtick run it encloses.
  std::size_t ticks = 3;
  std::size_t run = 0;
  for (const std::string& line : lines) {
    for (char ch : line) {
      run = ch == '`' ? run + 1 : 0;
      ticks = std::max(ticks, run + 1);
    }
    run = 0;
  }

  // A `\verbatim` block is preformatted text rather than code, so it gets no
  // info string; a `\code` block without an explicit language is C++, which is
  // the only language a libclang-driven parser sees.
  std::string info = language;
  if (info.empty() && kind == "code") info = "cpp";

  std::string fence(ticks, '`');
  std::string out = fence + info + "\n";
  for (const std::string& line : lines) {
    out += is_blank(line) ? std::string{} : line.substr(indent);
    out += '\n';
  }
  out += fence;
  return out;
}

// True for a `detail` entry that fenced_block produced. Such a block is never a
// one-line summary, so it must not be promoted to the brief.
bool is_fenced_block(const std::string& s) { return s.rfind("```", 0) == 0; }

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
// internal documentation and `\li` is a list marker, so everything following
// them is the entity's own prose.
bool takes_nothing(const std::string& cmd) {
  // `\code`/`\verbatim` are NOT here: they open a block that parse_raw reads
  // line by line, and their `\end...` is consumed by that block. These entries
  // only catch a stray terminator with no block open.
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
  // Deliberately not `code`/`endcode`: nothing about a verbatim block needs the
  // raw path. Both paths now render one as a fenced block with its lines
  // intact, so a comment reaches either one none the worse.
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
                   const std::string& text,
                   const std::string& direction = {}) {
  if (name == "brief" || name == "short") {
    if (m.brief.empty()) m.brief = text;
  } else if (name == "return" || name == "returns" || name == "result") {
    if (!m.returns.empty()) m.returns += ' ';
    m.returns += text;
  } else if (name == "param") {
    auto [n, d] = split_first_token(text);
    m.params.push_back(model::CommentParam{n, d, direction});
  } else if (name == "tparam") {
    auto [n, d] = split_first_token(text);
    m.tparams.push_back(model::CommentParam{n, d, direction});
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
// @brief the lead paragraphs are all detail; otherwise the first prose
// paragraph is the brief. A verbatim block is skipped over rather than
// promoted: it is never a one-line summary, and a comment that opens with a
// code example would otherwise have no brief at all.
void apply_lead(model::CommentModel& m, const std::vector<std::string>& lead,
                bool explicit_brief) {
  std::size_t brief_at = lead.size();
  if (!explicit_brief) {
    for (std::size_t i = 0; i < lead.size(); ++i) {
      if (!is_fenced_block(lead[i])) {
        brief_at = i;
        break;
      }
    }
  }
  if (brief_at < lead.size()) m.brief = lead[brief_at];
  for (std::size_t i = 0; i < lead.size(); ++i) {
    if (i != brief_at) m.detail.push_back(lead[i]);
  }
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

// Strips the comment markers from one raw line, returning its content.
//
// Whitespace *after* the marker is kept: inside a `\code` block it is the only
// record of the example's indentation, and everywhere else normalize_ws
// collapses it anyway. Only the single space that conventionally separates the
// marker from the text (`/// foo`, ` * foo`) is removed. A line holding nothing
// but markers and whitespace comes back empty, which is the paragraph break
// parse_raw looks for.
std::string strip_line_markers(std::string line) {
  auto rtrim = [](std::string& s) {
    std::size_t b = s.find_last_not_of(" \t\r");
    s.erase(b == std::string::npos ? 0 : b + 1);
  };
  rtrim(line);
  if (line.size() >= 2 && line.compare(line.size() - 2, 2, "*/") == 0) {
    line.erase(line.size() - 2);
    rtrim(line);
  }
  std::size_t a = line.find_first_not_of(" \t");
  if (a == std::string::npos) return {};

  // Post-item "<" variants first, so the trailing '<' is not left as content.
  auto marker = [&](std::string_view p) {
    return line.compare(a, p.size(), p) == 0;
  };
  std::size_t k = 0;
  bool stripped = true;
  if (marker("///<") || marker("//!<") || marker("/**<") || marker("/*!<")) {
    k = a + 4;
  } else if (marker("/**") || marker("/*!") || marker("///") || marker("//!")) {
    k = a + 3;
  } else if (marker("/*") || marker("//")) {
    k = a + 2;
  } else {
    stripped = false;
  }

  // A leading '*' is a Javadoc continuation marker, not content.
  std::size_t c = line.find_first_not_of(" \t", k);
  if (c != std::string::npos && line[c] == '*') {
    k = c + 1;
    stripped = true;
  }
  if (stripped && k < line.size() && line[k] == ' ') ++k;
  if (k >= line.size()) return {};
  std::string content = line.substr(k);
  rtrim(content);
  return content;
}

// Removes Doxygen/C++ comment markers, returning the documentation lines. Used
// only as a fallback when libclang produced no parsed comment.
std::vector<std::string> strip_markers(const std::string& raw) {
  std::vector<std::string> lines;
  std::string cur;
  std::size_t i = 0;
  while (i <= raw.size()) {
    if (i == raw.size() || raw[i] == '\n') {
      lines.push_back(strip_line_markers(cur));
      cur.clear();
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
  std::string dir;          // direction attribute of the active command
  std::string buf;          // accumulated text for the active section
  bool have_lead_para = false;

  std::string verbatim;     // open `code`/`verbatim` block (empty => none)
  std::string verbatim_language;
  std::vector<std::string> verbatim_lines;

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
      route_command(m, cmd, text, dir);
    }
    cmd.clear();
    dir.clear();
  };

  // Appends the finished block where the prose paragraphs go, so it keeps its
  // place among them once apply_lead runs.
  auto close_verbatim = [&]() {
    std::string block =
        fenced_block(verbatim, verbatim_language, std::move(verbatim_lines));
    if (!block.empty()) lead.push_back(std::move(block));
    verbatim.clear();
    verbatim_language.clear();
    verbatim_lines.clear();
  };

  for (const std::string& source_line : strip_markers(raw)) {
    // Inside a verbatim block every line is body text -- commands, blank lines
    // and all -- until the matching `\endcode` / `\endverbatim`.
    if (!verbatim.empty()) {
      std::size_t s = source_line.find_first_not_of(" \t");
      bool ends = false;
      if (s != std::string::npos &&
          (source_line[s] == '@' || source_line[s] == '\\')) {
        std::size_t e = source_line.find_first_of(" \t", s + 1);
        ends = lower(source_line.substr(
                   s + 1, (e == std::string::npos ? source_line.size() : e) -
                              s - 1)) == "end" + verbatim;
      }
      if (ends) {
        close_verbatim();
      } else {
        verbatim_lines.push_back(source_line);
      }
      continue;
    }

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
        CommandWord word = split_command_word(
            lower(line.substr(s + 1,
                              (e == std::string::npos ? line.size() : e) - s - 1)));
        cmd = word.name;
        dir = word.direction;
        std::string rest = e == std::string::npos ? std::string{} : line.substr(e + 1);
        if (cmd == "code" || cmd == "verbatim") {
          verbatim = cmd;
          verbatim_language = word.language;
          cmd.clear();
          dir.clear();
          // Anything after the opening command is already body text, and the
          // rescan must not read it as one: inside a block nothing is a command
          // but the matching `\end...`.
          if (!is_blank(rest)) verbatim_lines.push_back(rest);
          break;
        }
        if (takes_nothing(cmd)) {
          route_command(m, cmd, "");
          cmd.clear();
          dir.clear();
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
      if (is_blank(line)) {
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
  // An unterminated block still carries documentation; keep what it holds.
  if (!verbatim.empty()) close_verbatim();
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

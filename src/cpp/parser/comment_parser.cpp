#include "parser/comment_parser.hpp"

namespace clangquill::parser {
namespace {

// `comment_fields` has one slot for a field's argument, so a directed parameter
// carries its direction there in the bracketed form Doxygen itself writes:
// `[out] result`. model_from_fields (Python) splits it back off. An undirected
// parameter is spelled exactly as before, so existing rows keep their meaning.
std::string param_arg(const model::CommentParam& p) {
  if (p.direction.empty()) return p.name;
  return "[" + p.direction + "] " + p.name;
}

}  // namespace

std::vector<model::CommentField> to_comment_fields(
    const std::string& usr, const model::CommentModel& m) {
  std::vector<model::CommentField> fields;
  int ordinal = 0;
  auto add = [&](const std::string& name, const std::string& arg,
                 const std::string& value) {
    model::CommentField f;
    f.symbol_usr = usr;
    f.name = name;
    f.arg = arg;
    f.value = value;
    f.ordinal = ordinal++;
    fields.push_back(std::move(f));
  };

  if (!m.brief.empty()) add("brief", "", m.brief);
  for (const auto& d : m.detail) add("detail", "", d);
  for (const auto& p : m.params) add("param", param_arg(p), p.description);
  for (const auto& p : m.tparams) add("tparam", param_arg(p), p.description);
  if (!m.returns.empty()) add("returns", "", m.returns);
  for (const auto& r : m.retvals) add("retval", r.value, r.description);
  for (const auto& t : m.throws) add("throws", t.exception, t.description);
  for (const auto& s : m.see) add("see", "", s);
  for (const auto& s : m.since) add("since", "", s);
  for (const auto& s : m.deprecated) add("deprecated", "", s);
  for (const auto& s : m.note) add("note", "", s);
  for (const auto& s : m.warning) add("warning", "", s);
  for (const auto& s : m.pre) add("pre", "", s);
  for (const auto& s : m.post) add("post", "", s);
  for (const auto& [name, values] : m.custom) {
    for (const auto& v : values) add(name, "", v);
  }
  return fields;
}

}  // namespace clangquill::parser

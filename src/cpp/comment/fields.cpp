#include "comment/fields.hpp"

#include <nlohmann/json.hpp>

#include <string>
#include <vector>

namespace clangquill::comment {
namespace {

using nlohmann::json;

json params_to_json(const std::vector<model::CommentParam>& items) {
  json arr = json::array();
  for (const auto& p : items) {
    arr.push_back({{"name", p.name},
                   {"description", p.description},
                   {"direction", p.direction}});
  }
  return arr;
}

}  // namespace

std::string encode_param_arg(const model::CommentParam& p) {
  if (p.direction.empty()) return p.name;
  return "[" + p.direction + "] " + p.name;
}

std::string to_fields_json(const model::CommentModel& m) {
  json retvals = json::array();
  for (const auto& r : m.retvals) {
    retvals.push_back({{"value", r.value}, {"description", r.description}});
  }
  json throws = json::array();
  for (const auto& t : m.throws) {
    throws.push_back({{"exception", t.exception}, {"description", t.description}});
  }

  json j = {
      {"brief", m.brief},
      {"detail", m.detail},
      {"params", params_to_json(m.params)},
      {"tparams", params_to_json(m.tparams)},
      {"returns", m.returns},
      {"retvals", retvals},
      {"throws", throws},
      {"see", m.see},
      {"since", m.since},
      {"deprecated", m.deprecated},
      {"note", m.note},
      {"warning", m.warning},
      {"pre", m.pre},
      {"post", m.post},
      {"invariant", m.invariant},
      {"todo", m.todo},
      {"bug", m.bug},
      {"author", m.author},
      {"version", m.version},
      {"date", m.date},
      {"custom", m.custom},
  };
  return j.dump();
}

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
  for (const auto& p : m.params) add("param", encode_param_arg(p), p.description);
  for (const auto& p : m.tparams) add("tparam", encode_param_arg(p), p.description);
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
  for (const auto& s : m.invariant) add("invariant", "", s);
  for (const auto& s : m.todo) add("todo", "", s);
  for (const auto& s : m.bug) add("bug", "", s);
  for (const auto& s : m.author) add("author", "", s);
  for (const auto& s : m.version) add("version", "", s);
  for (const auto& s : m.date) add("date", "", s);
  for (const auto& [name, values] : m.custom) {
    for (const auto& v : values) add(name, "", v);
  }
  return fields;
}

}  // namespace clangquill::comment

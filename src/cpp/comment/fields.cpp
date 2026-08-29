#include "comment/fields.hpp"

#include <nlohmann/json.hpp>

#include <string>
#include <utility>
#include <vector>

namespace clangquill::comment {
namespace {

using nlohmann::json;

// --- Per-shape primitives ---------------------------------------------------
//
// Every field's shape falls out of its member type rather than being spelled in
// the macro table: one overload set per shape, resolved once per X() expansion.
// Adding a shape means adding three overloads, not touching twenty rows.

using Add = void (*)(void*, const std::string&, const std::string&,
                     const std::string&);

/// Appends one row per value the field holds.
void flatten(Add add, void* sink, const char* row, const std::string& v) {
  if (!v.empty()) add(sink, row, "", v);
}
void flatten(Add add, void* sink, const char* row,
             const std::vector<std::string>& vs) {
  for (const std::string& v : vs) add(sink, row, "", v);
}
void flatten(Add add, void* sink, const char* row,
             const std::vector<model::CommentParam>& ps) {
  for (const model::CommentParam& p : ps) {
    add(sink, row, encode_param_arg(p), p.description);
  }
}
void flatten(Add add, void* sink, const char* row,
             const std::vector<model::CommentRetval>& rs) {
  for (const model::CommentRetval& r : rs) add(sink, row, r.value, r.description);
}
void flatten(Add add, void* sink, const char* row,
             const std::vector<model::CommentThrow>& ts) {
  for (const model::CommentThrow& t : ts) {
    add(sink, row, t.exception, t.description);
  }
}

/// Absorbs one row back into the field it came from.
void absorb(std::string& out, const std::string&, const std::string& value) {
  out = value;
}
void absorb(std::vector<std::string>& out, const std::string&,
            const std::string& value) {
  out.push_back(value);
}
void absorb(std::vector<model::CommentParam>& out, const std::string& arg,
            const std::string& value) {
  auto [name, direction] = split_param_arg(arg);
  out.push_back(model::CommentParam{name, value, direction});
}
void absorb(std::vector<model::CommentRetval>& out, const std::string& arg,
            const std::string& value) {
  out.push_back(model::CommentRetval{arg, value});
}
void absorb(std::vector<model::CommentThrow>& out, const std::string& arg,
            const std::string& value) {
  out.push_back(model::CommentThrow{arg, value});
}

/// The JSON value a field serializes to.
json to_json_value(const std::string& v) { return v; }
json to_json_value(const std::vector<std::string>& vs) { return vs; }
json to_json_value(const std::vector<model::CommentParam>& ps) {
  json arr = json::array();
  for (const model::CommentParam& p : ps) {
    arr.push_back({{"name", p.name},
                   {"description", p.description},
                   {"direction", p.direction}});
  }
  return arr;
}
json to_json_value(const std::vector<model::CommentRetval>& rs) {
  json arr = json::array();
  for (const model::CommentRetval& r : rs) {
    arr.push_back({{"value", r.value}, {"description", r.description}});
  }
  return arr;
}
json to_json_value(const std::vector<model::CommentThrow>& ts) {
  json arr = json::array();
  for (const model::CommentThrow& t : ts) {
    arr.push_back({{"exception", t.exception}, {"description", t.description}});
  }
  return arr;
}

/// The shape name exported alongside each field, keyed by member type.
template <class T>
struct Shape;
template <>
struct Shape<std::string> {
  static constexpr const char* kName = "scalar";
};
template <>
struct Shape<std::vector<std::string>> {
  static constexpr const char* kName = "list";
};
template <>
struct Shape<std::vector<model::CommentParam>> {
  static constexpr const char* kName = "param";
};
template <>
struct Shape<std::vector<model::CommentRetval>> {
  static constexpr const char* kName = "retval";
};
template <>
struct Shape<std::vector<model::CommentThrow>> {
  static constexpr const char* kName = "throws";
};

}  // namespace

std::string encode_param_arg(const model::CommentParam& p) {
  if (p.direction.empty()) return p.name;
  return "[" + p.direction + "] " + p.name;
}

std::pair<std::string, std::string> split_param_arg(const std::string& arg) {
  if (arg.empty() || arg.front() != '[') return {arg, ""};
  const std::size_t close = arg.find(']');
  if (close == std::string::npos) return {arg, ""};
  const std::string direction = arg.substr(1, close - 1);
  if (direction != "in" && direction != "out" && direction != "in,out") {
    return {arg, ""};
  }
  std::size_t name_at = arg.find_first_not_of(' ', close + 1);
  if (name_at == std::string::npos) name_at = arg.size();
  return {arg.substr(name_at), direction};
}

std::string to_fields_json(const model::CommentModel& m) {
  json j;
#define CLANGQUILL_COMMENT_FIELD_JSON(member, row) \
  j[#member] = to_json_value(m.member);
  CLANGQUILL_COMMENT_FIELDS(CLANGQUILL_COMMENT_FIELD_JSON)
#undef CLANGQUILL_COMMENT_FIELD_JSON
  j["custom"] = m.custom;
  return j.dump();
}

std::vector<model::CommentField> to_comment_fields(
    const std::string& usr, const model::CommentModel& m) {
  struct Sink {
    const std::string& usr;
    std::vector<model::CommentField> fields;
    int ordinal = 0;
  } sink{usr, {}, 0};

  const Add add = [](void* s, const std::string& name, const std::string& arg,
                     const std::string& value) {
    Sink& out = *static_cast<Sink*>(s);
    model::CommentField f;
    f.symbol_usr = out.usr;
    f.name = name;
    f.arg = arg;
    f.value = value;
    f.ordinal = out.ordinal++;
    out.fields.push_back(std::move(f));
  };

#define CLANGQUILL_COMMENT_FIELD_FLATTEN(member, row) \
  flatten(add, &sink, row, m.member);
  CLANGQUILL_COMMENT_FIELDS(CLANGQUILL_COMMENT_FIELD_FLATTEN)
#undef CLANGQUILL_COMMENT_FIELD_FLATTEN

  for (const auto& [name, values] : m.custom) {
    for (const std::string& v : values) add(&sink, name, "", v);
  }
  return std::move(sink.fields);
}

model::CommentModel from_comment_fields(
    const std::vector<model::CommentField>& fields) {
  model::CommentModel m;
  for (const model::CommentField& f : fields) {
#define CLANGQUILL_COMMENT_FIELD_ABSORB(member, row) \
  if (f.name == (row)) {                             \
    absorb(m.member, f.arg, f.value);                \
    continue;                                        \
  }
    CLANGQUILL_COMMENT_FIELDS(CLANGQUILL_COMMENT_FIELD_ABSORB)
#undef CLANGQUILL_COMMENT_FIELD_ABSORB
    m.custom[f.name].push_back(f.value);
  }
  return m;
}

const std::vector<CommentFieldInfo>& comment_field_table() {
  static const std::vector<CommentFieldInfo> table = {
#define CLANGQUILL_COMMENT_FIELD_INFO(member, row) \
  {row, #member, Shape<decltype(model::CommentModel::member)>::kName},
      CLANGQUILL_COMMENT_FIELDS(CLANGQUILL_COMMENT_FIELD_INFO)
#undef CLANGQUILL_COMMENT_FIELD_INFO
  };
  return table;
}

}  // namespace clangquill::comment

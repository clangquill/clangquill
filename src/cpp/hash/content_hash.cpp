#include "hash/content_hash.hpp"

#include <cstddef>
#include <cstdint>
#include <iterator>
#include <string>
#include <string_view>
#include <vector>

#include "hash/sha256.hpp"

namespace clangquill::hash {

namespace {

/// @brief Feeds a big-endian 64-bit length prefix into @p h.
///
/// Framing every variable-length value (and every variable-length list) with
/// an explicit count, rather than relying on a separator byte, ensures no
/// byte value appearing inside a field (e.g. in `raw_comment` or a parameter
/// token) can make differently-structured inputs hash identically.
void update_length(Sha256& h, std::uint64_t len) {
  std::uint8_t be[8];
  for (int i = 7; i >= 0; --i) {
    be[i] = static_cast<std::uint8_t>(len & 0xff);
    len >>= 8;
  }
  h.update(be, sizeof(be));
}

/// @brief Feeds one length-framed field into the digest.
using FieldSink = void (*)(void*, std::string_view);

/// @brief One Symbol field folded into the hash, in hashing order.
struct SymbolHashField {
  const char* name;  ///< The `Symbol` member's name, as Python spells it.
  /// @brief Feeds this field's value into @p sink.
  void (*feed)(const model::Symbol&, FieldSink, void*);
};

/// @brief Every Symbol field the hash covers, in the order it covers them.
///
/// content_hash() walks this table and content_hash_symbol_fields() exports it,
/// so a field cannot be hashed without being exported or exported without being
/// hashed. That matters because the Python side derives the *complement* from
/// it: `Generator._wide_tokens` must cover exactly the `Symbol` fields left out
/// here, or a custom template renders from data no fingerprint tracks.
///
/// The order and the framing are the on-disk hash. Changing either invalidates
/// every incremental cache in existence; `tests/cpp/test_hash.cpp` pins the
/// digest of a fully-populated symbol so that cannot happen by accident.
constexpr SymbolHashField kSymbolHashFields[] = {
    {"usr",
     [](const model::Symbol& s, FieldSink f, void* h) { f(h, s.usr); }},
    {"kind",
     [](const model::Symbol& s, FieldSink f, void* h) {
       f(h, std::to_string(static_cast<int>(s.kind)));
     }},
    {"qualified_name",
     [](const model::Symbol& s, FieldSink f, void* h) { f(h, s.qualified_name); }},
    {"signature",
     [](const model::Symbol& s, FieldSink f, void* h) { f(h, s.signature); }},
    {"type_repr",
     [](const model::Symbol& s, FieldSink f, void* h) { f(h, s.type_repr); }},
    {"access",
     [](const model::Symbol& s, FieldSink f, void* h) {
       f(h, std::to_string(static_cast<int>(s.access)));
     }},
    {"storage",
     [](const model::Symbol& s, FieldSink f, void* h) {
       f(h, std::to_string(static_cast<int>(s.storage)));
     }},
    {"is_definition",
     [](const model::Symbol& s, FieldSink f, void* h) {
       f(h, s.is_definition ? "1" : "0");
     }},
};

/// @brief The FieldSink that writes into a Sha256.
void feed_sha256(void* h, std::string_view v) {
  Sha256& sha = *static_cast<Sha256*>(h);
  update_length(sha, v.size());
  sha.update(v);
}

}  // namespace

std::vector<std::string> content_hash_symbol_fields() {
  std::vector<std::string> names;
  names.reserve(std::size(kSymbolHashFields));
  for (const SymbolHashField& f : kSymbolHashFields) names.emplace_back(f.name);
  return names;
}

std::string content_hash(const model::Symbol& sym,
                         const std::vector<model::FunctionParameter>& params,
                         const std::string& raw_comment) {
  Sha256 h;
  auto field = [&](std::string_view v) {
    update_length(h, v.size());
    h.update(v);
  };

  for (const SymbolHashField& f : kSymbolHashFields) f.feed(sym, feed_sha256, &h);
  update_length(h, params.size());
  for (const auto& p : params) {
    field(p.type_repr);
    field(p.name);
    field(p.default_value);
  }
  field(raw_comment);
  return h.hexdigest();
}

}  // namespace clangquill::hash

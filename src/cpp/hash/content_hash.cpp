#include "hash/content_hash.hpp"

#include <cstdint>

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

}  // namespace

std::string content_hash(const model::Symbol& sym,
                         const std::vector<model::FunctionParameter>& params,
                         const std::string& raw_comment) {
  Sha256 h;
  auto field = [&](std::string_view v) {
    update_length(h, v.size());
    h.update(v);
  };

  field(sym.usr);
  field(std::to_string(static_cast<int>(sym.kind)));
  field(sym.qualified_name);
  field(sym.signature);
  field(sym.type_repr);
  field(std::to_string(static_cast<int>(sym.access)));
  field(std::to_string(static_cast<int>(sym.storage)));
  field(sym.is_definition ? "1" : "0");
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

#include <catch2/catch_test_macros.hpp>

#include "hash/content_hash.hpp"
#include "hash/sha256.hpp"
#include "model/parameters.hpp"
#include "model/symbol.hpp"

using clangquill::hash::content_hash;
using clangquill::hash::sha256_hex;
using clangquill::model::FunctionParameter;
using clangquill::model::Symbol;

TEST_CASE("SHA-256 known-answer vectors", "[hash]") {
  // Standard NIST/RFC test vectors.
  CHECK(sha256_hex("") ==
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  CHECK(sha256_hex("abc") ==
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  CHECK(sha256_hex(
            "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq") ==
        "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
}

TEST_CASE("SHA-256 is streaming-stable across chunk boundaries", "[hash]") {
  clangquill::hash::Sha256 a;
  a.update("hello ");
  a.update("world");
  clangquill::hash::Sha256 b;
  b.update("hello world");
  CHECK(a.hexdigest() == b.hexdigest());
}

TEST_CASE("hexdigest resets state for reuse", "[hash]") {
  clangquill::hash::Sha256 h;
  h.update("abc");
  CHECK(h.hexdigest() ==
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  // After digesting, the object must behave like a fresh instance.
  CHECK(h.hexdigest() ==
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
}

TEST_CASE("content_hash frames raw_comment against a literal separator byte",
          "[hash]") {
  // Regression test for issue #219: an embedded 0x1f must not let a single
  // field collide with two fields split at that byte.
  Symbol base;
  base.usr = "c:@F@f#";

  std::string embedded = "a";
  embedded += '\x1f';
  embedded += "b";

  CHECK(content_hash(base, {}, embedded) !=
        content_hash(base, {}, "a") + content_hash(base, {}, "b"));
  CHECK(content_hash(base, {}, embedded) != content_hash(base, {}, "a"));
  CHECK(content_hash(base, {}, embedded) != content_hash(base, {}, "b"));
}

TEST_CASE("content_hash frames the parameter list against a differently-split "
          "sequence",
          "[hash]") {
  // Two parameters ("a","b","") each and ("a\x1fb","","") as a single
  // parameter must not collide once fields are length-prefixed.
  Symbol sym;
  sym.usr = "c:@F@g#";

  FunctionParameter p1;
  p1.name = "a";
  FunctionParameter p2;
  p2.name = "b";
  std::vector<FunctionParameter> two_params{p1, p2};

  FunctionParameter merged;
  merged.name = "a\x1f" "b";
  std::vector<FunctionParameter> one_param{merged};

  CHECK(content_hash(sym, two_params, "") != content_hash(sym, one_param, ""));
}

TEST_CASE("content_hash changes when a field value moves across a field "
          "boundary",
          "[hash]") {
  // "ab", "" must not hash the same as "a", "b" for two adjacent string
  // fields (qualified_name/signature here).
  Symbol a;
  a.usr = "c:@S@X";
  a.qualified_name = "ab";
  a.signature = "";

  Symbol b;
  b.usr = "c:@S@X";
  b.qualified_name = "a";
  b.signature = "b";

  CHECK(content_hash(a, {}, "") != content_hash(b, {}, ""));
}

TEST_CASE("content_hash is stable for a fully-populated symbol", "[hash]") {
  // A known-answer test, not a property: `symbols.content_hash` is persisted in
  // the IR and drives the incremental cache, so a change to the field list, the
  // field order or the length framing silently invalidates every user's cache
  // and re-renders every page. The properties above would not notice any of
  // that. If this digest changes, the change is either a mistake or a schema
  // bump -- it is never incidental.
  Symbol sym;
  sym.usr = "c:@N@geo@S@Circle@F@area#1";
  sym.parent_usr = "c:@N@geo@S@Circle";
  sym.kind = clangquill::model::SymbolKind::Method;
  sym.spelling = "area";
  sym.qualified_name = "geo::Circle::area";
  sym.display_name = "area(int) const";
  sym.signature = "double area(int precision) const";
  sym.type_repr = "double (int) const";
  sym.access = clangquill::model::AccessKind::Public;
  sym.storage = clangquill::model::StorageKind::None;
  sym.is_definition = true;
  sym.is_documented = true;
  sym.location.file_path = "/src/geo.hpp";
  sym.location.line = 42;

  FunctionParameter p;
  p.type_repr = "int";
  p.name = "precision";
  p.default_value = "2";

  CHECK(content_hash(sym, {p}, "/// @brief The area.") ==
        "42f3a18c284f2d4ed8541fbddb47c8789a98a0631c04709add615011f5b01426");
}

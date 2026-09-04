"""Parsing C++23 / C++26 sources and the libclang-version helper.

The `std` value is handed to clang verbatim, so newer standards already work;
these tests prove libclang actually ingests the newer *syntax* on a capable
backend and skip where the linked libclang is too old.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clangquill import _core
from clangquill._libclang import libclang_major

# The project's target libclang major — the bundled-libclang pin (single source
# of truth in tools/ci/llvm-version.txt). C++26 dogfooding is gated on it to
# match docs/conf.py.
PIN_MAJOR = int((Path(__file__).parents[1] / "tools" / "ci" / "llvm-version.txt").read_text().strip().split(".")[0])

requires_libclang = pytest.mark.skipif(not _core.have_libclang(), reason="core built without libclang")

# A member using the C++23 explicit object parameter (deducing this), which
# clang first accepts in version seventeen.
CPP23_SRC = """
/// A documented widget.
struct Widget {
  int width = 0;
  /// Return the width through an explicit object parameter (C++23).
  template <typename Self>
  auto value(this Self&& self) { return self.width; }
};
"""

# A function template using C++26 pack indexing, which needs a recent clang. We
# gate on the project's target of version twenty-three to match the docs dogfood.
CPP26_SRC = """
/// First element of a parameter pack (C++26 pack indexing).
template <typename... Ts>
auto first(Ts... values) { return values...[0]; }
"""


def _parse(tmp_path: Path, src: str, std: str) -> _core.ParseResult:
    header = tmp_path / "fixture.hpp"
    header.write_text(src)
    db = tmp_path / "out.sqlite"
    opt = _core.ParseOptions()
    opt.std_flag = std
    return _core.parse_to_sqlite([str(header)], str(db), opt)


@requires_libclang
@pytest.mark.skipif((libclang_major() or 0) < 17, reason="C++23 needs clang >= 17")
def test_parses_cpp23_deducing_this(tmp_path: Path) -> None:
    result = _parse(tmp_path, CPP23_SRC, "c++23")
    assert result.symbol_count > 0
    assert not result.diagnostics


@requires_libclang
@pytest.mark.skipif((libclang_major() or 0) < 17, reason="gnu++ needs clang >= 17")
def test_gnu_extension_std_passes_through(tmp_path: Path) -> None:
    # The GNU-extension spelling is forwarded verbatim and must parse cleanly.
    result = _parse(tmp_path, CPP23_SRC, "gnu++23")
    assert result.symbol_count > 0
    assert not result.diagnostics


@requires_libclang
@pytest.mark.skipif((libclang_major() or 0) < PIN_MAJOR, reason="C++26 pack indexing needs the pinned clang")
def test_parses_cpp26_pack_indexing(tmp_path: Path) -> None:
    result = _parse(tmp_path, CPP26_SRC, "c++26")
    assert result.symbol_count > 0
    assert not result.diagnostics


def test_libclang_major_matches_backend() -> None:
    major = libclang_major()
    if _core.have_libclang():
        assert isinstance(major, int)
        assert major >= 1
    else:
        assert major is None

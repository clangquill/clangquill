"""Tests for the compiled `clangquill._core` extension (M1)."""

import re

from clangquill import _core


def test_core_importable():
    """The version string looks like a real release, not just a non-empty stand-in."""
    assert re.match(r"^\d+\.\d+", _core.__core_version__)


def test_have_libclang_is_bool():
    """Pin the type, and cross-check it against the other signal of the same fact.

    ``have_libclang()`` gates every libclang-dependent test (the
    ``requires_libclang`` marker in test_clangquill.py and friends); a non-bool
    return would silently skip them instead of failing loudly. It also must
    never disagree with ``libclang_version()`` about which backend is linked.
    """
    have = _core.have_libclang()
    assert isinstance(have, bool)
    assert have == bool(_core.libclang_version())


def test_libclang_version_matches_backend():
    version = _core.libclang_version()
    if _core.have_libclang():
        # When linked, the C API call must return a real version string.
        assert "clang" in version.lower()
    else:
        assert version == ""

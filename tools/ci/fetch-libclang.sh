#!/usr/bin/env bash
# Fetch a self-contained libclang from the official LLVM release and unpack the
# pieces clangquill needs into a prefix usable as CMake's LibClang_ROOT.
#
# Why the official "LLVM-<ver>-Linux-<arch>" package: from LLVM 19/20 onward it
# ships a libclang.so with libLLVM *statically linked in* (no shared libLLVM.so
# — just libc/libstdc++/libgcc/libm/libz). So `auditwheel repair` vendors that
# self-contained libclang (plus libstdc++/libgcc) rather than the ~129 MB
# monolithic libLLVM the distro libclang drags in (see docs ADR-0001). The
# trade-off is a GLIBC_2.34 floor, so wheels build in a manylinux_2_34 image.
#
# Usage:  fetch-libclang.sh [PREFIX]      (PREFIX defaults to /opt/libclang)
# Env:    LLVM_VERSION                     (defaults to the pinned version in the
#                                           sibling llvm-version.txt — the single
#                                           source of truth for the pin)
#         FETCH_RESOURCE_HEADERS=1         (also unpack lib/clang/<major>/include)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ver="${LLVM_VERSION:-$(cat "$here/llvm-version.txt")}"
prefix="${1:-/opt/libclang}"

if [ "$(uname -s)" != "Linux" ]; then
  echo "fetch-libclang: this script only supports Linux" >&2
  exit 1
fi

case "$(uname -m)" in
  x86_64 | amd64) arch="X64" ;;
  aarch64 | arm64) arch="ARM64" ;;
  *)
    echo "fetch-libclang: unsupported architecture $(uname -m)" >&2
    exit 1
    ;;
esac

tarball="LLVM-${ver}-Linux-${arch}.tar.xz"
url="https://github.com/llvm/llvm-project/releases/download/llvmorg-${ver}/${tarball}"

mkdir -p "$prefix"
echo "fetch-libclang: downloading ${url}" >&2
# Extract only the shared library and the C API headers; the rest of the
# release (clang/lld/etc.) is multiple GB unpacked and unused here. The binary
# tarball ships no license file, so it is fetched separately below.
#
# FETCH_RESOURCE_HEADERS=1 also keeps lib/clang/<major>/include (stddef.h and
# the other builtin headers). Wheels do not need them -- clangquill is handed a
# resource directory -- but a bare libclang.so cannot parse <cstddef> without
# them, which the pre-bump retest of the C++ suite does.
members=('*/lib/libclang.so*' '*/include/clang-c/*')
if [ "${FETCH_RESOURCE_HEADERS:-0}" = "1" ]; then
  members+=('*/lib/clang/*/include/*')
fi
curl -fsSL --retry 3 --retry-delay 2 "$url" \
  | tar -xJ -C "$prefix" --strip-components=1 --wildcards --no-anchored \
        "${members[@]}"

# FindLibClang looks for a bare `libclang.so`; the release ships the versioned
# names plus that symlink, but recreate it defensively if it is missing.
if [ ! -e "${prefix}/lib/libclang.so" ]; then
  real="$(find "${prefix}/lib" -maxdepth 1 -name 'libclang.so.*' -type f | head -1 || true)"
  if [ -n "$real" ]; then
    ln -sf "$(basename "$real")" "${prefix}/lib/libclang.so"
  fi
fi

# Fail loudly here rather than letting CMake silently fall back to the stub.
test -f "${prefix}/include/clang-c/Index.h" \
  || { echo "fetch-libclang: clang-c/Index.h missing" >&2; exit 1; }
test -e "${prefix}/lib/libclang.so" \
  || { echo "fetch-libclang: libclang.so missing" >&2; exit 1; }

# The binary release tarball ships no license file, so fetch clang's license
# (Apache-2.0 WITH LLVM-exception) from the matching source tag — wheels that
# bundle libclang.so must redistribute it. Pinned to the same version as the .so
# so the shipped text always matches the bundled library.
license_url="https://raw.githubusercontent.com/llvm/llvm-project/llvmorg-${ver}/clang/LICENSE.TXT"
echo "fetch-libclang: downloading ${license_url}" >&2
curl -fsSL --retry 3 --retry-delay 2 "$license_url" -o "${prefix}/LICENSE.TXT"
test -s "${prefix}/LICENSE.TXT" \
  || { echo "fetch-libclang: LICENSE.TXT download failed" >&2; exit 1; }

echo "fetch-libclang: installed libclang ${ver} (${arch}) to ${prefix}" >&2
ls -l "${prefix}/lib/"libclang.so* >&2

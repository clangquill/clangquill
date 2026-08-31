# ADR 0001 — libclang sourcing for the manylinux wheel

**Status:** Accepted · **Date:** 2026-05-31 · **Issue:** #4

## Context

`clangquill` links **libclang** in its compiled core (`clangquill._core`).
To ship a `manylinux` wheel, the libclang shared library must be bundled into
the wheel by `auditwheel repair`. We needed to decide *where libclang comes
from* at build time, because that choice dominates the wheel size and the CI
build cost.

Candidates considered:

1. **Distro / system libclang** (e.g. `libclang-dev` on the build image).
2. **Official LLVM release tarball** (`clang+llvm-*-<arch>-linux-gnu`, e.g.
   `x86_64` and `aarch64`).
3. **vcpkg `llvm` port.**
4. **The PyPI `libclang` wheel**, which ships a prebuilt, self-contained
   `libclang.so`.

## Spike results (measured)

All numbers from building the M1 `_core` extension against libclang 18 and
running `auditwheel` locally.

### Distro libclang + `auditwheel repair`

`auditwheel show` correctly flags `libclang-18.so.18` as an external library.
`auditwheel repair` vendors it — **and everything it pulls in**:

| vendored lib | uncompressed |
| --- | --- |
| `libLLVM-*.so.1` | **129 MB** |
| `libclang-18-*.so.18` | 33 MB |
| `libicudata-*.so` | 29 MB |
| (libxml2, libicuuc, libedit, …) | ~5 MB |

Repaired wheel: **~61 MB compressed**. The blowup is because the distro's
`libclang.so` is the *shared* variant that dynamically depends on the
monolithic `libLLVM.so`, so auditwheel must vendor the entire libLLVM.

The repaired wheel was installed in a clean venv (no LLVM present) and verified:
`_core` resolves `libclang` from `clangquill.libs/` and `clang_getClangVersion()`
returns the expected version. So the mechanism works — it is just large.

### PyPI `libclang` wheel

Ships a single, self-contained `clang/native/libclang.so`:

* **60 MB uncompressed / 24.5 MB compressed** — libLLVM is statically linked
  into one `libclang.so`, with internal symbols hidden.

This is less than half the size of the distro-based repaired wheel, with no
separate libLLVM/ICU payload.

## Decision

**Build against the `libclang.so` from the PyPI `libclang` wheel** (or an
equivalent self-contained libclang where libLLVM is statically linked and
symbols are hidden), rather than a distro/shared-libLLVM libclang.

Rationale:

* **Size**: ~24 MB vs ~61 MB repaired — a self-contained libclang avoids
  vendoring the full 129 MB libLLVM.
* **Reproducibility**: a pinned PyPI version is trivial to fetch in CI on any
  arch the project targets; no multi-hour LLVM compile (ruling out the vcpkg
  `llvm` port without a binary cache).
* **CMake already supports it**: `cmake/FindLibClang.cmake` discovers libclang
  via `LibClang_ROOT` / `llvm-config` / standard paths, so CI only needs to
  point `LibClang_ROOT` at the unpacked libclang and provide `clang-c` headers.

The official LLVM release tarball remains a viable fallback (it also ships a
self-contained-ish libclang and the matching `clang-c` headers); it is heavier
to download but useful if a libclang build newer than the PyPI package is
needed.

## Consequences / follow-ups

* `cmake/FindLibClang.cmake` needs the `clang-c/Index.h` headers available at
  build time. The PyPI `libclang` wheel ships the `.so` but **not** the
  headers. To preserve the download-size/speed win of the PyPI wheel, CI should
  obtain headers cheaply — **preferably a pinned, vendored copy of the
  `clang-c` headers in the repo** (they are small and stable across patch
  releases), or by downloading only those headers from the LLVM repository —
  rather than pulling the full LLVM release tarball just for headers. This is
  wired up in the M2 build, not M1 (M1 deliberately keeps libclang linkage
  optional).
* `cibuildwheel` `CIBW_BEFORE_ALL` will fetch + unpack libclang and export
  `LibClang_ROOT`; `CLANGQUILL_WITH_LIBCLANG=ON` is set so a missing libclang
  fails the build loudly rather than silently producing the stub backend.
* Wheel size will be in the ~25–40 MB range — acceptable for a libclang-backed
  tool; documented for users.

## Licensing

LLVM/Clang (incl. libclang) is licensed under **Apache-2.0 WITH
LLVM-exception**, which is permissive and compatible with clangquill's
**BSD-2-Clause**. Wheels that bundle `libclang.so` must include the LLVM
license. Action: ship the LLVM license file alongside `LICENSE` in the wheel
metadata when libclang is bundled (to be added with the M2 libclang-enabled
build).

## Update (M2 implementation)

The libclang-enabled wheel build was implemented with one refinement to the
source. The PyPI `libclang` wheel tops out at **18.1.1**, which would cap the
shipped wheels at C++23. Meanwhile the **official `LLVM-<ver>-Linux-<arch>`
release packages** (from LLVM 19/20 onward) changed to ship a libclang.so with
**libLLVM statically linked in** — there is no shared `libLLVM.so` dependency
(its `NEEDED` is just libc/libstdc++/libgcc/libm/libz). That gives the *same*
single-payload, no-monolithic-libLLVM vendoring benefit the ADR chose the PyPI
wheel for, **and** a newer libclang that parses C++26.

Realized decision: **bundle libclang from the official LLVM release tarball**
(the pin lives in `tools/ci/llvm-version.txt`, the single source of truth read by
the fetch script, the wheel workflows, the freshness check and the docs/tests),
fetched per-arch by `tools/ci/fetch-libclang.sh` (it also provides the `clang-c` headers, so no
separate header vendoring is needed; the binary tarball ships no license file,
so the script fetches `clang/LICENSE.TXT` from the matching source tag).
`auditwheel repair`
vendors that libclang.so (plus libstdc++/libgcc).

Consequences:

* That libclang requires **GLIBC_2.34**, so the wheels are built in the
  **manylinux_2_34** image (`CIBW_MANYLINUX_*_IMAGE`) — still the newest image
  pypa publishes for x86_64/aarch64.
* From **LLVM 23** the release libclang also imports
  `std::condition_variable::wait` at **GLIBCXX_3.4.30** (GCC 12's libstdc++),
  above the 3.4.29 cap of auditwheel's `manylinux_2_34` policy, so the wheels are
  repaired to **manylinux_2_35** — the lowest policy that admits that symbol
  (`CIBW_REPAIR_WHEEL_COMMAND_LINUX`). The build image is unchanged: libclang is a
  prebuilt binary the image's own GCC never compiles against. The wheels
  therefore install on glibc ≥ 2.35 systems; older ones — including the glibc
  2.34 of RHEL/AlmaLinux/Rocky 9 — build from source. The floor tracks whatever
  the bundled libclang needs, so it moves again when a future LLVM raises it.
* Wheel size is larger than the ~24 MB PyPI-wheel estimate (the self-contained
  libclang.so is ~200 MB uncompressed), but still avoids the monolithic shared
  `libLLVM`.
* `CLANGQUILL_WITH_LIBCLANG=ON` is set in the wheel build so a missing libclang
  fails loudly; `CIBW_TEST_COMMAND` asserts `have_libclang()`, and a separate
  `smoke_test` job installs the repaired wheel in a clean `python:*-slim-bookworm`
  image (no system LLVM, no `LD_LIBRARY_PATH`) and parses a header to prove the
  bundled libclang is self-sufficient. Debian 12 rather than a manylinux image
  because pip in the 2_34 image would refuse a manylinux_2_35 wheel, and pypa
  publishes no 2_35 image for these arches.
* The LLVM license ships in the wheel as `LICENSE-LLVM.txt`. It is **not** tracked
  in git: `tools/ci/fetch-libclang.sh` downloads `clang/LICENSE.TXT` from the
  matching LLVM source tag (`llvmorg-<LLVM_VERSION>`) into the libclang prefix, and
  the wheel build copies it into the project root (in `CIBW_BEFORE_ALL`), so the
  shipped license always matches the pinned `LLVM_VERSION`. `project.license-files`
  uses the glob `["LICENSE*"]`, which picks the file up when present (wheels) and is
  a no-op for sdists/source builds that bundle no libclang. The project's own license
  stays BSD-2-Clause.

## Update (Windows wheels)

Windows wheels bundle libclang the same way, with a Windows-specific asset:
`LLVM_BUILD_LLVM_DYLIB` (the shared-libLLVM option the distro-libclang problem
on Linux stems from) isn't available on Windows at all, so `libclang.dll` from
any official Windows release is inherently self-contained the way the Linux
release tarball is — there's no separate `libLLVM.dll` to vendor.

The Windows release ships no slim tarball equivalent to
`LLVM-<ver>-Linux-<arch>.tar.xz` (only an NSIS `.exe` installer, which isn't
stream-extractable the way `fetch-libclang.sh` unpacks the Linux archive), so
`tools/ci/fetch-libclang.ps1` downloads the full
`clang+llvm-<ver>-x86_64-pc-windows-msvc.tar.xz` archive instead and extracts
only `bin/libclang.dll`, `lib/libclang.lib` (the import library `FindLibClang.cmake`
links against), and the `clang-c` headers.

`sqlite3` and `nlohmann-json` come from vcpkg on Windows too, using the
`x64-windows-static-md` triplet so both are statically linked against the
dynamic CRT (the same one Python's own DLLs use) — `libclang.dll` ends up the
only third-party DLL cibuildwheel needs to vendor into the wheel, which it
does via `delvewheel` (cibuildwheel 4's default Windows repair tool, analogous
to `auditwheel` on Linux). `tools/ci/vcpkg-asset-fetch.ps1` mirrors the Linux
asset-fetch workaround for vcpkg's sqlite.org 403.

Windows wheel building targets `x86_64` only for now; Windows-on-Arm is a
possible follow-up.

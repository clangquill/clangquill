# Locate libclang (the C API) and expose it as the imported target
# clangquill::libclang plus the cache variable CLANGQUILL_LIBCLANG_FOUND.
#
# Honors CLANGQUILL_WITH_LIBCLANG (ON | OFF | AUTO):
#   OFF  - never look; build the stub backend.
#   AUTO - look; build with libclang only if found (default, no hard failure).
#   ON   - look; fail the configure if it cannot be found.
#
# Discovery order: an explicit hint (LibClang_ROOT / llvm-config), then common
# system locations; within a directory, versioned names win newest-first. The
# manylinux build wires this to a prebuilt LLVM (see #4).
#
# A library whose LLVM version can be determined and is below
# CLANGQUILL_LIBCLANG_MIN_MAJOR is rejected -- fatally under ON, by falling back
# to the stub backend under AUTO.

set(CLANGQUILL_LIBCLANG_FOUND FALSE)

if(CLANGQUILL_WITH_LIBCLANG STREQUAL "OFF")
  return()
endif()

# The oldest LLVM whose libclang C API clangquill is known to build against.
set(CLANGQUILL_LIBCLANG_MIN_MAJOR 17)

# Best-effort probe of the LLVM major version behind a resolved libclang. The C
# API headers carry only CINDEX_VERSION (which does not track LLVM releases) and
# the real version is reachable only by *running* clang_getClangVersion(), so
# derive it from the surrounding install instead: llvm-config when it owns the
# library, otherwise the versioned soname/filename or an llvm-<N> prefix dir.
# Sets ${out_var} to the major version, or to the empty string when unknown.
function(_clangquill_libclang_major out_var lib_path)
  set(_major "")

  # llvm-config --version, but only when it describes this very library.
  if(LLVM_CONFIG_EXECUTABLE AND _llvm_libdir)
    get_filename_component(_lib_dir "${lib_path}" DIRECTORY)
    get_filename_component(_lib_dir "${_lib_dir}" REALPATH)
    get_filename_component(_cfg_dir "${_llvm_libdir}" REALPATH)
    if(_lib_dir STREQUAL _cfg_dir)
      execute_process(
        COMMAND "${LLVM_CONFIG_EXECUTABLE}" --version
        OUTPUT_STRIP_TRAILING_WHITESPACE OUTPUT_VARIABLE _cfg_version
        ERROR_QUIET)
      if(_cfg_version MATCHES "^([0-9]+)\\.")
        set(_major "${CMAKE_MATCH_1}")
      endif()
    endif()
  endif()

  # Follow symlinks: `libclang.so` usually points at `libclang.so.<major>[...]`.
  # LLVM stamps the library's SONAME/name with the release major, so both of the
  # forms below carry it; the `llvm-<N>` prefix directory is the last resort.
  if(NOT _major)
    get_filename_component(_real "${lib_path}" REALPATH)
    get_filename_component(_name "${_real}" NAME)
    if(_name MATCHES "clang-([0-9]+)")               # libclang-17.so[.17.0.6]
      set(_major "${CMAKE_MATCH_1}")
    elseif(_name MATCHES "\\.so\\.([0-9]+)")           # libclang.so.17[.0.6]
      set(_major "${CMAKE_MATCH_1}")
    elseif(_name MATCHES "\\.([0-9]+)\\.dylib")          # libclang.17.dylib
      set(_major "${CMAKE_MATCH_1}")
    elseif(_real MATCHES "[/\\]llvm-([0-9]+)[/\\]")      # /usr/lib/llvm-17/lib/..
      set(_major "${CMAKE_MATCH_1}")
    endif()
  endif()

  # Guard against a SONAME that is not an LLVM release number at all (no shared
  # libclang predates LLVM 4): report "unknown" rather than a bogus rejection.
  if(_major AND _major LESS 4)
    set(_major "")
  endif()

  set(${out_var} "${_major}" PARENT_SCOPE)
endfunction()

# Build versioned tool/library name lists from the bundled-libclang pin (the
# single source of truth in tools/ci/llvm-version.txt) down to a supported floor,
# so the search ceiling tracks the pin without a second place to edit.
file(STRINGS "${CMAKE_CURRENT_LIST_DIR}/../tools/ci/llvm-version.txt"
     _llvm_pin LIMIT_COUNT 1)
# Fail clearly on a malformed/edited pin rather than with a cryptic foreach error.
if(NOT _llvm_pin MATCHES "^[0-9]+\\.[0-9]+\\.[0-9]+$")
  message(FATAL_ERROR
    "Invalid LLVM pin '${_llvm_pin}' in tools/ci/llvm-version.txt; "
    "expected MAJOR.MINOR.PATCH")
endif()
string(REGEX REPLACE "^([0-9]+)\\..*$" "\\1" _llvm_major "${_llvm_pin}")
if(_llvm_major LESS CLANGQUILL_LIBCLANG_MIN_MAJOR)
  message(FATAL_ERROR
    "LLVM pin major ${_llvm_major} is below the supported floor "
    "(${CLANGQUILL_LIBCLANG_MIN_MAJOR})")
endif()
# foreach(RANGE) only counts up, so collect ascending then reverse to newest-first.
set(_llvm_config_versioned "")
set(_llvm_lib_versioned "")
foreach(_v RANGE ${CLANGQUILL_LIBCLANG_MIN_MAJOR} ${_llvm_major})
  list(APPEND _llvm_config_versioned "llvm-config-${_v}")
  list(APPEND _llvm_lib_versioned "clang-${_v}")
endforeach()
list(REVERSE _llvm_config_versioned)
list(REVERSE _llvm_lib_versioned)
# Versioned names newest-first, then the unversioned fallback: a `clang-22` next
# to a `clang` of unknown vintage is the one that provably clears the floor.
set(_llvm_config_names ${_llvm_config_versioned} llvm-config)
set(_llvm_lib_names ${_llvm_lib_versioned} clang libclang)

# Allow an llvm-config to point us at the right prefix. Newer versions are
# listed first so a recent toolchain wins (c++23/c++26 need a recent clang).
find_program(LLVM_CONFIG_EXECUTABLE NAMES ${_llvm_config_names})
if(LLVM_CONFIG_EXECUTABLE)
  execute_process(
    COMMAND "${LLVM_CONFIG_EXECUTABLE}" --includedir
    OUTPUT_STRIP_TRAILING_WHITESPACE OUTPUT_VARIABLE _llvm_incdir
    ERROR_QUIET)
  execute_process(
    COMMAND "${LLVM_CONFIG_EXECUTABLE}" --libdir
    OUTPUT_STRIP_TRAILING_WHITESPACE OUTPUT_VARIABLE _llvm_libdir
    ERROR_QUIET)
endif()

find_path(
  LibClang_INCLUDE_DIR
  NAMES clang-c/Index.h
  HINTS ${LibClang_ROOT} ${_llvm_incdir}
  PATH_SUFFIXES include)

# NAMES_PER_DIR keeps the *directory* order authoritative -- without it CMake
# sweeps every directory for `clang-22` before ever trying `libclang` in the
# hinted prefix, so a system libclang would outrank an explicit LibClang_ROOT.
find_library(
  LibClang_LIBRARY
  NAMES ${_llvm_lib_names}
  NAMES_PER_DIR
  HINTS ${LibClang_ROOT} ${_llvm_libdir}
  PATH_SUFFIXES lib lib64)

# A found library still has to clear the floor: the unversioned names above
# match any vintage, so check before committing to it.
if(LibClang_INCLUDE_DIR AND LibClang_LIBRARY)
  _clangquill_libclang_major(_libclang_major "${LibClang_LIBRARY}")
  if(_libclang_major AND _libclang_major LESS CLANGQUILL_LIBCLANG_MIN_MAJOR)
    set(_libclang_too_old
        "libclang at ${LibClang_LIBRARY} is LLVM ${_libclang_major}, below the "
        "supported floor (${CLANGQUILL_LIBCLANG_MIN_MAJOR}). Install "
        "libclang-${CLANGQUILL_LIBCLANG_MIN_MAJOR}-dev (or newer) or set "
        "LibClang_ROOT to a newer LLVM prefix.")
    if(CLANGQUILL_WITH_LIBCLANG STREQUAL "ON")
      message(FATAL_ERROR ${_libclang_too_old})
    endif()
    message(STATUS "clangquill: ignoring libclang -- " ${_libclang_too_old})
    message(STATUS "clangquill: libclang not usable; building stub backend")
    return()
  endif()
  if(NOT _libclang_major)
    message(STATUS
      "clangquill: could not determine the LLVM version of "
      "${LibClang_LIBRARY}; assuming it is at least "
      "${CLANGQUILL_LIBCLANG_MIN_MAJOR}")
  endif()

  add_library(clangquill::libclang UNKNOWN IMPORTED)
  set_target_properties(
    clangquill::libclang PROPERTIES
    IMPORTED_LOCATION "${LibClang_LIBRARY}"
    INTERFACE_INCLUDE_DIRECTORIES "${LibClang_INCLUDE_DIR}")
  set(CLANGQUILL_LIBCLANG_FOUND TRUE)
  message(STATUS "clangquill: found libclang at ${LibClang_LIBRARY}")
elseif(CLANGQUILL_WITH_LIBCLANG STREQUAL "ON")
  message(
    FATAL_ERROR
    "CLANGQUILL_WITH_LIBCLANG=ON but libclang was not found. "
    "Install libclang-dev or set LibClang_ROOT to an LLVM prefix.")
else()
  message(STATUS "clangquill: libclang not found; building stub backend")
endif()

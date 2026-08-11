cmake_minimum_required(VERSION 3.26...3.29)

# Must run before project(): it sets CMAKE_TOOLCHAIN_FILE.
include(cmake/VcpkgBootStrap.cmake)

project(
  clangquill
  LANGUAGES CXX
  DESCRIPTION "libclang -> Sphinx MyST API-docs generator core")

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# --- Python + nanobind -------------------------------------------------------
# scikit-build-core puts the right Python here; find nanobind via its pip wheel.
# Development.SABIModule is required for nanobind's STABLE_ABI (below): it
# checks for the `Python::SABIModule` target and silently falls back to a
# regular per-version build (cp3<X>-cp3<X> tag, one wheel per interpreter)
# if that target isn't found -- it does not error, so this is easy to miss.
find_package(Python 3.11 REQUIRED
             COMPONENTS Interpreter Development.Module Development.SABIModule)

execute_process(
  COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
  OUTPUT_STRIP_TRAILING_WHITESPACE
  OUTPUT_VARIABLE nanobind_ROOT
  COMMAND_ERROR_IS_FATAL ANY)
find_package(nanobind CONFIG REQUIRED)

# --- C++ dependencies (always from vcpkg, see cmake/VcpkgBootStrap.cmake) ----
find_package(SQLite3 REQUIRED)
find_package(nlohmann_json CONFIG REQUIRED)

# --- Optional libclang -------------------------------------------------------
# CLANGQUILL_WITH_LIBCLANG: ON | OFF | AUTO (default). When ON/AUTO and libclang
# is found, the core links it and CLANGQUILL_HAVE_LIBCLANG is defined.
set(CLANGQUILL_WITH_LIBCLANG "AUTO" CACHE STRING "Link libclang: ON, OFF or AUTO")
include(cmake/FindLibClang.cmake)

# --- Core static library -----------------------------------------------------
# Always-built, libclang-independent sources (store + hashing). The parser
# sources additionally need libclang and are added below only when available.
set(CLANGQUILL_CORE_SOURCES
    src/cpp/hash/sha256.cpp
    src/cpp/hash/content_hash.cpp
    src/cpp/store/sqlite_store.cpp)

if(CLANGQUILL_LIBCLANG_FOUND)
  list(APPEND CLANGQUILL_CORE_SOURCES
       src/cpp/parser/cursor_utils.cpp
       src/cpp/parser/compile_db.cpp
       src/cpp/parser/references.cpp
       src/cpp/parser/comment_parser.cpp
       src/cpp/parser/doxygen_comment_parser.cpp
       src/cpp/parser/ast_visitor.cpp
       src/cpp/parser/parser.cpp)
endif()

add_library(clangquill_core STATIC ${CLANGQUILL_CORE_SOURCES})
target_include_directories(clangquill_core PUBLIC src/cpp)
target_compile_features(clangquill_core PUBLIC cxx_std_20)
set_target_properties(clangquill_core PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_link_libraries(clangquill_core
                      PUBLIC SQLite::SQLite3 nlohmann_json::nlohmann_json)

if(CLANGQUILL_LIBCLANG_FOUND)
  target_link_libraries(clangquill_core PUBLIC clangquill::libclang)
  target_compile_definitions(clangquill_core PUBLIC CLANGQUILL_HAVE_LIBCLANG=1)
  message(STATUS "clangquill: building WITH libclang")
else()
  message(STATUS "clangquill: building WITHOUT libclang (stub backend)")
endif()

# --- nanobind extension ------------------------------------------------------
nanobind_add_module(
  _core
  STABLE_ABI
  NB_STATIC
  src/cpp/bindings/module.cpp)
target_link_libraries(_core PRIVATE clangquill_core)
install(TARGETS _core LIBRARY DESTINATION clangquill)

# --- Catch2 tests (opt-in; never built in the wheel) -------------------------
option(CLANGQUILL_BUILD_TESTS "Build the C++ Catch2 tests" OFF)
if(CLANGQUILL_BUILD_TESTS)
  find_package(Catch2 3 CONFIG REQUIRED)
  enable_testing()
  file(GLOB CLANGQUILL_TEST_SOURCES CONFIGURE_DEPENDS tests/cpp/*.cpp)
  add_executable(clangquill_tests ${CLANGQUILL_TEST_SOURCES})
  target_link_libraries(clangquill_tests
                        PRIVATE clangquill_core Catch2::Catch2WithMain)
  target_compile_definitions(
    clangquill_tests
    PRIVATE CLANGQUILL_FIXTURE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/cpp/fixtures")
  include(Catch)
  catch_discover_tests(clangquill_tests)
endif()

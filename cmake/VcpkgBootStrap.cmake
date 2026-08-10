# Optional vcpkg bootstrap, included from the top-level CMakeLists *before*
# project() so the toolchain file it selects actually takes effect.
#
# Off by default: a build against system packages (libsqlite3-dev,
# nlohmann-json3-dev, catch2) needs none of this. Configure with
# -DCLANGQUILL_USE_VCPKG=ON and CMake fetches, bootstraps and hooks up vcpkg
# by itself -- callers (CI, developers) need no `git clone` + bootstrap step.
option(CLANGQUILL_USE_VCPKG "Fetch/bootstrap vcpkg and use it for the C++ dependencies" OFF)

if(CLANGQUILL_USE_VCPKG AND NOT DEFINED CMAKE_TOOLCHAIN_FILE)
    # Catch2 sits behind the `tests` feature of vcpkg.json; ask for it when the
    # tests are being built (CLANGQUILL_BUILD_TESTS is declared after project(),
    # but a -D on the command line is already in the cache here).
    if(CLANGQUILL_BUILD_TESTS)
        list(APPEND VCPKG_MANIFEST_FEATURES tests)
    endif()

    # VCPKG_ROOT in the environment means "use this existing installation".
    if(DEFINED ENV{VCPKG_ROOT})
        set(VCPKG_ROOT $ENV{VCPKG_ROOT})
        set(VCPKG_ENV_VAR "VCPKG_ROOT")
    endif()
    # _VCPKG_ROOT_DIR means we are being built as a dependency of another
    # package and want to share its vcpkg.
    if(DEFINED _VCPKG_ROOT_DIR)
        set(VCPKG_ROOT ${_VCPKG_ROOT_DIR})
        set(VCPKG_ENV_VAR "_VCPKG_ROOT_DIR")
    endif()

    set(VCPKG_FOUND 0)
    if(DEFINED VCPKG_ROOT)
        if(EXISTS "${VCPKG_ROOT}/scripts/buildsystems/vcpkg.cmake")
            message(STATUS "Using existing vcpkg (since ${VCPKG_ENV_VAR} was specified) from ${VCPKG_ROOT}")
            set(CMAKE_TOOLCHAIN_FILE
                "${VCPKG_ROOT}/scripts/buildsystems/vcpkg.cmake"
                CACHE FILEPATH "" FORCE)
            set(VCPKG_FOUND 1)
        else()
            message(
                WARNING "${VCPKG_ENV_VAR} was specified (see below), but does not contain a working vcpkg! "
                        "Resorting to the local checkout below. "
                        "In future invocations, consider to either not specify ${VCPKG_ENV_VAR} or point it "
                        "to a working vcpkg installation!"
                        "\n${VCPKG_ENV_VAR}: '${VCPKG_ROOT}'")
        endif()
    endif()

    # No usable VCPKG_ROOT, so work with a local copy of vcpkg.
    if(NOT ${VCPKG_FOUND})
        set(VCPKG_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/.vcpkg-checkout")
        if(EXISTS "${VCPKG_ROOT}/scripts/buildsystems/vcpkg.cmake")
            # If .vcpkg-checkout exists, we assume we've set it up and want to reuse it.
            message(STATUS "Using existing vcpkg from .vcpkg-checkout")
        else()
            message(STATUS "Setting up vcpkg for re-use in .vcpkg-checkout (this may take some time)")
            include(FetchContent)
            FetchContent_Declare(
                vcpkg
                GIT_REPOSITORY https://github.com/microsoft/vcpkg/
                GIT_TAG 2026.06.01
                SOURCE_DIR ${VCPKG_ROOT})
            FetchContent_MakeAvailable(vcpkg)
            message(STATUS "Setting up vcpkg for re-use in .vcpkg-checkout (this may take some time) - done")
        endif()
        # vcpkg.cmake builds the vcpkg tool on first use; keep that off the telemetry.
        set(VCPKG_BOOTSTRAP_OPTIONS "-disableMetrics" CACHE STRING "" FORCE)
        set(CMAKE_TOOLCHAIN_FILE
            "${VCPKG_ROOT}/scripts/buildsystems/vcpkg.cmake"
            CACHE FILEPATH "" FORCE)
    endif()
endif()

# We mainly use the toolchain file to hook up vcpkg with cmake, while compiler and generator are defined in
# (user) presets. A defined CMAKE_TOOLCHAIN_FILE pointing to a non-existing file is thus a very likely
# indication of a missing vcpkg installation. Independently, there is no good reason to continue with a
# declared intent of using a toolchain file, and the file missing.
if(DEFINED CMAKE_TOOLCHAIN_FILE AND NOT EXISTS "${CMAKE_TOOLCHAIN_FILE}")
    message(
        FATAL_ERROR
            "CMAKE_TOOLCHAIN_FILE points to a file that does not exist (or is not readable, see below).\n"
            "Continuing any further is not sensible!\n"
            "Either ensure the required dependencies are installed (e.g., vcpkg), "
            "or don't use a preset that requires a valid CMAKE_TOOLCHAIN_FILE, "
            "or don't define CMAKE_TOOLCHAIN_FILE!\n"
            "CMAKE_TOOLCHAIN_FILE points to '${CMAKE_TOOLCHAIN_FILE}'")
endif()

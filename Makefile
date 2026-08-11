#!/usr/bin/env make

# this loads $(ENV_FILE) as both makefile variables and into shell env
ENV_FILE?=.env
ifneq ($(wildcard $(ENV_FILE)),)
include $(ENV_FILE)
export $(shell sed 's/=.*//' $(ENV_FILE))
endif

.PHONY:  deps format docs cpp-test

deps:
	./dependencies.py

format:
	ruff format .
	ruff check --fix .

docs:
	make -C docs html

# Configure, build and run the C++ Catch2 tests. sqlite3, nlohmann-json and
# catch2 come from vcpkg, which the CMake build fetches and bootstraps itself
# (into .vcpkg-checkout); only libclang-dev is needed from the system.
cpp-test:
	cmake -S . -B build-cpp -G Ninja \
		-DCLANGQUILL_WITH_LIBCLANG=ON -DCLANGQUILL_BUILD_TESTS=ON \
		-DPython_EXECUTABLE=$$(which python3)
	cmake --build build-cpp
	ctest --test-dir build-cpp --output-on-failure

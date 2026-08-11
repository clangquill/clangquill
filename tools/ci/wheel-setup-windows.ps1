<#
One-shot Windows wheel-build setup, invoked from CIBW_BEFORE_ALL_WINDOWS.

Mirrors the manylinux CIBW_BEFORE_ALL_LINUX steps in the wheel workflows:
fetches the bundled libclang, stages the CI helper scripts at a fixed path
(CIBW_ENVIRONMENT_WINDOWS gets no {project} substitution, same restriction as
CIBW_ENVIRONMENT_LINUX -- hence copying to a fixed path rather than
referencing the checkout directly). vcpkg itself is fetched and bootstrapped
by the CMake build (see cmake/VcpkgBootStrap.cmake).
#>
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ciDir = "C:\ci"

New-Item -ItemType Directory -Force -Path $ciDir | Out-Null
Copy-Item "$repoRoot\tools\ci\fetch-libclang.ps1" "$ciDir\fetch-libclang.ps1" -Force
Copy-Item "$repoRoot\tools\ci\vcpkg-asset-fetch.ps1" "$ciDir\vcpkg-asset-fetch.ps1" -Force
Copy-Item "$repoRoot\tools\ci\llvm-version.txt" "$ciDir\llvm-version.txt" -Force

& "$ciDir\fetch-libclang.ps1" -Prefix C:\libclang
Copy-Item C:\libclang\LICENSE.TXT "$repoRoot\LICENSE-LLVM.txt" -Force

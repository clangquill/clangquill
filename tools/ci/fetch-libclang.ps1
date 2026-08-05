<#
Fetch a self-contained libclang for Windows from the official LLVM release and
unpack the pieces clangquill needs into a prefix usable as CMake's
LibClang_ROOT. Mirrors fetch-libclang.sh (see that file and docs ADR-0001 for
the full rationale); LLVM_BUILD_LLVM_DYLIB is unavailable on Windows in the
first place, so libclang.dll here is inherently self-contained (no separate
LLVM shared library to vendor), unlike the distro-libclang case that ADR-0001
ruled out on Linux.

Usage:  fetch-libclang.ps1 [-Prefix C:\libclang]
Env:    LLVM_VERSION (defaults to the pinned version in the sibling
                       llvm-version.txt -- the single source of truth read by
                       fetch-libclang.sh too)
#>
param(
    [string]$Prefix = "C:\libclang"
)

$ErrorActionPreference = "Stop"

$ver = $env:LLVM_VERSION
if (-not $ver) {
    $ver = (Get-Content (Join-Path $PSScriptRoot "llvm-version.txt") -Raw).Trim()
}

# The Windows release ships no slim "LLVM-<ver>-win64" tarball (only an NSIS
# .exe installer, which isn't stream-extractable); use the full clang+llvm
# archive instead and extract only the files needed.
$tarball = "clang+llvm-$ver-x86_64-pc-windows-msvc.tar.xz"
$url = "https://github.com/llvm/llvm-project/releases/download/llvmorg-$ver/$tarball"

New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
$archivePath = Join-Path $env:TEMP $tarball

Write-Host "fetch-libclang: downloading $url"
curl.exe -fsSL --retry 3 --retry-delay 2 $url -o $archivePath
if ($LASTEXITCODE -ne 0) {
    Write-Error "fetch-libclang: download failed"
    exit 1
}

Write-Host "fetch-libclang: extracting to $Prefix"
# Only unpack the pieces clangquill needs; the rest of the release (clang-cl,
# lld, static component libs, ...) is multiple GB unpacked and unused here.
tar.exe -xf $archivePath -C $Prefix --strip-components=1 `
    "*/bin/libclang.dll" "*/lib/libclang.lib" "*/include/clang-c/*"
Remove-Item $archivePath

if (-not (Test-Path (Join-Path $Prefix "include\clang-c\Index.h"))) {
    Write-Error "fetch-libclang: clang-c\Index.h missing"
    exit 1
}
if (-not (Test-Path (Join-Path $Prefix "bin\libclang.dll"))) {
    Write-Error "fetch-libclang: libclang.dll missing"
    exit 1
}
if (-not (Test-Path (Join-Path $Prefix "lib\libclang.lib"))) {
    Write-Error "fetch-libclang: libclang.lib missing"
    exit 1
}

# The archive ships no license file, so fetch clang's license (Apache-2.0
# WITH LLVM-exception) from the matching source tag, same as the Linux script.
$licenseUrl = "https://raw.githubusercontent.com/llvm/llvm-project/llvmorg-$ver/clang/LICENSE.TXT"
Write-Host "fetch-libclang: downloading $licenseUrl"
curl.exe -fsSL --retry 3 --retry-delay 2 $licenseUrl -o (Join-Path $Prefix "LICENSE.TXT")
if ($LASTEXITCODE -ne 0 -or (Get-Item (Join-Path $Prefix "LICENSE.TXT")).Length -eq 0) {
    Write-Error "fetch-libclang: LICENSE.TXT download failed"
    exit 1
}

Write-Host "fetch-libclang: installed libclang $ver (x86_64) to $Prefix"
Get-ChildItem (Join-Path $Prefix "bin\libclang.dll"), (Join-Path $Prefix "lib\libclang.lib")

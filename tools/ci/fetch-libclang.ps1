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
# Windows' built-in tar.exe (bsdtar) is pathologically slow decompressing a
# multi-GB .tar.xz like this one -- it can take *hours* even with a wildcard
# filter, since xz decompression can't skip unwanted entries. 7-Zip (shipped
# on GitHub's windows-2022 image) decodes xz far faster; pipe the archive
# through it twice (outer .xz -> .tar stream, inner .tar -> filtered files).
$extractTmp = Join-Path $env:TEMP "libclang-extract"
if (Test-Path $extractTmp) {
    Remove-Item -Recurse -Force $extractTmp
}
New-Item -ItemType Directory -Force -Path $extractTmp | Out-Null

$sevenZip = (Get-Command 7z.exe -ErrorAction SilentlyContinue).Source
if (-not $sevenZip) {
    $sevenZip = Join-Path ${env:ProgramFiles} "7-Zip\7z.exe"
}

& $sevenZip x $archivePath -so |
    & $sevenZip x -si -ttar -y -r -o"$extractTmp" `
        "libclang.dll" "libclang.lib" "clang-c/*" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "fetch-libclang: extraction failed"
    exit 1
}
Remove-Item $archivePath

# Emulate tar's --strip-components=1: the archive unpacks under a single
# top-level "clang+llvm-<ver>-..." directory; hoist its contents into $Prefix.
$topDir = Get-ChildItem $extractTmp | Select-Object -First 1
if (-not $topDir) {
    Write-Error "fetch-libclang: extraction produced no files"
    exit 1
}
Get-ChildItem $topDir.FullName | Move-Item -Destination $Prefix -Force
Remove-Item -Recurse -Force $extractTmp

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

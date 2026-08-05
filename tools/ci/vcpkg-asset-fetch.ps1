<#
vcpkg asset-cache fetch helper for Windows, wired up via
  X_VCPKG_ASSET_SOURCES="x-script,powershell ... -File <this> {url} {sha512} {dst}"

Mirrors vcpkg-asset-fetch.sh (see that file for the full rationale): when the
Windows wheel is built in CI, vcpkg has to download the sqlite3 amalgamation
from sqlite.org, which serves HTTP 403 to vcpkg's built-in downloader (it
rejects non-browser User-Agents and some cloud egress IPs). Fetching the same
artifact with a normal browser User-Agent -- and a couple of byte-identical
mirrors as a backup -- gets past that block. vcpkg verifies the SHA512 of
whatever we produce, so a wrong or corrupt file is rejected and vcpkg falls
back to the authoritative URL.

Args (substituted by vcpkg): Url Sha512 Dst
#>
param(
    [string]$Url,
    [string]$Sha512,
    [string]$Dst
)

if (-not $Url -or -not $Dst) {
    Write-Error "vcpkg-asset-fetch: missing url/dst arguments"
    exit 2
}

# A real browser UA: sqlite.org's anti-robot filter rejects vcpkg/curl/wget.
$ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/128.0"
$base = Split-Path -Leaf ([Uri]$Url).LocalPath

function Fetch([string]$Source) {
    Write-Host "vcpkg-asset-fetch: trying $Source"
    # --retry covers transient/5xx errors; a hard 4xx (e.g. sqlite.org's 403)
    # fails fast so we move on to the next source instead of hammering it.
    curl.exe -fsSL --retry 3 --retry-delay 1 -A $ua -o $Dst $Source
    return ($LASTEXITCODE -eq 0) -and (Test-Path $Dst) -and ((Get-Item $Dst).Length -gt 0)
}

# 1) The authoritative URL, but with a browser User-Agent.
if (Fetch $Url) { exit 0 }

# 2) Byte-identical mirrors of the sqlite autoconf tarballs (SHA512-guarded by
#    vcpkg), in case the upstream block is IP-based rather than UA-based.
$mirrors = @(
    "https://distfiles.macports.org/sqlite3/$base",
    "https://fossies.org/linux/misc/$base"
)
foreach ($mirror in $mirrors) {
    if (Fetch $mirror) { exit 0 }
}

Write-Error "vcpkg-asset-fetch: all sources failed for $Url"
exit 1

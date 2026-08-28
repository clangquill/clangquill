#!/usr/bin/env bash
# vcpkg asset-cache fetch helper, wired up via
#   X_VCPKG_ASSET_SOURCES="x-script,<this> {url} {sha512} {dst}"
#
# Why this exists: when the manylinux wheels are built in CI, vcpkg has to
# download the sqlite3 amalgamation from sqlite.org. sqlite.org serves HTTP 403
# to vcpkg's built-in downloader (it rejects non-browser User-Agents and some
# cloud egress IPs), which fails the whole wheel build right after the (fast)
# nlohmann-json build. Fetching the same artifact with a normal browser
# User-Agent — and a couple of byte-identical mirrors as a backup — gets past
# that block. vcpkg verifies the SHA512 of whatever we produce, so a wrong or
# corrupt file is rejected and vcpkg falls back to the authoritative URL.
#
# Args (substituted and shell-escaped by vcpkg):
#   $1 = {url}     upstream asset URL
#   $2 = {sha512}  expected SHA512 (vcpkg checks it; we keep it for clarity)
#   $3 = {dst}     output path to write the asset to
set -u

url="${1:-}"
dst="${3:-}"
if [ -z "$url" ] || [ -z "$dst" ]; then
  echo "vcpkg-asset-fetch: missing url/dst arguments" >&2
  exit 2
fi

base="${url##*/}"
# A real browser UA: sqlite.org's anti-robot filter rejects vcpkg/curl/wget.
ua='Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0'

fetch() {
  echo "vcpkg-asset-fetch: trying $1" >&2
  # --retry covers transient/5xx errors; a hard 4xx (e.g. sqlite.org's 403)
  # fails fast so we move on to the next source instead of hammering it.
  curl -fsSL --retry 3 --retry-delay 1 \
       -A "$ua" -o "$dst" "$1" && [ -s "$dst" ]
}

# 1) The authoritative URL, but with a browser User-Agent.
fetch "$url" && exit 0

# 2) Byte-identical mirrors of the sqlite autoconf tarballs (SHA512-guarded by
#    vcpkg), in case the upstream block is IP-based rather than UA-based.
for mirror in \
  "https://distfiles.macports.org/sqlite3/${base}" \
  "https://fossies.org/linux/misc/${base}"; do
  fetch "$mirror" && exit 0
done

echo "vcpkg-asset-fetch: all sources failed for $url" >&2
exit 1

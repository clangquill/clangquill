#!/usr/bin/env bash
# Install LLVM 22 from apt.llvm.org using a repository-controlled key and a
# pinned llvm-toolchain-<ubuntu-codename>-22 apt source.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
key_file="${here}/apt.llvm.org-snapshot.gpg.key"
expected_fpr="6084F3CF814B57C1CF12EFD515CF4D18AF4F7421"

if [ ! -s "${key_file}" ]; then
  echo "install-llvm-22-apt: missing key file ${key_file}" >&2
  exit 1
fi

actual_fpr="$(gpg --show-keys --with-colons "${key_file}" | awk -F: '/^fpr:/ { print $10; exit }')"
if [ "${actual_fpr}" != "${expected_fpr}" ]; then
  echo "install-llvm-22-apt: unexpected apt.llvm.org key fingerprint ${actual_fpr}" >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [ "${ID:-}" != "ubuntu" ] || [ -z "${VERSION_CODENAME:-}" ]; then
  echo "install-llvm-22-apt: expected Ubuntu with VERSION_CODENAME" >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y --no-install-recommends ca-certificates gpg

sudo install -d -m 0755 /usr/share/keyrings
sudo gpg --dearmor --yes --output /usr/share/keyrings/apt.llvm.org.gpg "${key_file}"

sudo tee /etc/apt/sources.list.d/apt.llvm.org.sources >/dev/null <<APT_SOURCE
Types: deb
URIs: https://apt.llvm.org/${VERSION_CODENAME}/
Suites: llvm-toolchain-${VERSION_CODENAME}-22
Components: main
Signed-By: /usr/share/keyrings/apt.llvm.org.gpg
APT_SOURCE

sudo apt-get update
sudo apt-get install -y --no-install-recommends libclang-22-dev llvm-22-dev

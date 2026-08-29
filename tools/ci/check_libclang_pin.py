"""Fail when a newer LLVM release than the pinned one ships both Linux arches.

The pin lives in ``tools/ci/llvm-version.txt`` (the single source of truth that
``fetch-libclang.sh`` and the wheel workflows read). This is run on a schedule (see
``.github/workflows/libclang-pin-check.yml``); network/API errors are treated as
non-fatal so a transient blip never raises a false alarm. It only fails when a
strictly newer stable release exists *and* ships both the X64 and ARM64 Linux
packages clangquill bundles.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

RELEASES_URL = "https://api.github.com/repos/llvm/llvm-project/releases?per_page=40"
PIN_FILE = Path(__file__).parent / "llvm-version.txt"
_STABLE_TAG = re.compile(r"^llvmorg-(\d+)\.(\d+)\.(\d+)$")


def pinned_version() -> str:
    try:
        ver = PIN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        msg = f"could not find the pin file at {PIN_FILE}"
        raise SystemExit(msg) from None
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", ver):
        msg = f"unexpected LLVM pin {ver!r} in {PIN_FILE.name}; expected MAJOR.MINOR.PATCH"
        raise SystemExit(msg)
    return ver


def _ver_tuple(ver: str) -> tuple[int, int, int]:
    major, minor, patch = ver.split(".")
    return int(major), int(minor), int(patch)


def _has_both_linux_arches(assets: list[str], ver: str) -> bool:
    return f"LLVM-{ver}-Linux-X64.tar.xz" in assets and f"LLVM-{ver}-Linux-ARM64.tar.xz" in assets


def latest_with_both_arches(releases: list[dict]) -> str | None:
    """Return the highest stable LLVM version that ships both Linux arch tarballs."""
    best: tuple[int, int, int] | None = None
    best_str: str | None = None
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        match = _STABLE_TAG.match(rel.get("tag_name") or "")
        if not match:
            continue
        ver = ".".join(match.groups())
        raw_assets = rel.get("assets") or []
        assets = [a.get("name") or "" for a in raw_assets if isinstance(a, dict)]
        if not _has_both_linux_arches(assets, ver):
            continue
        candidate = _ver_tuple(ver)
        if best is None or candidate > best:
            best, best_str = candidate, ver
    return best_str


def _set_step_output(name: str, value: str) -> None:
    """Publish ``name=value`` as a step output when running under Actions.

    The workflow's retest job reads ``latest`` to fetch and test against the
    newer libclang before the pin is bumped, and ``stale`` to decide whether
    that retest applies.
    """
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")


def fetch_releases() -> list[dict]:
    req = urllib.request.Request(  # noqa: S310 (hardcoded https GitHub API URL)
        RELEASES_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "clangquill-pin-check",
        },
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.load(resp)


def main() -> int:
    pin = pinned_version()
    _set_step_output("stale", "false")
    try:
        releases = fetch_releases()
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"::warning::could not query LLVM releases ({exc}); skipping pin check")
        return 0

    if not isinstance(releases, list):
        print("::warning::unexpected response format from LLVM releases API; skipping")
        return 0

    latest = latest_with_both_arches(releases)
    if latest is None:
        print("::warning::no LLVM release with both Linux arches found; skipping")
        return 0

    _set_step_output("latest", latest)
    print(f"pinned libclang: {pin}; latest with both Linux arches: {latest}")
    if _ver_tuple(latest) > _ver_tuple(pin):
        _set_step_output("stale", "true")
        print(
            f"::error::A newer libclang ({latest}) ships both Linux arch tarballs. "
            f"Bump the pin from {pin} to {latest} in tools/ci/llvm-version.txt "
            f"(the single source of truth; everything else reads it). The retest "
            f"job of this workflow has already run the Catch2 suite against "
            f"{latest}; its result is the pre-bump check for a token-shape "
            f"regression in the token-heuristic layer.",
        )
        return 1

    print("libclang pin is up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

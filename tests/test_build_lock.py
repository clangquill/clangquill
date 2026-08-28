"""Unit tests for the single-writer ``cache_dir`` lock (see #311).

These exercise :func:`clangquill._lock.build_lock` directly, independent of
the pipeline and of libclang, since the lock itself is pure Python/OS calls.
Two independent ``os.open()`` calls on the same path get separate open file
descriptions, so contending for the lock across two threads in one process
(as done here) exercises exactly the same OS-level mutual exclusion two
separate build() *processes* would — that is what makes ``flock``/
``msvcrt.locking`` process-level locks in the first place.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import pytest

from clangquill._lock import LOCK_NAME, BuildLockTimeoutError, build_lock

if TYPE_CHECKING:
    from pathlib import Path


def test_a_second_acquire_waits_for_the_first_to_release(tmp_path: Path) -> None:
    events: list[str] = []
    events_lock = threading.Lock()

    def record(label: str) -> None:
        with events_lock:
            events.append(label)

    def holder() -> None:
        with build_lock(tmp_path, timeout=5):
            record("A-acquired")
            time.sleep(0.2)
            # Recorded while still holding the lock, so this can never be
            # reordered after "B-acquired" by the OS releasing early.
            record("A-released")

    started = threading.Event()

    def waiter() -> None:
        started.wait()
        with build_lock(tmp_path, timeout=5):
            record("B-acquired")

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=waiter)
    t1.start()
    time.sleep(0.05)  # let the holder actually acquire before the waiter tries
    started.set()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert events == ["A-acquired", "A-released", "B-acquired"]


def test_lock_times_out_naming_the_holder(tmp_path: Path) -> None:
    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with build_lock(tmp_path, timeout=5):
            holding.set()
            release.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert holding.wait(timeout=5)
        with pytest.raises(BuildLockTimeoutError) as excinfo, build_lock(tmp_path, timeout=0.3):
            pass
        # Whoever holds it is named by pid, not left as a bare "timed out".
        assert "pid" in str(excinfo.value)
        assert str(tmp_path) in str(excinfo.value)
    finally:
        release.set()
        t.join(timeout=5)


def test_lock_is_released_on_exception(tmp_path: Path) -> None:
    boom = "boom"
    with pytest.raises(RuntimeError, match=boom), build_lock(tmp_path, timeout=5):
        raise RuntimeError(boom)

    # A crash while holding the lock must not wedge every future build: the OS
    # releases it when the process/fd goes away, so re-acquiring right after
    # must succeed immediately rather than time out.
    with build_lock(tmp_path, timeout=0.5):
        pass


def test_lock_creates_a_lockfile_inside_cache_dir(tmp_path: Path) -> None:
    with build_lock(tmp_path, timeout=5):
        pass
    assert (tmp_path / LOCK_NAME).is_file()


def test_lock_creates_cache_dir_if_missing(tmp_path: Path) -> None:
    cache_dir = tmp_path / "nested" / "cache"
    with build_lock(cache_dir, timeout=5):
        pass
    assert cache_dir.is_dir()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_lock_rejects_a_timeout_that_could_never_be_reached(tmp_path: Path, timeout: float) -> None:
    # Config.validate() is the primary gate (see tests/test_config.py), but
    # build_lock itself must not silently wait forever if that gate is ever
    # bypassed -- NaN and +inf both pass a bare `timeout <= 0` check.
    with pytest.raises(ValueError, match="cache_lock_timeout"), build_lock(tmp_path, timeout=timeout):
        pass

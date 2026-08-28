"""A single-writer file lock serializing incremental builds on one ``cache_dir``.

An incremental build (see :mod:`clangquill.pipeline`) mutates the bookkeeping
database, the IR database and the output tree in a non-atomic sequence, and
none of the three is guarded by a cross-store transaction. Two builds against
the same ``cache_dir`` running at once (a ``sphinx-autobuild`` racing a manual
build, two CI jobs sharing a workspace) can therefore observe or leave
inconsistent state. This module makes that impossible by design rather than
by argument: :func:`build_lock` is a single OS-level advisory lock that
:func:`clangquill.pipeline.build` holds for the whole of an incremental
build, so at most one build runs against a given ``cache_dir`` at a time. A
second build waits, and either acquires the lock once the first finishes or
times out with an error naming the process that is still holding it.

The lock is a plain advisory file lock (``fcntl.flock`` on POSIX,
``msvcrt.locking`` on Windows) on a lockfile inside ``cache_dir``, held for
as long as the holding process keeps its file descriptor open — including if
that process is killed, since the OS releases the lock when the descriptor
closes. It does not protect readers (a `Store.open` of the IR while a build
is in flight is the project's existing "concurrent readers are fine"
contract; see ``docs/guides/configuration.md``), only concurrent writers.
"""

from __future__ import annotations

import os
import socket
import time
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

#: Lockfile name within a configured cache directory.
LOCK_NAME = ".clangquill-build.lock"

#: How often to retry a non-blocking lock attempt while waiting.
_POLL_INTERVAL_S = 0.1


class BuildLockTimeoutError(RuntimeError):
    """Raised when a build cannot acquire the ``cache_dir`` lock in time.

    Another clangquill build already holds the lock; either it is legitimately
    still running (raise the timeout, or wait longer) or it crashed while
    holding an OS-level lock the crash itself could never release by hand (in
    which case the OS already released it when that process exited, and this
    error means a *different*, still-live process holds it now).
    """


def _try_lock(fd: int) -> bool:
    """Attempt to take the lock without blocking; return whether it succeeded."""
    if os.name == "nt":
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        with suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        with suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


def _read_holder(lock_path: Path) -> str:
    """Best-effort description of who last held the lock, for the timeout error.

    Reading the file's content is not itself synchronized with the holder
    writing it, so this is advisory diagnostic text, not a source of truth.
    """
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "an unknown process"
    return text or "an unknown process"


@contextmanager
def build_lock(cache_dir: Path, *, timeout: float) -> Iterator[None]:
    """Hold the single-writer lock on ``cache_dir`` for the block's duration.

    Blocks (polling) until the lock is free or ``timeout`` seconds have
    passed, whichever comes first; on timeout raises
    :class:`BuildLockTimeoutError` naming the process the lockfile's content
    says is holding it.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / LOCK_NAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # msvcrt locks a byte range; the file needs at least one byte to lock.
        if os.name == "nt" and os.fstat(fd).st_size < 1:
            os.write(fd, b"\0")
            os.fsync(fd)
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                holder = _read_holder(lock_path)
                msg = (
                    f"Timed out after {timeout:g}s waiting for the build lock on "
                    f"{cache_dir} (currently held by {holder}). Only one clangquill "
                    "build may run against a given cache_dir at a time; wait for the "
                    "other build to finish, or raise cache_lock_timeout if it is "
                    "just slow."
                )
                raise BuildLockTimeoutError(msg)
            time.sleep(_POLL_INTERVAL_S)
        # Held from here on: record who we are so the next waiter's timeout
        # error (if any) can name us instead of whoever held it before.
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"pid {os.getpid()} on {socket.gethostname()}\n".encode())
        os.fsync(fd)
        try:
            yield
        finally:
            with suppress(OSError):
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
    finally:
        _unlock(fd)
        os.close(fd)

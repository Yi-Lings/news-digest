"""Locked, durable, atomic writes for Admin-managed configuration files."""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_FILE_MODE = 0o600


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def locked_path(path: Path) -> Iterator[None]:
    """Serialize access to *path* across threads and processes."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f".{target.name}.lock")
    thread_lock = _thread_lock(lock_path)
    with thread_lock:
        with lock_path.open("a+b") as handle:
            os.chmod(lock_path, _FILE_MODE)
            _lock_handle(handle)
            try:
                yield
            finally:
                _unlock_handle(handle)


def _lock_handle(handle) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_handle(handle) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes_unlocked(
    path: Path,
    content: bytes,
    *,
    mode: int = _FILE_MODE,
) -> None:
    """Replace *path* durably; caller must already hold ``locked_path(path)``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:  # pragma: no cover - Windows has no os.fchmod
            os.chmod(temporary, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, mode)
        _fsync_directory(target.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_bytes(path: Path, content: bytes) -> None:
    with locked_path(path):
        atomic_write_bytes_unlocked(path, content)


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def update_text(path: Path, transform: Callable[[str], str]) -> str:
    """Read, transform, and atomically replace a UTF-8 file under one lock."""
    target = Path(path)
    with locked_path(target):
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
        updated = transform(current)
        atomic_write_bytes_unlocked(target, updated.encode("utf-8"))
        return updated

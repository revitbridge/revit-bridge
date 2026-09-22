"""One JSON document on disk, shared between hosts: read, locked read-modify-write, atomic replace.

``usage.json`` (capability statistics) and ``auth/devices.json`` (pairing
codes and device tokens) are small documents that several hosts (the MCP
server, the web host) may update from the same data directory. Each update
is a read-modify-write of the whole file, serialised through a
``<name>.lock`` file created with ``O_CREAT | O_EXCL``. A writer that cannot
take the lock within ``lock_timeout`` seconds goes ahead anyway - a lost
update costs less than a stuck host - and a lock older than
``LOCK_STALE_SECONDS`` is treated as left behind by a dead process. Only
``FileExistsError`` means "busy"; a ``PermissionError`` is retried for at
most ``PERMISSION_RETRY_SECONDS`` (on Windows the create fails that way for
a moment while the previous holder's unlink is pending) and any other
``OSError`` (read-only data root, missing directory) skips the lock at
once. The write goes to ``<name>.tmp`` and is moved into place with
``os.replace``; on Windows a reader holding the file blocks the replace for
a moment, so that is retried for ``REPLACE_RETRY_SECONDS``.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_SUFFIX = ".lock"
LOCK_TIMEOUT_SECONDS = 2.0     # how long a writer waits for another host
LOCK_STALE_SECONDS = 10.0      # a lock this old belongs to a process that died
PERMISSION_RETRY_SECONDS = 0.1 # Windows pending-delete window; a read-only root stalls no longer
REPLACE_RETRY_SECONDS = 1.0    # Windows: a reader holding the file blocks os.replace
_LOCK_POLL_SECONDS = 0.02


class LockedJsonFile:
    """A JSON object file with a sibling lock file (see module doc).

    ``read`` never raises: a missing, unreadable or corrupt file - or one
    whose top level is not an object - reads as ``{}``. Callers hold
    ``locked()`` around every read-modify-write and call ``write`` inside it.
    """

    def __init__(self, path: Path | str, lock_timeout: float = LOCK_TIMEOUT_SECONDS):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + LOCK_SUFFIX)
        self.lock_timeout = lock_timeout

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write(self, data: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # Readers do not take the lock; on Windows a file that one of them has
        # open cannot be replaced, so wait it out.
        deadline = time.monotonic() + REPLACE_RETRY_SECONDS
        while True:
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_LOCK_POLL_SECONDS)

    @contextmanager
    def locked(self):
        """Hold the lock file for one read-modify-write (see module doc)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # the write will report it
        deadline = time.monotonic() + self.lock_timeout
        denied_until = None
        held = False
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._lock_is_stale():
                    self._release()
                    continue
                if time.monotonic() >= deadline:
                    break  # proceed unlocked: a lost update beats a stuck host
                time.sleep(_LOCK_POLL_SECONDS)
                continue
            except PermissionError:
                # Either the directory is not writable, or (Windows) the previous
                # holder's unlink is still pending: wait a moment, not the full timeout.
                if denied_until is None:
                    denied_until = time.monotonic() + PERMISSION_RETRY_SECONDS
                if time.monotonic() >= denied_until:
                    break
                time.sleep(_LOCK_POLL_SECONDS)
                continue
            except OSError:
                break  # cannot create files here at all; the write will say so
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            held = True
            break
        try:
            yield
        finally:
            if held:
                self._release()

    def _lock_is_stale(self) -> bool:
        try:
            return time.time() - self.lock_path.stat().st_mtime > LOCK_STALE_SECONDS
        except OSError:
            return False  # gone already; the next attempt will take it

    def _release(self) -> None:
        try:
            self.lock_path.unlink()
        except OSError:
            pass

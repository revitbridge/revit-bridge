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
once. The write goes to ``<name>.tmp``, is flushed to the disk (``fsync``)
and is moved into place with ``os.replace``; on Windows a reader holding the
file blocks the replace for a moment, so that is retried for
``REPLACE_RETRY_SECONDS``.

The read of an update (``read_for_update``, inside ``locked()``) is strict
where the plain ``read`` is tolerant: only a missing or empty file is ``{}``.
Any other read failure (a scanner holding the file, a sharing violation) is
retried for ``READ_RETRY_SECONDS`` and then raised - going on with ``{}``
would write an empty document over the real one (every paired device, every
counter gone). A file that reads but is not a JSON object is copied to
``<name>.bad`` (``.bad.2``, ``.bad.3``, ... - never over an earlier copy)
before the update replaces it. Both reads accept a leading UTF-8 BOM (a file
saved by Notepad or Windows PowerShell 5.1); what is written has none.
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
READ_RETRY_SECONDS = 1.0       # an update's read: a scanner or a reader holding the file
BAD_SUFFIX = ".bad"            # where a file that does not parse is kept before an update replaces it
_LOCK_POLL_SECONDS = 0.02


class LockedJsonFile:
    """A JSON object file with a sibling lock file (see module doc).

    ``read`` never raises: a missing, unreadable or corrupt file - or one
    whose top level is not an object - reads as ``{}``; it is for callers
    that only read. Callers hold ``locked()`` around every read-modify-write
    and read with ``read_for_update`` and call ``write`` inside it.
    """

    def __init__(self, path: Path | str, lock_timeout: float = LOCK_TIMEOUT_SECONDS):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + LOCK_SUFFIX)
        self.lock_timeout = lock_timeout

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def read_for_update(self) -> dict:
        """The document a read-modify-write starts from (call it inside ``locked()``).

        A missing or empty file is ``{}``. Any other read failure is retried for
        ``READ_RETRY_SECONDS`` and then raised, so the update never writes back an
        empty document that is not the file's content. A file that reads but is not
        a JSON object is copied aside (``_keep_bad``; raising when that copy cannot be
        written) and is then ``{}``, for the update to replace.
        """
        deadline = time.monotonic() + READ_RETRY_SECONDS
        while True:
            try:
                raw = self.path.read_bytes()
                break
            except FileNotFoundError:
                return {}
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            text = raw.decode("utf-8-sig")                # a BOM (Notepad, PowerShell 5.1) is fine
        except UnicodeDecodeError:
            text = None
        if text is not None and not text.strip():
            return {}
        try:
            data = json.loads(text) if text is not None else None
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data
        self._keep_bad(raw)
        return {}

    def _keep_bad(self, raw: bytes) -> Path:
        """Copy a file that does not parse to ``<name>.bad`` - or ``.bad.2``, ``.bad.3``, ... when
        that exists: an earlier copy is never overwritten, since each may hold the only trace of
        what was written between two corruptions (the files are small; corruption is rare)."""
        n = 1
        while True:
            bad = self.path.with_name(self.path.name + BAD_SUFFIX + (f".{n}" if n > 1 else ""))
            try:
                with open(bad, "xb") as handle:
                    handle.write(raw)
                return bad
            except FileExistsError:
                n += 1

    def write(self, data: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())                     # on the disk before it replaces the old file
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

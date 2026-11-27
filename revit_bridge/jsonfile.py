"""One JSON document on disk, shared between hosts: read, locked read-modify-write, atomic replace.

``usage.json`` (capability statistics), ``auth/devices.json`` (pairing
codes and device tokens) and the verification plans are small documents
that several hosts (the MCP server, the web host) may update from the same
data directory. Each update is a read-modify-write of the whole file,
serialised through a ``<name>.lock`` file created with ``O_CREAT | O_EXCL``
that holds a token of its holder.

Taking the lock. ``FileExistsError`` means "busy" and the writer polls. It
waits as long as the lock keeps changing hands (a new lock file is a holder
that finished and the next one that started); when one holder keeps the
lock for ``lock_timeout`` seconds, or after ``LOCK_WAIT_LIMIT_SECONDS`` in
all, it goes ahead without the lock - a lost update costs less than a stuck
host. A lock older than ``LOCK_STALE_SECONDS`` was left behind by a dead
process: the writer renames it to a name of its own (of several writers only
one moves it), deletes it when what it moved is still stale and puts it back
when it is not (another writer took the stale lock over first and holds this
one). A ``PermissionError`` has two causes that ``os.open`` reports alike
(``EACCES``, no ``winerror``): on Windows the lock's name is still taken by
a deleted lock some handle keeps open (pending delete), which is waited for
like a busy lock, or no file can be created in the directory at all (a
read-only data root). The writer tells them apart by creating a probe file
of its own next to the document: when that fails too, the writer goes ahead
without the lock after ``PERMISSION_RETRY_SECONDS``. Any other ``OSError``
skips the lock at once; the write reports what is wrong. Releasing deletes
the lock only while it holds the holder's token (a holder whose lock was
taken over never deletes its successor's). On Windows a handle without
delete sharing refuses the unlink for a moment, so that is retried for
``RELEASE_RETRY_SECONDS``; a lock that still cannot be deleted is dated back
for the next writer to take over instead of waiting it out.

Writing. Each write goes to a temp file of its own,
``<name>.<pid>-<random>.tmp`` (created exclusively), is flushed to the disk
(``fsync``) and is moved into place with ``os.replace``. Two writers - one
of them without the lock - never write into or move each other's temp file
(sharing one name lost a write to ``FileNotFoundError`` or left the start of
one write and the end of the other), so the document is always one complete
write; when two race, the later replace wins. On Windows a reader holding
the document blocks the replace for a moment, so that is retried for
``REPLACE_RETRY_SECONDS``. A temp file that is not moved into place is
deleted; one that disappeared before its replace is written once more.
After a write, temp files of the document older than ``TEMP_STALE_SECONDS``
(a writer that died, the ``<name>.tmp`` of earlier versions) are deleted;
no read looks at a temp file. The retry budgets of a holder stay well below
a waiter's ``lock_timeout`` (see the constants), so a waiter does not go on
unlocked while a live holder is still inside its read-modify-write.

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
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_SUFFIX = ".lock"
# A holder spends at most READ_RETRY_SECONDS + REPLACE_RETRY_SECONDS (1.5 s) in retries inside its
# read-modify-write and RELEASE_RETRY_SECONDS (0.25 s) more until its lock is gone, plus fsync and its
# own work; a waiter goes on unlocked only when one holder has kept the lock for LOCK_TIMEOUT_SECONDS
# (4 s, more than twice that) and takes a lock over only after LOCK_STALE_SECONDS (10 s). A live
# holder is therefore neither outwaited nor taken over (tests/test_usage.py keeps the ratios). While
# the lock changes hands a writer keeps waiting, for LOCK_WAIT_LIMIT_SECONDS at most. A temp file
# lives for one write (fsync and REPLACE_RETRY_SECONDS), far less than TEMP_STALE_SECONDS.
LOCK_TIMEOUT_SECONDS = 4.0      # how long a writer waits for one holder (another host)
LOCK_WAIT_LIMIT_SECONDS = 30.0  # how long a writer waits in all while the lock keeps changing hands
LOCK_STALE_SECONDS = 10.0       # a lock this old belongs to a process that died
PERMISSION_RETRY_SECONDS = 0.1  # no file can be created here (read-only root): it stalls no longer
REPLACE_RETRY_SECONDS = 1.0     # Windows: a reader holding the file blocks os.replace
READ_RETRY_SECONDS = 0.5        # an update's read: a scanner or a reader holding the file
RELEASE_RETRY_SECONDS = 0.25    # Windows: a handle without delete sharing blocks the lock's unlink
TEMP_SUFFIX = ".tmp"            # <name>.<pid>-<random>.tmp: a writer's new document, a probe, a moved lock
TEMP_STALE_SECONDS = 60.0       # a temp file this old belongs to a writer that died
BAD_SUFFIX = ".bad"             # where a file that does not parse is kept before an update replaces it
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
        """Replace the document with ``data`` through a temp file of this writer's own (module doc)."""
        text = json.dumps(data, ensure_ascii=False, indent=2)
        deadline = None                                   # one replace budget for both attempts
        for attempt in (1, 2):
            tmp = self._temp_path()
            try:
                with open(tmp, "x", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())             # on the disk before it replaces the old file
                if deadline is None:
                    deadline = time.monotonic() + REPLACE_RETRY_SECONDS
                self._replace(tmp, deadline)
            except FileNotFoundError:
                _remove(tmp)
                if attempt == 2:
                    raise
                continue  # removed before the replace (a sweep, a person): write it once more
            except BaseException:
                _remove(tmp)
                raise
            break
        self._sweep_temp_files()

    def _replace(self, tmp: Path, deadline: float) -> None:
        # Readers do not take the lock; on Windows a file that one of them has
        # open cannot be replaced, so wait it out.
        while True:
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_LOCK_POLL_SECONDS)

    def _temp_path(self) -> Path:
        """A new name next to the document that no other writer uses: ``<name>.<pid>-<random>.tmp``."""
        return self.path.with_name(f"{self.path.name}.{os.getpid()}-{secrets.token_hex(6)}{TEMP_SUFFIX}")

    def _sweep_temp_files(self) -> None:
        """Delete this document's temp files (``_temp_path`` names and the ``<name>.tmp`` of earlier
        versions) older than ``TEMP_STALE_SECONDS``; never raises."""
        pattern = re.compile(re.escape(self.path.name) + r"(\.[0-9]+-[0-9a-f]{12})?" + re.escape(TEMP_SUFFIX))
        try:
            with os.scandir(self.path.parent) as entries:
                temps = [entry for entry in entries if pattern.fullmatch(entry.name)]
        except OSError:
            return
        cutoff = time.time() - TEMP_STALE_SECONDS
        for entry in temps:
            try:
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.path)
            except OSError:
                pass  # gone already, or still open: the next sweep

    @contextmanager
    def locked(self, *, required: bool = False):
        """Hold the writer lock. With required=True, raise if it could not be acquired."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # the write will report it
        token = self._acquire()
        if required and token is None:
            raise TimeoutError("could not acquire the file lock")
        try:
            yield
        finally:
            if token is not None:
                self._release(token)

    def _acquire(self) -> bytes | None:
        """Create the lock file: its token, or None when the writer goes on without the lock."""
        token = f"{os.getpid()} {secrets.token_hex(8)}".encode("ascii")
        started = time.monotonic()
        give_up_at = started + max(self.lock_timeout, LOCK_WAIT_LIMIT_SECONDS)
        holder, holder_since = None, started   # the lock file waited on, and since when
        writable = False                       # a probe file could be created next to the document
        denied_since = None
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    st = os.stat(self.lock_path)
                except OSError:
                    st = None                  # released just now; the next attempt will tell
                if st is not None and time.time() - st.st_mtime > LOCK_STALE_SECONDS:
                    if self._take_over_stale():
                        continue
                elif st is not None and (st.st_ino, st.st_mtime_ns) != holder:
                    holder, holder_since = (st.st_ino, st.st_mtime_ns), time.monotonic()
            except PermissionError:
                # Windows: a deleted lock some handle still keeps open (busy, wait) - or no file
                # can be created here at all (read-only data root: give up soon).
                writable = writable or self._can_create_files()
                if not writable:
                    if denied_since is None:
                        denied_since = time.monotonic()
                    elif time.monotonic() - denied_since >= PERMISSION_RETRY_SECONDS:
                        return None
            except OSError:
                return None  # cannot create files here at all; the write will say so
            else:
                try:
                    os.write(fd, token)
                    written = True
                except OSError:
                    written = False
                os.close(fd)
                if written:
                    return token
                _remove(self.lock_path)
                return None
            now = time.monotonic()
            if now - holder_since >= self.lock_timeout or now >= give_up_at:
                return None  # proceed unlocked: a lost update beats a stuck host
            time.sleep(_LOCK_POLL_SECONDS)

    def _can_create_files(self) -> bool:
        """Whether a new file can be created next to the document (a probe of a unique name)."""
        probe = self._temp_path()
        try:
            os.close(os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except OSError:
            return False
        _remove(probe)
        return True

    def _take_over_stale(self) -> bool:
        """Move a stale lock out of the way; True when the lock's name is free now."""
        grave = self._temp_path()
        try:
            os.rename(self.lock_path, grave)             # atomic: of several writers one moves it
        except FileNotFoundError:
            return True                                  # another writer took it over
        except OSError:
            return False                                 # held open (Windows): wait as for a busy lock
        if _is_stale(grave):
            _remove(grave)
            return True
        # A fresh lock: another writer took the stale one over first and holds this one. Put it
        # back, never over a lock that is there by now, and wait for it as for any busy lock.
        try:
            if os.name == "nt":
                os.rename(grave, self.lock_path)         # never replaces a file on Windows
            else:
                os.link(grave, self.lock_path)           # FileExistsError when a lock is there
        except OSError:
            pass
        _remove(grave)
        return False

    def _release(self, token: bytes) -> None:
        """Delete the lock while it is still this holder's (see module doc)."""
        deadline = time.monotonic() + RELEASE_RETRY_SECONDS
        ours = False
        while True:
            try:
                if self.lock_path.read_bytes() != token:
                    return                               # taken over as stale: another writer's now
                ours = True
                os.unlink(self.lock_path)
                return
            except FileNotFoundError:
                return
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_POLL_SECONDS)
        if ours:
            past = time.time() - 2 * LOCK_STALE_SECONDS
            try:
                os.utime(self.lock_path, (past, past))   # stale at once: the next writer takes it over
            except OSError:
                pass


def _is_stale(path: Path) -> bool:
    try:
        return time.time() - os.stat(path).st_mtime > LOCK_STALE_SECONDS
    except OSError:
        return False


def _remove(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass

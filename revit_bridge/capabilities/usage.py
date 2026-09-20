"""Execution statistics per capability pack, kept out of the pack files.

``<user_capabilities_dir>/usage.json`` maps a pack name to::

    {"execution_count": int, "last_used": "<ISO datetime>", "failure_count": int}

Pack files (built-in or user) are never rewritten to record a run, so the
built-in directory can be read-only and a source checkout stays clean.

Several hosts (the MCP server, the web host) may share one data directory.
Each update is a read-modify-write of the whole file, serialised through a
``usage.json.lock`` file created with ``O_CREAT | O_EXCL``. A writer that
cannot take the lock within ``lock_timeout`` seconds goes ahead anyway - a
lost increment costs less than a stuck host - and a lock older than
``LOCK_STALE_SECONDS`` is treated as left behind by a dead process. The
counters are advisory (health checks, listings), not an audit trail; the
evidence ledger is.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

USAGE_FILE = "usage.json"
USAGE_FIELDS = ("execution_count", "last_used", "failure_count")

LOCK_SUFFIX = ".lock"
LOCK_TIMEOUT_SECONDS = 2.0     # how long a writer waits for another host
LOCK_STALE_SECONDS = 10.0      # a lock this old belongs to a process that died
_LOCK_POLL_SECONDS = 0.02
_REPLACE_RETRY_SECONDS = 1.0   # Windows: a reader holding the file blocks os.replace


def empty_usage() -> dict:
    return {"execution_count": 0, "last_used": "", "failure_count": 0}


class UsageStore:
    """Reads and updates ``usage.json``. Missing or corrupt files read as empty."""

    def __init__(self, path: Path | str, lock_timeout: float = LOCK_TIMEOUT_SECONDS):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + LOCK_SUFFIX)
        self.lock_timeout = lock_timeout

    def read_all(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {name: self._clean(entry) for name, entry in data.items() if isinstance(entry, dict)}

    def get(self, name: str) -> dict:
        return self.read_all().get(name, empty_usage())

    def record(self, name: str, success: bool = True) -> dict:
        """Count one run. Success resets ``failure_count``; failure increments it."""
        with self._locked():
            data = self.read_all()
            entry = data.get(name, empty_usage())
            if success:
                entry["execution_count"] += 1
                entry["failure_count"] = 0
                entry["last_used"] = datetime.now().isoformat()
            else:
                entry["failure_count"] += 1
            data[name] = entry
            self._write(data)
        return entry

    def forget(self, name: str) -> None:
        with self._locked():
            data = self.read_all()
            if name in data:
                del data[name]
                self._write(data)

    # -- internals -------------------------------------------------------------

    @contextmanager
    def _locked(self):
        """Hold ``usage.json.lock`` for one read-modify-write (see module doc)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.lock_timeout
        held = False
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError):
                # Busy. On Windows the create also fails with "access denied"
                # while the previous holder's unlink is still pending.
                if self._lock_is_stale():
                    self._release()
                    continue
                if time.monotonic() >= deadline:
                    break  # proceed unlocked: a lost increment beats a stuck host
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

    @staticmethod
    def _clean(entry: dict) -> dict:
        clean = empty_usage()
        clean["execution_count"] = _as_int(entry.get("execution_count"))
        clean["failure_count"] = _as_int(entry.get("failure_count"))
        clean["last_used"] = str(entry.get("last_used") or "")
        return clean

    def _write(self, data: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # Readers (list_tools, health_check) do not take the lock; on Windows a
        # file that one of them has open cannot be replaced, so wait it out.
        deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
        while True:
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_LOCK_POLL_SECONDS)


def _as_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

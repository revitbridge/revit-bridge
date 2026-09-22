"""Execution statistics per capability pack, kept out of the pack files.

``<user_capabilities_dir>/usage.json`` maps a pack name to::

    {"execution_count": int, "last_used": "<ISO datetime>", "failure_count": int}

Pack files (built-in or user) are never rewritten to record a run, so the
built-in directory can be read-only and a source checkout stays clean.

Several hosts (the MCP server, the web host) may share one data directory;
each update is a locked read-modify-write of the whole file through
:class:`revit_bridge.jsonfile.LockedJsonFile` (``usage.json.lock``, stale
takeover, bounded waits - see that module). The counters are advisory
(health checks, listings), not an audit trail; the evidence ledger is.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from revit_bridge.jsonfile import (  # noqa: F401 - the lock constants stay importable from here
    LOCK_STALE_SECONDS,
    LOCK_SUFFIX,
    LOCK_TIMEOUT_SECONDS,
    PERMISSION_RETRY_SECONDS,
    LockedJsonFile,
)

USAGE_FILE = "usage.json"
USAGE_FIELDS = ("execution_count", "last_used", "failure_count")


def empty_usage() -> dict:
    return {"execution_count": 0, "last_used": "", "failure_count": 0}


class UsageStore:
    """Reads and updates ``usage.json``. Missing or corrupt files read as empty."""

    def __init__(self, path: Path | str, lock_timeout: float = LOCK_TIMEOUT_SECONDS):
        self._file = LockedJsonFile(path, lock_timeout)
        self.path = self._file.path
        self.lock_path = self._file.lock_path
        self.lock_timeout = lock_timeout

    def read_all(self) -> dict[str, dict]:
        data = self._file.read()
        return {name: self._clean(entry) for name, entry in data.items() if isinstance(entry, dict)}

    def get(self, name: str) -> dict:
        return self.read_all().get(name, empty_usage())

    def record(self, name: str, success: bool = True) -> dict:
        """Count one run. Success resets ``failure_count``; failure increments it."""
        with self._file.locked():
            data = self.read_all()
            entry = data.get(name, empty_usage())
            if success:
                entry["execution_count"] += 1
                entry["failure_count"] = 0
                entry["last_used"] = datetime.now().isoformat()
            else:
                entry["failure_count"] += 1
            data[name] = entry
            self._file.write(data)
        return entry

    def forget(self, name: str) -> None:
        with self._file.locked():
            data = self.read_all()
            if name in data:
                del data[name]
                self._file.write(data)

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _clean(entry: dict) -> dict:
        clean = empty_usage()
        clean["execution_count"] = _as_int(entry.get("execution_count"))
        clean["failure_count"] = _as_int(entry.get("failure_count"))
        clean["last_used"] = str(entry.get("last_used") or "")
        return clean


def _as_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

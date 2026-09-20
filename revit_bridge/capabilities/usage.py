"""Execution statistics per capability pack, kept out of the pack files.

``<user_capabilities_dir>/usage.json`` maps a pack name to::

    {"execution_count": int, "last_used": "<ISO datetime>", "failure_count": int}

Pack files (built-in or user) are never rewritten to record a run, so the
built-in directory can be read-only and a source checkout stays clean.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

USAGE_FILE = "usage.json"
USAGE_FIELDS = ("execution_count", "last_used", "failure_count")


def empty_usage() -> dict:
    return {"execution_count": 0, "last_used": "", "failure_count": 0}


class UsageStore:
    """Reads and updates ``usage.json``. Missing or corrupt files read as empty."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

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
        data = self.read_all()
        if name in data:
            del data[name]
            self._write(data)

    @staticmethod
    def _clean(entry: dict) -> dict:
        clean = empty_usage()
        clean["execution_count"] = _as_int(entry.get("execution_count"))
        clean["failure_count"] = _as_int(entry.get("failure_count"))
        clean["last_used"] = str(entry.get("last_used") or "")
        return clean

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def _as_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

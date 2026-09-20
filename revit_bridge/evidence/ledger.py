"""Evidence ledger: one line per execution, appended to ``<evidence_dir>/<YYYY-MM>.jsonl``.

A record (spec 9) says what ran, under which confirmation, against which
document, what came back and what the validator concluded::

    {"id": "ev_<ts>_<rand6>", "ts": "...Z", "host": "mcp|web",
     "action": "run_tool|execute_code", "tool": ..., "tool_version": ...,
     "spec_hash": ..., "projection_hash": ..., "token_prefix": ...,
     "confirmed_by": ..., "channel": ..., "params": {...},
     "code_sha256": ..., "code_head": "<first 200 chars>",
     "document": {"title": ..., "revit_version": ...},
     "success": bool, "error": ..., "result_summary": {...},
     "validation": {...} | null, "duration_ms": int,
     "preconditions_failed": [...], "warnings": [...]}

Parameters are recorded as given (packs carry no secrets); code only as its
hash and first 200 characters.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from revit_bridge.paths import evidence_dir

RECORD_FIELDS = (
    "id", "ts", "host", "action", "tool", "tool_version", "spec_hash", "projection_hash",
    "token_prefix", "confirmed_by", "channel", "params", "code_sha256", "code_head", "document",
    "success", "error", "result_summary", "validation", "duration_ms", "preconditions_failed", "warnings",
)
CODE_HEAD_CHARS = 200
TOKEN_PREFIX_CHARS = 6


def new_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"ev_{now.strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(3)}"


def code_fields(code: str | None) -> dict:
    if not code:
        return {"code_sha256": None, "code_head": None}
    return {
        "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "code_head": code[:CODE_HEAD_CHARS],
    }


def summarize_result(result: Any) -> dict:
    """The part of a result worth keeping: ids, a status, or a size."""
    from revit_bridge.validators.base import extract_ids

    summary: dict = {"ids": extract_ids(result)}
    if isinstance(result, dict):
        for key in ("Status", "status"):
            if key in result:
                summary["status"] = result[key]
                break
        for key in ("Count", "count"):
            if key in result and isinstance(result[key], int):
                summary["count"] = result[key]
                break
        if "Message" in result and isinstance(result["Message"], str):
            summary["message"] = result["Message"][:CODE_HEAD_CHARS]
    elif isinstance(result, list):
        summary["items"] = len(result)
    elif result is not None:
        summary["value"] = str(result)[:CODE_HEAD_CHARS]
    return summary


class Ledger:
    """Append-only JSONL, one file per month."""

    def __init__(self, directory: Path | str | None = None):
        self.directory = Path(directory) if directory else evidence_dir()

    def append(self, record: dict) -> str:
        """Complete and write one record; returns its id."""
        now = datetime.now(timezone.utc)
        full = {field: record.get(field) for field in RECORD_FIELDS}
        full["id"] = record.get("id") or new_id(now)
        full["ts"] = record.get("ts") or now.isoformat(timespec="seconds").replace("+00:00", "Z")
        for field in ("preconditions_failed", "warnings"):
            full[field] = list(full[field] or [])
        full["params"] = full["params"] if full["params"] is not None else {}
        full["duration_ms"] = int(full["duration_ms"] or 0)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._file_for(full["id"])
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(full, ensure_ascii=False) + "\n")
        return full["id"]

    def recent(self, limit: int = 20, tool: str | None = None) -> list[dict]:
        """The newest ``limit`` records, newest first, optionally for one tool."""
        found: list[dict] = []
        for path in sorted(self.directory.glob("*.jsonl"), reverse=True):
            for record in reversed(self._read(path)):
                if tool is not None and record.get("tool") != tool:
                    continue
                found.append(record)
                if len(found) >= limit:
                    return found
        return found

    def get(self, record_id: str) -> dict | None:
        path = self._file_for(record_id)
        if not path.exists():
            return None
        for record in self._read(path):
            if record.get("id") == record_id:
                return record
        return None

    # -- internals ---------------------------------------------------------------

    def _file_for(self, record_id: str) -> Path:
        # ev_20260920T071440_ab12cd -> 2026-09.jsonl
        stamp = record_id[3:9] if record_id.startswith("ev_") and len(record_id) > 9 else ""
        if len(stamp) == 6 and stamp.isdigit():
            return self.directory / f"{stamp[:4]}-{stamp[4:]}.jsonl"
        return self.directory / "undated.jsonl"

    @staticmethod
    def _read(path: Path) -> list[dict]:
        records: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
        except OSError:
            pass
        return records

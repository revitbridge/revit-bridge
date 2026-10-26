"""Planned read-back checks on disk: ``<data root>/verifications/<verification_id>.json``.

One JSON file per plan, written by ``plan_verification`` and redeemed once by
``verify_execution``::

    {"verification_id": "v_<16 hex>", "template": ..., "params": {...},
     "baseline": {"count": int} | null, "document": {"title", "revit_version"},
     "created_at": "...Z", "expires_at": "...Z", "used_at": null | "...Z",
     "evidence_id": null | "ev_..."}

A plan is valid for ``TTL_SECONDS`` (30 minutes) and for one
``verify_execution``; ``consume`` marks it used under the file's lock (the
same locked read-modify-write as ``usage.json``). An id that does not have
the ``v_<16 hex>`` shape is unknown without touching the disk. Plans that
expired more than ``KEEP_SECONDS`` ago are deleted when a new one is written.
"""
from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from revit_bridge.jsonfile import LockedJsonFile
from revit_bridge.paths import verifications_dir

TTL_SECONDS = 30 * 60
KEEP_SECONDS = 24 * 3600
ID_RE = re.compile(r"^v_[0-9a-f]{16}$")

UNKNOWN = "unknown_verification"
EXPIRED = "verification_expired"
USED = "verification_used"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """``2026-10-26T09:30:00Z``: the ledger's timestamp format."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class VerificationStore:
    """Plans of ``plan_verification`` (see module doc). ``directory`` defaults to
    ``paths.verifications_dir()``, resolved at every call; ``clock`` is for tests."""

    def __init__(self, directory: Path | str | None = None, ttl_seconds: int = TTL_SECONDS,
                 clock: Callable[[], datetime] = _now):
        self._directory = Path(directory) if directory else None
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    @property
    def directory(self) -> Path:
        return self._directory or verifications_dir()

    def _file(self, verification_id: str) -> LockedJsonFile:
        return LockedJsonFile(self.directory / f"{verification_id}.json")

    def create(self, template: str, params: dict, baseline: dict | None, document: dict) -> dict:
        """Write a new plan and return it (raises OSError when it cannot be written)."""
        now = self.clock()
        self.prune(now)
        plan = {
            "verification_id": f"v_{secrets.token_hex(8)}",
            "template": template,
            "params": params,
            "baseline": baseline,
            "document": document,
            "created_at": iso(now),
            "expires_at": iso(now + timedelta(seconds=self.ttl_seconds)),
            "used_at": None,
            "evidence_id": None,
        }
        handle = self._file(plan["verification_id"])
        with handle.locked():
            handle.write(plan)
        return plan

    def get(self, verification_id: str) -> dict | None:
        if not isinstance(verification_id, str) or not ID_RE.match(verification_id):
            return None
        plan = self._file(verification_id).read()
        return plan if plan.get("verification_id") == verification_id else None

    def state(self, plan: dict | None) -> str | None:
        """Why ``plan`` cannot be redeemed now (unknown, used, expired), or None."""
        if plan is None:
            return UNKNOWN
        if plan.get("used_at"):
            return USED
        expires = parse_iso(plan.get("expires_at"))
        if expires is None or self.clock() >= expires:
            return EXPIRED
        return None

    def consume(self, verification_id: str, evidence_id: str) -> tuple[dict | None, str | None]:
        """Mark the plan used for ``evidence_id``: ``(plan, None)``, or ``(plan, reason)`` when it
        cannot be redeemed (checked again under the lock, so two callers cannot both win)."""
        if self.get(verification_id) is None:
            return None, UNKNOWN
        handle = self._file(verification_id)
        with handle.locked():
            plan = handle.read() or None
            reason = self.state(plan)
            if reason is not None:
                return plan, reason
            plan["used_at"] = iso(self.clock())
            plan["evidence_id"] = evidence_id
            handle.write(plan)
        return plan, None

    def prune(self, now: datetime | None = None) -> int:
        """Delete plans that expired more than ``KEEP_SECONDS`` ago; returns how many."""
        now = now or self.clock()
        directory = self.directory
        if not directory.is_dir():
            return 0
        dropped = 0
        for path in directory.glob("v_*.json"):
            expires = parse_iso(LockedJsonFile(path).read().get("expires_at"))
            if expires is not None and now - expires > timedelta(seconds=KEEP_SECONDS):
                try:
                    path.unlink()
                    dropped += 1
                except OSError:
                    pass
        return dropped

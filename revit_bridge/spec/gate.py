"""Confirmation tokens: one designer confirmation, one execution.

``confirm_spec`` issues a token bound to the hash of the spec's execution
projection (tool + params, or code + parameters). ``run_tool`` and
``execute_code`` ``verify`` it against the projection they are about to run
- the token must exist, be unused, be unexpired, and the projection must
hash to the same value, so confirming A and executing B is refused - run
every check that does not touch Revit (parameter validation, rendering,
the sandbox review), and ``consume`` it the moment before the code is
sent. A refused validation therefore leaves the token redeemable.

Tokens live in memory and, until redeemed or expired, in
``<evidence_dir>/pending/<token[:12]>.json`` so a restarted server can still
honour a confirmation once.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

from revit_bridge.paths import evidence_dir
from revit_bridge.spec.models import TaskSpec, projection_hash

ENV_CONFIRM_TTL = "REVIT_BRIDGE_CONFIRM_TTL"
DEFAULT_TTL_SECONDS = 600
PENDING_DIR = "pending"
PREFIX_LEN = 12


class Confirmation(BaseModel):
    token: str                       # secrets.token_urlsafe(24)
    spec_hash: str
    projection_hash: str             # sha256(canonical(execution_projection))
    issued_at: str
    expires_at: str
    confirmed_by: str                # "designer"
    channel: str                     # "host_ui" | "chat"
    used_at: str | None = None

    def expired(self, now: datetime | None = None) -> bool:
        return (now or _now()) >= datetime.fromisoformat(self.expires_at)


class GateError(Exception):
    """A token that cannot be redeemed; ``reason`` is expired | used | mismatch | unknown."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def ttl_from_env(env=None) -> int:
    env = os.environ if env is None else env
    raw = env.get(ENV_CONFIRM_TTL, "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_TTL_SECONDS
    except ValueError:
        return DEFAULT_TTL_SECONDS


class Gate:
    """Issues and redeems confirmation tokens (see module doc)."""

    def __init__(self, pending_dir: Path | str | None = None, ttl_seconds: int | None = None):
        self.pending_dir = Path(pending_dir) if pending_dir else evidence_dir() / PENDING_DIR
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else ttl_from_env()
        self._tokens: dict[str, Confirmation] = {}

    # -- issue -----------------------------------------------------------------

    def issue(self, spec: TaskSpec, confirmed_by: str = "designer", channel: str = "chat") -> Confirmation:
        self.purge_expired()
        now = _now()
        conf = Confirmation(
            token=secrets.token_urlsafe(24),
            spec_hash=spec.spec_hash(),
            projection_hash=projection_hash(spec.execution_projection()),
            issued_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=self.ttl_seconds)),
            confirmed_by=confirmed_by,
            channel=channel,
        )
        self._tokens[conf.token] = conf
        self._persist(conf)
        return conf

    # -- redeem ----------------------------------------------------------------

    def verify(self, token: str, projection: dict | None = None) -> Confirmation:
        """Check ``token`` (exists, unused, unexpired, and - when given - bound
        to ``projection``) without consuming it; raises GateError."""
        conf = self._tokens.get(token) or self._load(token)
        if conf is None:
            raise GateError("unknown", "no confirmation with this token")
        if conf.used_at:
            raise GateError("used", f"confirmation already redeemed at {conf.used_at}")
        if conf.expired():
            self._forget(conf)
            raise GateError("expired", f"confirmation expired at {conf.expires_at}")
        if projection is not None and projection_hash(projection) != conf.projection_hash:
            raise GateError("mismatch", "the execution does not match the confirmed spec")
        return conf

    def consume(self, token: str, projection: dict | None = None) -> Confirmation:
        """Mark ``token`` used; call it the moment before the execution is sent.

        Re-runs ``verify`` so a token that expired between the checks and the
        dispatch is still refused.
        """
        conf = self.verify(token, projection)
        conf.used_at = _iso(_now())
        self._tokens[token] = conf
        self._unlink(conf)
        return conf

    def redeem(self, token: str, projection: dict) -> Confirmation:
        """``verify`` + ``consume`` in one step."""
        return self.consume(token, projection)

    def peek(self, token: str) -> Confirmation | None:
        return self._tokens.get(token) or self._load(token)

    # -- housekeeping ----------------------------------------------------------

    def purge_expired(self) -> int:
        """Drop expired tokens from memory and disk; returns how many."""
        now = _now()
        dropped = 0
        for token, conf in list(self._tokens.items()):
            if conf.expired(now):
                self._forget(conf)
                dropped += 1
        if self.pending_dir.is_dir():
            for path in self.pending_dir.glob("*.json"):
                conf = self._read(path)
                if conf is None or conf.expired(now):
                    self._unlink_path(path)
                    dropped += conf is not None
        return dropped

    # -- persistence -----------------------------------------------------------

    def _path(self, token: str) -> Path:
        return self.pending_dir / f"{token[:PREFIX_LEN]}.json"

    def _persist(self, conf: Confirmation) -> None:
        try:
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            self._path(conf.token).write_text(conf.model_dump_json(indent=2), encoding="utf-8")
        except OSError:
            pass  # memory still holds it; only restart resilience is lost

    def _load(self, token: str) -> Confirmation | None:
        if not token:
            return None
        conf = self._read(self._path(token))
        if conf is None or not secrets.compare_digest(conf.token, token):
            return None
        self._tokens[token] = conf
        return conf

    @staticmethod
    def _read(path: Path) -> Confirmation | None:
        try:
            return Confirmation.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _forget(self, conf: Confirmation) -> None:
        self._tokens.pop(conf.token, None)
        self._unlink(conf)

    def _unlink(self, conf: Confirmation) -> None:
        self._unlink_path(self._path(conf.token))

    @staticmethod
    def _unlink_path(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass


def confirmation_required(env=None) -> dict:
    """The refusal payload for a call without a token."""
    return {
        "success": False,
        "error": "confirmation_required",
        "hint": (
            "Show the designer the spec card, get an explicit confirmation, call "
            "confirm_spec with the TaskSpec to obtain a token, then call again with "
            "token=<token>. The token binds the exact tool and parameters (or code)."
        ),
    }


def confirmation_invalid(exc: GateError) -> dict:
    return {
        "success": False,
        "error": "confirmation_invalid",
        "reason": exc.reason,
        "message": str(exc),
    }

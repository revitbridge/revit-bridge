"""Pairing codes and device tokens (phase 7): who may drive which Revit.

A *device* is one add-in installation. A host pairs it in three steps:

1. ``create_pairing(label)`` - a new, inactive device, a one-time pairing
   code (``XXXX-XXXX``, ten minutes) and the *browser key* of whoever asked
   for the pairing. The browser key lets its holder drive and revoke the
   device; it is returned once, in plain text.
2. The add-in (or its installer) ``redeem(code)``s the pairing and receives
   the *device token*, again in plain text, once. From then on the device
   is active: ``verify_device(device_id, token)`` is its WebSocket handshake.
3. ``verify_browser(device_id, key)`` guards every request a browser makes
   on that device; ``revoke`` ends both.

Only ``sha256`` digests of the code, the token and the key are stored; the
plain text never touches the disk or a log, and every comparison is
constant time over the digests. The store is ``<data_root>/auth/devices.json``::

    {"schema_version": 1,
     "devices":  {device_id: {device_id, label, created_at, last_seen, revoked_at,
                              token_hash, browser_key_hash, addin_version}},
     "pairings": {code_hash: {device_id, expires_at}}}

updated through the same locked read-modify-write as ``usage.json``
(:mod:`revit_bridge.jsonfile`). A device whose pairing expired unredeemed
can never become active, so ``purge`` drops the pairing and that device.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

from revit_bridge.jsonfile import LockedJsonFile
from revit_bridge.paths import auth_dir

DEVICES_FILE = "devices.json"
SCHEMA_VERSION = 1

# The pairing code: two groups of four from an alphabet without 0/O/1/I.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_GROUPS = 2
CODE_GROUP_LEN = 4
PAIRING_TTL_SECONDS = 600

DEVICE_ID_PREFIX = "dev_"
DEVICE_ID_LEN = 12
_DEVICE_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"   # base32, lower case
DEVICE_TOKEN_BYTES = 32
BROWSER_KEY_BYTES = 24


class PairingCode(BaseModel):
    """What ``create_pairing`` returns: the code and the browser key, in plain text, once."""
    code: str
    device_id: str
    expires_at: str
    browser_key: str


class Device(BaseModel):
    """A device as hosts see it: never a hash. ``last_seen`` is None until redeemed."""
    device_id: str
    label: str
    created_at: str
    last_seen: str | None
    revoked_at: str | None
    addin_version: str | None


class Redeemed(BaseModel):
    """What ``redeem`` returns: the now-active device and its token, in plain text, once."""
    device: Device
    device_token: str


class DeviceError(Exception):
    """A pairing code that cannot be redeemed; ``reason`` is ``invalid_code``
    whether the code is unknown, expired, already used or for a revoked device."""

    def __init__(self, reason: str = "invalid_code", message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _matches(stored_hash, secret) -> bool:
    """Constant-time: the digest of ``secret`` against the stored digest."""
    if not isinstance(stored_hash, str) or not isinstance(secret, str) or not secret:
        return False
    return hmac.compare_digest(stored_hash.encode("ascii"), _digest(secret).encode("ascii"))


def new_code() -> str:
    groups = ("".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_GROUP_LEN)) for _ in range(CODE_GROUPS))
    return "-".join(groups)


def normalize_code(code) -> str | None:
    """``xxxx xxxx`` / ``xxxxxxxx`` / ``XXXX-XXXX`` as typed -> ``XXXX-XXXX``; None when it is no code."""
    if not isinstance(code, str):
        return None
    flat = "".join(ch for ch in code.upper() if ch not in " -")
    if len(flat) != CODE_GROUPS * CODE_GROUP_LEN or any(ch not in CODE_ALPHABET for ch in flat):
        return None
    return "-".join(flat[i:i + CODE_GROUP_LEN] for i in range(0, len(flat), CODE_GROUP_LEN))


def new_device_id() -> str:
    return DEVICE_ID_PREFIX + "".join(secrets.choice(_DEVICE_ID_ALPHABET) for _ in range(DEVICE_ID_LEN))


class DeviceStore:
    """``devices.json``: pairings and devices, hashes only (see module doc)."""

    def __init__(self, path: Path | None = None):
        self._file = LockedJsonFile(path if path is not None else auth_dir() / DEVICES_FILE)
        self.path = self._file.path

    # -- pairing ---------------------------------------------------------------

    def create_pairing(self, label: str = "") -> PairingCode:
        """A new inactive device with a one-time code and the browser key of the requester."""
        now = _now()
        expires_at = _iso(now + timedelta(seconds=PAIRING_TTL_SECONDS))
        browser_key = secrets.token_urlsafe(BROWSER_KEY_BYTES)
        with self._file.locked():
            data = self._load()
            self._purge(data, now)
            device_id = new_device_id()
            while device_id in data["devices"]:
                device_id = new_device_id()
            code = new_code()
            while _digest(code) in data["pairings"]:
                code = new_code()
            data["devices"][device_id] = {
                "device_id": device_id,
                "label": str(label or ""),
                "created_at": _iso(now),
                "last_seen": None,
                "revoked_at": None,
                "token_hash": None,
                "browser_key_hash": _digest(browser_key),
                "addin_version": None,
            }
            data["pairings"][_digest(code)] = {"device_id": device_id, "expires_at": expires_at}
            self._file.write(data)
        return PairingCode(code=code, device_id=device_id, expires_at=expires_at, browser_key=browser_key)

    def redeem(self, code: str, addin_version: str | None = None) -> Redeemed:
        """Turn a pairing code into the device token (once); anything else is ``invalid_code``."""
        canonical = normalize_code(code)
        if canonical is None:
            raise DeviceError("invalid_code")
        code_hash = _digest(canonical)
        now = _now()
        token = secrets.token_urlsafe(DEVICE_TOKEN_BYTES)
        with self._file.locked():
            data = self._load()
            dropped = self._purge(data, now)
            pairing = data["pairings"].get(code_hash)
            device = data["devices"].get(pairing.get("device_id")) if pairing else None
            if device is None or device.get("revoked_at"):
                if dropped:
                    self._file.write(data)      # the housekeeping stands even when the code does not
                raise DeviceError("invalid_code")
            del data["pairings"][code_hash]
            device["token_hash"] = _digest(token)
            device["last_seen"] = _iso(now)
            device["addin_version"] = str(addin_version) if addin_version else None
            self._file.write(data)
        return Redeemed(device=_public(device), device_token=token)

    # -- verification ------------------------------------------------------------

    def verify_device(self, device_id: str, token: str) -> Device | None:
        """The device when ``token`` is its token; None when unknown, inactive, revoked or wrong."""
        device = self._get(device_id)
        if device is None or device.get("revoked_at") or not device.get("token_hash"):
            return None
        return _public(device) if _matches(device["token_hash"], token) else None

    def verify_browser(self, device_id: str, key: str) -> Device | None:
        """The device when ``key`` is the browser key of its pairing; None when unknown, revoked or wrong."""
        device = self._get(device_id)
        if device is None or device.get("revoked_at"):
            return None
        return _public(device) if _matches(device.get("browser_key_hash"), key) else None

    # -- lifecycle ---------------------------------------------------------------

    def revoke(self, device_id: str) -> bool:
        """Revoke ``device_id`` (idempotent); False only when there is no such device."""
        with self._file.locked():
            data = self._load()
            device = data["devices"].get(device_id) if isinstance(device_id, str) else None
            if device is None:
                return False
            if not device.get("revoked_at"):
                device["revoked_at"] = _iso(_now())
                for code_hash in [h for h, p in data["pairings"].items() if p.get("device_id") == device_id]:
                    del data["pairings"][code_hash]
                self._file.write(data)
        return True

    def touch(self, device_id: str) -> None:
        """Record activity on ``device_id`` (``last_seen``); unknown ids are ignored."""
        with self._file.locked():
            data = self._load()
            device = data["devices"].get(device_id) if isinstance(device_id, str) else None
            if device is not None:
                device["last_seen"] = _iso(_now())
                self._file.write(data)

    def list(self) -> list[Device]:
        """Every device, in creation order, without any hash."""
        return [_public(d) for d in self._load()["devices"].values()]

    def purge(self) -> int:
        """Drop expired, unredeemed pairings (and their never-activated devices); returns how many."""
        with self._file.locked():
            data = self._load()
            dropped = self._purge(data, _now())
            if dropped:
                self._file.write(data)
        return dropped

    # -- internals ---------------------------------------------------------------

    def _load(self) -> dict:
        raw = self._file.read()
        devices = raw.get("devices") if isinstance(raw.get("devices"), dict) else {}
        pairings = raw.get("pairings") if isinstance(raw.get("pairings"), dict) else {}
        return {
            "schema_version": SCHEMA_VERSION,
            "devices": {k: v for k, v in devices.items() if isinstance(v, dict)},
            "pairings": {k: v for k, v in pairings.items() if isinstance(v, dict)},
        }

    def _get(self, device_id) -> dict | None:
        if not isinstance(device_id, str) or not device_id:
            return None
        return self._load()["devices"].get(device_id)

    @staticmethod
    def _purge(data: dict, now: datetime) -> int:
        dropped = 0
        for code_hash, pairing in list(data["pairings"].items()):
            try:
                expired = now >= datetime.fromisoformat(str(pairing.get("expires_at")))
            except ValueError:
                expired = True
            if not expired:
                continue
            del data["pairings"][code_hash]
            device_id = pairing.get("device_id")
            device = data["devices"].get(device_id)
            if device is not None and not device.get("token_hash") and not device.get("revoked_at"):
                del data["devices"][device_id]
            dropped += 1
        return dropped


def _public(device: dict) -> Device:
    return Device(
        device_id=str(device.get("device_id") or ""),
        label=str(device.get("label") or ""),
        created_at=str(device.get("created_at") or ""),
        last_seen=device.get("last_seen") or None,
        revoked_at=device.get("revoked_at") or None,
        addin_version=device.get("addin_version") or None,
    )

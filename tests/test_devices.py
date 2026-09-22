"""Pairing codes and device tokens: the lifecycle, hashes-only storage, constant-time checks."""
from __future__ import annotations

import hmac
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import revit_bridge.auth.devices as devices_module
from revit_bridge.auth import Device, DeviceError, DeviceStore, PairingCode, Redeemed
from revit_bridge.auth.devices import CODE_ALPHABET, PAIRING_TTL_SECONDS, normalize_code
from revit_bridge.jsonfile import LockedJsonFile
from revit_bridge.paths import auth_dir, data_root

CODE_RE = re.compile(r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}-[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}$")
DEVICE_ID_RE = re.compile(r"^dev_[a-z2-7]{12}$")


def test_pairing_lifecycle(tmp_path):
    """create -> redeem once -> second redeem invalid_code -> expired invalid_code."""
    store = DeviceStore(tmp_path / "devices.json")
    pairing = store.create_pairing("Office PC")
    assert isinstance(pairing, PairingCode)
    assert CODE_RE.match(pairing.code) and DEVICE_ID_RE.match(pairing.device_id)
    assert len(pairing.browser_key) >= 32
    expires = datetime.fromisoformat(pairing.expires_at)
    assert timedelta(seconds=PAIRING_TTL_SECONDS - 5) < expires - datetime.now(timezone.utc) <= timedelta(seconds=PAIRING_TTL_SECONDS)

    # inactive until redeemed: the browser key works, no device token exists yet
    listed = store.list()
    assert [d.device_id for d in listed] == [pairing.device_id]
    assert listed[0] == Device(device_id=pairing.device_id, label="Office PC", created_at=listed[0].created_at,
                               last_seen=None, revoked_at=None, addin_version=None)
    assert store.verify_browser(pairing.device_id, pairing.browser_key).device_id == pairing.device_id
    assert store.verify_device(pairing.device_id, "anything") is None

    redeemed = store.redeem(pairing.code.lower().replace("-", " "), addin_version="0.2.0")   # as typed
    assert isinstance(redeemed, Redeemed) and len(redeemed.device_token) >= 43
    assert redeemed.device.device_id == pairing.device_id and redeemed.device.addin_version == "0.2.0"
    assert redeemed.device.last_seen and redeemed.device.revoked_at is None
    assert store.verify_device(pairing.device_id, redeemed.device_token) == redeemed.device
    assert store.verify_device(pairing.device_id, redeemed.device_token + "x") is None
    assert store.verify_device("dev_nope", redeemed.device_token) is None
    assert store.verify_browser(pairing.device_id, redeemed.device_token) is None      # token is not the key
    assert store.verify_device(pairing.device_id, pairing.browser_key) is None        # key is not the token

    # one-time
    with pytest.raises(DeviceError) as excinfo:
        store.redeem(pairing.code)
    assert excinfo.value.reason == "invalid_code" and str(excinfo.value) == "invalid_code"
    for bad in ("", "ZZZZ-ZZZZ", "0000-0000", None, 12345678):
        with pytest.raises(DeviceError) as excinfo:
            store.redeem(bad)
        assert excinfo.value.reason == "invalid_code"

    # expired: never redeemable, and purge drops the pairing with its never-activated device
    late = store.create_pairing("late")
    data = json.loads(store.path.read_text(encoding="utf-8"))
    (pairing_entry,) = [p for p in data["pairings"].values() if p["device_id"] == late.device_id]
    pairing_entry["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    store.path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(DeviceError) as excinfo:
        store.redeem(late.code)
    assert excinfo.value.reason == "invalid_code"
    assert [d.device_id for d in store.list()] == [pairing.device_id]
    assert store.verify_browser(late.device_id, late.browser_key) is None

    # touch and revoke
    before = store.verify_device(pairing.device_id, redeemed.device_token).last_seen
    store.touch(pairing.device_id)
    store.touch("dev_unknown")                                                   # ignored
    assert store.verify_device(pairing.device_id, redeemed.device_token).last_seen >= before
    assert store.revoke(pairing.device_id) is True
    assert store.revoke(pairing.device_id) is True                               # idempotent
    assert store.revoke("dev_unknown") is False
    assert store.verify_device(pairing.device_id, redeemed.device_token) is None
    assert store.verify_browser(pairing.device_id, pairing.browser_key) is None
    (revoked,) = store.list()
    assert revoked.revoked_at and revoked.device_id == pairing.device_id

    # a revoked device cannot be paired again through a code that was still open
    open_pairing = store.create_pairing("open")
    assert store.revoke(open_pairing.device_id) is True
    with pytest.raises(DeviceError):
        store.redeem(open_pairing.code)


def test_plaintext_never_reaches_the_disk(tmp_path):
    store = DeviceStore(tmp_path / "auth" / "devices.json")
    pairing = store.create_pairing("secret test")
    redeemed = store.redeem(pairing.code)
    store.touch(pairing.device_id)
    text = store.path.read_text(encoding="utf-8")
    for secret in (pairing.code, pairing.code.replace("-", ""), pairing.browser_key, redeemed.device_token):
        assert secret not in text, "plain text on disk"
    on_disk = json.loads(text)
    assert on_disk["schema_version"] == 1 and on_disk["pairings"] == {}          # redeemed: gone
    (record,) = on_disk["devices"].values()
    assert set(record) == {"device_id", "label", "created_at", "last_seen", "revoked_at",
                           "token_hash", "browser_key_hash", "addin_version"}
    assert re.fullmatch(r"[0-9a-f]{64}", record["token_hash"]) and re.fullmatch(r"[0-9a-f]{64}", record["browser_key_hash"])
    # an open pairing is stored under the hash of its code
    another = store.create_pairing()
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    (code_hash,) = on_disk["pairings"]
    assert re.fullmatch(r"[0-9a-f]{64}", code_hash) and another.code not in code_hash
    # nothing but the public fields leaves list()
    for device in store.list():
        assert "hash" not in json.dumps(device.model_dump())
    # no lock or temp file is left behind
    assert sorted(p.name for p in store.path.parent.iterdir()) == ["devices.json"]


def test_verify_compares_digests_in_constant_time(tmp_path, monkeypatch):
    store = DeviceStore(tmp_path / "devices.json")
    pairing = store.create_pairing()
    redeemed = store.redeem(pairing.code)
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(devices_module.hmac, "compare_digest", spy)
    assert store.verify_device(pairing.device_id, redeemed.device_token) is not None
    assert store.verify_device(pairing.device_id, "wrong") is None
    assert store.verify_browser(pairing.device_id, pairing.browser_key) is not None
    assert store.verify_browser(pairing.device_id, "x" * 200) is None
    assert len(calls) == 4
    for a, b in calls:
        assert len(a) == 64 and len(b) == 64                           # digests, never the plain text
        assert redeemed.device_token.encode() not in (a, b) and pairing.browser_key.encode() not in (a, b)
    # no compare at all without a candidate secret or for an inactive / unknown device
    calls.clear()
    assert store.verify_device(pairing.device_id, "") is None
    assert store.verify_device(pairing.device_id, None) is None
    fresh = store.create_pairing()
    assert store.verify_device(fresh.device_id, "anything") is None
    assert store.verify_device("dev_unknown", "anything") is None
    assert calls == []


def test_codes_come_from_the_unambiguous_alphabet():
    assert len(CODE_ALPHABET) == 32 and not set("0O1I") & set(CODE_ALPHABET)
    for _ in range(50):
        assert CODE_RE.match(devices_module.new_code())
    assert normalize_code("abcd-efgh") == "ABCD-EFGH"
    assert normalize_code(" abcdefgh ") == "ABCD-EFGH"
    assert normalize_code("ABCD EFGH") == "ABCD-EFGH"
    assert normalize_code("ABCD-EFG") is None and normalize_code("ABCD-EFG0") is None and normalize_code(None) is None


def test_default_path_and_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIT_BRIDGE_DATA_DIR", str(tmp_path / "root"))
    assert auth_dir() == data_root() / "auth"
    store = DeviceStore()
    assert store.path == tmp_path / "root" / "auth" / "devices.json"
    assert store.list() == [] and store.purge() == 0
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("not json", encoding="utf-8")
    assert store.list() == []
    pairing = store.create_pairing()                                     # a corrupt file is replaced
    assert [d.device_id for d in store.list()] == [pairing.device_id]
    store.path.write_text(json.dumps({"schema_version": 1, "devices": [], "pairings": {"x": 1}}), encoding="utf-8")
    assert store.list() == []
    with pytest.raises(DeviceError):
        store.redeem(pairing.code)


def test_purge_counts_only_expired_open_pairings(tmp_path):
    store = DeviceStore(tmp_path / "devices.json")
    keep = store.create_pairing("keep")
    done = store.create_pairing("done")
    store.redeem(done.code)
    stale = [store.create_pairing(f"stale {i}") for i in range(3)]
    data = json.loads(store.path.read_text(encoding="utf-8"))
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    for entry in data["pairings"].values():
        if entry["device_id"] in {s.device_id for s in stale}:
            entry["expires_at"] = past
    store.path.write_text(json.dumps(data), encoding="utf-8")
    assert store.purge() == 3
    assert store.purge() == 0
    assert sorted(d.label for d in store.list()) == ["done", "keep"]
    assert store.verify_browser(keep.device_id, keep.browser_key) is not None


def test_concurrent_pairings_lose_nothing(tmp_path):
    store = DeviceStore(tmp_path / "devices.json")
    codes: list[PairingCode] = []
    lock = threading.Lock()

    def work():
        for _ in range(10):
            p = store.create_pairing()
            with lock:
                codes.append(p)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.list()) == 40 and len({p.device_id for p in codes}) == 40
    for p in codes:
        assert store.redeem(p.code).device.device_id == p.device_id
    assert not store.path.with_name("devices.json.lock").exists()


def test_the_shared_json_file_is_the_usage_lock(tmp_path):
    """The lock and retry pattern of usage.json is one helper now, used by both stores."""
    from revit_bridge.capabilities import usage as usage_module
    from revit_bridge.capabilities.usage import UsageStore

    assert usage_module.LockedJsonFile is LockedJsonFile
    store = UsageStore(tmp_path / "usage.json", lock_timeout=0.1)
    assert isinstance(store._file, LockedJsonFile) and store.lock_path.name == "usage.json.lock"
    devices = DeviceStore(tmp_path / "devices.json")
    assert isinstance(devices._file, LockedJsonFile)
    # a live lock is waited for, then bypassed, exactly like usage.json
    devices._file.lock_timeout = 0.1
    devices._file.lock_path.parent.mkdir(parents=True, exist_ok=True)
    devices._file.lock_path.write_text("other host")
    started = time.monotonic()
    devices.create_pairing()
    assert time.monotonic() - started >= 0.1
    assert devices._file.lock_path.read_text() == "other host"
    old = time.time() - 60
    os.utime(devices._file.lock_path, (old, old))
    devices.create_pairing()                                             # stale: taken over and released
    assert not devices._file.lock_path.exists()

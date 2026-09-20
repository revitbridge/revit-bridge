"""usage.json: counters, the lock file shared between hosts (review item 5)."""
from __future__ import annotations

import json
import os
import threading
import time

from revit_bridge.capabilities.usage import UsageStore, empty_usage


def test_record_and_forget_round_trip(tmp_path):
    store = UsageStore(tmp_path / "caps" / "usage.json")
    assert store.get("x") == empty_usage() and store.read_all() == {}

    assert store.record("x", success=False)["failure_count"] == 1
    entry = store.record("x", success=True)
    assert entry["execution_count"] == 1 and entry["failure_count"] == 0 and entry["last_used"]
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert on_disk == {"x": entry}
    assert not store.lock_path.exists()                  # released after each write
    assert not store.path.with_name("usage.json.tmp").exists()

    store.forget("x")
    assert store.read_all() == {}

    store.path.write_text('{"x": {"execution_count": "7", "failure_count": -3, "last_used": null}, "bad": 1}',
                          encoding="utf-8")
    assert store.read_all() == {"x": {"execution_count": 7, "failure_count": 0, "last_used": ""}}


def test_concurrent_writers_lose_no_increment(tmp_path):
    store = UsageStore(tmp_path / "usage.json")
    threads = [
        threading.Thread(target=lambda: [store.record("shared") for _ in range(25)])
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.get("shared")["execution_count"] == 100
    assert not store.lock_path.exists()


def test_writer_waits_for_a_live_lock_then_proceeds(tmp_path):
    store = UsageStore(tmp_path / "usage.json", lock_timeout=0.1)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.lock_path.write_text("other host")             # fresh: somebody else is writing

    started = time.monotonic()
    assert store.record("x")["execution_count"] == 1     # gave up waiting, still counted
    assert time.monotonic() - started >= 0.1
    assert store.lock_path.read_text() == "other host"   # not ours: left in place


def test_stale_lock_is_taken_over(tmp_path):
    store = UsageStore(tmp_path / "usage.json", lock_timeout=5.0)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.lock_path.write_text("dead host")
    old = time.time() - 60
    os.utime(store.lock_path, (old, old))

    started = time.monotonic()
    assert store.record("x")["execution_count"] == 1
    assert time.monotonic() - started < 1.0              # no waiting on a dead lock
    assert not store.lock_path.exists()

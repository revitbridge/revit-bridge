"""usage.json: counters, the lock file shared between hosts (review item 5)."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager

import pytest

from revit_bridge.capabilities.usage import UsageStore, empty_usage
from revit_bridge.jsonfile import LockedJsonFile


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
    assert os.listdir(store.path.parent) == ["usage.json"]   # no temp file, no probe, no lock left

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
    assert os.listdir(store.path.parent) == ["usage.json"]


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


def test_transient_windows_errors_are_retried_not_bypassed(tmp_path, monkeypatch):
    """A pending-delete on the lock or a reader holding the file are waits, not failures."""
    store = UsageStore(tmp_path / "usage.json", lock_timeout=2.0)
    real_open, real_replace = os.open, os.replace
    denied = {"open": 2, "replace": 2}
    seen_lock_during_write = []

    def flaky_open(path, flags, *args):
        if str(path) == str(store.lock_path) and denied["open"]:
            denied["open"] -= 1
            raise PermissionError(13, "pending delete")
        return real_open(path, flags, *args)

    def flaky_replace(src, dst):
        seen_lock_during_write.append(store.lock_path.exists())
        if denied["replace"]:
            denied["replace"] -= 1
            raise PermissionError(32, "in use by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "open", flaky_open)
    monkeypatch.setattr(os, "replace", flaky_replace)
    assert store.record("x")["execution_count"] == 1
    assert denied == {"open": 0, "replace": 0}
    assert seen_lock_during_write == [True, True, True]    # never wrote without the lock
    assert not store.lock_path.exists()


def test_a_slow_but_live_holder_never_outlasts_a_waiter(tmp_path, monkeypatch):
    """F3: a holder may spend its whole read retry and replace retry inside the lock, plus a slow
    disk; a writer waiting on the lock gives up after LOCK_TIMEOUT_SECONDS and goes on unlocked.
    With the holder's worst case close to the waiter's timeout, the waiter wrote over the
    holder's update (count 1, not 2). The holder's worst case must stay well below it."""
    from pathlib import Path

    from revit_bridge import jsonfile

    holder_retries = jsonfile.READ_RETRY_SECONDS + jsonfile.REPLACE_RETRY_SECONDS + jsonfile.RELEASE_RETRY_SECONDS
    assert holder_retries <= jsonfile.LOCK_TIMEOUT_SECONDS / 2
    assert jsonfile.LOCK_TIMEOUT_SECONDS < jsonfile.LOCK_STALE_SECONDS     # a live holder is never "stale"
    assert jsonfile.LOCK_TIMEOUT_SECONDS < jsonfile.LOCK_WAIT_LIMIT_SECONDS
    assert jsonfile.LOCK_STALE_SECONDS <= jsonfile.TEMP_STALE_SECONDS      # a live writer's temp file is kept

    store = UsageStore(tmp_path / "usage.json")                      # the default lock timeout
    store.path.write_text("{}", encoding="utf-8")
    holder_in_lock = threading.Event()
    holder = {}
    real_read, real_replace, real_fsync = Path.read_bytes, os.replace, os.fsync

    def slow_read(self):
        if threading.current_thread().name == "holder" and self == store.path:
            holder.setdefault("read_until", time.monotonic() + jsonfile.READ_RETRY_SECONDS - 0.05)
            holder_in_lock.set()
            if time.monotonic() < holder["read_until"]:
                raise PermissionError(32, "a scanner holds the file")
        return real_read(self)

    def slow_replace(src, dst):
        if threading.current_thread().name == "holder":
            holder.setdefault("replace_until", time.monotonic() + jsonfile.REPLACE_RETRY_SECONDS - 0.05)
            if time.monotonic() < holder["replace_until"]:
                raise PermissionError(32, "a reader holds the file")
        return real_replace(src, dst)

    def slow_fsync(fd):
        if threading.current_thread().name == "holder":
            time.sleep(0.5)                                          # a slow disk
        return real_fsync(fd)

    monkeypatch.setattr(Path, "read_bytes", slow_read)
    monkeypatch.setattr(os, "replace", slow_replace)
    monkeypatch.setattr(os, "fsync", slow_fsync)
    first = threading.Thread(target=lambda: store.record("x"), name="holder")
    first.start()
    assert holder_in_lock.wait(5)
    store.record("x")                                                # the waiter
    first.join(10)
    monkeypatch.undo()
    assert store.get("x")["execution_count"] == 2


def test_unwritable_lock_never_stalls_for_the_full_timeout(tmp_path, monkeypatch):
    """Review B-8: a directory where no file can be created skips the lock quickly; any OSError
    but FileExistsError and PermissionError skips it at once."""
    from revit_bridge.capabilities import usage as usage_module

    store = UsageStore(tmp_path / "usage.json", lock_timeout=2.0)
    real_open = os.open
    refused = []

    def read_only_root(path, flags, *args):
        if os.path.dirname(str(path)) == str(tmp_path) and flags & os.O_CREAT:
            refused.append(os.path.basename(str(path)))
            raise PermissionError(13, "read-only data root")              # the lock and the probe
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", read_only_root)
    started = time.monotonic()
    assert store.record("x")["execution_count"] == 1                    # open() writes; not refused here
    elapsed = time.monotonic() - started
    assert usage_module.PERMISSION_RETRY_SECONDS <= elapsed < 0.6     # bounded, not 2 s
    assert not store.lock_path.exists()
    assert "usage.json.lock" in refused and any(name.endswith(".tmp") for name in refused)   # probed

    def no_such_device(path, flags, *args):
        if str(path) == str(store.lock_path):
            raise OSError(30, "Read-only file system")
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", no_such_device)
    started = time.monotonic()
    assert store.record("x")["execution_count"] == 2
    assert time.monotonic() - started < 0.6                            # no lock wait at all; the write itself (fsync) may take a moment on a slow disk


@contextmanager
def _read_only_directory(path):
    """No new file can be created in ``path``: a deny entry for Everyone on Windows, mode 555 elsewhere."""
    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/deny", "*S-1-1-0:(WD,AD)"], check=True, capture_output=True)
    else:
        os.chmod(path, 0o555)
    try:
        yield
    finally:
        if os.name == "nt":
            subprocess.run(["icacls", str(path), "/remove:d", "*S-1-1-0"], check=True, capture_output=True)
        else:
            os.chmod(path, 0o755)


def test_a_read_only_data_root_fails_the_update_quickly(tmp_path):
    """A real directory that refuses new files: the update raises after a short try, not after the
    lock timeout, and the document is left as it was."""
    root = tmp_path / "root"
    root.mkdir()
    store = UsageStore(root / "usage.json", lock_timeout=2.0)
    store.record("x")
    before = store.path.read_bytes()
    with _read_only_directory(root):
        try:
            os.close(os.open(root / "probe", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except PermissionError:
            pass
        else:
            os.unlink(root / "probe")
            pytest.skip("this account creates files in a read-only directory (root)")
        started = time.monotonic()
        with pytest.raises(PermissionError):
            store.record("x")
        assert time.monotonic() - started < 0.6                       # not the 2 s lock timeout
        assert store.get("x")["execution_count"] == 1
    assert store.path.read_bytes() == before
    assert os.listdir(root) == ["usage.json"]


def _note_errors(errors, fn, *args):
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - the test asserts there are none
        errors.append(exc)


def test_a_denied_lock_create_while_another_writer_holds_the_lock_is_waited_for(tmp_path, monkeypatch):
    """On Windows creating the lock fails with PermissionError while its name is still taken by a
    deleted lock that some handle keeps open. That was waited for 0.1 s at most - counted from the
    first denial of the whole wait - and the writer then went on without the lock and wrote over
    the holder's update. In a directory that accepts new files it is a busy lock."""
    store = UsageStore(tmp_path / "usage.json", lock_timeout=2.0)
    real_open, real_fsync = os.open, os.fsync
    holding = threading.Event()

    def denied_while_held(path, flags, *args):
        if (str(path) == str(store.lock_path) and threading.current_thread().name != "holder"
                and holding.is_set()):
            raise PermissionError(13, "delete pending")
        return real_open(path, flags, *args)

    def slow_fsync(fd):
        if threading.current_thread().name == "holder":
            holding.set()
            time.sleep(0.5)                                              # five times the old 0.1 s
            holding.clear()
        return real_fsync(fd)

    monkeypatch.setattr(os, "open", denied_while_held)
    monkeypatch.setattr(os, "fsync", slow_fsync)
    errors = []
    holder = threading.Thread(target=_note_errors, args=(errors, store.record, "x"), name="holder")
    holder.start()
    assert holding.wait(5)
    store.record("x")                                                    # the waiter
    holder.join(10)
    monkeypatch.undo()
    assert errors == []
    assert store.get("x")["execution_count"] == 2
    assert os.listdir(tmp_path) == ["usage.json"]


def _other_hosts(lock_path, hold_seconds, holds, started):
    """Other hosts taking the lock one after another, each for ``hold_seconds``."""
    for n in range(holds):
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except (FileExistsError, PermissionError):
                time.sleep(0.005)
        os.write(fd, f"other host {n}".encode("ascii"))
        os.close(fd)
        started.set()
        time.sleep(hold_seconds)
        for _ in range(100):
            try:
                os.unlink(lock_path)
                break
            except PermissionError:                                      # a scanner holds it for a moment
                time.sleep(0.005)


def test_a_waiter_keeps_waiting_while_the_lock_changes_hands(tmp_path, monkeypatch):
    """CI (windows, 3.14: 45 == 100): writers that release the lock and take it again at once
    starve a writer that polls. It waited out lock_timeout across several holders, went on unlocked
    and wrote over their updates. lock_timeout is how long one holder may keep the lock."""
    store = UsageStore(tmp_path / "usage.json", lock_timeout=0.4)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    started = threading.Event()
    others = threading.Thread(target=_other_hosts, args=(store.lock_path, 0.1, 6, started))
    others.start()
    assert started.wait(5)
    real_replace = os.replace
    lock_during_write = []

    def replace_(src, dst):
        lock_during_write.append(store.lock_path.read_bytes())
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_)
    store.record("x")                                                    # six holders, 0.6 s in all
    others.join(10)
    assert not lock_during_write[0].startswith(b"other host")            # written under its own lock


def test_a_waiter_still_gives_up_when_the_lock_never_comes_free(tmp_path, monkeypatch):
    """However often the lock changes hands, the wait is bounded: a lost update beats a stuck host."""
    from revit_bridge import jsonfile

    monkeypatch.setattr(jsonfile, "LOCK_WAIT_LIMIT_SECONDS", 0.6)
    store = UsageStore(tmp_path / "usage.json", lock_timeout=0.3)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.lock_path.write_text("other hosts")
    stop = threading.Event()

    def next_holder_again_and_again():                                   # the lock file changes all the time
        while not stop.wait(0.05):
            try:
                os.utime(store.lock_path, None)
            except OSError:
                pass

    others = threading.Thread(target=next_holder_again_and_again)
    others.start()
    try:
        started = time.monotonic()
        assert store.record("x")["execution_count"] == 1
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        others.join(5)
    assert 0.6 <= elapsed < 3.0
    assert store.lock_path.read_text() == "other hosts"                  # not ours: left in place


def test_two_writers_inside_write_at_once_never_touch_each_others_temp_file(tmp_path, monkeypatch):
    """A writer that went on unlocked and the holder shared <name>.tmp: one replace found no file
    (FileNotFoundError, the CI failure), or the document was the start of one write and the end of
    the other - not JSON, so the next update kept it aside and started from {}."""
    f = LockedJsonFile(tmp_path / "usage.json")
    docs = [{"writer": "a", "pad": "a" * 4000}, {"writer": "b"}]
    both_written = threading.Barrier(2, timeout=5)
    real_fsync = os.fsync

    def fsync_(fd):
        both_written.wait()                                              # both temp files open at once
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync_)
    errors = []
    writers = [threading.Thread(target=_note_errors, args=(errors, f.write, doc)) for doc in docs]
    for t in writers:
        t.start()
    for t in writers:
        t.join(10)
    monkeypatch.undo()
    assert errors == []
    assert json.loads(f.path.read_text(encoding="utf-8")) in docs
    assert os.listdir(tmp_path) == ["usage.json"]


def test_a_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    from revit_bridge import jsonfile

    store = UsageStore(tmp_path / "usage.json")
    store.record("x")
    before = store.path.read_bytes()
    monkeypatch.setattr(jsonfile, "REPLACE_RETRY_SECONDS", 0.05)

    def reader_holds_it(src, dst):
        raise PermissionError(32, "in use by another process")

    monkeypatch.setattr(os, "replace", reader_holds_it)
    with pytest.raises(PermissionError):
        store.record("x")

    def disk_error(fd):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "fsync", disk_error)
    with pytest.raises(OSError):
        store.record("x")
    monkeypatch.undo()
    assert store.path.read_bytes() == before
    assert os.listdir(tmp_path) == ["usage.json"]                        # no temp file, no lock


def test_a_temp_file_removed_before_its_replace_is_written_again(tmp_path, monkeypatch):
    store = UsageStore(tmp_path / "usage.json")
    real_replace = os.replace
    moved = []

    def swept_first(src, dst):
        moved.append(os.path.basename(src))
        if len(moved) == 1:
            os.unlink(src)                                               # a sweep elsewhere, a person
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", swept_first)
    assert store.record("x")["execution_count"] == 1                     # no FileNotFoundError
    assert len(moved) == 2 and moved[0] != moved[1]                      # a second temp file of its own
    assert os.listdir(tmp_path) == ["usage.json"]


def test_temp_files_of_a_writer_that_died_are_swept_and_never_read(tmp_path):
    from revit_bridge import jsonfile

    store = UsageStore(tmp_path / "usage.json")
    old = time.time() - jsonfile.TEMP_STALE_SECONDS - 60
    names = {
        "dead": "usage.json.4242-0123456789ab.tmp",                      # a writer that died
        "legacy": "usage.json.tmp",                                      # what 0.7.1 and earlier wrote
        "live": "usage.json.4343-ba9876543210.tmp",                      # a writer busy right now
        "other": "devices.json.4242-0123456789ab.tmp",                   # another document's
        "unrelated": "usage.json.notes.tmp",
        "bad": "usage.json.bad",
    }
    for key, name in names.items():
        (tmp_path / name).write_text('{"x": {"execution_count": 99}}', encoding="utf-8")
        if key != "live":
            os.utime(tmp_path / name, (old, old))
    assert store.record("x")["execution_count"] == 1                     # no temp file is the document
    kept = {names[key] for key in ("live", "other", "unrelated", "bad")}
    assert set(os.listdir(tmp_path)) == kept | {"usage.json"}


def _stale_lock(store, text="dead host"):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.lock_path.write_text(text)
    old = time.time() - 60
    os.utime(store.lock_path, (old, old))


def test_a_stale_lock_taken_over_twice_never_removes_the_new_holders_lock(tmp_path, monkeypatch):
    """Two writers found the same stale lock. The first took it over and held a fresh lock; the
    second then deleted that fresh lock and went in as well: two writers in one update."""
    store = UsageStore(tmp_path / "usage.json", lock_timeout=0.3)
    _stale_lock(store)
    real_rename = os.rename

    def first_writer_was_quicker(src, dst):
        if str(src) == str(store.lock_path) and store.lock_path.read_text() == "dead host":
            os.unlink(src)
            store.lock_path.write_text("first writer")                   # took it over, holds this one
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", first_writer_was_quicker)
    assert store.record("x")["execution_count"] == 1                     # waited for it, then went on
    assert store.lock_path.read_text() == "first writer"                 # put back, not deleted
    assert sorted(os.listdir(tmp_path)) == ["usage.json", "usage.json.lock"]


def test_a_stale_lock_that_cannot_be_removed_is_waited_out_not_spun_on(tmp_path, monkeypatch):
    """The stale branch retried at once, forever, when the old lock could not be deleted (held open,
    read-only): the writer hung at full speed."""
    store = UsageStore(tmp_path / "usage.json", lock_timeout=0.3)
    _stale_lock(store)
    real_rename, real_unlink = os.rename, os.unlink

    def held_open(real):
        def deny(src, *args, **kwargs):
            if str(src) == str(store.lock_path):
                raise PermissionError(32, "in use by another process")
            return real(src, *args, **kwargs)
        return deny

    monkeypatch.setattr(os, "rename", held_open(real_rename))
    monkeypatch.setattr(os, "unlink", held_open(real_unlink))
    done = threading.Event()
    writer = threading.Thread(target=lambda: (store.record("x"), done.set()), daemon=True)
    writer.start()
    assert done.wait(5)
    assert store.get("x")["execution_count"] == 1


def test_a_holder_releases_only_its_own_lock(tmp_path):
    f = LockedJsonFile(tmp_path / "usage.json")
    with f.locked():
        f.lock_path.write_bytes(b"another writer")                       # ours was taken over meanwhile
    assert f.lock_path.read_bytes() == b"another writer"


def test_a_lock_that_cannot_be_deleted_is_left_stale_for_the_next_writer(tmp_path, monkeypatch):
    """On Windows a handle without delete sharing refuses the unlink: the lock stayed, and every
    writer after it waited out the lock timeout. It is dated back for the next writer to take over."""
    from revit_bridge import jsonfile

    store = UsageStore(tmp_path / "usage.json", lock_timeout=2.0)
    real_unlink = os.unlink

    def held_open(path, *args, **kwargs):
        if str(path) == str(store.lock_path):
            raise PermissionError(32, "in use by another process")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", held_open)
    store.record("x")
    monkeypatch.undo()
    assert time.time() - store.lock_path.stat().st_mtime > jsonfile.LOCK_STALE_SECONDS
    started = time.monotonic()
    assert store.record("x")["execution_count"] == 2
    assert time.monotonic() - started < 1.0                              # taken over, not waited out
    assert not store.lock_path.exists()


def test_a_read_that_fails_inside_record_never_resets_the_counters(tmp_path, monkeypatch):
    """B5: a sharing violation read as {} and written back reset every pack's counters."""
    from tests.test_devices import _deny_reads

    store = UsageStore(tmp_path / "usage.json")
    store.record("a")
    store.record("b", success=False)
    before = store.path.read_bytes()
    _deny_reads(monkeypatch, store.path)
    try:
        store.record("c")
        raise AssertionError("record must not go on from a file it could not read")
    except OSError:
        pass
    assert store.read_all() == {}                                    # a plain read stays tolerant
    monkeypatch.undo()
    assert store.path.read_bytes() == before
    store.path.write_text("[1, 2]", encoding="utf-8")                # not an object: kept, then replaced
    assert store.record("c")["execution_count"] == 1
    assert store.path.with_name("usage.json.bad").read_text(encoding="utf-8") == "[1, 2]"


def test_a_usage_file_saved_with_a_bom_keeps_its_counters(tmp_path):
    """F2a: the counters of a usage.json saved with a BOM were reset by the next update."""
    store = UsageStore(tmp_path / "usage.json")
    store.record("a")
    store.path.write_bytes(b"\xef\xbb\xbf" + store.path.read_bytes())
    assert store.get("a")["execution_count"] == 1
    assert store.record("a")["execution_count"] == 2
    assert not store.path.with_name("usage.json.bad").exists()


def test_record_usage_never_fails_the_run(tmp_path, monkeypatch):
    from revit_bridge.capabilities.store import ToolStore

    store = ToolStore(user_dir=tmp_path / "user")

    def cannot_write(name, success=True):
        raise PermissionError(13, "usage.json: read-only")

    monkeypatch.setattr(store.usage, "record", cannot_write)
    store.record_usage("create_wall", success=True)                     # no exception
    assert store.load("create_wall").execution_count == 0

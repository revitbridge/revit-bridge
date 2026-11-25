import asyncio
from unittest.mock import AsyncMock

import pytest

from revit_bridge import mentions
from revit_bridge.revit.client import RevitResponse
from revit_bridge.snapshot.query import RevitQueryError, RevitQueryExecutor


METHOD = "get_selection_state_in_revit"


def state(stamp, count, document_id="a"):
    return dict(stamp=stamp, count=count, document_id=document_id)


class Device:
    def __init__(self, *states):
        self.states = list(states)
        self.calls = []
        self.now = 0.0
        self.listed = True

    async def send_command(self, method, params):
        self.calls.append((self.now, method, params))
        if method == "get_bridge_capabilities":
            return RevitResponse(success=True, result={"methods": [METHOD] if self.listed else []})
        assert method == METHOD
        reply = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return reply if isinstance(reply, RevitResponse) else RevitResponse(success=True, result=reply)

    async def sleep(self, seconds):
        assert seconds > 0
        self.now += seconds

    def watch(self, mode="multiple", **kwargs):
        executor = RevitQueryExecutor(self, clock=lambda: self.now, sleep=self.sleep)
        return executor.watch_selection(mode, **kwargs)


def item(n, category="OST_Walls", doc="a"):
    return dict(kind="element", id=n, document_id=doc, name="Example", label="Example",
                category=category, category_label=category, family=None, level=None,
                detail=category, summary=category)


def pages(monkeypatch, rows):
    async def read(executor, kind, *, source, offset=0, limit=50):
        assert kind == "element" and source == "selection" and 0 < limit <= 50
        return dict(kind=kind, items=rows[offset:offset + limit], offset=offset, total=len(rows),
                    truncated=len(rows) > offset + limit, category_matched=[], facets=[])
    reader = AsyncMock(side_effect=read)
    monkeypatch.setattr(mentions, "mention_candidates", reader)
    return reader


@pytest.mark.parametrize("mode", ["multiple", "box"])
def test_watch_resets_settle_on_replacement_empty_and_baseline(monkeypatch, mode):
    base, first, last = state("original", 2), state("first", 2), state("last", 2)
    device = Device(base, base, first, first, state("empty", 0), first, base, last, first, last, last, last)
    reader = pages(monkeypatch, [item(1), item(2)])
    result = asyncio.run(device.watch(mode, settle_ms=500, poll_ms=250))
    assert result["items"] == [item(1), item(2)] and result["total"] == 2
    assert device.now == 2.75 and reader.await_count == 1
    commands = [params for _, method, params in device.calls if method == METHOD]
    assert commands[0]["focus"] is True
    assert all(not params.get("focus") and "prompt" not in params for params in commands[1:])


def test_single_completes_immediately_only_on_changed_singleton(monkeypatch):
    device = Device(state("old", 1), state("old", 1), state("multi", 2), state("new", 1))
    pages(monkeypatch, [item(3)])
    assert asyncio.run(device.watch("single", poll_ms=250))["items"] == [item(3)]
    assert device.now == 0.75


def test_single_waits_through_stable_multi_selection(monkeypatch):
    device = Device(state("empty", 0), state("pair", 2))
    reader = pages(monkeypatch, [])
    assert asyncio.run(device.watch("single", settle_ms=0, timeout_s=1)) == {"timed_out": True}
    reader.assert_not_awaited()


def test_watch_returns_same_element_as_get_current_selection():
    device = Device(state("old", 0), state("new", 1))
    snapshot = dict(view="a:view", members=["21"], page=["21"], total=1)
    row = dict(id="21", name="Panel", family="Basic", category="OST_Walls",
               shown="Walls", length=1200, level="L2")
    replies = [dict(snapshot, items=[row]), dict(document_id="a", items=[row]), snapshot]
    device.send_code = AsyncMock(side_effect=[RevitResponse(success=True, result=r) for r in replies * 2])
    expected = asyncio.run(mentions.mention_candidates(RevitQueryExecutor(device), "element", source="selection"))
    assert asyncio.run(device.watch("single")) == expected
    assert expected["items"][0]["document_id"] == "a" and expected["items"][0]["detail"] == "Walls, 1200 mm, L2"


def test_filter_counts_full_selection_across_mention_pages(monkeypatch):
    rows = [item(n, "OST_Doors") for n in range(1, 51)] + [item(51)]
    device = Device(state("old", 0), state("new", len(rows)))
    pages(monkeypatch, rows)
    result = asyncio.run(device.watch(category="OST_Walls", settle_ms=0))
    assert result == dict(kind="element", items=[item(51)], total=1, offset=0, truncated=False,
                          category_matched=[], facets=[dict(category="OST_Walls", label="OST_Walls", count=1)],
                          filtered_out=50)


def test_timeout_stops_polling_at_deadline(monkeypatch):
    device = Device(state("old", 1))
    reader = pages(monkeypatch, [])
    assert asyncio.run(device.watch("single", timeout_s=1, poll_ms=400)) == {"timed_out": True}
    assert device.now == 1
    assert max(t for t, _, _ in device.calls) < 1
    reader.assert_not_awaited()


def test_deadline_cancels_a_blocked_two_argument_rpc(monkeypatch):
    async def run():
        device = Device()
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "time", lambda: device.now)
        stopped = asyncio.Event()

        async def slow(method, params):
            assert method == "get_bridge_capabilities"
            device.now += 2
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        device.send_command = slow
        assert await device.watch(timeout_s=1) == {"timed_out": True}
        assert stopped.is_set()
    asyncio.run(run())


def test_document_switch_precedes_completion(monkeypatch):
    device = Device(state("old", 0), state("new", 1, "b"))
    reader = pages(monkeypatch, [])
    assert asyncio.run(device.watch("single")) == {"document_changed": True}
    reader.assert_not_awaited()


@pytest.mark.parametrize("change_during_detail_read", [True, False])
def test_document_switch_during_detail_read(monkeypatch, change_during_detail_read):
    doc = "b" if change_during_detail_read else "a"
    device = Device(state("old", 0), state("new", 1), state("new", 1, doc))
    pages(monkeypatch, [item(1, doc="b")])
    assert asyncio.run(device.watch("single")) == {"document_changed": True}


def test_changed_stamp_during_details_restarts_settling(monkeypatch):
    device = Device(state("empty", 0), state("first", 1), state("second", 1))
    reader = pages(monkeypatch, [item(2)])
    assert asyncio.run(device.watch(poll_ms=250, settle_ms=0))["items"] == [item(2)]
    assert reader.await_count == 2


@pytest.mark.parametrize("listed", [False, True])
def test_missing_command_returns_upgrade_hint(listed):
    device = Device(RevitResponse(success=False, error_code=-32601, error="Unknown method"))
    device.listed = listed
    result = asyncio.run(device.watch())
    assert result["error"] == "unsupported_addin"
    assert "upgrade" in result["hint"].lower() and METHOD in result["hint"]
    assert len(device.calls) == 1 + listed


def test_rpc_failure_and_invalid_state_do_not_suggest_an_upgrade():
    with pytest.raises(RuntimeError, match="busy"):
        asyncio.run(Device(RevitResponse(success=False, error="busy")).watch())
    with pytest.raises(RevitQueryError, match="selection state"):
        asyncio.run(Device(state("hash", True)).watch())


def test_large_selection_reports_count_before_reading_mentions(monkeypatch):
    reader = pages(monkeypatch, [])
    with pytest.raises(RevitQueryError, match="5001"):
        asyncio.run(Device(state("old", 0), state("large", 5001)).watch(settle_ms=0))
    reader.assert_not_awaited()


@pytest.mark.parametrize("options", [
    {"mode": "other"}, {"category": "Walls"}, {"poll_ms": 0}, {"settle_ms": -1},
    {"timeout_s": float("nan")}, {"timeout_s": True},
])
def test_invalid_arguments_do_not_focus_revit(options):
    device = Device()
    with pytest.raises(ValueError):
        asyncio.run(device.watch(**options))
    assert device.calls == []


@pytest.mark.parametrize("where", ["sleep", "command", "details"])
def test_cancelling_coroutine_leaves_no_pending_work(monkeypatch, where):
    async def run():
        entered = asyncio.Event()

        async def block(*args, **kwargs):
            entered.set()
            await asyncio.Future()

        device = Device(state("old", 0), state("new", 1))
        if where == "command":
            device.send_command = block
        elif where == "sleep":
            device.sleep = block
        monkeypatch.setattr(mentions, "mention_candidates", block)
        task = asyncio.create_task(device.watch("single"))
        await asyncio.wait_for(entered.wait(), 1)
        before = list(device.calls)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert device.calls == before
        assert asyncio.all_tasks() == {asyncio.current_task()}
    asyncio.run(run())

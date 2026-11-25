import asyncio
from unittest.mock import AsyncMock

import pytest

from revit_bridge.revit.client import RevitResponse
from revit_bridge.snapshot.query import RevitQueryError, RevitQueryExecutor


def client_for(**fields):
    client = AsyncMock()
    client.send_command.return_value = RevitResponse(**fields)
    return client


@pytest.mark.parametrize("mode", ["single", "multiple", "box"])
def test_pick_modes_return_mentions_from_the_document_picked(mode):
    row = {"id": "4294967298", "name": "Panel", "family": "Wall", "category": "OST_Walls",
           "shown": "Walls", "level": "L2", "length": 1200.0}
    client = client_for(success=True, result={"items": [row], "document_id": "opening-a"})
    items = asyncio.run(RevitQueryExecutor(client).trigger_selection(mode, "OST_Walls", "Pick panels", 45))
    assert items == [dict(kind="element", id=4294967298, document_id="opening-a", name="Panel",
                          label="Panel", family="Wall", level="L2", category="OST_Walls", category_label="Walls",
                          detail="Walls, 1200 mm, L2", summary="Walls, 1200 mm, L2")]
    args, kwargs = client.send_command.await_args
    assert args == ("trigger_selection_in_revit", dict(mode=mode, category="OST_Walls", prompt="Pick panels", timeout_s=45))
    assert kwargs["timeout"] > 45
    client.send_code.assert_not_awaited()


@pytest.mark.parametrize("flag", ["cancelled", "timed_out"])
def test_native_end_of_pick_is_not_a_failure(flag):
    client = client_for(success=True, result={flag: True})
    assert asyncio.run(RevitQueryExecutor(client).trigger_selection()) == {flag: True}
    _, params = client.send_command.await_args.args
    assert params["mode"] == "single" and params["timeout_s"] == 120
    assert client.send_command.await_count == 1


def test_box_keeps_all_picks_and_refuses_an_oversized_reply_with_its_count():
    rows = [{"id": str(n)} for n in range(1, 5001)]
    data = {"items": rows, "document_id": "opened-here"}
    client = client_for(success=True, result=data)
    picked = asyncio.run(RevitQueryExecutor(client).trigger_selection("box"))
    assert [p["id"] for p in picked] == list(range(1, 5001))
    assert {p["document_id"] for p in picked} == {"opened-here"}
    rows.append({"id": "5001"})
    with pytest.raises(RevitQueryError, match="5001"):
        asyncio.run(RevitQueryExecutor(client).trigger_selection("box"))


@pytest.mark.parametrize("reply", [
    {"success": False, "error_code": -32601, "error": "Unknown method"},
    {"success": False, "error": "Timeout after 125s"},
    {"success": True, "result": {"items": [{"id": 1}]}},
    {"success": True, "result": {"document_id": "a", "items": [{"id": False}]}},
    {"success": True, "result": {"cancelled": True, "timed_out": True}},
])
def test_failed_or_malformed_pick_never_returns_old_selection(reply):
    client = client_for(**reply)
    with pytest.raises(RevitQueryError):
        asyncio.run(RevitQueryExecutor(client).trigger_selection())
    assert client.send_command.await_count == 1
    client.send_code.assert_not_awaited()


@pytest.mark.parametrize("values", [{"mode": "other"}, {"category": "Walls"}, {"timeout_s": 0},
                                    {"timeout_s": True}, {"prompt": 100}])
def test_invalid_pick_does_not_enter_revit(values):
    client = AsyncMock()
    with pytest.raises(ValueError):
        asyncio.run(RevitQueryExecutor(client).trigger_selection(**values))
    client.send_command.assert_not_awaited()

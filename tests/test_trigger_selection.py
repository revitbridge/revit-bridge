import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from revit_bridge.revit.client import RevitResponse
from revit_bridge.snapshot.query import RevitQueryError, RevitQueryExecutor


METHOD = "trigger_selection_in_revit"
MISSING = RevitResponse(success=False, error_code=-32601, error="Method not found")
PICK = {"Id": 28, "Name": "Panel", "Category": "Walls"}


def test_release_notes_describe_legacy_selection():
    notes = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = notes.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    for term in ("trigger_selection()", "PickObject", "get_selected_elements", "cancelled", "timed_out"):
        assert term in unreleased


def client_for(*, offered=True, discovery=None, **fields):
    client = AsyncMock()

    async def reply(method, params, **kwargs):
        if method == "get_bridge_capabilities":
            return discovery or RevitResponse(success=True, result={"methods": [METHOD] if offered else []})
        assert method == METHOD and offered
        return RevitResponse(**fields)
    client.send_command.side_effect = reply
    return client


@pytest.mark.parametrize("mode", ["single", "multiple", "box"])
def test_pick_modes_return_mentions_from_the_document_picked(mode):
    row = {"id": "4294967298", "name": "Panel", "family": "Wall", "category": "OST_Walls",
           "shown": "Walls", "level": "L2", "length": 1200.0}
    client = client_for(success=True, result={"elements": [dict(row, document_id="opening-a")]})
    items = asyncio.run(RevitQueryExecutor(client).trigger_selection(mode, "OST_Walls", "Pick panels", 45))
    assert items == [dict(kind="element", id=4294967298, document_id="opening-a", name="Panel",
                          label="Panel", family="Wall", level="L2", category="OST_Walls", category_label="Walls",
                          detail="Walls, 1200 mm, L2", summary="Walls, 1200 mm, L2")]
    args, kwargs = client.send_command.await_args
    assert args == ("trigger_selection_in_revit", dict(mode=mode, category="OST_Walls", prompt="Pick panels", timeout_s=45))
    assert kwargs["timeout"] > 45
    client.send_code.assert_not_awaited()


@pytest.mark.parametrize("flag", ["cancelled", "timed_out"])
@pytest.mark.parametrize("options", [{}, {"mode": "single"}, {"category": None}, {"prompt": "Select in Revit"},
                                     {"timeout_s": 120}])
def test_native_end_of_pick_is_not_a_failure(flag, options):
    client = client_for(success=True, result={flag: True})
    result = asyncio.run(RevitQueryExecutor(client).trigger_selection(**options))
    assert result == ({flag: True} if options else [])
    _, params = client.send_command.await_args.args
    assert params["mode"] == "single" and params["timeout_s"] == 120
    assert client.send_command.await_count == 2


@pytest.mark.parametrize("row", [PICK, {"id": 28, "name": "Panel", "category": "OST_Walls",
                                         "category_label": "Walls", "document_id": "project"}])
def test_web_client_without_timeout_keyword_returns_legacy_elements(row):
    class Device:
        async def send_command(self, method, params):
            if method == "get_bridge_capabilities":
                return RevitResponse(success=True, result={"methods": [METHOD]})
            assert method == METHOD and params["timeout_s"] == 120
            return RevitResponse(success=True, result={"elements": [row]})

    assert asyncio.run(RevitQueryExecutor(Device()).trigger_selection()) == [PICK]


def test_pre_discovery_addin_uses_legacy_pick_for_explicit_single():
    client = client_for(discovery=MISSING)
    client.send_code.return_value = RevitResponse(success=True, result=PICK)
    assert asyncio.run(RevitQueryExecutor(client).trigger_selection("single")) == [PICK]
    client.send_command.assert_awaited_once_with("get_bridge_capabilities", {})


def test_unreadable_capabilities_do_not_start_a_legacy_pick():
    client = client_for(discovery=RevitResponse(success=False, error="Access denied"))
    with pytest.raises(RevitQueryError, match="Access denied"):
        asyncio.run(RevitQueryExecutor(client).trigger_selection())
    client.send_code.assert_not_awaited()


@pytest.mark.parametrize("second", [{"id": 2}, {"id": 2, "document_id": "another-project"}])
def test_pick_requires_each_elements_origin_without_an_envelope_identity(second):
    rows = [{"id": 1, "document_id": "project"}, second]
    client = client_for(success=True, result={"elements": rows})
    with pytest.raises(RevitQueryError, match="document identity"):
        asyncio.run(RevitQueryExecutor(client).trigger_selection("multiple"))


@pytest.mark.parametrize("offered", [False, True], ids=["not_listed", "stale_list"])
@pytest.mark.parametrize("pick_ok", [True, False], ids=["pick", "current_selection"])
def test_old_single_pick_uses_0101_snippet_then_current_selection(offered, pick_ok):
    client = AsyncMock()
    selected = [dict(PICK, Id=31)]
    replies = {"get_bridge_capabilities": RevitResponse(success=True, result={"methods": [METHOD] if offered else []}),
               METHOD: MISSING, "get_selected_elements": RevitResponse(success=True, result=selected)}
    client.send_command.side_effect = lambda method, params, **kwargs: replies[method]
    client.send_code.return_value = RevitResponse(success=pick_ok, result=PICK if pick_ok else None)
    result = asyncio.run(RevitQueryExecutor(client).trigger_selection())
    assert result == ([PICK] if pick_ok else selected)
    snippet, = client.send_code.await_args.args
    assert 'uidoc.Selection.PickObject(' in snippet and 'Id = element.Id.Value' in snippet
    assert 'Please select a host element (wall/floor)' in snippet
    assert [a.args[0] for a in client.send_command.await_args_list] == (
        ["get_bridge_capabilities"] + ([METHOD] if offered else []) + ([] if pick_ok else ["get_selected_elements"]))


@pytest.mark.parametrize("mode,category", [("multiple", None), ("box", None), ("single", "OST_Walls")])
@pytest.mark.parametrize("offered", [True, False])
def test_unsupported_pick_reports_upgrade_without_starting_another_pick(mode, category, offered):
    client = client_for(offered=offered, success=False, error_code=-32601, error="Method not found")
    with pytest.raises(RevitQueryError, match="upgrade the add-in to <version placeholder> or later"):
        asyncio.run(RevitQueryExecutor(client).trigger_selection(mode, category))
    assert client.send_command.await_count == 1 + offered
    client.send_code.assert_not_awaited()


def test_box_keeps_all_picks_and_refuses_an_oversized_reply_with_its_count():
    rows = [{"id": str(n)} for n in range(1, 5001)]
    data = {"elements": rows, "document_id": "opened-here"}
    client = client_for(success=True, result=data)
    picked = asyncio.run(RevitQueryExecutor(client).trigger_selection("box"))
    assert [p["id"] for p in picked] == list(range(1, 5001))
    assert {p["document_id"] for p in picked} == {"opened-here"}
    rows.append({"id": "5001"})
    with pytest.raises(RevitQueryError, match="5001"):
        asyncio.run(RevitQueryExecutor(client).trigger_selection("box"))


@pytest.mark.parametrize("reply", [
    {"success": False, "error": "Timeout after 125s"},
    {"success": True, "result": {"elements": [{"id": 1}]}},
    {"success": True, "result": {"document_id": "a", "elements": [{"id": False}]}},
    {"success": True, "result": {"cancelled": True, "timed_out": True}},
])
def test_failed_or_malformed_pick_never_returns_old_selection(reply):
    client = client_for(**reply)
    with pytest.raises(RevitQueryError):
        asyncio.run(RevitQueryExecutor(client).trigger_selection("single"))
    assert client.send_command.await_count == 2
    client.send_code.assert_not_awaited()


@pytest.mark.parametrize("values", [{"mode": "other"}, {"category": "Walls"}, {"timeout_s": 0},
                                    {"timeout_s": True}, {"prompt": 100}])
def test_invalid_pick_does_not_enter_revit(values):
    client = AsyncMock()
    with pytest.raises(ValueError):
        asyncio.run(RevitQueryExecutor(client).trigger_selection(**values))
    client.send_command.assert_not_awaited()

"""The native view tools as called by a host, with a two-argument device client."""
import asyncio
import base64
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from revit_bridge import mcp_server
from revit_bridge.revit.client import RevitResponse


ACTIVATE = "activate_view_in_revit"
EXPORT = "export_view_image_in_revit"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
                       "/x8AAwMCAO+a/VEAAAAASUVORK5CYII=")
ENCODED = base64.b64encode(PNG).decode()


class Device:
    def __init__(self, response, methods=(ACTIVATE, EXPORT)):
        self.calls = []
        self.methods = methods
        self.response = response

    async def send_command(self, method, params):
        self.calls.append((method, params))
        if method == "get_bridge_capabilities":
            return RevitResponse(success=True, result={"methods": list(self.methods)})
        assert method in self.methods
        return self.response


def invoke(monkeypatch, device, tool, params):
    monkeypatch.setattr(mcp_server.RevitClientPool, "get_client", AsyncMock(return_value=device))
    reply = asyncio.run(mcp_server.mcp.call_tool(tool, params))
    assert not reply.is_error, reply.content
    return json.loads(reply.content[0].text)


def test_host_descriptors_extend_mcp_listing_on_request():
    from revit_bridge import list_host_functions

    # A user pack can have the same name as a host function.
    mcp_server._tool_store.solidify("open_view", "return 1;", "Example pack")
    packs = json.loads(mcp_server.list_tools())
    assert any(pack["name"] == "open_view" for pack in packs)
    result = asyncio.run(mcp_server.mcp.call_tool("list_tools", {"include_host_functions": True}))
    descriptors = list_host_functions()
    assert not result.is_error and json.loads(result.content[0].text) == packs + descriptors
    open_tool, capture = descriptors
    assert [(tool["name"], tool["class"], tool["required_addin_command"]) for tool in descriptors] == [
        ("open_view", "ui_only", ACTIVATE), ("capture_view_image", "read_only", EXPORT)]
    assert open_tool["description"].startswith("View only.") and capture["description"].startswith("Read only.")
    assert all(tool["kind"] == "host_function" and "\n" not in tool["description"] for tool in descriptors)
    assert open_tool["parameters"] == [dict(name="name", type="string", required=True)]
    assert capture["parameters"] == [dict(name="width", type="integer", required=False, default=1600),
                                     dict(name="view_id", type="integer", required=False, default=None)]


def test_web_host_can_call_descriptor_names_directly():
    from revit_bridge import list_host_functions, view_commands

    results = [dict(view_id=2, view_name="L1", view_type="FloorPlan"),
               dict(png_base64=ENCODED, width=1, height=1, view_name="L1")]
    for descriptor, result in zip(list_host_functions(), results, strict=True):
        params = {"name": "L1"} if descriptor["name"] == "open_view" else {}
        device = Device(RevitResponse(success=True, result=result))
        function = getattr(view_commands, descriptor["name"])
        reply = asyncio.run(function(device, **params, ledger=mcp_server._ledger, host="web"))
        assert reply["error"] is None and all(reply[k] == v for k, v in result.items())
        assert device.calls[-1][0] == descriptor["required_addin_command"]


def test_open_view_calls_native_command_and_records_ui_only(monkeypatch):
    view = dict(view_id=518, view_name="L1", view_type="FloorPlan")
    device = Device(RevitResponse(success=True, result=view))
    result = invoke(monkeypatch, device, "open_view", {"name": "L1"})
    assert {k: result[k] for k in view} == view and result["error"] is None
    assert device.calls == [("get_bridge_capabilities", {}), (ACTIVATE, {"name": "L1"})]
    assert mcp_server._ledger.get(result["evidence_id"])["confirmed_by"] == "host:ui-only"


def test_open_view_preserves_ambiguous_name_candidates(monkeypatch):
    msg = "Ambiguous view. Candidates: Level 1 (42), Level 1 (84)"
    device = Device(RevitResponse(success=False, error=msg))
    assert invoke(monkeypatch, device, "open_view", {"name": "Level 1"})["error"] == msg


@pytest.mark.parametrize("params,expected", [({}, {"width": 1600}), ({"width": 950, "view_id": 2},
                                                                    {"width": 950, "view_id": 2})])
def test_capture_returns_export_and_records_only_image_digest(monkeypatch, params, expected):
    image = dict(png_base64=ENCODED, width=1, height=1, view_name="L1")
    device = Device(RevitResponse(success=True, result={**image, "warnings": [ENCODED]}))
    result = invoke(monkeypatch, device, "capture_view_image", params)
    assert {k: result[k] for k in image} == image and result["error"] is None
    assert device.calls == [("get_bridge_capabilities", {}), (EXPORT, expected)]
    record = mcp_server._ledger.get(result["evidence_id"])
    assert record["tool"] == "capture_view"
    assert record["result_summary"] == {"sha256": hashlib.sha256(PNG).hexdigest(), "width": 1, "height": 1}
    assert ENCODED not in json.dumps(record)


@pytest.mark.parametrize("image", [ENCODED, {"png_base64": ENCODED, "width": 1601, "height": 1},
                                    {"png_base64": "a" * 1400000, "width": 800, "height": 1}])
def test_invalid_capture_cannot_leak_image_into_reply_or_evidence(monkeypatch, image):
    device = Device(RevitResponse(success=True, result=image))
    result = invoke(monkeypatch, device, "capture_view_image", {})
    assert result["error"] and "png_base64" not in result
    record = mcp_server._ledger.get(result["evidence_id"])
    assert record["result_summary"] == {} and ENCODED not in json.dumps([record, result])


def test_capture_size_refusal_after_native_retries_is_final(monkeypatch):
    reason = "REFUSED: PNG exceeds 1 MiB at 800 pixels. Choose a smaller view."
    device = Device(RevitResponse(success=False, error=reason))
    result = invoke(monkeypatch, device, "capture_view_image", {})
    assert result["error"] == reason
    assert device.calls == [("get_bridge_capabilities", {}), (EXPORT, {"width": 1600})]
    assert mcp_server._ledger.get(result["evidence_id"])["result_summary"] == {}


@pytest.mark.parametrize("tool,params", [("open_view", {"name": "  "}),
                                         ("capture_view_image", {"width": 1601}),
                                         ("capture_view_image", {"view_id": 0})])
def test_invalid_view_arguments_do_not_reach_device(monkeypatch, tool, params):
    device = Device(None)
    assert invoke(monkeypatch, device, tool, params)["error"] == "invalid_args"
    assert device.calls == []


@pytest.mark.parametrize("tool,params,method", [("open_view", {"name": "L1"}, ACTIVATE),
                                                ("capture_view_image", {}, EXPORT)])
@pytest.mark.parametrize("listed", [False, True], ids=["not_listed", "stale_list"])
def test_missing_native_command_has_versioned_upgrade_hint(monkeypatch, listed, tool, params, method):
    device = Device(RevitResponse(success=False, error="Unknown method", error_code=-32601),
                    methods=[method] if listed else [])
    result = invoke(monkeypatch, device, tool, params)
    assert result["error"] == "unsupported_addin"
    assert "upgrade the add-in to <version placeholder> or later" in result["hint"]
    assert len(device.calls) == 1 + listed

"""Execution gate: execute_code / run_tool refuse unconfirmed specs.

Rewritten from the former agent-workflow contract tests: the contract is now
enforced by the package, not by prompt text.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import revit_bridge.mcp_server as server
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.revit.pool import RevitClientPool

from tests.fake_revit import FakeRevit


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point the server at a private copy of the built-in capability packs."""
    store = ToolStore(tmp_path / "caps")
    for src in ToolStore().tools_dir.glob("*.yaml"):
        (store.tools_dir / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(server, "_tool_store", store)
    return store


@pytest.fixture
def revit_env(monkeypatch):
    """Direct the connection pool at a fake add-in for the duration of a test."""
    def apply(port: int):
        monkeypatch.setenv("REVIT_BRIDGE_HOST", "127.0.0.1")
        monkeypatch.setenv("REVIT_BRIDGE_PORT", str(port))
        monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "2")
        monkeypatch.delenv("REVIT_BRIDGE_TOKEN", raising=False)
    return apply


def _call(tool: str, **arguments) -> dict:
    result = asyncio.run(server.mcp.call_tool(tool, arguments))
    return json.loads(result.content[0].text)


def test_gate_refusal_logic():
    assert server.gate_refusal(True, {}) is None
    assert server.gate_refusal(False, {"REVIT_BRIDGE_ALLOW_UNCONFIRMED": "1"}) is None
    refusal = server.gate_refusal(False, {})
    assert refusal["success"] is False
    assert refusal["error"] == "refused_unconfirmed_spec"
    assert "spec_confirmed=true" in refusal["message"]


def test_execute_code_refuses_without_confirmation(monkeypatch):
    monkeypatch.delenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", raising=False)
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")  # nothing listens here; gate must fire first
    out = _call("execute_code", code="return 1;")
    assert out["success"] is False
    assert out["error"] == "refused_unconfirmed_spec"


def test_run_tool_refuses_without_confirmation(monkeypatch, isolated_store):
    monkeypatch.delenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", raising=False)
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    out = _call("run_tool", name="query_levels", params="{}")
    assert out["success"] is False
    assert out["error"] == "refused_unconfirmed_spec"


def test_execute_code_runs_when_confirmed(monkeypatch, revit_env):
    monkeypatch.delenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", raising=False)

    async def scenario():
        async with FakeRevit() as fake:
            revit_env(fake.port)
            try:
                result = await server.mcp.call_tool(
                    "execute_code", {"code": "return 1;", "spec_confirmed": True})
                out = json.loads(result.content[0].text)
                assert out["success"] is True
                assert out["result"] == {"Status": "Created", "ElementId": 4242}
                assert fake.requests[-1]["method"] == "send_code_to_revit"
            finally:
                await RevitClientPool.disconnect()

    asyncio.run(scenario())


def test_env_override_lifts_gate_for_host_flows(monkeypatch, revit_env):
    monkeypatch.setenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", "1")

    async def scenario():
        async with FakeRevit() as fake:
            revit_env(fake.port)
            try:
                result = await server.mcp.call_tool("execute_code", {"code": "return 1;"})
                out = json.loads(result.content[0].text)
                assert out["success"] is True
            finally:
                await RevitClientPool.disconnect()

    asyncio.run(scenario())


def test_execute_code_blocks_dangerous_code_before_dispatch(monkeypatch):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    out = _call("execute_code", code="System.IO.File.Delete(\"x\");", spec_confirmed=True)
    assert out["success"] is False
    assert out["error"] == "blocked"
    assert any("System.IO" in w for w in out["warnings"])


def test_run_tool_requires_queried_parameters(monkeypatch, isolated_store):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    out = _call("run_tool", name="create_wall", params="{}", spec_confirmed=True)
    assert out["success"] is False
    assert "level_name" in out["error"]


def test_run_tool_executes_confirmed_tool(monkeypatch, isolated_store, revit_env):
    monkeypatch.delenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", raising=False)

    async def scenario():
        async with FakeRevit() as fake:
            revit_env(fake.port)
            try:
                result = await server.mcp.call_tool(
                    "run_tool", {"name": "query_levels", "params": "{}", "spec_confirmed": True})
                out = json.loads(result.content[0].text)
                assert out["success"] is True
                assert out["tool"] == "query_levels"
                assert "FilteredElementCollector" in fake.requests[-1]["params"]["code"]
            finally:
                await RevitClientPool.disconnect()

    asyncio.run(scenario())
    assert isolated_store.load("query_levels").execution_count == 1


def test_list_tools_and_choices(monkeypatch, isolated_store, revit_env):
    listing = asyncio.run(server.mcp.call_tool("list_tools", {})).content[0].text
    assert "- create_wall:" in listing
    assert "- query_levels:" in listing

    def handler(request):
        if request["method"] == "send_code_to_revit":
            return FakeRevit.code_result(request["id"], [
                {"Id": 1, "Name": "L1", "ElevationMm": 0.0},
                {"Id": 2, "Name": "L2", "ElevationMm": 4000.0},
            ])
        return FakeRevit.default_handler(request)

    async def scenario():
        async with FakeRevit(handler) as fake:
            revit_env(fake.port)
            try:
                result = await server.mcp.call_tool("get_tool_choices", {"name": "create_wall"})
                return json.loads(result.content[0].text)
            finally:
                await RevitClientPool.disconnect()

    choices = asyncio.run(scenario())
    assert [c["value"] for c in choices["level_name"]] == ["L1", "L2"]


def test_check_connection_reports_both_outcomes():
    async def scenario():
        async with FakeRevit() as fake:
            settings = server.RevitSettings(host="127.0.0.1", port=fake.port, timeout=2.0, connect_timeout=1.0)
            up = await server.check_connection(settings)
        down = await server.check_connection(
            server.RevitSettings(host="127.0.0.1", port=fake.port, timeout=1.0, connect_timeout=0.5))
        return up, down

    up, down = asyncio.run(scenario())
    assert up["reachable"] is True and up["status"] == "connected" and up["error"] is None
    assert down["reachable"] is False and down["status"] == "disconnected" and down["error"]


def test_main_check_exit_code(monkeypatch, capsys):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "1")
    assert server.main(["check"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "disconnected"

"""Execution gate: execute_code / run_tool run only with a token from confirm_spec.

The token is one-time, expires, and is bound to the exact execution
projection; the check subcommand and the read-only listing tools live here
too because they share the fake add-in fixtures.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import revit_bridge.mcp_server as server
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.revit.pool import RevitClientPool

from revit_bridge.spec.gate import Gate
from revit_bridge.spec.models import Action, ParamBinding, Source, TaskSpec

from tests.fake_revit import FakeRevit


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point the server at a store whose user directory is private to the test."""
    store = ToolStore(user_dir=tmp_path / "caps")
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


def _call(tool_name: str, **arguments) -> dict:
    result = asyncio.run(server.mcp.call_tool(tool_name, arguments))
    return json.loads(result.content[0].text)


async def _acall(tool_name: str, **arguments) -> dict:
    result = await server.mcp.call_tool(tool_name, arguments)
    return json.loads(result.content[0].text)


def spec_for(tool: str, _units: dict | None = None, **values) -> dict:
    """A confirmable run_tool spec: every value sourced as the designer's answer."""
    units = _units or {}
    spec = TaskSpec(
        task=f"run {tool}",
        action=Action(kind="run_tool", tool=tool),
        parameters=[ParamBinding(name=k, value=v,
                                 unit=units.get(k, "mm" if isinstance(v, (int, float)) else None),
                                 source=Source.answer, evidence=f"q_{k}") for k, v in values.items()],
        snapshot_fingerprint=None,
    )
    return spec.model_dump(mode="json")


WALL = {"level_name": "L1", "start_x": 0, "start_y": 0, "end_x": 5000, "end_y": 0, "height": 3000}

LEAKY = "schema_version: 1\nname: leaky\nversion: 1.0.0\ncode_template: return {count};\nparameters: []\n"


def drop_pack(store: ToolStore, name: str, text: str) -> None:
    """Write a pack file by hand (solidify would refuse an invalid one)."""
    store.user_dir.mkdir(parents=True, exist_ok=True)
    (store.user_dir / f"{name}.yaml").write_text(text, encoding="utf-8")


def counting_handler(before: int, after: int, result=None):
    """A fake add-in whose count probe answers ``before`` until code has run, then ``after``."""
    state = {"ran": False}

    def handler(request):
        rid, method = request["id"], request["method"]
        if method == "send_code_to_revit":
            code = request["params"]["code"]
            if "CategoryNames" in code:                                   # the snapshot block
                return FakeRevit.code_result(rid, {
                    "Document": {"Title": "Project1", "RevitVersion": "2026", "IsWorkshared": False},
                    "Levels": [{"Id": 1, "Name": "L1", "ElevationMm": 0.0}],
                    "CategoryNames": {}, "Warnings": []})
            if "GetElementCount" in code and "Enum.Parse(typeof(BuiltInCategory)" in code:
                return FakeRevit.code_result(rid, after if state["ran"] else before)
            if code == server.DOCUMENT_PROBE:
                return FakeRevit.code_result(rid, {"Title": "Project1", "RevitVersion": "2026"})
            state["ran"] = True
            return FakeRevit.code_result(rid, result if result is not None else {"Status": "Created", "ElementId": 4242})
        return FakeRevit.default_handler(request)
    return handler


def code_spec(code: str, parameters: list | None = None) -> dict:
    return TaskSpec(task="run code", action=Action(kind="execute_code", code=code, code_parameters=parameters),
                    parameters=[], snapshot_fingerprint=None).model_dump(mode="json")


def confirm(spec: dict) -> dict:
    return _call("confirm_spec", spec=spec)


def test_gate_refusal_logic():
    projection = {"kind": "run_tool", "tool": "query_levels", "params": {}}
    assert server.gate_refusal("", projection, {"REVIT_BRIDGE_ALLOW_UNCONFIRMED": "1"}) is None
    refusal = server.gate_refusal("", projection, {})
    assert refusal["success"] is False and refusal["error"] == "confirmation_required"
    assert "confirm_spec" in refusal["hint"]
    unknown = server.gate_refusal("not-a-token", projection, {})
    assert unknown["error"] == "confirmation_invalid" and unknown["reason"] == "unknown"


def test_execute_code_refuses_without_token(monkeypatch):
    monkeypatch.delenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", raising=False)
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")  # nothing listens here; gate must fire first
    out = _call("execute_code", code="return 1;")
    assert out["success"] is False and out["error"] == "confirmation_required"
    out = _call("execute_code", code="return 1;", token="   ")
    assert out["error"] == "confirmation_required"
    out = _call("execute_code", code="return 1;", token="bogus")
    assert out["error"] == "confirmation_invalid" and out["reason"] == "unknown"


def test_run_tool_refuses_without_token(monkeypatch, isolated_store):
    monkeypatch.delenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", raising=False)
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    out = _call("run_tool", name="query_levels", params="{}")
    assert out["success"] is False and out["error"] == "confirmation_required"
    # spec_confirmed is gone: the old flag lifts nothing and is not in the schemas
    out = _call("run_tool", name="query_levels", params="{}", spec_confirmed=True)
    assert out["error"] == "confirmation_required"
    out = _call("execute_code", code="return 1;", spec_confirmed=True)
    assert out["error"] == "confirmation_required"
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("run_tool", "execute_code"):
        props = tools[name].input_schema["properties"]
        assert "token" in props and "spec_confirmed" not in props, name


def test_confirm_spec_issues_a_token_and_execute_code_redeems_it(monkeypatch, revit_env):
    monkeypatch.delenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", raising=False)

    async def scenario():
        async with FakeRevit() as fake:
            revit_env(fake.port)
            try:
                issued = await _acall("confirm_spec", spec=code_spec("return 1;"))
                assert set(issued) == {"token", "spec_hash", "expires_at", "card"}
                assert issued["card"].startswith("Task: run code\nCode: execute_code (1 lines)")
                result = await server.mcp.call_tool(
                    "execute_code", {"code": "return 1;", "token": issued["token"]})
                out = json.loads(result.content[0].text)
                assert out["success"] is True
                assert out["result"] == {"Status": "Created", "ElementId": 4242}
                assert fake.requests[-1]["method"] == "send_code_to_revit"
                return issued["token"]
            finally:
                await RevitClientPool.disconnect()

    token = asyncio.run(scenario())
    # the same token cannot run again
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    again = _call("execute_code", code="return 1;", token=token)
    assert again["error"] == "confirmation_invalid" and again["reason"] == "used"


def test_token_is_bound_to_the_confirmed_parameters(monkeypatch, isolated_store):
    """Confirm A, execute B: refused with reason mismatch."""
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    issued = confirm(spec_for("create_wall", **WALL))
    assert "token" in issued
    token = issued["token"]
    out = _call("run_tool", name="create_wall", params=json.dumps({**WALL, "height": 4000}), token=token)
    assert out == {"success": False, "error": "confirmation_invalid", "reason": "mismatch",
                   "message": "the execution does not match the confirmed spec"}
    out = _call("run_tool", name="query_levels", params="{}", token=token)
    assert out["reason"] == "mismatch"
    out = _call("execute_code", code="return 1;", token=token)
    assert out["reason"] == "mismatch"
    # a mismatch does not consume the token: the confirmed call still works (3000.0 == 3000)
    assert server._gate.peek(token).used_at is None
    projection = {"kind": "run_tool", "tool": "create_wall", "params": {**WALL, "height": 3000.0}}
    assert server.gate_refusal(token, projection, {}) is None


def test_a_refused_validation_does_not_consume_the_token(monkeypatch, isolated_store, revit_env):
    """Review C-2: verify first, consume the moment before send_code."""
    spec = spec_for("create_wall", _units={"height": "mm"}, **{**WALL, "height": "tall"})  # not a number
    token = confirm(spec)["token"]
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    out = _call("run_tool", name="create_wall", params=json.dumps({**WALL, "height": "tall"}), token=token)
    assert out["success"] is False and "expects double" in out["error"]
    assert server._gate.peek(token).used_at is None                     # still redeemable
    # a leftover placeholder, an unhealthy tool and a blocked execute_code leave it alone too
    drop_pack(isolated_store, "leaky", LEAKY)
    leaky = confirm(spec_for("leaky"))["token"]
    assert "placeholder" in _call("run_tool", name="leaky", params="{}", token=leaky)["error"]
    assert server._gate.peek(leaky).used_at is None
    # an unreachable Revit does not consume it either
    good = confirm(spec_for("query_levels"))["token"]
    out = _call("run_tool", name="query_levels", params="{}", token=good)
    assert out["success"] is False and server._gate.peek(good).used_at is None

    async def scenario():
        async with FakeRevit(counting_handler(2, 2)) as fake:
            revit_env(fake.port)
            try:
                out = await _acall("run_tool", name="query_levels", params="{}", token=good)
                assert out["success"] is True, out                         # the same token, later
                assert server._gate.peek(good).used_at
                fake.requests.clear()
                blocked = (await _acall("confirm_spec", spec=code_spec("return 1;")))["token"]
                monkeypatch.setattr(server.sandbox, "review", lambda code: (False, ["Blocked pattern: test"]))
                out = await _acall("execute_code", code="return 1;", token=blocked)
                assert out["error"] == "blocked" and fake.requests == []
                assert server._gate.peek(blocked).used_at is None
            finally:
                await RevitClientPool.disconnect()

    asyncio.run(scenario())


def test_gate_verify_and_consume(isolated_data_dir):
    gate = Gate(ttl_seconds=600)
    spec = TaskSpec.model_validate(spec_for("query_levels"))
    conf = gate.issue(spec)
    projection = spec.execution_projection()
    assert gate.verify(conf.token, projection).used_at is None
    assert gate.verify(conf.token).used_at is None                      # projection optional for verify
    with pytest.raises(Exception) as excinfo:
        gate.verify(conf.token, {"kind": "run_tool", "tool": "other", "params": {}})
    assert excinfo.value.reason == "mismatch" and gate.peek(conf.token).used_at is None
    assert gate.consume(conf.token, projection).used_at
    with pytest.raises(Exception) as excinfo:
        gate.verify(conf.token, projection)
    assert excinfo.value.reason == "used"
    with pytest.raises(Exception) as excinfo:
        gate.consume(conf.token, projection)
    assert excinfo.value.reason == "used"


def test_token_expires(monkeypatch, isolated_store):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    monkeypatch.setattr(server._gate, "ttl_seconds", 0)
    token = confirm(spec_for("query_levels"))["token"]
    out = _call("run_tool", name="query_levels", params="{}", token=token)
    assert out["error"] == "confirmation_invalid" and out["reason"] == "expired"
    out = _call("run_tool", name="query_levels", params="{}", token=token)
    assert out["reason"] == "unknown"                    # expired tokens are forgotten


def test_token_survives_a_server_restart_once(isolated_data_dir, isolated_store, monkeypatch):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    token = confirm(spec_for("query_levels"))["token"]
    pending = list((isolated_data_dir / "evidence" / "pending").glob("*.json"))
    assert len(pending) == 1 and pending[0].name == f"{token[:12]}.json"

    fresh = Gate()                                        # same data dir, empty memory
    conf = fresh.redeem(token, {"kind": "run_tool", "tool": "query_levels", "params": {}})
    assert conf.used_at and not pending[0].exists()
    with pytest.raises(Exception) as excinfo:
        fresh.redeem(token, {"kind": "run_tool", "tool": "query_levels", "params": {}})
    assert excinfo.value.reason == "used"
    assert Gate().peek(token) is None                     # gone from disk


def test_confirm_spec_returns_errors_without_a_token(isolated_store):
    out = confirm(spec_for("create_wall", **{k: v for k, v in WALL.items() if k != "level_name"}))
    assert "token" not in out
    assert [e["code"] for e in out["errors"]] == ["missing_param"]
    assert out["errors"][0]["param"] == "level_name"
    out = confirm({"task": "x"})
    assert out["errors"][0]["code"] == "invalid_spec"
    out = confirm(json.dumps(spec_for("query_levels")))   # JSON text is accepted too
    assert "token" in out


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
    code = "System.IO.File.Delete(\"x\");"
    refused = confirm(code_spec(code))                  # confirm_spec already refuses it
    assert refused["errors"][0]["code"] == "blocked_code"
    monkeypatch.setenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", "1")
    out = _call("execute_code", code=code)               # and so does the tool itself
    assert out["success"] is False and out["error"] == "blocked"
    assert any("System.IO" in w for w in out["warnings"])


def test_run_tool_requires_queried_parameters(monkeypatch, isolated_store):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    monkeypatch.setenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", "1")
    out = _call("run_tool", name="create_wall", params="{}")
    assert out["success"] is False
    assert "level_name" in out["error"]


def test_run_tool_never_ships_a_leftover_placeholder(monkeypatch, isolated_store):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")  # nothing listens: the refusal must come first
    drop_pack(isolated_store, "leaky", LEAKY)
    token = confirm(spec_for("leaky"))["token"]
    out = _call("run_tool", name="leaky", params="{}", token=token)
    assert out["success"] is False
    assert "placeholder(s) ['count']" in out["error"]


def test_run_tool_executes_confirmed_tool(monkeypatch, isolated_store, revit_env):
    monkeypatch.delenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", raising=False)

    async def scenario():
        async with FakeRevit(counting_handler(2, 2, result=[{"Id": 1, "Name": "L1"}])) as fake:
            revit_env(fake.port)
            try:
                token = (await _acall("confirm_spec", spec=spec_for("query_levels")))["token"]
                result = await server.mcp.call_tool(
                    "run_tool", {"name": "query_levels", "params": "{}", "token": token})
                out = json.loads(result.content[0].text)
                assert out["success"] is True and out["error"] is None
                assert out["tool"] == "query_levels" and out["result"] == [{"Id": 1, "Name": "L1"}]
                assert out["validation"]["passed"] is True and out["validation"]["validator"] == "count_delta"
                assert out["evidence_id"].startswith("ev_") and out["warnings"] == []
                codes = [r["params"]["code"] for r in fake.requests if r["method"] == "send_code_to_revit"]
                assert any("typeof(Level)" in c and "OrderBy" in c for c in codes)
            finally:
                await RevitClientPool.disconnect()

    asyncio.run(scenario())
    assert isolated_store.load("query_levels").execution_count == 1


def test_missing_params_and_reconcile_tools(isolated_store, monkeypatch):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")           # no Revit: best effort, no options
    monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "1")
    questions = _call("missing_params", tool="create_wall", known={k: v for k, v in WALL.items() if k != "level_name"})
    assert [q["param"] for q in questions] == ["level_name"]
    assert questions[0]["id"] == "q_level_name" and questions[0]["options"] == []
    assert _call("missing_params", tool="nope") == {"error": "unknown_tool", "tool": "nope"}
    with pytest.raises(Exception):                        # the MCP layer rejects a non-object `known`
        _call("missing_params", tool="create_wall", known=[1])
    assert _call("missing_params", tool="create_wall", known=json.dumps(WALL)) == []          # JSON text ok

    snapshot = {
        "taken_at": "2026-09-20T00:00:00Z", "duration_ms": 1,
        "document": {"title": "P", "revit_version": "2026", "is_workshared": False},
        "units": {"length": "mm", "raw": ""}, "active_view": None,
        "levels": [{"id": 1, "name": "L1", "elevation_mm": 0.0}], "grids": {"count": 0, "names": []},
        "family_types": [], "selection": [], "selection_count": 0, "links": [], "phases": [],
        "warnings": [], "fingerprint": "f" * 16,
    }
    # a snapshot passed in supplies the options; a broken one is refused
    questions = _call("missing_params", tool="create_wall", known={}, snapshot=snapshot)
    assert [q["param"] for q in questions] == ["level_name", "start_x", "start_y", "end_x", "end_y"]
    assert questions[0]["options"] == [{"label": "L1 (0.0mm)", "value": "L1", "source": "tool:levels"}]
    assert _call("missing_params", tool="create_wall", snapshot={"nope": 1})["error"] == "invalid_snapshot"

    draft = spec_for("create_wall", **{**WALL, "level_name": "l1"})
    out = _call("reconcile", spec=draft, snapshot=snapshot)
    assert [c["kind"] for c in out["conflicts"]] == ["not_found"]
    assert out["conflicts"][0]["available"][0] == "L1" and out["ready"] is False
    assert _call("reconcile", spec={"task": "x"}, snapshot=snapshot)["error"] == "invalid_spec"
    assert _call("reconcile", spec=draft, snapshot={"nope": 1})["error"] == "invalid_snapshot"

    # without a snapshot the server takes one: unreachable and broken map like get_project_snapshot (C-4)
    out = _call("reconcile", spec=draft)
    assert out["error"] == "revit_unreachable"

    class Client:
        pass

    async def fake_client(*args, **kwargs):
        return Client()

    async def broken_snapshot(client, cats=None):
        raise KeyError("Levels")

    monkeypatch.setattr(RevitClientPool, "get_client", fake_client)
    monkeypatch.setattr(server, "take_snapshot", broken_snapshot)
    out = _call("reconcile", spec=draft)
    assert out == {"error": "snapshot_failed", "message": "KeyError: 'Levels'"}


def test_hook_denies_calls_without_a_token():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "spec_gate.py"
    module_spec = importlib.util.spec_from_file_location("spec_gate", path)
    hook = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(hook)

    for tool in ("mcp__revit-bridge__run_tool", "mcp__plugin_revit-bridge_revit-bridge__execute_code"):
        assert hook.decide(tool, {}) == hook.REASON
        assert hook.decide(tool, {"token": ""}) == hook.REASON
        assert hook.decide(tool, {"token": "   "}) == hook.REASON
        assert hook.decide(tool, {"token": True}) == hook.REASON
        assert hook.decide(tool, {"spec_confirmed": True}) == hook.REASON   # the 0.1 flag no longer counts
        assert hook.decide(tool, {"token": "abc"}) is None
    assert hook.decide("mcp__revit-bridge__query", {}) is None
    assert hook.decide("Bash", {"command": "ls"}) is None
    assert "confirm_spec" in hook.REASON and "spec_confirmed" not in hook.REASON


def test_list_tools_and_choices(monkeypatch, isolated_store, revit_env):
    listing = {t["name"]: t for t in json.loads(asyncio.run(server.mcp.call_tool("list_tools", {})).content[0].text)}
    assert set(listing) >= {"create_wall", "query_levels"}
    wall = listing["create_wall"]
    assert wall["version"] == "1.0.0" and wall["validator"] == "count_delta" and wall["used"] == 0
    assert wall["preconditions"][0] == {"kind": "levels_min", "value": 1}
    assert wall["parameters"][0] == {"name": "level_name", "type": "string", "source": "tool:levels",
                                     "required": True, "choices_from": "levels"}

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
    assert up["document"] == "Project1" and up["document_error"] is None
    assert down["reachable"] is False and down["status"] == "disconnected" and down["error"]
    assert down["document"] is None and down["document_error"] is None


def test_check_probe_is_a_read_only_snippet_not_say_hello():
    async def scenario():
        async with FakeRevit() as fake:
            settings = server.RevitSettings(host="127.0.0.1", port=fake.port, timeout=2.0, connect_timeout=1.0)
            await server.check_connection(settings)
            return fake.requests

    requests = asyncio.run(scenario())
    assert [r["method"] for r in requests] == ["send_code_to_revit"]
    assert requests[0]["params"]["code"] == "return document.Title;"

    # The add-in answered but could not run the probe: reachable, document error reported
    def no_document(request):
        return FakeRevit.code_result(request["id"], None, success=False,
                                     error="NullReferenceException: no document")

    def wrong_token(request):
        return FakeRevit.error(request["id"], -32600, "Unauthorized: invalid or missing token")

    def hangs(request):
        return []

    async def probe(handler, timeout=2.0):
        async with FakeRevit(handler) as fake:
            settings = server.RevitSettings(host="127.0.0.1", port=fake.port, timeout=timeout, connect_timeout=1.0)
            return await server.check_connection(settings)

    status = asyncio.run(probe(no_document))
    assert status["reachable"] is True and status["status"] == "connected" and status["error"] is None
    assert status["document"] is None and "no document" in status["document_error"]

    status = asyncio.run(probe(wrong_token))
    assert status["reachable"] is True and "Unauthorized" in status["document_error"]

    # No reply at all is not reachable
    status = asyncio.run(probe(hangs, timeout=0.3))
    assert status["reachable"] is False and "Timeout" in status["error"] and status["document_error"] is None


def test_main_check_exit_code_follows_reachable(monkeypatch, capsys):
    """`revit-bridge check` exits 0 when the add-in answered, even without a document."""
    import threading

    def no_document(request):
        return FakeRevit.code_result(request["id"], None, success=False, error="no document")

    ready, stop = threading.Event(), threading.Event()
    port: list[int] = []

    def serve():  # the check runs its own event loop, so the fake needs one of its own
        async def run():
            async with FakeRevit(no_document) as fake:
                port.append(fake.port)
                ready.set()
                while not stop.is_set():
                    await asyncio.sleep(0.02)
        asyncio.run(run())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(5)
    try:
        monkeypatch.setenv("REVIT_BRIDGE_PORT", str(port[0]))
        monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "2")
        assert server.main(["check"]) == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed["status"] == "connected" and printed["document_error"] == "no document"
    finally:
        stop.set()
        thread.join(5)


def test_main_check_exit_code(monkeypatch, capsys):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "1")
    assert server.main(["check"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "disconnected"


def test_missing_params_takes_a_snapshot_when_none_is_given(isolated_store, revit_env):
    """Review C-B: options come from a live snapshot when the caller passes none."""
    def handler(request):
        rid, method = request["id"], request["method"]
        if method == "send_code_to_revit" and "CategoryNames" in request["params"]["code"]:
            return FakeRevit.code_result(rid, {
                "Document": {"Title": "P", "RevitVersion": "2026", "IsWorkshared": False},
                "Levels": [{"Id": 1, "Name": "L1", "ElevationMm": 0.0}, {"Id": 2, "Name": "L2", "ElevationMm": 3600.0}],
                "CategoryNames": {}, "Warnings": [],
            })
        return FakeRevit.default_handler(request)

    async def scenario():
        async with FakeRevit(handler) as fake:
            revit_env(fake.port)
            try:
                return await _acall("missing_params", tool="create_wall",
                                    known={k: v for k, v in WALL.items() if k != "level_name"})
            finally:
                await RevitClientPool.disconnect()

    questions = asyncio.run(scenario())
    assert [q["param"] for q in questions] == ["level_name"]
    assert [o["value"] for o in questions[0]["options"]] == ["L1", "L2"]
    assert questions[0]["text"].startswith("请选择 level_name")


# -- spec 8/9: validators, preconditions and the ledger in the execution flow ------------------

def test_run_tool_success_needs_the_validator_to_pass(isolated_store, revit_env, isolated_data_dir):
    """success = Revit succeeded AND the validator passed; the ledger keeps the report."""
    async def scenario():
        async with FakeRevit(counting_handler(1, 1)) as fake:   # a wall was "created" but the count did not move
            revit_env(fake.port)
            try:
                token = (await _acall("confirm_spec", spec=spec_for("create_wall", **WALL)))["token"]
                out = await _acall("run_tool", name="create_wall", params=json.dumps(WALL), token=token)
                assert out["success"] is False and out["error"] == "validation_failed"
                assert out["result"] == {"Status": "Created", "ElementId": 4242}     # attached as is
                assert out["validation"]["validator"] == "count_delta" and out["validation"]["passed"] is False
                assert out["validation"]["checks"][0]["detail"] == "OST_Walls: before 1, after 1, delta 0, expected 1"
                assert out["validation"]["before"] == {"category": "OST_Walls", "count": 1}
                record = server._ledger.get(out["evidence_id"])
                assert record["success"] is False and record["error"] == "validation_failed"
                assert record["validation"]["passed"] is False
                assert record["tool"] == "create_wall" and record["tool_version"] == "1.0.0"
                assert record["token_prefix"] == token[:6] and record["confirmed_by"] == "designer"
                assert record["document"] == {"title": "Project1", "revit_version": "2026"}
                assert record["params"] == WALL and record["result_summary"]["ids"] == [4242]
                assert record["host"] == "mcp" and record["action"] == "run_tool"
                assert isolated_store.load("create_wall").failure_count == 1   # counted as a failure

                token = (await _acall("confirm_spec", spec=spec_for("create_wall", **WALL)))["token"]
                fake.handler = counting_handler(1, 2)
                out = await _acall("run_tool", name="create_wall", params=json.dumps(WALL), token=token)
                assert out["success"] is True and out["error"] is None and out["validation"]["passed"] is True
                assert server._ledger.get(out["evidence_id"])["success"] is True
                assert isolated_store.load("create_wall").execution_count == 1
            finally:
                await RevitClientPool.disconnect()

    asyncio.run(scenario())
    files = list((isolated_data_dir / "evidence").glob("*.jsonl"))
    assert len(files) == 1 and len(files[0].read_text(encoding="utf-8").splitlines()) == 2


def test_run_tool_refuses_when_preconditions_fail_without_consuming_the_token(isolated_store, revit_env):
    counting = counting_handler(1, 2)

    def no_levels(request):
        rid = request["id"]
        if request["method"] == "send_code_to_revit" and "CategoryNames" in request["params"]["code"]:
            return FakeRevit.code_result(rid, {
                "Document": {"Title": "Empty", "RevitVersion": "2026", "IsWorkshared": False},
                "Levels": [], "CategoryNames": {}, "Warnings": []})
        return counting(request)

    async def scenario():
        async with FakeRevit(no_levels) as fake:
            revit_env(fake.port)
            try:
                token = (await _acall("confirm_spec", spec=spec_for("create_wall", **WALL)))["token"]
                out = await _acall("run_tool", name="create_wall", params=json.dumps(WALL), token=token)
                assert out["success"] is False and out["error"] == "preconditions_failed"
                assert out["preconditions_failed"] == ["levels_min 1: the model has 0 level(s)"]
                assert server._gate.peek(token).used_at is None
                codes = [r["params"]["code"] for r in fake.requests if r["method"] == "send_code_to_revit"]
                assert not any("Wall.Create" in c for c in codes)             # nothing was executed
            finally:
                await RevitClientPool.disconnect()

    asyncio.run(scenario())


def test_run_tool_skips_preconditions_when_the_snapshot_times_out(isolated_store, revit_env, monkeypatch):
    monkeypatch.setattr(server, "PRECONDITION_SNAPSHOT_TIMEOUT", 0.3)

    counting = counting_handler(1, 2)

    def slow_snapshot(request):
        if request["method"] == "send_code_to_revit" and "CategoryNames" in request["params"]["code"]:
            return []                                                   # never answers
        return counting(request)

    async def scenario():
        async with FakeRevit(slow_snapshot) as fake:
            revit_env(fake.port)
            try:
                token = (await _acall("confirm_spec", spec=spec_for("create_wall", **WALL)))["token"]
                out = await _acall("run_tool", name="create_wall", params=json.dumps(WALL), token=token)
                assert out["success"] is True
                assert out["warnings"][0].startswith("preconditions skipped: snapshot timed out")
            finally:
                await RevitClientPool.disconnect()

    asyncio.run(scenario())


def test_execute_code_writes_a_ledger_line_and_evidence_lists_it(revit_env, isolated_store):
    code = 'return new { ElementId = 77, Status = "Created" };'

    async def scenario():
        async with FakeRevit(counting_handler(0, 0, result={"ElementId": 77, "Status": "Created"})) as fake:
            revit_env(fake.port)
            try:
                token = (await _acall("confirm_spec", spec=code_spec(code)))["token"]
                out = await _acall("execute_code", code=code, token=token)
                assert out["success"] is True and out["validation"] is None
                assert out["result"] == {"ElementId": 77, "Status": "Created"}
                record = server._ledger.get(out["evidence_id"])
                assert record["action"] == "execute_code" and record["tool"] is None
                assert len(record["code_sha256"]) == 64 and record["code_head"] == code
                assert record["params"] == {"parameters": []}
                assert record["result_summary"] == {"ids": [77], "status": "Created"}
                assert record["document"]["title"] == "Project1"
                listed = await _acall("evidence", limit=5)
                assert [r["id"] for r in listed] == [out["evidence_id"]]
                assert await _acall("evidence", tool="create_wall") == []
                return out["evidence_id"]
            finally:
                await RevitClientPool.disconnect()

    evidence_id = asyncio.run(scenario())
    assert _call("validate", evidence_id=evidence_id) == {
        "error": "no_validator", "message": "only run_tool executions carry a validator"}
    assert _call("validate", evidence_id="ev_20000101T000000_ffffff")["error"] == "unknown_evidence"


def test_validate_reruns_the_recorded_execution_now(isolated_store, revit_env):
    async def scenario():
        async with FakeRevit(counting_handler(1, 2)) as fake:
            revit_env(fake.port)
            try:
                token = (await _acall("confirm_spec", spec=spec_for("create_wall", **WALL)))["token"]
                out = await _acall("run_tool", name="create_wall", params=json.dumps(WALL), token=token)
                assert out["success"] is True
                again = await _acall("validate", evidence_id=out["evidence_id"])
                assert again["evidence_id"] == out["evidence_id"] and again["tool"] == "create_wall"
                assert again["validator"] == "count_delta" and again["passed"] is True
                assert again["before"] == {"category": "OST_Walls", "count": 1}      # from the record
                fake.handler = counting_handler(1, 1)                                # the wall is gone now
                gone = await _acall("validate", evidence_id=out["evidence_id"])
                assert gone["passed"] is False and "delta 0, expected 1" in gone["checks"][0]["detail"]
            finally:
                await RevitClientPool.disconnect()

    asyncio.run(scenario())


COUNTED = """schema_version: 1
name: counted
version: 1.0.0
code_template: return new { Status = "Created", Count = {count} };
parameters:
  - {name: count, type: integer, source: default, required: false, default: 2}
validator: {kind: count_delta, category: OST_Walls, expected: "{count}"}
"""


def test_validator_sees_pack_defaults(isolated_store, revit_env):
    """Review D-1: a validator may reference a parameter the projection never bound."""
    drop_pack(isolated_store, "counted", COUNTED)

    async def scenario():
        async with FakeRevit(counting_handler(1, 3)) as fake:
            revit_env(fake.port)
            try:
                token = (await _acall("confirm_spec", spec=spec_for("counted")))["token"]
                out = await _acall("run_tool", name="counted", params="{}", token=token)
                assert out["success"] is True, out
                assert out["validation"]["checks"][0]["detail"].endswith("delta 2, expected 2")
                assert isolated_store.load("counted").failure_count == 0
                assert server._ledger.get(out["evidence_id"])["params"] == {}      # recorded as given
                again = await _acall("validate", evidence_id=out["evidence_id"])
                assert again["passed"] is True                                     # defaults filled here too
            finally:
                await RevitClientPool.disconnect()

    asyncio.run(scenario())


def test_run_tool_refuses_when_validator_before_fails_without_consuming_the_token(isolated_store, revit_env):
    """Review D-2: no sample, no run - otherwise `after` fails and a retry duplicates the work."""
    counting = counting_handler(1, 2)

    def count_probe_fails(request):
        if request["method"] == "send_code_to_revit":
            code = request["params"]["code"]
            if "GetElementCount" in code and "Enum.Parse(typeof(BuiltInCategory)" in code:
                return FakeRevit.code_result(request["id"], None, success=False, error="no document open")
        return counting(request)

    async def scenario():
        async with FakeRevit(count_probe_fails) as fake:
            revit_env(fake.port)
            try:
                token = (await _acall("confirm_spec", spec=spec_for("create_wall", **WALL)))["token"]
                out = await _acall("run_tool", name="create_wall", params=json.dumps(WALL), token=token)
                assert out["success"] is False and out["error"] == "validator_before_failed"
                assert out["warnings"] == ["validator.before: ValidatorError: no document open"]
                assert "validation" not in out
                assert server._gate.peek(token).used_at is None                  # still redeemable
                codes = [r["params"]["code"] for r in fake.requests if r["method"] == "send_code_to_revit"]
                assert not any("Wall.Create" in c for c in codes)               # nothing was executed
                assert isolated_store.load("create_wall").failure_count == 0
            finally:
                await RevitClientPool.disconnect()

    asyncio.run(scenario())

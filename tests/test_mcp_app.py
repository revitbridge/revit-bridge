import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager

import httpx2
import pytest
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.routing import Mount

from revit_bridge import mcp_server
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.execution import DOCUMENT_PROBE
from revit_bridge.mcp_app import _DeviceGate, build_asgi_app, scope_directory
from revit_bridge.revit.client import RevitClient
from revit_bridge.revit.settings import RevitSettings
from revit_bridge.snapshot.query import LEVELS_CODE
from revit_bridge.spec.gate import GateError
from revit_bridge.spec.models import TaskSpec
from revit_bridge.workflows import WorkflowStore
from tests.fake_revit import FakeRevit
from tests.test_check_code import CLEAN_CHECK
from tests.test_dry_run import dry_addin
from tests.test_gate import code_spec


@asynccontextmanager
async def device(label):
    def handler(request):
        rid = request["id"]
        if request["method"] == "check_code_in_revit":
            return FakeRevit.ok(rid, CLEAN_CHECK)
        code = request["params"].get("code")
        if code == LEVELS_CODE:
            return FakeRevit.code_result(rid, [{"Name": label, "Id": 1, "ElevationMm": 0}])
        if code == "return document.Title;":
            return FakeRevit.code_result(rid, label)
        if code == DOCUMENT_PROBE:
            return FakeRevit.code_result(rid, {"Title": label, "RevitVersion": "2026"})
        return dry_addin()(request)

    async with FakeRevit(handler) as fake:
        client = RevitClient(settings=RevitSettings(port=fake.port))
        await client.connect()
        try:
            yield client
        finally:
            await client.disconnect()


@asynccontextmanager
async def session(app, label):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={"x-device": label}) as http:
        async with streamable_http_client("http://localhost:8000/bridge/mcp", http_client=http) as streams:
            async with ClientSession(*streams) as client:
                init = await client.initialize()
                assert init.instructions == mcp_server.SERVER_INSTRUCTIONS
                yield client


async def call(client, tool, **args):
    reply = await client.call_tool(tool, args)
    assert not reply.is_error, reply
    return json.loads(reply.content[0].text)


async def resource(client, uri):
    return json.loads((await client.read_resource(uri)).contents[0].text)


async def check_workflow_catalog(east, west, east_root):
    store = WorkflowStore(ToolStore(east_root / "capabilities"))
    flow = {"name": "pair", "kind": "workflow", "schema_version": 1, "version": "1.0.0",
            "display_name": "Pair", "summary": "Two steps", "description": "Example.", "inputs": [],
            "steps": [{"id": name, "tool": "east_only", "title": name, "params": {}} for name in ("a", "b")]}
    store.save(flow)
    assert await call(east, "get_workflow", name="pair") == flow
    reply = await east.read_resource("revit://workflows/pair")
    assert yaml.safe_load(reply.contents[0].text) == flow
    assert (await call(east, "list_workflows"))[0]["name"] == "pair"
    assert await call(west, "list_workflows") == []
    assert (await call(west, "get_workflow", name="pair"))["error"] == "unknown_workflow"


def test_mounted_mcp_routes_two_simultaneous_clients_and_their_state(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", "1")

    async def scenario():
        async with AsyncExitStack() as stack:
            devices = {key: await stack.enter_async_context(device(key)) for key in ("east", "west")}

            async def resolve(request):
                key = request.headers["x-device"]
                return key, devices[key]

            app = build_asgi_app(resolve, channel="remote-mcp", data_dir=tmp_path)
            parent = Starlette(routes=[Mount("/bridge", app=app)], lifespan=app.router.lifespan_context)
            await stack.enter_async_context(parent.router.lifespan_context(parent))
            east = await stack.enter_async_context(session(parent, "east"))
            west = await stack.enter_async_context(session(parent, "west"))

            tools = (await east.list_tools()).tools
            assert [t.model_dump() for t in tools] == [t.model_dump() for t in await mcp_server.mcp.list_tools()]
            assert {"list_workflows", "get_workflow"} <= {t.name for t in tools}
            assert "run_workflow" not in {t.name for t in tools}

            async def read_and_dry_run(client, label):
                assert (await call(client, "query", kind="levels"))["items"][0]["name"] == label
                assert (await resource(client, "revit://connection-status"))["document"] == label
                assert (await call(client, "check_code", code="return 1;"))["compiled"]
                result = await call(client, "dry_run_code", code="return 1;")
                assert result["success"] and result["rolled_back"]
                return result["evidence_id"]

            ids = await asyncio.gather(read_and_dry_run(east, "east"), read_and_dry_run(west, "west"))
            for client, label, own, other in ((east, "east", *ids), (west, "west", *reversed(ids))):
                lines = await call(client, "evidence")
                assert [(r["id"], r["scope"], r["host"]) for r in lines] == [(own, label, "remote-mcp")]
                assert await resource(client, "revit://evidence/recent") == lines
                assert (await call(client, "validate", evidence_id=other))["error"] == "unknown_evidence"
                assert (await call(client, "verify_execution", evidence_id=other, verification_id="x"))[
                    "error"] == "unknown_evidence"

            code = "return 1;"
            token = (await call(east, "confirm_spec", spec=code_spec(code)))["token"]
            assert (await call(west, "execute_code", code=code))["error"] == "confirmation_required"
            assert (await call(west, "execute_code", code=code, token=token))["reason"] == "unknown"
            executed = await call(east, "execute_code", code=code, token=token)
            assert executed["success"]
            assert (await call(east, "execute_code", code=code, token=token))["reason"] == "used"
            assert len(await call(west, "evidence")) == 1
            plan = await call(west, "plan_verification", template="created_ids", params={"category": "OST_Walls"})
            args = {"evidence_id": executed["evidence_id"], "verification_id": plan["verification_id"]}
            assert (await call(west, "verify_execution", **args))["error"] == "unknown_evidence"
            assert (await call(east, "verify_execution", **args))["error"] == "unknown_verification"

            await call(east, "solidify_tool", name="east_only", code=code)
            assert "east_only" in {p["name"] for p in await call(east, "list_tools")}
            assert "east_only" not in {p["name"] for p in await call(west, "list_tools")}
            east_root = scope_directory("east", tmp_path)
            await check_workflow_catalog(east, west, east_root)
            own = east_root / "files"
            own.mkdir()
            (own / "sample.cs").write_text("class Example {}", encoding="utf-8")
            # The fake supports dry runs without includes. Reaching this refusal
            # proves the staged relative path passed local source validation.
            result = await call(east, "dry_run_code", code=code, include=["sample.cs"])
            assert result["error"] == "no_dry_run_include"
            denied = await call(west, "dry_run_code", code=code, include=[str(own / "sample.cs")])
            assert denied["error"] == "invalid_include"
            denied = await call(west, "test_addin_command", dll=str(own / "sample.dll"), command_class="Example")
            assert denied["error"] == "invalid_dll"

    asyncio.run(scenario())


def test_two_workers_cannot_both_redeem_a_cached_confirmation(tmp_path):
    first = _DeviceGate(tmp_path / "pending")
    second = _DeviceGate(first.pending_dir)
    spec = TaskSpec.model_validate(code_spec("return 1;"))
    token = first.issue(spec, scope="east").token
    projection = spec.execution_projection()
    second.verify(token, projection, "east")
    first.consume(token, projection, "east")
    with pytest.raises(GateError, match="already redeemed"):
        second.consume(token, projection, "east")

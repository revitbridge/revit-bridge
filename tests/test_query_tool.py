"""The read-only ``query(kind, args)`` tool: every kind, the whitelist, the errors."""
from __future__ import annotations

import asyncio
import json

import revit_bridge.mcp_server as server
from revit_bridge.revit.client import RevitClient
from revit_bridge.revit.pool import RevitClientPool
from revit_bridge.snapshot.query import QUERY_KINDS, RevitQueryExecutor, run_query

from tests.fake_revit import FakeRevit


def handler(request):
    rid, method = request["id"], request["method"]
    if method == "send_code_to_revit":
        code = request["params"]["code"]
        if "typeof(Level)" in code:
            return FakeRevit.code_result(rid, [{"Id": 1, "Name": "L1", "ElevationMm": 0.0}])
        if "typeof(Grid)" in code:
            return FakeRevit.code_result(rid, [{"Id": 5, "Name": "A"}, {"Id": 6, "Name": "1"}])
        if "document.ActiveView" in code:
            return FakeRevit.code_result(rid, {"View": "L1 - Plan", "ViewType": "FloorPlan", "Total": 3,
                                               "Items": [{"Id": 9, "Name": "Basic Wall", "Category": "Walls"}]})
        if "OfCategory(bic)" in code and "Take(" in code:
            return FakeRevit.code_result(rid, {"Total": 12, "Items": [
                {"Id": 7, "Name": "Generic - 200mm", "Category": "Walls", "Type": "Generic - 200mm", "Level": "L1"}]})
        if "GetUnits" in code:
            return FakeRevit.code_result(rid, {"LengthUnit": "autodesk.unit.unit:meters-1.0.1", "DisplayName": "Meters"})
        if "new string[]" in code:
            return FakeRevit.code_result(rid, [{"Category": "OST_Walls", "Count": 12},
                                               {"Category": "OST_Nope", "Count": -1, "Error": "not found"}])
        if "CS0103" in code:
            return FakeRevit.code_result(rid, None, success=False, error="CS0103: boom")
    if method == "get_selected_elements":
        return FakeRevit.ok(rid, [{"Id": 3, "UniqueId": "u", "Name": "Basic Wall", "Category": "Walls"}])
    return FakeRevit.default_handler(request)


def ask(kind, args=None, h=handler):
    async def go():
        async with FakeRevit(h) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            try:
                return await run_query(RevitQueryExecutor(client), kind, args), fake
            finally:
                await client.disconnect()
    return asyncio.run(go())


def test_every_kind_returns_its_shape():
    assert ask("levels")[0] == {"kind": "levels", "items": [{"id": 1, "name": "L1", "elevation_mm": 0.0}]}
    assert ask("grids")[0] == {"kind": "grids", "count": 2, "items": [{"id": 5, "name": "A"}, {"id": 6, "name": "1"}]}

    out, fake = ask("family_types", {"categories": ["OST_Walls", "OST_Doors"]})
    assert out == {"kind": "family_types", "categories": ["OST_Walls", "OST_Doors"], "items": [
        {"id": None, "family": "", "name": "OST_Walls-TypeA", "category": ""},
        {"id": None, "family": "", "name": "OST_Doors-TypeA", "category": ""},
    ]}
    assert fake.requests[-1]["method"] == "get_available_family_types"

    out, fake = ask("elements", {"category": "OST_Walls", "limit": 5})
    assert out == {"kind": "elements", "category": "OST_Walls", "total": 12, "limit": 5, "items": [
        {"id": 7, "name": "Generic - 200mm", "category": "Walls", "type": "Generic - 200mm", "level": "L1"}]}
    assert 'Enum.Parse(typeof(BuiltInCategory), "OST_Walls")' in fake.requests[-1]["params"]["code"]
    assert ".Take(5)" in fake.requests[-1]["params"]["code"]

    assert ask("selection")[0] == {"kind": "selection", "count": 1,
                                   "items": [{"id": 3, "name": "Basic Wall", "category": "Walls"}]}

    out, fake = ask("view_elements", {"limit": 2})
    assert out == {"kind": "view_elements", "view": "L1 - Plan", "view_type": "FloorPlan", "total": 3, "limit": 2,
                   "items": [{"id": 9, "name": "Basic Wall", "category": "Walls"}]}
    assert ".Take(2)" in fake.requests[-1]["params"]["code"]

    assert ask("units")[0] == {"kind": "units", "length": "m", "raw": "autodesk.unit.unit:meters-1.0.1",
                               "display_name": "Meters"}

    out, fake = ask("counts", {"categories": ["OST_Walls", "OST_Nope"]})
    assert out == {"kind": "counts", "items": [{"category": "OST_Walls", "count": 12},
                                               {"category": "OST_Nope", "count": -1, "error": "not found"}]}
    assert 'new string[] { "OST_Walls", "OST_Nope" }' in fake.requests[-1]["params"]["code"]


def test_limits_are_clamped_and_reported():
    out, fake = ask("elements", {"category": "OST_Walls", "limit": 999})
    assert out["limit"] == 200 and ".Take(200)" in fake.requests[-1]["params"]["code"]
    out, fake = ask("elements", {"category": "OST_Walls", "limit": "lots"})
    assert out["limit"] == 100
    out, _ = ask("view_elements", {"limit": 0})
    assert out["limit"] == 1
    out, _ = ask("view_elements")
    assert out["limit"] == 100


def test_unknown_kind_and_bad_args_never_reach_revit():
    out, fake = ask("walls")
    assert out == {"error": "unknown_kind", "kind": "walls", "kinds": sorted(QUERY_KINDS)}
    assert fake.requests == []

    out, fake = ask("levels", {"category": "OST_Walls"})
    assert out["error"] == "invalid_args" and "unexpected args ['category']" in out["message"]
    assert out["allowed"] == [] and fake.requests == []

    out, fake = ask("elements", {"category": "OST_Walls; System.IO.File.Delete"})
    assert out["error"] == "invalid_args" and "invalid category" in out["message"] and fake.requests == []
    out, fake = ask("elements", {})
    assert out["error"] == "invalid_args" and fake.requests == []
    out, fake = ask("counts", {"categories": []})
    assert out["error"] == "invalid_args" and fake.requests == []
    out, fake = ask("counts", {"categories": ["OST_Walls", "Walls"]})
    assert out["error"] == "invalid_args" and fake.requests == []
    out, fake = ask("family_types", {"categories": "OST_Walls"})
    assert out["error"] == "invalid_args" and fake.requests == []
    out, fake = ask("levels", "nope")
    assert out == {"error": "invalid_args", "message": "args must be an object"}


def test_revit_errors_are_reported_not_raised():
    def failing(request):
        if request["method"] == "send_code_to_revit":
            return FakeRevit.code_result(request["id"], None, success=False, error="no document open")
        return FakeRevit.error(request["id"], -32000, "no document open")

    for kind, args in (("levels", None), ("grids", None), ("family_types", {"categories": ["OST_Walls"]}),
                       ("elements", {"category": "OST_Walls"}), ("selection", None),
                       ("view_elements", None), ("units", None), ("counts", {"categories": ["OST_Walls"]})):
        out, _ = ask(kind, args, failing)
        assert out == {"error": "revit_error", "kind": kind, "message": "no document open"}, kind

    # A timeout is a failure too, never an empty model
    def silent(request):
        return []

    for kind in ("levels", "selection"):
        async def go():
            async with FakeRevit(silent) as fake:
                client = RevitClient(host="127.0.0.1", port=fake.port, timeout=0.3)
                try:
                    return await run_query(RevitQueryExecutor(client), kind)
                finally:
                    await client.disconnect()
        out = asyncio.run(go())
        assert out["error"] == "revit_error" and "Timeout" in out["message"], kind


def test_lenient_queries_keep_their_shape_for_the_web_host():
    """The 0.1 methods still answer [] on failure; strict=True raises."""
    from revit_bridge.snapshot.query import RevitQueryError

    def failing(request):
        if request["method"] == "send_code_to_revit":
            return FakeRevit.code_result(request["id"], None, success=False, error="boom")
        return FakeRevit.error(request["id"], -32000, "boom")

    async def go():
        async with FakeRevit(failing) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            ex = RevitQueryExecutor(client)
            try:
                assert await ex.get_levels() == []
                assert await ex.get_family_types(["OST_Walls"]) == []
                assert await ex.get_selected_elements() == []
                for call in (ex.get_levels(strict=True), ex.get_family_types(["OST_Walls"], strict=True),
                             ex.get_selected_elements(strict=True)):
                    try:
                        await call
                    except RevitQueryError as exc:
                        assert str(exc) == "boom"
                    else:
                        raise AssertionError("strict call did not raise")
            finally:
                await client.disconnect()

    asyncio.run(go())


def test_mcp_query_tool(monkeypatch):
    async def scenario():
        async with FakeRevit(handler) as fake:
            monkeypatch.setenv("REVIT_BRIDGE_HOST", "127.0.0.1")
            monkeypatch.setenv("REVIT_BRIDGE_PORT", str(fake.port))
            monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "2")
            monkeypatch.delenv("REVIT_BRIDGE_TOKEN", raising=False)
            try:
                levels = await server.mcp.call_tool("query", {"kind": "levels"})
                counts = await server.mcp.call_tool("query", {"kind": "counts", "args": {"categories": ["OST_Walls"]}})
                unknown = await server.mcp.call_tool("query", {"kind": "walls", "args": {}})
                return [json.loads(r.content[0].text) for r in (levels, counts, unknown)]
            finally:
                await RevitClientPool.disconnect()

    levels, counts, unknown = asyncio.run(scenario())
    assert levels["items"][0]["name"] == "L1"
    assert counts["items"][0] == {"category": "OST_Walls", "count": 12}
    assert unknown["error"] == "unknown_kind"

    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "1")
    down = json.loads(asyncio.run(server.mcp.call_tool("query", {"kind": "levels"})).content[0].text)
    assert down["error"] == "revit_unreachable" and down["kind"] == "levels"

"""Project snapshot against the fake add-in: every field, partial failures, the MCP tool."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

import revit_bridge.mcp_server as server
from revit_bridge.revit.client import RevitClient
from revit_bridge.revit.pool import RevitClientPool
from revit_bridge.snapshot.project import (
    DEFAULT_CATEGORIES,
    GridSummary,
    ProjectSnapshot,
    fingerprint_of,
    snapshot_code,
    take_snapshot,
    validate_categories,
)

from tests.fake_revit import FakeRevit

FULL = {
    "Document": {"Title": "Project1", "RevitVersion": "2026", "IsWorkshared": False},
    "Units": {"LengthUnit": "autodesk.unit.unit:millimeters-1.0.1", "DisplayName": "Millimeters"},
    "ActiveView": {"Name": "L1 - Architectural", "ViewType": "FloorPlan", "Level": "L1"},
    "Levels": [{"Id": 1, "Name": "L1", "ElevationMm": 0.0}, {"Id": 2, "Name": "L2", "ElevationMm": 4000.0}],
    "Grids": {"Count": 3, "Names": ["A", "B", "1"]},
    "Selection": [{"Id": 10, "Category": "Walls", "Name": "Basic Wall"}],
    "SelectionCount": 1,
    "Links": [{"Name": "site.rvt", "Loaded": True}, {"Name": "mep.rvt", "Loaded": False}],
    "Phases": ["Existing", "New Construction"],
    "CategoryNames": {"OST_Walls": "Walls", "OST_Doors": "Doors"},
    "Warnings": [],
}

TYPES = {
    "Walls": [{"FamilyName": "Basic Wall", "TypeName": "Generic - 200mm", "Category": "Walls"},
              {"FamilyName": "Basic Wall", "TypeName": "Generic - 300mm", "Category": "Walls"}],
    "Doors": [{"FamilyName": "M_Single-Flush", "TypeName": "0915 x 2134mm", "Category": "Doors"}],
}


def make_handler(payload=FULL, block_ok=True):
    """Answer the snapshot block with ``payload`` and family types by category label."""
    labels = {"OST_Walls": "Walls", "OST_Doors": "Doors", "OST_Windows": "Windows"}

    def handler(request):
        rid, method = request["id"], request["method"]
        if method == "send_code_to_revit":
            if "CategoryNames" in request["params"]["code"]:
                if not block_ok:
                    return FakeRevit.code_result(rid, None, success=False, error="CS0103: boom")
                return FakeRevit.code_result(rid, payload)
        if method == "get_available_family_types":
            items = []
            for cat in request["params"]["categoryList"]:
                items.extend(TYPES.get(labels.get(cat, ""), []))
            return FakeRevit.ok(rid, items)
        return FakeRevit.default_handler(request)
    return handler


def snapshot_with(handler, categories=None):
    async def go():
        async with FakeRevit(handler) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            try:
                return await take_snapshot(client, categories), fake
            finally:
                await client.disconnect()
    return asyncio.run(go())


def test_every_field_is_filled_from_one_block_and_one_types_call():
    snap, fake = snapshot_with(make_handler(), ["OST_Walls", "OST_Doors"])
    assert isinstance(snap, ProjectSnapshot) and snap.schema_version == 1
    assert snap.taken_at.endswith("Z") and "T" in snap.taken_at
    assert isinstance(snap.duration_ms, int) and snap.duration_ms >= 0
    assert snap.document == {"title": "Project1", "revit_version": "2026", "is_workshared": False}
    assert snap.units == {"length": "mm", "raw": "autodesk.unit.unit:millimeters-1.0.1"}
    assert snap.active_view.model_dump() == {"name": "L1 - Architectural", "view_type": "FloorPlan", "level": "L1"}
    assert [(lv.id, lv.name, lv.elevation_mm) for lv in snap.levels] == [(1, "L1", 0.0), (2, "L2", 4000.0)]
    assert snap.grids == GridSummary(count=3, names=["A", "B", "1"])
    assert [t.model_dump() for t in snap.family_types] == [
        {"category": "OST_Walls", "count": 2, "names": ["Generic - 200mm", "Generic - 300mm"]},
        {"category": "OST_Doors", "count": 1, "names": ["0915 x 2134mm"]},
    ]
    assert [s.model_dump() for s in snap.selection] == [{"id": 10, "category": "Walls", "name": "Basic Wall"}]
    assert snap.selection_count == 1
    assert [(l.name, l.loaded) for l in snap.links] == [("site.rvt", True), ("mep.rvt", False)]
    assert snap.phases == ["Existing", "New Construction"]
    assert snap.warnings == []
    assert len(snap.fingerprint) == 16 and int(snap.fingerprint, 16) >= 0
    assert snap.fingerprint == fingerprint_of("Project1", "2026", snap.levels)

    # one C# block naming the categories, then one family-types call for both
    methods = [r["method"] for r in fake.requests]
    assert methods == ["send_code_to_revit", "get_available_family_types"]
    assert '"OST_Walls", "OST_Doors"' in fake.requests[0]["params"]["code"]
    assert fake.requests[1]["params"]["categoryList"] == ["OST_Walls", "OST_Doors"]


def test_fingerprint_tracks_title_version_and_levels_only():
    snap, _ = snapshot_with(make_handler(), ["OST_Walls"])
    moved = copy.deepcopy(FULL)
    moved["Levels"][1]["ElevationMm"] = 4200.0
    other, _ = snapshot_with(make_handler(moved), ["OST_Walls"])
    assert other.fingerprint != snap.fingerprint

    reordered = copy.deepcopy(FULL)
    reordered["Levels"].reverse()
    reordered["Selection"] = []
    reordered["Phases"] = []
    same, _ = snapshot_with(make_handler(reordered), ["OST_Walls"])
    assert same.fingerprint == snap.fingerprint


def test_partial_failures_become_warnings_not_exceptions():
    broken = copy.deepcopy(FULL)
    broken["Warnings"] = ["links: no permission"]      # reported by the C# side
    broken["Links"] = []
    broken["ActiveView"] = None                         # no open view
    broken["Grids"] = None                              # part skipped
    broken["Levels"] = [{"Id": "not-a-number", "Name": "L1"}]   # malformed reply
    broken["SelectionCount"] = "many"
    snap, _ = snapshot_with(make_handler(broken), ["OST_Walls"])
    assert snap.links == [] and snap.active_view is None
    assert snap.grids == GridSummary(count=0, names=[])
    assert snap.levels == []
    assert snap.selection_count == 1                    # falls back to len(selection)
    assert snap.document["title"] == "Project1"         # the rest is intact
    assert snap.family_types[0].count == 2
    assert snap.warnings[0] == "links: no permission"
    assert any(w.startswith("levels: ValueError") for w in snap.warnings)
    assert any(w.startswith("selection_count: ValueError") for w in snap.warnings)
    assert snap.fingerprint == fingerprint_of("Project1", "2026", [])


def test_block_failure_still_returns_a_snapshot_with_types_per_category():
    snap, fake = snapshot_with(make_handler(block_ok=False), ["OST_Walls", "OST_Doors"])
    assert snap.document == {"title": "", "revit_version": "", "is_workshared": False}
    assert snap.units == {"length": "mm", "raw": ""}
    assert snap.levels == [] and snap.phases == [] and snap.selection_count == 0
    assert snap.warnings == ["snapshot code failed: CS0103: boom"]
    # No category labels from the block: types are fetched one category at a time
    assert [t.model_dump() for t in snap.family_types] == [
        {"category": "OST_Walls", "count": 2, "names": ["Generic - 200mm", "Generic - 300mm"]},
        {"category": "OST_Doors", "count": 1, "names": ["0915 x 2134mm"]},
    ]
    assert [r["params"].get("categoryList") for r in fake.requests[1:]] == [["OST_Walls"], ["OST_Doors"]]


def test_family_types_failure_is_a_warning_not_zero_types():
    """Review B-2: an add-in failure must not read as "no types of any category"."""
    def types_fail(request):
        if request["method"] == "get_available_family_types":
            return FakeRevit.error(request["id"], -32000, "types: no document")
        return make_handler()(request)

    snap, _ = snapshot_with(types_fail, ["OST_Walls", "OST_Doors"])
    assert snap.family_types == []
    assert snap.warnings == ["family_types: types: no document"]
    assert snap.document["title"] == "Project1"          # the block itself is fine

    # Per-category fallback (block failed): the failing category is left out, the other kept
    calls = {"n": 0}

    def second_category_fails(request):
        if request["method"] == "get_available_family_types":
            calls["n"] += 1
            if request["params"]["categoryList"] == ["OST_Doors"]:
                return FakeRevit.error(request["id"], -32000, "doors: boom")
        return make_handler(block_ok=False)(request)

    snap, _ = snapshot_with(second_category_fails, ["OST_Walls", "OST_Doors"])
    assert [t.model_dump() for t in snap.family_types] == [
        {"category": "OST_Walls", "count": 2, "names": ["Generic - 200mm", "Generic - 300mm"]},
    ]
    assert snap.warnings == ["snapshot code failed: CS0103: boom", "family_types OST_Doors: doors: boom"]
    assert calls["n"] == 2

    # A timeout on the types call is a failure too
    def types_hang(request):
        if request["method"] == "get_available_family_types":
            return []
        return make_handler()(request)

    async def go():
        async with FakeRevit(types_hang) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=0.3)
            try:
                return await take_snapshot(client, ["OST_Walls"])
            finally:
                await client.disconnect()

    snap = asyncio.run(go())
    assert snap.family_types == [] and snap.warnings[0].startswith("family_types: Timeout")


def test_lists_are_capped_on_this_side_too():
    big = copy.deepcopy(FULL)
    big["Grids"] = {"Count": 70, "Names": [f"G{i}" for i in range(70)]}
    big["Selection"] = [{"Id": i, "Category": "Walls", "Name": f"W{i}"} for i in range(30)]
    big["SelectionCount"] = 30
    snap, _ = snapshot_with(make_handler(big), ["OST_Walls"])
    assert snap.grids.count == 70 and len(snap.grids.names) == 50
    assert len(snap.selection) == 20 and snap.selection_count == 30


def test_categories_are_validated_and_defaulted():
    assert validate_categories(None) == DEFAULT_CATEGORIES
    assert validate_categories(["OST_Walls", " OST_Walls ", "OST_Rooms"]) == ["OST_Walls", "OST_Rooms"]
    with pytest.raises(ValueError, match="invalid category"):
        validate_categories(["OST_Walls; System.IO.File.Delete"])
    with pytest.raises(ValueError):
        validate_categories("OST_Walls")
    code = snapshot_code(["OST_Walls"])
    assert 'new string[] { "OST_Walls" }' in code and "{categories}" not in code

    snap, fake = snapshot_with(make_handler())
    assert [t.category for t in snap.family_types] == DEFAULT_CATEGORIES
    assert all(c in fake.requests[0]["params"]["code"] for c in DEFAULT_CATEGORIES)


def test_transport_failure_propagates():
    async def go():
        client = RevitClient(host="127.0.0.1", port=1, timeout=1, connect_timeout=0.5)
        with pytest.raises(Exception):
            await take_snapshot(client)
    asyncio.run(go())


def test_mcp_get_project_snapshot_tool(monkeypatch):
    async def scenario():
        async with FakeRevit(make_handler()) as fake:
            monkeypatch.setenv("REVIT_BRIDGE_HOST", "127.0.0.1")
            monkeypatch.setenv("REVIT_BRIDGE_PORT", str(fake.port))
            monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "2")
            monkeypatch.delenv("REVIT_BRIDGE_TOKEN", raising=False)
            try:
                ok = await server.mcp.call_tool("get_project_snapshot", {"categories": ["OST_Walls"]})
                bad = await server.mcp.call_tool("get_project_snapshot", {"categories": ["Walls"]})
                return json.loads(ok.content[0].text), json.loads(bad.content[0].text)
            finally:
                await RevitClientPool.disconnect()

    ok, bad = asyncio.run(scenario())
    assert ok["schema_version"] == 1 and ok["document"]["title"] == "Project1"
    assert ok["family_types"] == [{"category": "OST_Walls", "count": 2,
                                   "names": ["Generic - 200mm", "Generic - 300mm"]}]
    assert bad == {"error": "invalid_category", "message": "invalid category 'Walls': expected an OST_* name"}

    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "1")
    down = json.loads(asyncio.run(server.mcp.call_tool("get_project_snapshot", {})).content[0].text)
    assert down["error"] == "revit_unreachable" and down["message"]

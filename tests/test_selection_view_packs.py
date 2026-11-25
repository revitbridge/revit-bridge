"""Classification and API boundaries of the built-in selection and view packs."""
import asyncio
import json
import re

import pytest
import yaml

from revit_bridge import mcp_server
from revit_bridge.capabilities.schema import validate_pack
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.evidence.ledger import Ledger
from revit_bridge.execution import run_pack
from revit_bridge.revit.sandbox import review
from revit_bridge.spec.gate import Gate
from revit_bridge.spec.models import Action, ParamBinding, Source, TaskSpec
from revit_bridge.spec.rules import validate_spec
from tests.csharp_simulation import simulate_packs


UI_PACKS = {"select_elements", "select_in_box", "select_by_filter", "clear_selection",
            "zoom_to_elements", "set_view_orientation"}
YEARS = ["2021", "2023", "2024", "2025", "2026", "2027"]


def test_ui_only_in_catalogue_and_effective_pack(tmp_path, monkeypatch):
    store = ToolStore(user_dir=tmp_path)
    monkeypatch.setattr(mcp_server, "_tool_store", store)

    def listed():
        return {t["name"] for t in json.loads(mcp_server.list_tools()) if t["ui_only"]}

    assert listed() == UI_PACKS
    assert {t.name for t in store.list_tools() if t.ui_only} == UI_PACKS
    assert not store.load("query_levels").ui_only
    assert store.update("clear_selection", {"description": "Custom label"}).ui_only
    path = store.path_of("clear_selection")
    assert path.parent == tmp_path and yaml.safe_load(path.read_bytes())["ui_only"] is True
    path.write_text("name: clear_selection\ncode_template: return 1;", encoding="utf-8")
    assert listed() == UI_PACKS - {"clear_selection"}


@pytest.mark.parametrize("override", [True, False])
def test_ui_only_pack_uses_existing_confirmation_gate(tmp_path, override):
    store = ToolStore(user_dir=tmp_path)
    if override:
        store.update("clear_selection", {"code_template": "return 1;"})
    assert store.load("clear_selection").ui_only
    result = asyncio.run(run_pack(store=store, ledger=Ledger(tmp_path / "evidence"), gate=Gate(tmp_path / "gate"),
                                  client=None, name="clear_selection", params={}, token="", host="web"))
    assert result.error == "confirmation_required" and result.evidence_id is None


@pytest.mark.parametrize("bad", [1, "true", None, []])
def test_ui_only_requires_literal_true(tmp_path, bad):
    store = ToolStore(user_dir=tmp_path)
    data = dict(schema_version=1, version="1.0.0", name="example", code_template="return 1;", ui_only=bad)
    assert "ui_only: must be a boolean" in validate_pack(data)
    (tmp_path / "example.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    assert store.load("example").ui_only is False


def test_conflicting_trust_flags_rejected(tmp_path):
    data = dict(schema_version=1, version="1.0.0", name="example", code_template="return 1;",
                ui_only=True, read_only=True)
    assert any("ui_only" in e and "read_only" in e for e in validate_pack(data))
    (tmp_path / "example.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    assert ToolStore(user_dir=tmp_path).load("example") is None


def test_type_filter_accepts_the_existing_type_query_as_evidence(tmp_path):
    pack = ToolStore(user_dir=tmp_path).load("select_by_filter")
    binding = ParamBinding(name="type_name", value="Typical", source=Source.tool, evidence="list_types")
    spec = TaskSpec(task="select this type", action=Action(kind="run_tool", tool=pack.name), parameters=[binding])
    assert validate_spec(spec, pack) == []


@pytest.mark.parametrize("name", sorted(UI_PACKS | {"set_section_box"}))
def test_reviewed_templates_and_years(tmp_path, name):
    store = ToolStore(user_dir=tmp_path)
    pack = store.load(name)
    assert pack and pack.ui_only == (name in UI_PACKS) and not pack.read_only
    assert pack.revit_versions == YEARS
    assert pack.description.startswith("View only. ") == pack.ui_only
    assert len(pack.fixtures) >= 2
    assert not validate_pack(yaml.safe_load(store.path_of(name).read_bytes()))
    for param in pack.parameters:
        token = "{" + param["name"] + "}"
        assert pack.code_template.count(token) == pack.code_template.count('"' + token + '"') > 0
    for fixture in pack.fixtures:
        code, errors = store.render(name, fixture["params"])
        assert not errors and review(code) == (True, [])
        source = re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"', "", code, flags=re.S)
        assert not re.search(r"\b(?:Transaction|SubTransaction|TransactionGroup|Delete)\b", source)
        if pack.ui_only:
            assert not re.search(r"\.\s*\w+\s*(?:[+*/-]?=(?!=|>)|\+\+|--)", source)
            assert not re.search(r"\.(?:Set|set_\w+|SaveOrientation\w*|SetSectionBox|Create|ChangeTypeId)\s*\(", source)


def test_rendered_selection_geometry_and_atomic_refusals(tmp_path):
    corners = {"corner1_mm": "609.6,609.6,609.6", "corner2_mm": "0,0,0"}
    filters = {"category": "OST_Walls", "level_name": "L1", "type_name": "Basic: Typical"}
    output = simulate_packs(tmp_path, "selection_simulation", {
        "select_elements": {
            "Replace": {"element_ids": "101,102,101"}, "Add": {"element_ids": "102,103", "mode": "add"},
            "Remove": {"element_ids": "101", "mode": "remove"}, "Stale": {"element_ids": "101,999"},
            "AddOne": {"element_ids": "101", "mode": "add"}, "AddOther": {"element_ids": "102", "mode": "add"},
        },
        "clear_selection": {"Clear": {}},
        "select_in_box": {"Inside": corners, "Intersect": {**corners, "mode": "intersect", "category": "OST_Walls"},
                          "MixedBox": {**corners, "element_ids": "101"}},
        "select_by_filter": {"Visible": filters, "Model": {**filters, "scope": "model"},
                             "MissingType": {**filters, "type_name": "Missing"}},
        "set_section_box": {"Section": {"element_ids": "101,102", "padding_mm": 304.8}, "Reset": {"reset": True},
                            "Flat": {**corners, "corner1_mm": "10,0,10"}},
        "set_view_orientation": {"Top": {"orientation": "top"}, "Axon": {"orientation": "iso_ne"},
                                 "Vertical": {"eye_mm": "0,0,1524", "target_mm": "0,0,0"},
                                 "Coincident": {"eye_mm": "0,0,0", "target_mm": "0,0,0"}},
        "zoom_to_elements": {"ZoomSelection": {}, "ZoomPair": {"element_ids": "101,102"}},
    })
    assert "PASS selection, filters, section box, camera, zoom and limits" in output

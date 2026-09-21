"""Capability pack v1: validate_pack, the 8 built-ins, preconditions."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from revit_bridge.capabilities.schema import (
    VALIDATOR_KINDS,
    evaluate_preconditions,
    precondition_categories,
    validate_pack,
)
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.paths import builtin_capabilities_dir
from revit_bridge.snapshot.project import GridSummary, LevelInfo, ProjectSnapshot, TypeSummary

GOOD = {
    "schema_version": 1, "name": "probe", "display_name": "Probe", "description": "d",
    "version": "1.0.0", "revit_versions": ["2026"],
    "code_template": 'return "{level_name}-{x}-{n}";',
    "parameters": [
        {"name": "level_name", "type": "string", "source": "tool:levels", "choices_from": "levels", "required": True},
        {"name": "x", "type": "double", "unit": "mm", "source": "designer", "required": True},
        {"name": "n", "type": "integer", "source": "default", "required": False, "default": 1},
    ],
    "preconditions": [{"kind": "levels_min", "value": 1}, {"kind": "category_present", "category": "OST_Walls"},
                      {"text": "free text"}],
    "not_for": ["x"], "applies_when": ["y"],
    "validator": {"kind": "count_delta", "category": "OST_Walls", "expected": "{n}"},
    "fixtures": [{"name": "basic", "params": {"level_name": "L1", "x": 0}, "expect": {"Status": "Created"}}],
    "approved_by": None, "approved_at": None, "created_at": "2026-09-20T00:00:00", "source_query": "q",
}


def variant(**changes) -> dict:
    data = copy.deepcopy(GOOD)
    data.update(changes)
    return data


def param_variant(index: int, **changes) -> dict:
    data = copy.deepcopy(GOOD)
    for key, value in changes.items():
        if value is None:
            data["parameters"][index].pop(key, None)
        else:
            data["parameters"][index][key] = value
    return data


# -- the built-ins ------------------------------------------------------------------------

def test_every_builtin_pack_is_valid_v1_with_a_validator():
    files = sorted(builtin_capabilities_dir().glob("*.yaml"))
    assert len(files) == 8
    for path in files:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert validate_pack(data) == [], path.name
        assert data["schema_version"] == 1 and data["version"] == "1.0.0", path.name
        assert data["validator"]["kind"] in VALIDATOR_KINDS, path.name
        for param in data["parameters"]:
            assert "source" in param and isinstance(param["required"], bool), (path.name, param["name"])
        for absent in ("tags", "execution_count", "last_used", "failure_count"):
            assert absent not in data, (path.name, absent)
        assert "first available" not in data["description"].lower(), path.name
        assert "first level" not in data["description"].lower(), path.name


def test_builtin_coordinates_are_the_designers_not_defaults():
    store = ToolStore()
    for name, coords in (("create_wall", ("start_x", "start_y", "end_x", "end_y")),
                         ("create_structural_column", ("x", "y")),
                         ("create_beam", ("start_x", "start_y", "end_x", "end_y", "elevation")),
                         ("create_floor", ("x", "y", "width", "length"))):
        params = {p["name"]: p for p in store.load(name).parameters}
        for coord in coords:
            assert params[coord]["source"] == "designer" and params[coord]["required"] is True, (name, coord)
            assert params[coord]["unit"] == "mm" and "default" not in params[coord], (name, coord)
    wall = {p["name"]: p for p in store.load("create_wall").parameters}
    assert wall["height"]["source"] == "default" and wall["height"]["default"] == 3000


# -- validate_pack -----------------------------------------------------------------------

def test_validate_pack_accepts_the_reference_pack():
    assert validate_pack(GOOD) == []


def test_validate_pack_rejects_v0_layout_and_unknown_fields():
    v0 = variant()
    del v0["schema_version"]
    assert any(e.startswith("schema_version:") for e in validate_pack(v0))
    assert validate_pack("nope") == ["pack must be a mapping"]
    assert "unknown field 'execution_count'" in validate_pack(variant(execution_count=3))
    assert validate_pack(variant(tags=["kept"])) == []          # 0.1 leftover is tolerated


def test_validate_pack_requires_explicit_source_and_required():
    """The review's condition for PR D: v1 files get no inference."""
    errors = validate_pack(param_variant(1, source=None))
    assert errors == ["parameters[1] (x): source is required (designer | tool:<query> | answer | default)"]
    errors = validate_pack(param_variant(1, required=None))
    assert errors == ["parameters[1] (x): required must be true or false"]
    assert validate_pack(param_variant(1, required="yes")) == ["parameters[1] (x): required must be true or false"]


def test_validate_pack_checks_sources_choices_defaults_and_units():
    assert "parameters[1] (x): unknown source 'guess'" in validate_pack(param_variant(1, source="guess"))
    assert any("tool:<query> needs a query kind" in e for e in validate_pack(param_variant(0, source="tool:")))
    assert any("choices_from must be" in e for e in validate_pack(param_variant(0, choices_from="walls")))
    # a tool source whose query is not a choices source needs choices_from
    assert any("names no usable query" in e for e in validate_pack(param_variant(0, source="tool:grids", choices_from=None)))
    assert validate_pack(param_variant(0, source="tool:family_types:OST_Walls", choices_from=None)) == []
    assert any("must have source tool" in e for e in validate_pack(param_variant(0, source="designer")))
    assert any("needs a default value" in e for e in validate_pack(param_variant(2, default=None)))
    assert any("is not required" in e for e in validate_pack(param_variant(2, required=True)))
    assert any("unit must be" in e for e in validate_pack(param_variant(1, unit="inch")))
    assert any("unit on a non-numeric" in e for e in validate_pack(param_variant(0, unit="mm")))
    assert any("unknown type" in e for e in validate_pack(param_variant(1, type="decimal")))
    assert any("declared twice" in e for e in validate_pack(variant(parameters=GOOD["parameters"] + [GOOD["parameters"][0]])))
    assert any("name must be an identifier" in e for e in validate_pack(param_variant(0, name="level name")))
    assert any("unknown field 'enrich'" in e for e in validate_pack(param_variant(0, enrich="x")))


def test_validate_pack_checks_template_version_and_preconditions():
    assert validate_pack(variant(code_template="return {missing};")) == [
        "code_template: placeholder {missing} is not a declared parameter"]
    assert any("non-empty string" in e for e in validate_pack(variant(code_template="  ")))
    assert any("semver" in e for e in validate_pack(variant(version="1.0")))
    assert any("revit_versions" in e for e in validate_pack(variant(revit_versions="2026")))
    assert any("unknown kind 'grids_min'" in e for e in validate_pack(variant(preconditions=[{"kind": "grids_min"}])))
    assert any("levels_min needs an integer" in e for e in validate_pack(variant(preconditions=[{"kind": "levels_min", "value": "1"}])))
    assert any("OST_* category" in e for e in validate_pack(variant(preconditions=[{"kind": "category_present", "category": "Walls"}])))
    assert any("needs a kind or a text" in e for e in validate_pack(variant(preconditions=[{"note": "x"}])))
    assert any("must be a mapping" in e for e in validate_pack(variant(preconditions=["a string"])))


def test_validate_pack_checks_validator_configuration():
    assert any("unknown kind 'exists'" in e for e in validate_pack(variant(validator={"kind": "exists"})))
    assert any("category must be an OST_* name or {param}" in e
               for e in validate_pack(variant(validator={"kind": "created_ids", "category": "Walls"})))
    assert validate_pack(variant(validator={"kind": "created_ids", "category": "{level_name}"})) == []
    assert any("expected must be an int or {param}" in e
               for e in validate_pack(variant(validator={"kind": "count_delta", "category": "OST_Walls", "expected": "{nope}"})))
    assert any("expected must be an int" in e
               for e in validate_pack(variant(validator={"kind": "count_delta", "category": "OST_Walls", "expected": True})))
    assert any("non-empty checks list" in e
               for e in validate_pack(variant(validator={"kind": "param_equals", "category": "OST_Walls"})))
    assert any("spec_param 'h' is not a parameter" in e for e in validate_pack(variant(validator={
        "kind": "param_equals", "category": "OST_Walls", "checks": [{"param_name": "WALL_USER_HEIGHT_PARAM", "spec_param": "h"}]})))
    assert validate_pack(variant(validator={
        "kind": "param_equals", "category": "OST_Walls", "checks": [{"param_name": "WALL_USER_HEIGHT_PARAM", "spec_param": "x"}]})) == []
    assert validate_pack(variant(validator=None)) == []
    assert any("fixtures[0]: needs a name" in e for e in validate_pack(variant(fixtures=[{"params": {}}])))


# -- store integration ----------------------------------------------------------------------

def test_solidify_and_update_refuse_invalid_packs(tmp_path):
    import pytest

    store = ToolStore(user_dir=tmp_path / "user")
    with pytest.raises(ValueError, match="placeholder {y}"):
        store.solidify("bad", "return {y};", parameters=[])
    store.solidify("ok", "return {x};", parameters=[{"name": "x", "type": "double", "source": "designer"}])
    with pytest.raises(ValueError, match="unknown kind"):
        store.update("ok", {"validator": {"kind": "nope"}})
    assert store.load("ok").validator is None                     # the refused update wrote nothing
    tool = store.update("ok", {"validator": {"kind": "count_delta", "category": "OST_Walls", "expected": 1}})
    assert tool.validator["kind"] == "count_delta" and tool.version == "1.0.0"


# -- preconditions ---------------------------------------------------------------------------

def snapshot(levels=2, wall_types=3, categories=("OST_Walls",)) -> ProjectSnapshot:
    return ProjectSnapshot(
        taken_at="2026-09-20T00:00:00Z", duration_ms=1,
        document={"title": "P", "revit_version": "2026", "is_workshared": False},
        units={"length": "mm", "raw": ""}, active_view=None,
        levels=[LevelInfo(id=i, name=f"L{i}", elevation_mm=float(i)) for i in range(1, levels + 1)],
        grids=GridSummary(count=0, names=[]),
        family_types=[TypeSummary(category=c, count=wall_types, names=[f"T{i}" for i in range(wall_types)])
                      for c in categories],
        selection=[], selection_count=0, links=[], phases=[], warnings=[], fingerprint="f",
    )


def test_evaluate_preconditions_reports_only_what_the_snapshot_can_answer():
    store = ToolStore()
    wall = store.load("create_wall")
    assert precondition_categories(wall) == ["OST_Walls"]
    column = store.load("create_structural_column")
    assert precondition_categories(column) == ["OST_StructuralColumns"]

    assert evaluate_preconditions(wall, snapshot()) == []
    assert evaluate_preconditions(wall, snapshot(levels=0)) == ["levels_min 1: the model has 0 level(s)"]
    assert evaluate_preconditions(wall, snapshot(wall_types=0)) == [
        "category_present OST_Walls: no family types of that category are loaded"]
    assert evaluate_preconditions(wall, snapshot(levels=0, wall_types=0)) == [
        "levels_min 1: the model has 0 level(s)",
        "category_present OST_Walls: no family types of that category are loaded"]
    # the snapshot did not cover the category: not a failure
    assert evaluate_preconditions(wall, snapshot(categories=("OST_Doors",))) == []
    assert evaluate_preconditions(wall, None) == []
    # text-only preconditions never fail
    assert evaluate_preconditions(store.load("delete_elements_by_category"), snapshot(levels=0)) == []

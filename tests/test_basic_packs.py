"""Contracts for the fourteen basic modeling packs; no live Revit access."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from revit_bridge.capabilities.schema import validate_pack
from revit_bridge.capabilities.store import ToolStore, escape_param_value
from revit_bridge.revit.sandbox import review


ROOT = Path(__file__).resolve().parents[1] / "capabilities"
NAMES = (
    "create_level", "create_grid", "create_door", "create_window", "create_room",
    "create_roof_by_footprint", "create_ceiling", "set_parameter", "move_elements",
    "change_type", "delete_selected", "count_by_level", "list_types", "elements_in_link",
)
PLACEHOLDER = re.compile(r"\{[A-Za-z_]\w*\}")
# Every string parameter is inserted into an ordinary C# string literal. These
# templates use the JSON-compatible subset of C# escapes for user text.
STRING_LITERAL = re.compile(r'"(?:[^"\\\r\n]|\\["\\rn])*"')
HOSTILE_TEXT = 'A "quoted" name\\path\r\n"; return null; //'


def data_for(name):
    return yaml.safe_load((ROOT / f"{name}.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path):
    return ToolStore(user_dir=tmp_path / "user", builtin_dir=ROOT)


@pytest.mark.parametrize("name", NAMES)
def test_pack_loads_validates_and_renders_a_fixture(store, name):
    data = data_for(name)
    assert validate_pack(data) == []
    loaded = store.load(name)
    assert loaded is not None and loaded.name == name
    code, errors = store.render(name, data["fixtures"][0]["params"])
    assert errors == [] and code
    assert PLACEHOLDER.search(code) is None
    assert review(code) == (True, [])
    assert not re.search(r"new\s+(?:Sub)?Transaction\s*\(", code)


@pytest.mark.parametrize("name", NAMES)
def test_string_values_remain_complete_string_literals(store, name):
    data = data_for(name)
    for parameter in data["parameters"]:
        if parameter["type"] != "string":
            continue
        values = {**data["fixtures"][0]["params"], parameter["name"]: HOSTILE_TEXT}
        code, errors = store.render(name, values)
        assert errors == [] and code
        escaped = '"' + escape_param_value(HOSTILE_TEXT) + '"'
        literals = STRING_LITERAL.findall(code)
        assert escaped in literals, (name, parameter["name"])
        assert json.loads(escaped) == HOSTILE_TEXT
        assert PLACEHOLDER.search(code) is None


@pytest.mark.parametrize("name", [n for n in NAMES if n.startswith("create_")])
def test_creation_validators_measure_new_instances(name):
    validator = data_for(name)["validator"]
    assert validator["kind"] == "count_delta"
    assert validator["expected"] == 1
    assert validator["category"].startswith("OST_")


@pytest.mark.parametrize("name", ("count_by_level", "list_types", "elements_in_link"))
def test_queries_are_bounded_and_report_truncation(store, name):
    data = data_for(name)
    limit = next(p for p in data["parameters"] if p["name"] == "limit")
    assert limit["default"] == 100
    code, errors = store.render(name, data["fixtures"][0]["params"])
    assert errors == []
    assert "Math.Max(1, Math.Min(200," in code
    assert "Truncated =" in code
    assert data["validator"]["expected"] == 0
    assert not re.search(r"\b(?:Set|Delete|Create|ChangeTypeId|MoveElement)\s*\(", code)


@pytest.mark.parametrize("name", ("set_parameter", "move_elements", "change_type", "delete_selected"))
def test_selection_commands_reject_empty_and_excessive_selections(name):
    code = data_for(name)["code_template"]
    assert 'Selection.GetElementIds()' in code
    assert 'selected.Count == 0' in code
    assert 'selected.Count > 100' in code
    assert 'Status = "Error"' in code
    assert 'ElementIds =' in code and 'Count =' in code


def test_unavailable_verification_is_not_misrepresented():
    # The existing validators cannot prove a translation, an arbitrary parameter
    # name's new value, or deletion across mixed categories. Do not silently use
    # count_delta=0 or created_ids to claim those operations were verified.
    for name in ("set_parameter", "move_elements", "delete_selected"):
        assert data_for(name)["validator"] is None
    assert data_for("change_type")["validator"] == {
        "kind": "param_equals", "category": "{category}",
        "checks": [{"param_name": "ELEM_TYPE_PARAM", "spec_param": "type_id"}],
    }


def test_creation_model_parameters_declare_existing_choices():
    expected = {
        "create_door": {"wall_id": "elements:OST_Walls", "type_name": "family_types:OST_Doors"},
        "create_window": {"wall_id": "elements:OST_Walls", "type_name": "family_types:OST_Windows"},
        "create_room": {"level_name": "levels"},
        "create_roof_by_footprint": {"level_name": "levels", "type_name": "family_types:OST_Roofs"},
        "create_ceiling": {"level_name": "levels", "type_name": "family_types:OST_Ceilings"},
        "elements_in_link": {"link_id": "elements:OST_RvtLinks"},
    }
    for name, sources in expected.items():
        params = {p["name"]: p for p in data_for(name)["parameters"]}
        for parameter, source in sources.items():
            assert params[parameter]["choices_from"] == source
            assert params[parameter]["source"].startswith("tool:")


def test_numeric_lengths_are_millimeters():
    for name in NAMES:
        for parameter in data_for(name)["parameters"]:
            if parameter["type"] == "double":
                assert parameter["unit"] == "mm", (name, parameter["name"])


def test_preexisting_packs_still_load_and_render(store):
    existing = {
        "create_beam", "create_floor", "create_structural_column", "create_wall",
        "delete_elements_by_category", "modify_wall_height", "query_levels", "query_model_stats",
    }
    for name in existing:
        data = data_for(name)
        assert validate_pack(data) == []
        assert store.load(name).code_template == data["code_template"]
        for fixture in data["fixtures"]:
            code, errors = store.render(name, fixture["params"])
            assert errors == [] and code

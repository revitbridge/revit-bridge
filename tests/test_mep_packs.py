"""Pack contract and offline regressions for point placement and rigid MEP curves."""
import re

import pytest
import yaml

from revit_bridge.capabilities.schema import validate_pack
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.revit.sandbox import review
from tests.csharp_simulation import simulate_packs


PACKS = ("place_family_instance", "create_pipe", "create_pipes_by_points", "connect_pipes",
         "create_duct", "list_mep_types")
YEARS = ["2021", "2023", "2024", "2025", "2026", "2027"]


@pytest.mark.parametrize("name", PACKS)
def test_mep_pack_contract_and_fixtures(tmp_path, name):
    store = ToolStore(user_dir=tmp_path)
    pack = store.load(name)
    assert pack and len(pack.fixtures) >= 2
    assert validate_pack(yaml.safe_load(store.path_of(name).read_text(encoding="utf-8"))) == []
    assert pack.revit_versions == YEARS
    assert all(any(year in p.get("text", "") for p in pack.preconditions) for year in YEARS)
    assert pack.read_only == (name == "list_mep_types")
    assert not pack.ui_only
    for p in pack.parameters:
        assert '"{' + p["name"] + '}"' in pack.code_template
    for fixture in pack.fixtures:
        code, errors = store.render(name, fixture["params"])
        assert code and not errors and review(code) == (True, [])
        assert not re.search(r"\{[a-z_]\w*\}|\b(?:SubTransaction|Transaction|TransactionGroup)\s*\(", code)
        assert fixture["expect"]


def test_rendered_mep_and_placement_scenarios(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")

    def fixture(name, index=0, **overrides):
        return {**store.load(name).fixtures[index]["params"], **overrides}

    scenarios = {
        "create_pipe": {
            "Pipe": fixture("create_pipe"),
            "UnknownType": fixture("create_pipe", pipe_type_name="Unknown"),
            "UnknownSystem": fixture("create_pipe", system_type_name="Unknown"),
            "ShortPipe": fixture("create_pipe", end_mm="0,0,251"),
            "BadSize": fixture("create_pipe", diameter_mm=0),
        },
        "create_pipes_by_points": {
            "Route": fixture("create_pipes_by_points"),
            "Gap": fixture("create_pipes_by_points", 1),
            "BadVertex": fixture("create_pipes_by_points", points_mm="0,0,0;0,1000,0;oops;0,0,300;1000,0,300"),
            "NoSegments": fixture("create_pipes_by_points", points_mm="0,0,0;0,0,1;0,0,2"),
        },
        "create_duct": {
            "Round": fixture("create_duct"),
            "Rectangular": fixture("create_duct", 1),
            "MixedDimensions": fixture("create_duct", width_mm=150),
            "UnknownDuct": fixture("create_duct", duct_type_name="Unknown"),
            "UnknownDuctSystem": fixture("create_duct", system_type_name="Unknown"),
        },
        "connect_pipes": {"Join": {"first_pipe_id": "101", "second_pipe_id": "102"}},
        "place_family_instance": {"Place": fixture("place_family_instance", 1)},
        "list_mep_types": {"Inventory": {"limit": 2}},
    }
    assert "MEP scenarios passed" in simulate_packs(tmp_path, "mep_simulation", scenarios)

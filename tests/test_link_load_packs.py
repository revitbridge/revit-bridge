"""Exercise the shipped link/load method bodies using synthetic Windows paths."""
import pytest

from revit_bridge.capabilities.store import ToolStore
from revit_bridge.revit.sandbox import review
from tests.csharp_simulation import simulate_packs


TOOLS = {
    "link_revit_model": {"path", "positioning"},
    "link_cad": {"path", "units", "positioning", "current_view_only", "level_name"},
    "load_family": {"path"},
}


@pytest.mark.parametrize("name", TOOLS)
def test_link_load_contract(tmp_path, name):
    store = ToolStore(user_dir=tmp_path)
    pack = store.load(name)
    assert pack and not pack.read_only and not pack.ui_only
    assert pack.revit_versions == ["2021", "2023", "2024", "2025", "2026", "2027"]
    assert "computer running Revit" in pack.description
    if name.startswith("link_"):
        assert "large file keeps Revit busy" in pack.description
    if name == "link_revit_model":
        assert pack.validator is None
        assert "60 seconds" in pack.description and "duplicate" in pack.description
    params = {p["name"]: p for p in pack.parameters}
    assert params.keys() == TOOLS[name]
    assert params["path"]["required"] and params["path"]["source"] == "designer"
    assert len(pack.fixtures) == 2
    for fixture in pack.fixtures:
        code, errors = store.render(name, fixture["params"])
        assert code and not errors and review(code) == (True, [])
        assert "File.Exists" not in code and "IFamilyLoadOptions" not in code
        assert "Transaction(" not in code
        assert fixture["expect"]


def test_link_load_rendered_scenarios(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")

    def fixture(name, index=0, **changes):
        return {**store.load(name).fixtures[index]["params"], **changes}

    packs = {
        "link_revit_model": {
            "Origin": fixture("link_revit_model"),
            "Shared": fixture("link_revit_model", 1),
            "Centered": fixture("link_revit_model", positioning="center"),
        },
        "link_cad": {
            "ViewDrawing": fixture("link_cad"),
            "LevelDrawing": fixture("link_cad", 1),
            "BadUnits": fixture("link_cad", units="parsecs"),
            "BadPlacement": fixture("link_cad", positioning="center"),
            "ConflictingLevel": fixture("link_cad", current_view_only=True, level_name="L2"),
        },
        "load_family": {
            "Load": fixture("load_family"),
            "OtherFamily": fixture("load_family", 1),
        },
    }
    for name, extension in (("link_revit_model", "rvt"), ("link_cad", "dwg"), ("load_family", "rfa")):
        for index, path in enumerate((f"relative.{extension}", f"X:relative.{extension}",
                                      f"\\relative.{extension}", fr"\\sample\share.{extension}",
                                      r"X:\synthetic\sample.txt")):
            packs[name][f"Invalid_{name}_{index}"] = fixture(name, path=path)
    assert "Link/load scenarios passed" in simulate_packs(tmp_path, "link_load_simulation", packs)

"""The shipped v1 pack data, rendered through the same store as run_tool."""
import re

import pytest

from revit_bridge.capabilities.store import ToolStore
from revit_bridge.revit.sandbox import review
from revit_bridge.validators.builtin import _compare


MODELING = (
    "create_wall", "create_wall_from_element", "create_walls_by_points", "set_wall_constraints",
    "create_structural_column", "create_column", "get_element_geometry", "list_elements",
    "get_element_parameters", "create_floor", "create_door", "create_window", "create_beam",
    "create_grid", "create_grids_array", "create_level", "create_ceiling", "create_roof_by_footprint",
    "copy_elements", "rotate_elements", "mirror_elements", "move_elements", "change_type",
    "set_parameter", "delete_elements",
)


def test_type_id_readback_distinguishes_adjacent_64_bit_ids():
    type_id = 2**53 + 1
    row = {"Kind": "elementid", "Value": type_id}
    assert _compare(row, str(type_id))[0]
    assert not _compare(row, type_id - 1)[0]
    assert _compare(row, type_id - 1, tolerance=1)[0]


@pytest.mark.parametrize("name", MODELING)
def test_every_modeling_fixture_renders_and_passes_review(name, tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    pack = store.load(name)
    assert pack and pack.fixtures and pack.applies_when and pack.not_for
    for fixture in pack.fixtures:
        code, errors = store.render(name, fixture["params"])
        assert not errors and code, (fixture["name"], errors)
        assert review(code) == (True, [])
        assert not re.search(r"\{[A-Za-z_]\w*\}", code)
        assert not re.search(r"\b(?:Transaction|SubTransaction|TransactionGroup)\s*\(", code)


def test_wall_top_level_preserves_the_difference_between_omitted_and_explicit_height(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    params = dict(level_name="L1", start_x=0, start_y=0, end_x=2000, end_y=0)
    for values, assignment in [
        (params, 'topName = "", heightText = ""'),
        ({**params, "top_level_name": "L2"}, 'topName = "L2", heightText = ""'),
        ({**params, "top_level_name": "L2", "height": ""}, 'topName = "L2", heightText = ""'),
        ({**params, "top_level_name": "L2", "height": 3000}, 'topName = "L2", heightText = "3000"'),
    ]:
        code, errors = store.render("create_wall", values)
        assert errors == [] and assignment in code
        assert code.index('if (topName != "" && heightText != "")') < code.index("Wall.Create(")
        assert 'feet(heightText == "" ? "3000" : heightText)' in code


@pytest.mark.parametrize("name", ["copy_elements", "mirror_elements"])
def test_transform_readback_is_bounded_even_when_revit_returns_dependents(name, tmp_path):
    pack = ToolStore(user_dir=tmp_path / "user").load(name)
    code = pack.code_template
    limit = code.index("if (resultIds.Count > MaxResultIds)")
    read_candidates = code.index("foreach (ElementId id in resultIds)")
    compare = code.index("foreach (var candidate in group)")
    assert limit < read_candidates < compare
    assert code[read_candidates:compare].count("Samples(candidate)") == 1
    assert "document.GetElement(" not in code[compare:] and "Samples(" not in code[compare:]


def test_reference_wall_defaults_to_the_anchors_model_elevation(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    params = dict(reference_id=101, anchor="mid", direction="normal_left", length=4200)
    for level_name in ("", "L2"):
        code, errors = store.render("create_wall_from_element", {**params, "level_name": level_name})
        assert errors == [] and f'string levelName = "{level_name}"' in code
        assert 'baseElevation = levelName == "" ? point.Z : level.ProjectElevation' in code
        assert "WALL_BASE_OFFSET, baseElevation - level.ProjectElevation" in code
        assert "top.ProjectElevation <= baseElevation" in code

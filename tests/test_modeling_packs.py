"""The shipped v1 pack data, rendered through the same store as run_tool."""
import re

import pytest

from revit_bridge.capabilities.store import ToolStore
from revit_bridge.revit.sandbox import review
from revit_bridge.spec.models import Action, ParamBinding, Source, TaskSpec
from revit_bridge.spec.rules import validate_spec


MODELING = (
    "create_wall", "create_wall_from_element", "create_walls_by_points", "set_wall_constraints",
    "create_structural_column", "create_column", "get_element_geometry", "list_elements",
    "get_element_parameters", "create_floor", "create_door", "create_window", "create_beam",
    "create_grid", "create_grids_array", "create_level", "create_ceiling", "create_roof_by_footprint",
    "copy_elements", "rotate_elements", "mirror_elements", "move_elements", "change_type",
    "set_parameter", "delete_elements",
    "create_floor_by_points", "create_ceiling_by_points", "create_roof_by_points",
    "create_door_on_wall", "create_window_on_wall", "change_type_by_name",
)


REQUIRED_091 = {
    "change_type": "category type_id", "count_by_level": "category",
    "create_beam": "type_name level_name start_x start_y end_x end_y elevation",
    "create_ceiling": "level_name type_name x y width length height",
    "create_door": "wall_id type_name x y", "create_floor": "type_name level_name x y width length",
    "create_grid": "name start_x start_y end_x end_y", "create_level": "name elevation",
    "create_roof_by_footprint": "level_name type_name x y width length", "create_room": "level_name x y",
    "create_structural_column": "type_name level_name x y", "create_wall": "level_name start_x start_y end_x end_y",
    "create_window": "wall_id type_name x y sill_height", "delete_elements_by_category": "category delta",
    "delete_selected": "", "elements_in_link": "link_id category", "list_types": "category",
    "modify_wall_height": "element_id height", "move_elements": "x y z", "query_levels": "",
    "query_model_stats": "", "set_parameter": "parameter_name value",
}


@pytest.mark.parametrize("name,required", REQUIRED_091.items())
def test_091_required_inputs_still_block_an_incomplete_task_spec(name, required, tmp_path):
    pack = ToolStore(user_dir=tmp_path / "user").load(name)
    names = required.split()
    assert {p["name"] for p in pack.parameters if p["required"]} == set(names)
    bindings = [ParamBinding(name=key, value=1, source=Source.answer, evidence="fixture") for key in names]
    for omitted in names or [None]:
        spec = TaskSpec(task="run fixture", action=Action(kind="run_tool", tool=name),
                        parameters=[p for p in bindings if p.name != omitted], snapshot_fingerprint="fixture")
        missing = {e.param for e in validate_spec(spec, pack) if e.code == "missing_param"}
        assert missing == ({omitted} if omitted else set())


def test_list_elements_limits_attempted_rows_even_when_every_read_fails(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    code, errors = store.render("list_elements", {"category": "OST_Walls", "limit": 1})
    assert not errors and code
    scan = code.index("while (items.Count + skipped.Count < limit && ids.MoveNext())")
    read = code.index("document.GetElement(id)", scan)
    skip = code.index("skipped.Add(new { Id = id.Value", read)
    lookahead = code.index("truncated = items.Count + skipped.Count == limit && ids.MoveNext();", skip)
    assert code[scan:lookahead].count("ids.MoveNext()") == 1
    assert "document.GetElement" not in code[lookahead:]


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


def test_wall_uses_legacy_default_height_unless_a_top_level_is_bound(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    params = dict(level_name="L1", start_x=0, start_y=0, end_x=2000, end_y=0)
    for values, assignment in [
        (params, 'topName = "", heightText = "3000"'),
        ({**params, "top_level_name": "L2"}, 'topName = "L2", heightText = "3000"'),
        ({**params, "top_level_name": "L2", "height": -1}, 'topName = "L2", heightText = "-1"'),
    ]:
        code, errors = store.render("create_wall", values)
        assert errors == [] and assignment in code
        assert 'top == null ? feet(heightText) : feet("3000")' in code


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


def test_polygon_ceiling_requires_height_and_checks_it_before_creation(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    pack = store.load("create_ceiling_by_points")
    params = dict(pack.fixtures[0]["params"])
    params.pop("height")
    assert store.render(pack.name, params)[1]
    code, errors = store.render(pack.name, {**params, "height": -1})
    assert not errors and 'toFeet("Height above the level", "-1")' in code
    assert code.index("if (height < 0)") < code.index("Ceiling.Create(document")


@pytest.mark.parametrize("name", ["create_wall_from_element", "create_walls_by_points"])
def test_referenced_and_path_walls_bind_top_even_with_negative_height(name, tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    pack = store.load(name)
    params = {**pack.fixtures[0]["params"], "top_level_name": "L2", "height": -1}
    code, errors = store.render(name, params)
    assert not errors and 'topName = "L2", heightText = "-1"' in code
    assert 'top != null || heightText == "" ? "3000" : heightText' in code
    assert "HeightIgnored = heightIgnored" in code

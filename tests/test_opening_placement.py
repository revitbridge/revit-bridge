"""Run the shipped door and window templates against independently specified host/instance geometry."""
import pytest

from tests.csharp_simulation import simulate


@pytest.mark.parametrize("tool", ["create_door", "create_door_on_wall", "create_window_on_wall"])
def test_opening_fits_host_and_reports_placed_point(tmp_path, tool):
    along = tool.endswith("_on_wall")
    placement = {"offset_along_wall": 1500} if along else {"x": 1500, "y": 0}
    basic = {"wall_id": 101, "type_name": "Opening", **placement}
    upper = {**basic, "level_name": "Upper", "flip": True}
    if along:
        upper["sill_height"] = 300
    output = simulate(tmp_path, "opening_simulation", tool, {"Default": basic, "Upper": upper})
    assert "PASS default placement and final point" in output
    assert "PASS cross-level placement and flip" in output
    assert "PASS actual XYZ after flip" in output
    if "door" in tool:
        assert "PASS wall bottom, head, fallback heights and unreadable dimensions" in output

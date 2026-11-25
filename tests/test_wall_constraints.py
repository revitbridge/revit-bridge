"""Exercise the effective constraints after every individual Revit parameter write."""
from tests.csharp_simulation import simulate


def test_move_wall_constraints_without_crossing_the_other_end(tmp_path):
    params = {
        "Raise": dict(base_level_name="L3", top_level_name="L4", height=99),
        "Bind": dict(top_level_name="L3"),
        "Offset": dict(top_level_name="L3", top_offset=125),
        "BaseOnly": dict(base_offset=200),
        "Height": dict(height=2000),
        "Invalid": dict(base_level_name="L3", top_level_name="L2"),
        "LowerTop": dict(base_level_name="L2", base_offset=1000, top_level_name="L3"),
        "RaiseBase": dict(base_level_name="L2", base_offset=-1000),
    }
    cases = {name: {"element_id": 101, **values} for name, values in params.items()}
    output = simulate(tmp_path, "wall_simulation", "set_wall_constraints", cases)
    assert len([line for line in output.splitlines() if line.startswith("PASS ")]) == len(cases), output

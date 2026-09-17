"""Capability store: built-in packs, parameter validation, solidify round trip."""
from __future__ import annotations

from pathlib import Path

from revit_bridge.capabilities.store import ToolStore, default_capabilities_dir

BUILTIN_NAMES = {
    "create_beam", "create_column", "create_column_2", "create_floor", "create_room",
    "create_structural_column", "create_wall", "delete_elements_by_category",
    "modify_wall_height", "query_levels", "query_model_stats",
}


def test_builtin_packs_are_found_and_loadable():
    store = ToolStore()
    files = sorted(p.name for p in store.tools_dir.glob("*.yaml"))
    assert len(files) == 12
    names = {t.name for t in store.list_tools()}
    assert BUILTIN_NAMES <= names
    for name in BUILTIN_NAMES:
        tool = store.load(name)
        assert tool is not None and tool.code_template.strip()


def test_env_override_selects_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIT_BRIDGE_CAPABILITIES_DIR", str(tmp_path / "mine"))
    assert default_capabilities_dir() == Path(tmp_path / "mine")
    store = ToolStore()
    assert store.tools_dir == tmp_path / "mine"
    assert store.list_tools() == []


def test_validate_params_enforces_sources(tmp_path):
    store = ToolStore(tmp_path)
    store.solidify(
        name="probe",
        code='return "{level_name}-{type_name}-{x}-{h}";',
        parameters=[
            {"name": "level_name", "type": "string", "source": "query:levels"},
            {"name": "type_name", "type": "string", "source": "ask_user"},
            {"name": "x", "type": "double", "source": "ask_user"},
            {"name": "h", "type": "double", "source": "default", "default": 3000},
        ],
    )
    valid, errors, _ = store.validate_params("probe", {})
    assert not valid
    joined = " ".join(errors)
    assert "level_name" in joined and "type_name" in joined and "x" in joined
    assert "h" not in joined  # default source fills itself

    valid, errors, filled = store.validate_params(
        "probe", {"level_name": "L1", "type_name": "Basic", "x": "12.5"})
    assert valid, errors
    assert filled["h"] == 3000

    valid, errors, _ = store.validate_params(
        "probe", {"level_name": "L1", "type_name": "Basic", "x": "twelve"})
    assert not valid and any("expects double" in e for e in errors)

    assert store.render_code("probe", {"level_name": "L1", "type_name": "Basic", "x": 1}) \
        == 'return "L1-Basic-1-3000";'
    assert store.render_code("probe", {}) is None


def test_solidify_round_trip_and_usage(tmp_path):
    store = ToolStore(tmp_path)
    store.solidify(name="hello world", code="return 1;", description="d", tags=["t"])
    assert (tmp_path / "hello_world.yaml").exists()
    tool = store.load("hello world")
    assert tool.display_name == "Hello World" and tool.tags == ["t"]

    store.record_usage("hello world", success=False)
    store.record_usage("hello world", success=False)
    health = store.health_check("hello world")
    assert health["status"] == "failing" and health["recommendation"] == "fallback_to_rag"

    store.record_usage("hello world", success=True)
    assert store.load("hello world").failure_count == 0
    assert store.health_check("hello world")["status"] == "healthy"

    assert store.delete("hello world") is True
    assert store.load("hello world") is None


def test_dynamic_params_of_builtin_create_wall():
    store = ToolStore()
    dynamic = store.get_dynamic_params("create_wall")
    assert [(d["name"], d["choices_from"]) for d in dynamic] == [("level_name", "levels")]

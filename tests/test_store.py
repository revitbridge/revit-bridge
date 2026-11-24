"""Capability store: two directories, v0 compatibility, usage.json, solidify."""
from __future__ import annotations

import json

import pytest
import yaml

from revit_bridge.capabilities.store import ToolStore, default_capabilities_dir, escape_param_value, normalize_pack
from revit_bridge.paths import builtin_capabilities_dir, user_capabilities_dir

BUILTIN_NAMES = {
    "create_beam", "create_floor", "create_structural_column", "create_wall",
    "delete_elements_by_category", "modify_wall_height", "query_levels", "query_model_stats",
    # the basic modelling packs
    "create_level", "create_grid", "create_door", "create_window", "create_room",
    "create_roof_by_footprint", "create_ceiling", "set_parameter", "move_elements",
    "change_type", "delete_selected", "count_by_level", "list_types", "elements_in_link",
}
# Demo artefacts with type, level and coordinates hard-coded in C#: kept as examples, never loaded.
# create_room is also a built-in name now; the example of that name must still not be what loads.
EXAMPLE_NAMES = {"create_column", "create_column_2", "create_room", "create_wall_v2"}


def names_of(store: ToolStore) -> list[str]:
    return [t.name for t in store.list_tools()]


# -- directories ------------------------------------------------------------------

def test_builtin_packs_are_found_and_loadable(isolated_data_dir):
    store = ToolStore()
    assert store.builtin_dir == builtin_capabilities_dir()
    assert store.user_dir == isolated_data_dir / "capabilities"
    assert store.tools_dir == store.user_dir            # 0.1 name still answers
    assert not store.user_dir.exists()                  # listing creates nothing
    files = sorted(p.name for p in store.builtin_dir.glob("*.yaml"))
    assert len(files) == len(BUILTIN_NAMES)
    names = names_of(store)
    assert len(names) == len(BUILTIN_NAMES) and set(names) == BUILTIN_NAMES
    for name in BUILTIN_NAMES:
        tool = store.load(name)
        assert tool is not None and tool.code_template.strip()
        assert store.path_of(name) == store.builtin_dir / f"{name}.yaml"
    assert not store.user_dir.exists()


def test_examples_subdirectory_and_underscore_files_are_never_loaded(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    examples = store.builtin_dir / "examples"
    assert {p.stem for p in examples.glob("*.yaml")} >= EXAMPLE_NAMES
    assert names_of(store).count("create_wall") == 1
    assert not {"create_column", "create_column_2"} & set(names_of(store))
    example_room = yaml.safe_load((examples / "create_room.yaml").read_text(encoding="utf-8"))
    assert names_of(store).count("create_room") == 1
    assert store.path_of("create_room") == store.builtin_dir / "create_room.yaml"
    assert store.load("create_room").code_template != example_room["code_template"]

    store.user_dir.mkdir()
    (store.user_dir / "_draft.yaml").write_text("name: draft\ncode_template: return 1;\n", encoding="utf-8")
    (store.user_dir / "sub").mkdir()
    (store.user_dir / "sub" / "nested.yaml").write_text("name: nested\ncode_template: return 1;\n", encoding="utf-8")
    (store.user_dir / "broken.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    assert "draft" not in names_of(store) and "nested" not in names_of(store)
    assert "broken" not in names_of(store) and store.load("broken") is None


def test_capabilities_dir_override_only_moves_the_user_directory(tmp_path, monkeypatch, isolated_data_dir):
    mine = tmp_path / "mine"
    monkeypatch.setenv("REVIT_BRIDGE_CAPABILITIES_DIR", str(mine))
    assert user_capabilities_dir() == mine
    assert default_capabilities_dir() == mine           # deprecated 0.1 helper
    store = ToolStore()
    assert store.user_dir == mine and store.builtin_dir == builtin_capabilities_dir()
    assert set(names_of(store)) == BUILTIN_NAMES        # built-ins still visible
    store.solidify("mine_only", "return 1;")
    assert (mine / "mine_only.yaml").exists()
    monkeypatch.delenv("REVIT_BRIDGE_CAPABILITIES_DIR")
    # Without the override the 0.1 helper still names the writable user directory,
    # never the read-only built-in packs (review item 4).
    assert default_capabilities_dir() == isolated_data_dir / "capabilities"
    assert ToolStore(default_capabilities_dir()).user_dir == isolated_data_dir / "capabilities"


def test_store_refuses_to_write_into_the_builtin_directory(tmp_path):
    with pytest.raises(ValueError, match="read-only"):
        ToolStore(builtin_capabilities_dir())
    with pytest.raises(ValueError):
        ToolStore(user_dir=tmp_path / "x", builtin_dir=tmp_path / "x")
    ToolStore(user_dir=tmp_path / "x", builtin_dir=tmp_path / "y")   # distinct: fine
    assert (builtin_capabilities_dir() / "create_wall.yaml").exists()


def test_user_pack_overrides_builtin_of_the_same_name(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    store.solidify("create_wall", "return 42;", description="mine")
    assert names_of(store).count("create_wall") == 1
    tool = store.load("create_wall")
    assert tool.description == "mine" and tool.code_template == "return 42;"
    assert store.path_of("create_wall") == store.user_dir / "create_wall.yaml"
    assert len(names_of(store)) == len(BUILTIN_NAMES)


def test_delete_builtin_leaves_a_disabled_marker(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    assert store.delete("query_levels") is True
    marker = store.user_dir / "query_levels.disabled"
    assert marker.exists() and marker.read_text() == ""
    assert store.load("query_levels") is None
    assert store.path_of("query_levels") is None
    assert "query_levels" not in names_of(store) and len(names_of(store)) == len(BUILTIN_NAMES) - 1
    assert store.delete("query_levels") is False        # already hidden
    assert store.delete("no_such_tool") is False
    assert (store.builtin_dir / "query_levels.yaml").exists()   # built-in untouched

    # Re-solidifying the name brings it back (as the user's version)
    store.solidify("query_levels", "return 2;")
    assert not marker.exists()
    assert store.load("query_levels").code_template == "return 2;"
    assert store.enable("query_levels") is False

    # Deleting the user copy of a built-in hides the built-in too
    assert store.delete("query_levels") is True
    assert marker.exists() and store.load("query_levels") is None
    assert store.enable("query_levels") is True
    assert store.load("query_levels").code_template.strip().startswith("var levels")


def test_update_builtin_copies_it_to_the_user_directory_and_bumps_the_patch(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    assert store.update("no_such_tool", {"description": "x"}) is None

    before = store.load("create_wall")
    assert before.version == "1.0.0" and before.schema_version == 1

    tool = store.update("create_wall", {"description": "walls, my way", "tags": ["ignored"]})
    assert tool.description == "walls, my way"
    assert tool.version == "1.0.0"                      # contract unchanged: no bump
    written = yaml.safe_load((store.user_dir / "create_wall.yaml").read_text(encoding="utf-8"))
    assert written["schema_version"] == 1 and written["version"] == "1.0.0"
    assert "tags" not in written and "execution_count" not in written
    assert (store.builtin_dir / "create_wall.yaml").read_text(encoding="utf-8").startswith("schema_version: 1\nname: create_wall")

    tool = store.update("create_wall", {"code_template": "return 7;"})
    assert tool.version == "1.0.1" and tool.code_template == "return 7;"
    tool = store.update("create_wall", {"parameters": tool.parameters})
    assert tool.version == "1.0.1"                      # identical parameters: no bump
    tool = store.update("create_wall", {"parameters": [{"name": "n", "type": "double", "default": 1}]})
    assert tool.version == "1.0.2"
    assert tool.parameters[0]["source"] == "default" and tool.parameters[0]["required"] is False


# -- v0 compatibility ----------------------------------------------------------------

V0_BEAM = """\
name: create_beam
display_name: Create Beam
description: Create a beam using first available beam family type.
code_template: |
  return "{type_name}-{level_name}-{start_x}";
parameters:
  - name: type_name
    type: string
    description: Beam family type name
    choices_from: family_types:OST_StructuralFraming
  - name: level_name
    type: string
    description: Target level name
    choices_from: levels
  - name: start_x
    type: double
    description: Beam start X (mm)
    default: '0'
  - name: category
    type: string
    description: BuiltInCategory name
tags:
  - beam
  - structural
preconditions:
  - at least one level
created_at: '2026-03-13T00:00:00'
source_query: create a structural beam
execution_count: 2
last_used: '2026-03-20T09:51:45'
"""


def test_v0_packs_are_normalised_on_load(tmp_path):
    """A 0.1 pack file (the layout the built-ins had before v1) still loads."""
    store = ToolStore(user_dir=tmp_path / "user")
    store.user_dir.mkdir()
    (store.user_dir / "create_beam.yaml").write_text(V0_BEAM, encoding="utf-8")
    beam = store.load("create_beam")
    assert beam.schema_version == 0 and beam.version == "0.0.0"
    by_name = {p["name"]: p for p in beam.parameters}
    assert by_name["type_name"]["source"] == "tool:family_types"
    assert by_name["type_name"]["choices_from"] == "family_types:OST_StructuralFraming"
    assert by_name["type_name"]["required"] is True and "unit" not in by_name["type_name"]
    assert by_name["level_name"]["source"] == "tool:levels"
    assert by_name["start_x"]["source"] == "default"
    assert by_name["start_x"]["unit"] == "mm" and by_name["start_x"]["required"] is False
    assert by_name["category"]["source"] == "designer" and by_name["category"]["required"] is True
    assert beam.preconditions == [{"text": "at least one level"}]
    assert beam.tags == ["beam", "structural"]
    assert beam.execution_count == 0 and beam.failure_count == 0 and beam.last_used == ""

    # the built-ins are v1 files now
    assert all(t.schema_version == 1 and t.version == "1.0.0" for t in ToolStore().list_tools())


def test_normalize_pack_covers_the_v0_vocabulary():
    data = normalize_pack({
        "name": "probe",
        "code_template": "return 1;",
        "parameters": [
            {"name": "level", "source": "query:levels"},
            {"name": "host", "source": "interactive:pick_object"},
            {"name": "x", "type": "double", "source": "ask_user", "description": "X (mm)"},
            {"name": "already", "source": "answer", "unit": "m", "required": False},
            {"name": "col", "source": "tool:family_types:OST_StructuralColumns"},
            {"name": "kept", "source": "tool:family_types", "choices_from": "family_types:OST_Walls"},
        ],
        "preconditions": ["at least one level", {"kind": "levels_min", "value": 1}],
        "execution_count": 3, "last_used": "2026-01-01T00:00:00", "failure_count": 1,
    })
    assert data["schema_version"] == 0 and data["version"] == "0.0.0"
    assert data["display_name"] == "Probe"
    sources = [p["source"] for p in data["parameters"]]
    assert sources == ["tool:levels", "tool:pick_object", "designer", "answer",
                       "tool:family_types:OST_StructuralColumns", "tool:family_types"]
    choices = [p.get("choices_from") for p in data["parameters"]]
    assert choices == ["levels", "pick_object", None, None,
                       "family_types:OST_StructuralColumns", "family_types:OST_Walls"]
    assert data["parameters"][2]["unit"] == "mm" and data["parameters"][2]["required"] is True
    assert data["parameters"][3]["unit"] == "m" and data["parameters"][3]["required"] is False
    assert data["preconditions"] == [{"text": "at least one level"}, {"kind": "levels_min", "value": 1}]
    for legacy in ("execution_count", "last_used", "failure_count"):
        assert legacy not in data
    assert data["validator"] is None and data["fixtures"] == [] and data["approved_by"] is None


# -- writing ------------------------------------------------------------------------

def test_solidify_writes_a_v1_pack_without_counters(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    store.solidify(
        name="hello world", code="var a = 1;\nreturn a;", description="d", tags=["t"],
        parameters=[{"name": "level_name", "choices_from": "levels"},
                    {"name": "h", "type": "double", "default": 3000, "description": "Height (mm)"}],
        preconditions=["one level"], applies_when=["hello"], not_for=["goodbye"],
        validator={"kind": "count_delta", "category": "OST_Walls", "expected": 1},
    )
    path = tmp_path / "user" / "hello_world.yaml"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("schema_version: 1\nname: hello world\n")
    assert "code_template: |" in text                    # multi-line code as a block
    written = yaml.safe_load(text)
    assert written["version"] == "1.0.0" and written["display_name"] == "Hello World"
    assert written["parameters"][0]["source"] == "tool:levels"
    assert written["parameters"][1] == {
        "name": "h", "type": "double", "default": 3000, "description": "Height (mm)",
        "source": "default", "unit": "mm", "required": False,
    }
    assert written["preconditions"] == [{"text": "one level"}]
    assert written["validator"]["kind"] == "count_delta"
    assert written["approved_by"] is None and written["fixtures"] == []
    for absent in ("tags", "execution_count", "last_used", "failure_count"):
        assert absent not in written

    tool = store.load("hello world")
    assert tool.schema_version == 1 and tool.version == "1.0.0" and tool.tags == []
    assert tool.validator == {"kind": "count_delta", "category": "OST_Walls", "expected": 1}
    assert tool.code_template == "var a = 1;\nreturn a;"


def test_usage_lives_in_usage_json_not_in_pack_files(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    usage_path = store.user_dir / "usage.json"

    store.record_usage("no_such_tool", success=True)
    assert not usage_path.exists()

    store.record_usage("create_wall", success=False)
    store.record_usage("create_wall", success=False)
    assert json.loads(usage_path.read_text(encoding="utf-8"))["create_wall"]["failure_count"] == 2
    assert not (store.user_dir / "create_wall.yaml").exists()      # built-in file untouched
    health = store.health_check("create_wall")
    assert health["status"] == "failing" and health["recommendation"] == "write_new_code"

    store.record_usage("create_wall", success=True)
    listed = {t.name: t for t in store.list_tools()}
    assert listed["create_wall"].execution_count == 1 and listed["create_wall"].failure_count == 0
    assert listed["create_wall"].last_used and listed["query_levels"].execution_count == 0
    assert store.load("create_wall").execution_count == 1
    assert store.health_check("create_wall")["status"] == "healthy"

    assert store.delete("create_wall") is True
    assert "create_wall" not in json.loads(usage_path.read_text(encoding="utf-8"))
    assert store.health_check("create_wall")["status"] == "not_found"

    usage_path.write_text("not json", encoding="utf-8")
    assert store.load("query_levels").execution_count == 0


# -- validation and rendering ---------------------------------------------------------

def test_validate_params_enforces_sources(tmp_path):
    store = ToolStore(tmp_path)
    store.solidify(
        name="probe",
        code='return "{level_name}-{type_name}-{x}-{h}-{note}";',
        parameters=[
            {"name": "level_name", "type": "string", "source": "tool:levels"},
            {"name": "type_name", "type": "string", "source": "designer"},
            {"name": "x", "type": "double", "source": "answer"},
            {"name": "h", "type": "double", "source": "default", "default": 3000},
            {"name": "note", "type": "string", "source": "designer", "required": False, "default": ""},
        ],
    )
    valid, errors, _ = store.validate_params("probe", {})
    assert not valid
    assert [e.split("'")[1] if "'" in e else e.rsplit(" ", 1)[1] for e in errors] \
        == ["level_name", "type_name", "x"]        # defaults fill themselves

    valid, errors, filled = store.validate_params(
        "probe", {"level_name": "L1", "type_name": "Basic", "x": "12.5"})
    assert valid, errors
    assert filled["h"] == 3000 and filled["note"] == ""

    valid, errors, _ = store.validate_params(
        "probe", {"level_name": "L1", "type_name": "Basic", "x": "twelve"})
    assert not valid and any("expects double" in e for e in errors)

    assert store.render_code("probe", {"level_name": "L1", "type_name": "Basic", "x": 1, "note": "n"}) \
        == 'return "L1-Basic-1-3000-n";'
    assert store.render_code("probe", {"level_name": "L1", "type_name": "Basic", "x": 1}) \
        == 'return "L1-Basic-1-3000-";'
    assert store.render_code("probe", {}) is None

    # 0.1 vocabulary keeps working through normalisation
    store.solidify(name="legacy", code="return 1;", parameters=[
        {"name": "level_name", "source": "query:levels"},
        {"name": "who", "source": "ask_user"},
    ])
    valid, errors, _ = store.validate_params("legacy", {})
    assert not valid and "level_name" in errors[0] and "who" in errors[1]
    assert [s["source"] for s in store.get_atom_sources("legacy")] == ["tool:levels"]


def test_dynamic_params_of_builtin_create_wall():
    store = ToolStore()
    dynamic = store.get_dynamic_params("create_wall")
    assert [(d["name"], d["choices_from"]) for d in dynamic] == [("level_name", "levels")]


def test_a_value_the_template_needs_is_never_left_blank(tmp_path):
    """required: false without a default is still a missing value (review item 1)."""
    store = ToolStore(user_dir=tmp_path / "user")
    store.solidify(name="optional", code='return "{note}";', parameters=[
        {"name": "note", "type": "string", "source": "designer", "required": False},
    ])
    valid, errors, filled = store.validate_params("optional", {})
    assert not valid and errors == ["Missing required parameter: note"] and filled == {}
    assert store.render("optional", {}) == (None, ["Missing required parameter: note"])
    assert store.render_code("optional", {}) is None
    assert store.render("optional", {"note": "n"}) == ('return "n";', [])


def test_render_refuses_code_with_a_leftover_placeholder(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    # solidify refuses such a pack (validate_pack); a hand-edited file can still carry one
    with pytest.raises(ValueError, match="placeholder {count}"):
        store.solidify(name="leaky", code='var n = {count}; return "{label}" + {count};', parameters=[
            {"name": "label", "type": "string", "source": "designer"},
        ])
    store.user_dir.mkdir(parents=True, exist_ok=True)
    (store.user_dir / "leaky.yaml").write_text(
        "schema_version: 1\nname: leaky\nversion: 1.0.0\n"
        "code_template: 'var n = {count}; return \"{label}\" + {count};'\n"
        "parameters:\n  - {name: label, type: string, source: designer, required: true}\n",
        encoding="utf-8")
    code, errors = store.render("leaky", {"label": "x"})
    assert code is None
    assert errors == [
        "Template still contains placeholder(s) ['count']: "
        "declare them as parameters or remove them from the code"
    ]
    assert store.render_code("leaky", {"label": "x"}) is None
    assert store.render("missing", {}) == (None, ["Tool 'missing' not found"])

    # C# braces that are not placeholders pass through untouched
    store.solidify(name="braces", code='return new { Status = "Created", Id = {id} };', parameters=[
        {"name": "id", "type": "integer", "source": "answer"},
    ])
    assert store.render("braces", {"id": 7}) == ('return new { Status = "Created", Id = 7 };', [])


def test_query_source_round_trips_through_choices_and_validation(tmp_path):
    """solidify_tool's documented ``source: query:levels`` (review item 2)."""
    store = ToolStore(user_dir=tmp_path / "user")
    store.solidify(name="probe", code='return "{level_name}";', parameters=[
        {"name": "level_name", "type": "string", "source": "query:levels", "description": "Level"},
    ])
    assert store.get_dynamic_params("probe") == [
        {"name": "level_name", "choices_from": "levels", "description": "Level"},
    ]
    valid, errors, _ = store.validate_params("probe", {})
    assert not valid and errors == [
        "Parameter 'level_name' must be resolved from Revit (source: tool:levels) - call get_tool_choices first"
    ]
    valid, errors, filled = store.validate_params("probe", {"level_name": "L1"})
    assert valid and filled == {"level_name": "L1"}
    assert store.render_code("probe", {"level_name": "L1"}) == 'return "L1";'


# -- parameter values: one scalar that fits the type, escaped for a C# literal -----------------

BREAKOUT = ['x"; document.Delete(new ElementId(5L)); return null; //']
NOT_SCALARS = ((BREAKOUT, "a list"), ({"a": 'b"; x(); //'}, "an object"), (("t",), "a list"),
               ({"x"}, "a value of type set"), (b"x", "a value of type bytes"))


def _typed_probe(store: ToolStore) -> None:
    store.solidify(
        name="probe",
        code='string levelName = "{name}"; double h = {h}; bool flag = bool.Parse("{flag}");\nreturn levelName;',
        parameters=[
            {"name": "name", "type": "string", "source": "designer", "required": True},
            {"name": "h", "type": "double", "source": "designer", "required": False, "default": 3000},
            {"name": "flag", "type": "bool", "source": "designer", "required": False, "default": False},
        ],
    )


def test_a_list_or_an_object_never_reaches_the_code(tmp_path):
    """B3: a string parameter sent as a JSON array was written with str(), whose quote closed the
    C# literal; the rest compiled as code and passed the sandbox. Refused before rendering now,
    for every parameter type."""
    store = ToolStore(user_dir=tmp_path / "user")
    _typed_probe(store)
    for value, kind in NOT_SCALARS:
        for pname in ("name", "h", "flag"):
            params = {"name": "L1", pname: value}
            valid, errors, _ = store.validate_params("probe", params)
            assert not valid and errors == [f"Parameter '{pname}' must be a single value, got {kind}"]
            assert store.render("probe", params) == (None, errors)


def test_render_refuses_what_it_cannot_write_without_validation(tmp_path):
    """Defence in depth: a hand-written pack whose placeholder is not declared gets no type check,
    so render itself refuses a value escape_param_value cannot write - a result, never a raise."""
    store = ToolStore(user_dir=tmp_path / "user")
    store.user_dir.mkdir(parents=True)
    (store.user_dir / "handmade.yaml").write_text(
        "name: handmade\ncode_template: 'string levelName = \"{name}\"; return levelName;'\n", encoding="utf-8")
    for value, kind in (*NOT_SCALARS, (None, "null")):
        assert store.render("handmade", {"name": value}) == (
            None, [f"Parameter 'name' must be a single value, got {kind}"])
        with pytest.raises(TypeError, match=f"must be a single value, got {kind}"):
            escape_param_value(value)
    assert store.render("handmade", {"name": 'L1"'}) == ('string levelName = "L1\\""; return levelName;', [])
    # a value no placeholder asks for is written nowhere and refused nowhere, as before
    assert store.render("handmade", {"name": "L1", "extra": [1], "other": None})[1] == []


def test_null_is_a_value_not_given(tmp_path):
    """None was written as the text None; it now means "not given": the default, or missing."""
    store = ToolStore(user_dir=tmp_path / "user")
    _typed_probe(store)
    for pname in ("h", "flag"):
        assert store.validate_params("probe", {"name": "L1", pname: None}) \
            == store.validate_params("probe", {"name": "L1"})
    assert store.validate_params("probe", {"name": None}) == store.validate_params("probe", {}) \
        == (False, ["Missing required parameter: name"], {"h": 3000, "flag": False})
    assert store.render("probe", {"name": None}) == (None, ["Missing required parameter: name"])

    wall = {"start_x": 0, "start_y": 0, "end_x": 5000, "end_y": 0}
    assert store.validate_params("create_wall", {**wall, "level_name": None}) \
        == store.validate_params("create_wall", wall)
    assert store.render("create_wall", {**wall, "level_name": "L1", "height": None}) \
        == store.render("create_wall", {**wall, "level_name": "L1"})


def test_a_number_is_a_finite_number_and_never_a_boolean(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    _typed_probe(store)
    for value in (True, False, float("nan"), float("inf"), float("-inf"), "nan", "inf", "-Infinity",
                  10 ** 400, "twelve"):
        valid, errors, _ = store.validate_params("probe", {"name": "L1", "h": value})
        assert not valid and len(errors) == 1, (value, errors)
        assert errors[0].startswith("Parameter 'h' expects double, got "), errors
    for value, text in ((3000, "3000"), (12.5, "12.5"), ("3000", "3000"), (" 42 ", " 42 "), ("1e3", "1e3"),
                        (-0.5, "-0.5"), (3000.0, "3000.0")):
        code, errors = store.render("probe", {"name": "L1", "h": value})
        assert errors == [] and f"double h = {text};" in code, (value, errors)


def test_a_boolean_is_true_or_false_and_a_string_takes_any_single_value(tmp_path):
    store = ToolStore(user_dir=tmp_path / "user")
    _typed_probe(store)
    for value, text in ((True, "True"), (False, "False"), ("true", "true"), ("False", "False")):
        code, errors = store.render("probe", {"name": "L1", "flag": value})
        assert errors == [] and f'bool.Parse("{text}")' in code, (value, errors)
    for value in (1, 0, 1.0, "yes", ""):
        assert store.validate_params("probe", {"name": "L1", "flag": value})[1] == [
            f"Parameter 'flag' expects bool (true or false), got {value!r}"]
    for value, text in ((2, "2"), (2.5, "2.5"), (True, "True"), ("", "")):       # as before
        code, errors = store.render("probe", {"name": value})
        assert errors == [] and f'string levelName = "{text}";' in code, (value, errors)


def test_every_character_that_ends_a_csharp_literal_or_line_is_escaped(tmp_path):
    """C# ends a regular string literal at CR, LF, U+0085, U+2028 and U+2029; NUL and a lone
    surrogate are escaped too. Everything else is written as given."""
    ends = "a\x85b\N{LINE SEPARATOR}c\N{PARAGRAPH SEPARATOR}d"
    assert escape_param_value(ends) == "a\\u0085b\\u2028c\\u2029d"
    assert escape_param_value("a\x00b\ud800c\udfff") == "a\\0b\\ud800c\\udfff"
    assert escape_param_value('a"b\\c\r\nd') == 'a\\"b\\\\c\\r\\nd'                       # as before
    plain = "tab\there \N{CJK UNIFIED IDEOGRAPH-6807} \N{GRINNING FACE} {x} $@'"
    assert escape_param_value(plain) == plain
    assert (escape_param_value(True), escape_param_value(3000.0), escape_param_value(-7)) == ("True", "3000.0", "-7")

    store = ToolStore(user_dir=tmp_path / "user")
    _typed_probe(store)
    code, errors = store.render("probe", {"name": "x\r\n" + ends + "\x00\ud800"})
    assert errors == [] and code.count("\n") == 1
    assert not any(ch in code for ch in "\r\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}\x00\ud800")
    assert code.startswith('string levelName = "x\\r\\na\\u0085b\\u2028c\\u2029d\\0\\ud800";')


def test_every_built_in_fixture_renders_as_before():
    """Known-good values are written as they always were: a number with str(), a plain string as is."""
    store = ToolStore()
    rendered = 0
    for tool in store.list_tools():
        for fixture in tool.fixtures:
            params = dict(fixture.get("params") or {})
            code, errors = store.render(tool.name, params)
            assert errors == [], (tool.name, errors)
            expected = tool.code_template
            for key, value in store.validate_params(tool.name, params)[2].items():
                expected = expected.replace("{" + key + "}", str(value))
            assert code == expected, tool.name
            rendered += 1
    assert rendered == 25                 # 6 from the earlier packs, 19 from the basic packs


def test_a_hand_dropped_user_pack_beats_a_stale_disabled_marker(tmp_path):
    """load, path_of and list_tools agree on what is visible (review item 3)."""
    store = ToolStore(user_dir=tmp_path / "user")
    store.user_dir.mkdir()
    (store.user_dir / "query_levels.disabled").touch()
    assert store.load("query_levels") is None and "query_levels" not in names_of(store)

    (store.user_dir / "query_levels.yaml").write_text(
        "name: query_levels\ncode_template: return 9;\n", encoding="utf-8")
    assert store.path_of("query_levels") == store.user_dir / "query_levels.yaml"
    assert store.load("query_levels").code_template == "return 9;"
    listed = {t.name: t for t in store.list_tools()}
    assert listed["query_levels"].code_template == "return 9;" and len(listed) == len(BUILTIN_NAMES)
    assert (store.user_dir / "query_levels.disabled").exists()   # untouched by reads

    # A stale marker for a user-only pack (no built-in) hides nothing either
    (store.user_dir / "mine.yaml").write_text("name: mine\ncode_template: return 1;\n", encoding="utf-8")
    (store.user_dir / "mine.disabled").touch()
    assert store.load("mine") is not None and "mine" in names_of(store)


@pytest.mark.parametrize("content", [b"name: [unclosed\ncode_template: x\n",          # not YAML
                                     b"- a\n- list\n",                               # not a mapping
                                     "name: query_levels\ncode_template: \u4e2d\n".encode("gbk")])  # not UTF-8
def test_a_broken_user_pack_gives_way_to_the_built_in_one_with_a_warning(tmp_path, content):
    """B6: list_tools listed the built-in while load returned None, so run_tool said "not found"
    for a tool the list showed. load, path_of and list_tools now agree on the built-in, and
    load_problem says why the user's file was passed over."""
    store = ToolStore(user_dir=tmp_path / "user")
    builtin = store.load("query_levels")
    store.user_dir.mkdir()
    user_file = store.user_dir / "query_levels.yaml"
    user_file.write_bytes(content)
    assert "query_levels" in names_of(store)
    tool = store.load("query_levels")
    assert tool is not None and tool.code_template == builtin.code_template
    assert store.path_of("query_levels") == store.builtin_dir / "query_levels.yaml"
    assert store.render("query_levels", {})[0] == builtin.code_template
    problem = store.load_problem("query_levels")
    assert problem.startswith(f"user pack {user_file} cannot be read") and "built-in" in problem
    assert store.update("query_levels", {"description": "x"}) is None    # never written over silently
    assert user_file.read_bytes() == content
    assert store.load_problem("create_wall") is None                     # no user file: nothing to say

    # without a built-in of that name a broken user pack is still "not found", as before
    (store.user_dir / "mine.yaml").write_bytes(content)
    assert store.load("mine") is None and store.load_problem("mine") is None
    assert store.path_of("mine") == store.user_dir / "mine.yaml"

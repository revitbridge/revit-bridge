import errno
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml

from revit_bridge import mcp_server
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.workflows import WorkflowStore, resolve_step_params, validate_workflow
from revit_bridge import workflows


@pytest.fixture
def tools(tmp_path):
    store = ToolStore(tmp_path / "capabilities", tmp_path / "builtin")
    store.solidify("make_wall", "return 1;", parameters=[{"name": "height", "type": "int"}])
    store.builtin_dir.mkdir()
    store._tool_path("make_wall").replace(store.builtin_dir / "make_wall.yaml")
    store.solidify("use_wall", "return 1;", parameters=[{"name": "id", "type": "int"}])
    return store


@pytest.fixture
def flow():
    return {
        "schema_version": 1, "kind": "workflow", "name": "wall_pair", "display_name": "Wall pair",
        "summary": "Create and use a wall.", "description": "Use for a single new wall.", "version": "1.0.0",
        "inputs": [{"name": "height", "type": "int", "description": "Height", "ask": True}],
        "steps": [
            {"id": "wall", "tool": "make_wall", "title": "Make", "params": {"height": {"input": "height"}}},
            {"id": "use", "tool": "use_wall", "title": "Use", "params": {"id": {"from": "wall", "path": "a.b.0"}}},
        ],
    }


def test_frozen_format_round_trip(tools, flow, monkeypatch):
    store = WorkflowStore(tools)
    assert validate_workflow(flow, tools) == []
    path = store.save(flow)
    assert path == tools.user_dir.parent / "workflows" / "wall_pair.yaml"
    assert yaml.safe_load(path.read_bytes()) == store.load("wall_pair") == flow
    assert store.list_workflows() == [flow]
    monkeypatch.setattr(mcp_server, "_tool_store", tools)
    assert json.loads(mcp_server.list_workflows())[0]["summary"] == flow["summary"]
    assert json.loads(mcp_server.get_workflow("wall_pair")) == flow
    edited = "# Author's comment\r\n" + path.read_text(encoding="utf-8")
    path.write_bytes(edited.encode("utf-8"))
    assert mcp_server.workflow_resource("wall_pair") == edited
    with pytest.raises(FileExistsError):
        store.save(flow)
    flow["summary"] = "Edited"
    store.save(flow, overwrite=True)
    assert store.load("wall_pair") == flow
    assert store.delete("wall_pair") and not store.delete("wall_pair")
    assert store.load("wall_pair") is None


@pytest.mark.parametrize("path,value,fragment", [
    (("name",), "../escape", "name"), (("name",), "NUL", "name"),
    (("name",), "a" * 101, "name"), (("name",), "make_wall", "tool"),
    (("summary",), "x" * 121, "summary"), (("description",), " ", "description"),
    (("kind",), "pack", "kind"), (("schema_version",), True, "schema_version"),
    (("steps",), [], "2"), (("version",), 1, "version"), (("extra",), True, "extra"),
    (("inputs", 0, "extra"), True, "inputs[0]"), (("inputs", 0, "ask"), 1, "ask"),
    (("inputs", 0, "type"), {}, "type"), (("steps", 1, "id"), "wall", "steps[1]"),
    (("steps", 1, "tool"), "wall_pair", "workflow"), (("steps", 1, "tool"), "missing", "unknown tool"),
    (("steps", 0, "extra"), 7, "extra"), (("steps", 1, "params"), {"typo": 1}, "typo"),
    (("steps", 1, "params"), {}, "id"), (("steps", 0, "params", "height"), {"input": "typo"}, "typo"),
    (("steps", 0, "params", "height"), {"from": "use", "path": "a"}, "earlier"),
    (("steps", 1, "params", "id"), {"from": "no_step", "path": "a"}, "no_step"),
    (("steps", 1, "params", "id", "extra"), True, "extra"),
    (("steps", 1, "params", "id", "path"), "a..1", "path"),
    (("steps", 1, "params", "id", "path"), "a.-1", "path"),
    (("steps", 0, "params", "height"), [1], "scalar"),
])
def test_item_errors(tools, flow, path, value, fragment):
    node = flow
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    problems = validate_workflow(flow, tools)
    assert any(fragment in error and str(path[0]) in error for error in problems), problems


def test_multiple_errors_and_stale_workflows(tools, flow):
    store = WorkflowStore(tools)
    store.save(flow)
    tools.delete("use_wall")
    tools.solidify("make_wall", "return 2;", parameters=[{"name": "length", "type": "int"}])
    errors = validate_workflow(flow, tools)
    assert any("steps[0]" in e and "length" in e for e in errors)
    assert any("steps[0]" in e and "height" in e for e in errors)
    assert any("steps[1]" in e and "use_wall" in e for e in errors)
    with pytest.raises(ValueError, match="use_wall"):
        store.load(flow["name"])
    nested = deepcopy(flow)
    nested["name"] = "other"
    nested["steps"][0]["tool"] = flow["name"]
    assert any("workflow" in e for e in validate_workflow(nested, tools))
    tools.solidify(flow["name"], "return 0;")
    assert store.list_workflows() == []
    with pytest.raises(ValueError, match="tool"):
        store.save(flow, overwrite=True)
    assert store.delete(flow["name"])


def test_step_count_and_unique_inputs(tools, flow):
    flow["inputs"] *= 2
    flow["steps"] *= 5
    errors = validate_workflow(flow, tools)
    assert any("inputs[1]" in e for e in errors)
    assert any("8" in e for e in errors)


def test_concurrent_create_publishes_one_complete_file(tools, flow):
    store = WorkflowStore(tools)

    def create(summary):
        try:
            store.save(dict(flow, summary=summary))
            return summary
        except FileExistsError:
            return None
    with ThreadPoolExecutor(max_workers=5) as pool:
        saved = [s for s in pool.map(create, list("abcde")) if s]
    assert saved == [store.load(flow["name"])["summary"]]


def test_failed_edit_preserves_previous_file(tools, flow, monkeypatch):
    store = WorkflowStore(tools)
    path = store.save(flow)
    previous = path.read_bytes()

    def fail_replace(*args):
        raise OSError("replace interrupted")

    monkeypatch.setattr("revit_bridge.workflows.store.os.replace", fail_replace)
    with pytest.raises(OSError, match="interrupted"):
        store.save(dict(flow, summary="New summary"), overwrite=True)
    assert path.read_bytes() == previous
    assert list(store.directory.iterdir()) == [path]


def test_resolve_sources_with_scalar_leaves(flow):
    first, second = flow["steps"]
    assert resolve_step_params(first, {"height": 0}, {}) == {"height": 0}
    with pytest.raises(ValueError, match="missing input 'height'"):
        resolve_step_params(first, {}, {})
    assert resolve_step_params(second, {}, {"wall": {"a": {"b": [12]}}}) == {"id": 12}
    first["params"]["height"] = False
    assert resolve_step_params(first, {}, {}) == {"height": False}
    for result in ({}, {"wall": {"a": {"b": []}}}, {"wall": {"a": {"b": [{}]}}}):
        with pytest.raises(ValueError, match=r"use.*id.*wall.*a\.b\.0"):
            resolve_step_params(second, {}, result)


@pytest.mark.parametrize("field", ["input", "from", "tool"])
def test_shared_value_never_formatted(tools, flow, field):
    class NoRepr(list):
        def __repr__(self):
            pytest.fail("validation formatted a container")

    value = NoRepr()
    for _ in range(9):
        value = NoRepr([value, value])
    step = flow["steps"][1]
    if field == "tool":
        step["tool"] = value
    else:
        step["params"]["id"] = {field: value, **({"path": "a"} if field == "from" else {})}
    problems = validate_workflow(flow, tools)
    assert problems and len(" ".join(problems)) < 500
    with pytest.raises(workflows.WorkflowError) as err:
        WorkflowStore(tools).save(flow)
    assert err.value.problems == problems


def test_aliases_rejected_across_workflow_readers(tools, flow, monkeypatch):
    store = WorkflowStore(tools)
    path = store.save(flow)
    # Each of nine levels is shared. Keep one edge between levels so a
    # regression on the original implementation is safe to run in CI.
    levels = [["leaf"]]
    for _ in range(8):
        levels.append([levels[-1]])
    flow["steps"][0]["params"]["height"]["input"] = levels
    flow["steps"][1]["params"]["id"]["from"] = levels[-1]
    source = yaml.safe_dump(flow, sort_keys=False)
    assert source.count("&id") == 9 and len(source.encode()) < 1024
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_tool_store", tools)
    started = time.monotonic()
    shared = yaml.safe_load(source)
    errors = validate_workflow(shared, tools)
    assert errors and len(" ".join(errors)) < 200
    with pytest.raises(workflows.WorkflowError) as direct:
        store.save(shared, overwrite=True)
    assert direct.value.problems == errors
    with pytest.raises(workflows.WorkflowError, match="anchor|alias") as err:
        store._read(flow["name"])
    assert len(str(err.value)) < 200
    assert json.loads(mcp_server.list_workflows()) == []
    for reader in (mcp_server.get_workflow, mcp_server.workflow_resource):
        reply = json.loads(reader(flow["name"]))
        assert reply == {"error": "invalid_workflow", "problems": err.value.problems}
    with pytest.raises(workflows.WorkflowError, match="anchor|alias"):
        workflows.parse_workflow_yaml(source)
    assert time.monotonic() - started < 5


def test_parse_workflow_limits(tools, flow):
    store = WorkflowStore(tools)
    path = store.save(flow)
    source = path.read_text(encoding="utf-8")
    assert workflows.parse_workflow_yaml(source) == flow
    for extra, reason in [("x: &alone 1", "anchor"), ("x: *missing", "alias"),
                          ("x: " + "[" * 65 + "1" + "]" * 65, "depth"),
                          ("# " + "界" * 90000, "256 KiB")]:
        text = source + extra
        path.write_text(text, encoding="utf-8")
        for parse in (lambda: workflows.parse_workflow_yaml(text), lambda: store.load(flow["name"])):
            with pytest.raises(workflows.WorkflowError, match=reason):
                parse()
    flow["description"] = "x" * (256 * 1024)
    with pytest.raises(workflows.WorkflowError, match="256 KiB"):
        store.save(flow, overwrite=True)


def test_shared_valid_parameters_save_as_plain_yaml(tools, flow):
    first = flow["steps"][0]
    flow["steps"][1] = dict(first, id="second")
    store = WorkflowStore(tools)
    source = store.save(flow).read_text(encoding="utf-8")
    assert workflows.parse_workflow_yaml(source) == store.load(flow["name"]) == flow


def test_error_budget_and_long_field_names(tools, flow):
    flow.update({str(i) + "x" * 2000: 0 for i in range(100)})
    errors = validate_workflow(flow, tools)
    assert 0 < len(errors) <= 50
    assert max(map(len, errors)) < 200


def test_case_collision_preserves_original(tools, flow):
    store = WorkflowStore(tools)
    store.save(flow)
    for overwrite in (False, True):
        with pytest.raises(workflows.WorkflowError, match="case"):
            store.save(dict(flow, name=flow["name"].upper()), overwrite=overwrite)
    assert store.list_workflows() == [flow]
    for name in ("MAKE_WALL", "USE_WALL"):
        with pytest.raises(workflows.WorkflowError, match="tool"):
            store.save(dict(flow, name=name))


def test_exclusive_create_without_hard_links(tools, flow, monkeypatch):
    from revit_bridge.workflows import store as store_module

    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, "no hard links")

    monkeypatch.setattr(store_module, "os", SimpleNamespace(name="posix", link=unsupported,
                        replace=os.replace, fsync=os.fsync))
    store = WorkflowStore(tools)
    store.save(flow)
    with pytest.raises(FileExistsError):
        store.save(dict(flow, summary="another writer"))
    assert store.load(flow["name"]) == flow


@pytest.mark.parametrize("source", ["literal", "input", "from"])
def test_resolve_declared_type(tools, flow, source):
    step = flow["steps"][1]
    tool = tools.load("use_wall")
    for kind, good, bad in [("int", 1, [True, 1.2, "1"]), ("double", 1.2, [True, "1"]),
                            ("boolean", False, [0, "false"]), ("string", "1", [1, True])]:
        tool.parameters[0]["type"] = kind
        def resolve(value):
            step["params"]["id"] = {"input": "i"} if source == "input" else (
                {"from": "wall", "path": "v"} if source == "from" else value)
            return resolve_step_params(step, {"i": value}, {"wall": {"v": value}}, tool=tool)
        assert resolve(good) == {"id": good}
        for value in bad:
            with pytest.raises(ValueError, match=f"id.*{kind}"):
                resolve(value)


def test_zero_padded_path_is_only_a_mapping_key(flow):
    step = flow["steps"][1]
    step["params"]["id"]["path"] = "007"
    assert resolve_step_params(step, {}, {"wall": {"007": 3}}) == {"id": 3}
    with pytest.raises(ValueError, match="007"):
        resolve_step_params(step, {}, {"wall": list(range(8))})

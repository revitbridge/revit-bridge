"""Metadata on the effective pack, and the reviewed API surface of each built-in reader."""
import asyncio
import json
import re

import pytest
import yaml

from revit_bridge import mcp_server
from revit_bridge.capabilities.schema import validate_pack
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.evidence.ledger import Ledger
from revit_bridge.execution import run_pack
from revit_bridge.spec.gate import Gate


READERS = {"count_by_level", "elements_in_link", "list_types", "query_levels", "query_model_stats",
           "list_elements", "get_element_geometry", "get_element_parameters"}
READ_CALLS = set("""
    Add AsDouble AsElementId AsInteger AsString AsValueString Cast ComputeDerivatives Contains
    ConvertFromInternalUnits Count CreateEqualsRule CrossProduct Equals Evaluate Except FirstOrDefault
    GetCategory GetDataType GetElement GetElementCount GetElementIdIterator GetElementIds GetEndPoint
    GetLength GetLinkDocument GetName GetTypeId GroupBy IsDefined Join Min MoveNext Normalize OfCategory
    OfClass OfType OrderBy OrderByDescending Parse Round Select Split StartsWith Sum Take ThenBy ToList
    ToString Trim TryGetValue TryParse Where WhereElementIsElementType WhereElementIsNotElementType
    WherePasses get_Parameter
""".split())
READ_CONSTRUCTORS = set("""
    Dictionary ElementId ElementLevelFilter ElementParameterFilter FilteredElementCollector HashSet
    InvalidOperationException List LogicalOrFilter UIDocument double
""".split())


@pytest.mark.parametrize("name", sorted(READERS))
def test_declared_reader_calls_only_reviewed_read_apis(tmp_path, name):
    store = ToolStore(user_dir=tmp_path)
    pack = store.load(name)
    assert pack.read_only is True
    for fixture in pack.fixtures or [{"params": {}}]:
        code, errors = store.render(name, fixture["params"])
        assert not errors
        source = re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"', "", code, flags=re.S)
        calls = set(re.findall(r"\.\s*(\w+)\s*(?:<[\w ,]+>)?\s*\(", source))
        assert not calls - READ_CALLS, calls - READ_CALLS
        local_collections = set(re.findall(r"\bvar\s+(\w+)\s*=\s*new\s+(?:List|HashSet|Dictionary)<", source))
        for call in re.finditer(r"\.\s*Add\s*\(", source):
            receiver = re.search(r"(?<![.\w])([A-Za-z_]\w*)\s*$", source[:call.start()])
            assert receiver and receiver[1] in local_collections
        assert not set(re.findall(r"\bnew\s+(\w+)", source)) - READ_CONSTRUCTORS
        assert not re.search(r"\.\s*\w+\s*(?:[+*/-]?=(?!=|>)|\+\+|--)", source)


def test_store_listing_resource_and_overrides_expose_the_effective_declaration(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    store = ToolStore(user_dir=user)
    monkeypatch.setattr(mcp_server, "_tool_store", store)
    def listing():
        return {t["name"]: t["read_only"] for t in json.loads(mcp_server.list_tools())}
    assert {name for name, marked in listing().items() if marked} == READERS
    assert {p.name for p in store.list_tools() if p.read_only} == READERS
    for p in store.list_tools():
        assert yaml.safe_load(mcp_server.tool_resource(p.name))["read_only"] is (p.name in READERS)
    for name in ("list_elements", "get_element_geometry", "get_element_parameters"):
        assert store.load(name).description.startswith("Read only. ")
    yaml_text = "name: query_levels\ncode_template: |\n  // example\n  return 1;\n"
    path = user / "query_levels.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    assert store.path_of("query_levels") == path
    assert listing()["query_levels"] is False
    assert mcp_server.tool_resource("query_levels") == "read_only: false\n" + yaml_text
    path.write_text(yaml_text + "read_only: true\n", encoding="utf-8")
    assert store.load("query_levels").read_only is True and store.path_of("query_levels") == path
    updated = store.update("query_levels", {"description": "New label"})
    assert updated.read_only is True and yaml.safe_load(path.read_text(encoding="utf-8"))["read_only"] is True


@pytest.mark.parametrize("value", [None, 1, "false", "true", []])
def test_read_only_accepts_only_a_boolean(tmp_path, value):
    store = ToolStore(user_dir=tmp_path)
    data = yaml.safe_load(store.path_of("query_levels").read_text(encoding="utf-8"))
    data["read_only"] = value
    assert any("read_only" in error for error in validate_pack(data))
    (tmp_path / "query_levels.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    assert store.load("query_levels").read_only is False


@pytest.mark.parametrize("user_override", [False, True])
def test_read_only_declaration_keeps_the_normal_execution_gate(tmp_path, user_override):
    store = ToolStore(user_dir=tmp_path)
    if user_override:
        (tmp_path / "query_levels.yaml").write_text("name: query_levels\nread_only: true\ncode_template: return 1;\n",
                                                   encoding="utf-8")
    assert store.load("query_levels").read_only is True
    result = asyncio.run(run_pack(store=store, gate=Gate(tmp_path / "pending"), ledger=Ledger(tmp_path / "evidence"),
                                  client=None, name="query_levels", params={}, token="", host="web"))
    assert result.error == "confirmation_required" and result.evidence_id is None

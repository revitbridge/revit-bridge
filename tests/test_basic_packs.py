"""Contracts for the fourteen basic modeling packs; no live Revit access."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from revit_bridge.capabilities.schema import validate_pack
from revit_bridge.capabilities.store import ToolStore, escape_param_value
from revit_bridge.revit.sandbox import review


ROOT = Path(__file__).resolve().parents[1] / "capabilities"
NAMES = (
    "create_level", "create_grid", "create_door", "create_window", "create_room",
    "create_roof_by_footprint", "create_ceiling", "set_parameter", "move_elements",
    "change_type", "delete_selected", "count_by_level", "list_types", "elements_in_link",
)
PLACEHOLDER = re.compile(r"\{[A-Za-z_]\w*\}")
# Every string parameter is inserted into an ordinary C# string literal. These
# templates use the JSON-compatible subset of C# escapes for user text.
STRING_LITERAL = re.compile(r'"(?:[^"\\\r\n]|\\["\\rn])*"')
HOSTILE_TEXT = 'A "quoted" name\\path\r\n"; return null; //'


def data_for(name):
    return yaml.safe_load((ROOT / f"{name}.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path):
    return ToolStore(user_dir=tmp_path / "user", builtin_dir=ROOT)


@pytest.mark.parametrize("name", NAMES)
def test_pack_loads_validates_and_renders_a_fixture(store, name):
    data = data_for(name)
    assert validate_pack(data) == []
    loaded = store.load(name)
    assert loaded is not None and loaded.name == name
    code, errors = store.render(name, data["fixtures"][0]["params"])
    assert errors == [] and code
    assert PLACEHOLDER.search(code) is None
    assert review(code) == (True, [])
    assert not re.search(r"new\s+(?:Sub)?Transaction\s*\(", code)


@pytest.mark.parametrize("name", NAMES)
def test_string_values_remain_complete_string_literals(store, name):
    data = data_for(name)
    for parameter in data["parameters"]:
        if parameter["type"] != "string":
            continue
        values = {**data["fixtures"][0]["params"], parameter["name"]: HOSTILE_TEXT}
        code, errors = store.render(name, values)
        assert errors == [] and code
        escaped = '"' + escape_param_value(HOSTILE_TEXT) + '"'
        literals = STRING_LITERAL.findall(code)
        assert escaped in literals, (name, parameter["name"])
        assert json.loads(escaped) == HOSTILE_TEXT
        assert PLACEHOLDER.search(code) is None


@pytest.mark.parametrize("name", [n for n in NAMES if n.startswith("create_")])
def test_creation_validators_measure_new_instances(name):
    validator = data_for(name)["validator"]
    assert validator["kind"] == "count_delta"
    assert validator["expected"] == 1
    assert validator["category"].startswith("OST_")


@pytest.mark.parametrize("name", ("count_by_level", "list_types", "elements_in_link"))
def test_queries_are_bounded_and_report_truncation(store, name):
    data = data_for(name)
    limit = next(p for p in data["parameters"] if p["name"] == "limit")
    assert limit["default"] == 100
    code, errors = store.render(name, data["fixtures"][0]["params"])
    assert errors == []
    assert "Math.Max(1, Math.Min(200," in code
    assert "Truncated =" in code
    assert data["validator"]["expected"] == 0
    assert not re.search(r"\b(?:Set|Delete|Create|ChangeTypeId|MoveElement)\s*\(", code)


@pytest.mark.parametrize("name", ("set_parameter", "move_elements", "change_type", "delete_selected"))
def test_selection_commands_reject_empty_and_excessive_selections(name):
    code = data_for(name)["code_template"]
    assert 'Selection.GetElementIds()' in code
    assert 'selected.Count == 0' in code
    assert 'selected.Count > 100' in code
    assert 'Status = "Error"' in code
    assert 'ElementIds =' in code and 'Count =' in code


def test_unavailable_verification_is_not_misrepresented():
    # The existing validators cannot prove a translation, an arbitrary parameter
    # name's new value, or deletion across mixed categories. Do not silently use
    # count_delta=0 or created_ids to claim those operations were verified.
    for name in ("set_parameter", "move_elements", "delete_selected"):
        assert data_for(name)["validator"] is None
    assert data_for("change_type")["validator"] == {
        "kind": "param_equals", "category": "{category}",
        "checks": [{"param_name": "ELEM_TYPE_PARAM", "spec_param": "type_id"}],
    }


def test_creation_model_parameters_declare_existing_choices():
    expected = {
        "create_door": {"wall_id": "elements:OST_Walls", "type_name": "family_types:OST_Doors"},
        "create_window": {"wall_id": "elements:OST_Walls", "type_name": "family_types:OST_Windows"},
        "create_room": {"level_name": "levels"},
        "create_roof_by_footprint": {"level_name": "levels", "type_name": "family_types:OST_Roofs"},
        "create_ceiling": {"level_name": "levels", "type_name": "family_types:OST_Ceilings"},
        "elements_in_link": {"link_id": "elements:OST_RvtLinks"},
    }
    for name, sources in expected.items():
        params = {p["name"]: p for p in data_for(name)["parameters"]}
        for parameter, source in sources.items():
            assert params[parameter]["choices_from"] == source
            assert params[parameter]["source"].startswith("tool:")


def test_numeric_lengths_are_millimeters():
    for name in NAMES:
        for parameter in data_for(name)["parameters"]:
            if parameter["type"] == "double":
                assert parameter["unit"] == "mm", (name, parameter["name"])


def test_preexisting_packs_still_load_and_render(store):
    existing = {
        "create_beam", "create_floor", "create_structural_column", "create_wall",
        "delete_elements_by_category", "modify_wall_height", "query_levels", "query_model_stats",
    }
    for name in existing:
        data = data_for(name)
        assert validate_pack(data) == []
        assert store.load(name).code_template == data["code_template"]
        for fixture in data["fixtures"]:
            code, errors = store.render(name, fixture["params"])
            assert errors == [] and code


# Execute the actual rendered snippet with a deliberately small, observable API
# stand-in. This checks mutation ordering, not Revit's transaction/type behavior.
CHANGE_TYPE_STUB = r'''
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;

public enum BuiltInCategory { OST_Walls = 1, OST_Doors = 2 }
public readonly record struct ElementId(long Value)
{
    public static readonly ElementId InvalidElementId = new(-1);
}
public sealed class Category
{
    public ElementId Id { get; set; } = new(1);
}
public class Element
{
    public ElementId Id { get; set; }
    public Category Category { get; set; } = new();
    public bool Pinned { get; set; }
    public bool ValidType { get; set; } = true;
    public bool CheckThrows { get; set; }
    public bool ChangeThrows { get; set; }
    public ElementId TypeId { get; set; } = new(101);
    public Document Owner { get; set; }
    public ElementId GetTypeId() => TypeId;
    public bool IsValidType(ElementId id)
    {
        Owner.Trace.Add("check:" + Id.Value);
        if (CheckThrows) throw new InvalidOperationException("type check failed");
        return ValidType;
    }
    public ElementId ChangeTypeId(ElementId id)
    {
        Owner.Trace.Add("change:" + Id.Value);
        Owner.Changes.Add(Id.Value);
        if (ChangeThrows) throw new InvalidOperationException("runtime change failed");
        TypeId = id;
        return ElementId.InvalidElementId;
    }
}
public sealed class ElementType : Element { }
public sealed class Document
{
    public Dictionary<long, Element> Elements = new();
    public List<ElementId> Selected = new() { new(1), new(2) };
    public List<long> Lookups = new();
    public List<long> Changes = new();
    public List<string> Trace = new();
    public Element GetElement(ElementId id)
    {
        Lookups.Add(id.Value);
        return Elements.TryGetValue(id.Value, out var element) ? element : null;
    }
}
public sealed class Selection
{
    private readonly Document document;
    public Selection(Document document) { this.document = document; }
    public ICollection<ElementId> GetElementIds() => document.Selected;
}
public sealed class UIDocument
{
    public Selection Selection { get; }
    public UIDocument(Document document) { Selection = new Selection(document); }
}
public static class Program
{
    public static object Execute(Document document)
    {
        // RENDERED_SNIPPET
    }
    public static void Main()
    {
        var reports = new Dictionary<string, object>();
        foreach (string name in new[] {
            "mixed", "mixed_first", "pinned", "pinned_first",
            "incompatible", "incompatible_first", "missing", "check_error",
            "already_target_pinned", "valid", "unchanged_and_valid", "runtime_error"
        })
        {
            var document = new Document();
            var first = new Element { Id = new(1), Owner = document };
            var second = new Element { Id = new(2), Owner = document };
            document.Elements[1] = first;
            document.Elements[2] = second;
            document.Elements[201] = new ElementType { Id = new(201), Owner = document };
            if (name.StartsWith("mixed")) second.Category.Id = new(2);
            if (name.StartsWith("pinned")) second.Pinned = true;
            if (name.StartsWith("incompatible")) second.ValidType = false;
            if (name == "missing") document.Elements.Remove(2);
            if (name == "check_error") second.CheckThrows = true;
            if (name == "already_target_pinned") { second.TypeId = new(201); second.Pinned = true; }
            if (name == "unchanged_and_valid") first.TypeId = new(201);
            if (name == "runtime_error") first.ChangeThrows = true;
            if (name.EndsWith("_first")) document.Selected.Reverse();
            reports[name] = new {
                Result = Execute(document), document.Changes, document.Trace, document.Lookups,
                Types = new[] { first.TypeId.Value, second.TypeId.Value }
            };
        }
        Console.WriteLine("REPORT:" + JsonSerializer.Serialize(reports));
    }
}
'''


@pytest.fixture(scope="module")
def change_type_reports(tmp_path_factory):
    dotnet = os.environ.get("REVIT_BRIDGE_TEST_DOTNET") or shutil.which("dotnet")
    if not dotnet:
        pytest.skip("The optional rendered-C# regression requires a .NET 8+ SDK.")
    directory = tmp_path_factory.mktemp("change_type_csharp")
    sdks = subprocess.run([dotnet, "--list-sdks"], cwd=directory, capture_output=True, text=True, check=True)
    majors = [int(value) for value in re.findall(r"^(\d+)\.", sdks.stdout, re.MULTILINE) if int(value) >= 8]
    if not majors:
        pytest.skip("The optional rendered-C# regression requires a .NET 8+ SDK.")
    # Framework reference packs ship with the SDK; prohibit package downloads.
    (directory / "NuGet.Config").write_text(
        '<configuration><packageSources><clear /></packageSources></configuration>', encoding="utf-8",
    )
    project = directory / "Regression.csproj"
    project.write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
        f'<TargetFramework>net{max(majors)}.0</TargetFramework>'
        '<Nullable>disable</Nullable></PropertyGroup></Project>', encoding="utf-8",
    )
    store = ToolStore(user_dir=directory / "user", builtin_dir=ROOT)
    code, errors = store.render("change_type", data_for("change_type")["fixtures"][0]["params"])
    assert errors == [] and code
    (directory / "Program.cs").write_text(CHANGE_TYPE_STUB.replace("// RENDERED_SNIPPET", code), encoding="utf-8")
    run = subprocess.run(
        [dotnet, "run", "--project", str(project), "--configuration", "Release", "--verbosity", "quiet"],
        cwd=directory, capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    report = next((line.removeprefix("REPORT:") for line in run.stdout.splitlines() if line.startswith("REPORT:")), None)
    assert report is not None, run.stdout + run.stderr
    return json.loads(report)


@pytest.mark.parametrize("scenario", (
    "mixed", "mixed_first", "pinned", "pinned_first", "incompatible", "incompatible_first",
    "missing", "check_error", "already_target_pinned",
))
def test_change_type_preflights_all_selected_elements_without_mutation(change_type_reports, scenario):
    report = change_type_reports[scenario]
    assert report["Result"]["Status"] == "Error"
    assert report["Result"]["Count"] == 0
    assert report["Result"]["ElementIds"] == []
    assert report["Result"]["Skipped"][0]["Id"] == 2
    assert report["Result"]["Skipped"][0]["Reason"]
    assert report["Changes"] == []
    assert {1, 2}.issubset(report["Lookups"])
    assert report["Types"] == [101, 201 if scenario == "already_target_pinned" else 101]


def test_change_type_preserves_valid_and_already_matching_inputs(change_type_reports):
    report = change_type_reports["valid"]
    assert report["Result"]["Status"] == "Modified"
    assert report["Result"]["ElementIds"] == [1, 2]
    assert report["Result"]["Count"] == 2
    assert report["Result"]["Skipped"] == []
    assert report["Trace"] == ["check:1", "check:2", "change:1", "change:2"]
    assert report["Types"] == [201, 201]
    matching = change_type_reports["unchanged_and_valid"]
    assert matching["Result"]["Status"] == "Modified"
    assert matching["Result"]["ElementIds"] == [2]
    assert matching["Changes"] == [2]
    assert matching["Types"] == [201, 201]


def test_change_type_reports_runtime_errors_and_continues_after_preflight(change_type_reports):
    report = change_type_reports["runtime_error"]
    assert report["Result"]["Status"] == "Error"
    assert report["Result"]["ElementIds"] == [2]
    assert report["Result"]["Count"] == 1
    assert report["Result"]["Skipped"] == [{"Id": 1, "Reason": "runtime change failed"}]
    assert report["Trace"] == ["check:1", "check:2", "change:1", "change:2"]
    assert report["Types"] == [101, 201]

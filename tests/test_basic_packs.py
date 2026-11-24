"""The fourteen basic modelling packs: format, rendering, refusal shape; no Revit.

What these tests cannot show: that the C# compiles and behaves in Revit. Every refusal
in these packs throws, because the add-in commits its transaction whenever a snippet
returns and rolls it back only when the snippet throws.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
import yaml

from revit_bridge import execution
from revit_bridge.capabilities.schema import validate_pack
from revit_bridge.capabilities.store import ToolStore, escape_param_value
from revit_bridge.evidence.ledger import Ledger
from revit_bridge.revit.client import RevitClient
from revit_bridge.revit.sandbox import review
from revit_bridge.snapshot.query import LEVELS_CODE
from revit_bridge.spec.gate import Gate
from revit_bridge.spec.models import Action, ParamBinding, Source, TaskSpec

from tests.fake_revit import FakeRevit

ROOT = Path(__file__).resolve().parents[1] / "capabilities"
NAMES = (
    "create_level", "create_grid", "create_door", "create_window", "create_room",
    "create_roof_by_footprint", "create_ceiling", "set_parameter", "move_elements",
    "change_type", "delete_selected", "count_by_level", "list_types", "elements_in_link",
)
PREEXISTING = (
    "create_beam", "create_floor", "create_structural_column", "create_wall",
    "delete_elements_by_category", "modify_wall_height", "query_levels", "query_model_stats",
)
CREATES = {
    "create_level": "OST_Levels", "create_grid": "OST_Grids", "create_door": "OST_Doors",
    "create_window": "OST_Windows", "create_room": "OST_Rooms", "create_roof_by_footprint": "OST_Roofs",
    "create_ceiling": "OST_Ceilings",
}
SELECTION = ("set_parameter", "move_elements", "change_type", "delete_selected")
QUERIES = ("count_by_level", "list_types", "elements_in_link")
# The years the add-in builds (no 2022) from 2021 on; 2020 has no UnitTypeId.
ALL_YEARS = ["2021", "2023", "2024", "2025", "2026", "2027"]
FIRST_YEAR = {
    "create_ceiling": "2023",      # Ceiling.Create is 2022+, and there is no 2022 add-in
    "set_parameter": "2023",       # Definition.GetDataType, SpecTypeId.Boolean are 2022+
    "change_type": "2024",         # its param_equals validator reads ids with ElementId.Value
}
MARKER = "REFUSED: "
THROW = "throw new InvalidOperationException("
# Revit exceptions a pack may catch: only to throw them on as a refusal of the designer's input.
CAUGHT = ("Autodesk.Revit.Exceptions.ArgumentException", "Autodesk.Revit.Exceptions.InvalidOperationException")
# Throws that are failures of the pack (it tried and the outcome is wrong), by the start of their
# message; every other throw is a refusal and carries the marker.
FAILURES = {
    "create_door": ['"Revit returned no door', '"Revit did not place the door'],
    "create_window": ['"Revit returned no window', '"Revit did not place the window',
                      "\"The window's sill height cannot be set", '"Revit refused the sill height',
                      '"Revit did not keep the sill height'],
    "create_room": ['"Revit returned no room', "\"The room's name parameter cannot be set"],
    "create_roof_by_footprint": ['"Revit did not create the roof'],
    "create_ceiling": ['"Revit returned no ceiling', "\"The ceiling's height above the level cannot be set",
                       '"Revit refused the height', '"Revit did not keep the height'],
    "set_parameter": ['"Element " + idOf(step.Item1) + " did not keep the value'],
    "move_elements": ['"Element " + idOf(id) + " cannot be found after the move'],
    "change_type": ['"Element " + idOf(id) + " did not take type'],
    "delete_selected": ['"Revit did not delete '],
}

PLACEHOLDER = re.compile(r"\{[A-Za-z_]\w*\}")
STRING_LITERAL = re.compile(r'"(?:[^"\\\r\n]|\\["\\rn])*"')
HOSTILE_TEXT = 'A "quoted" name\\path\r\n"; return null; //'
# Calls that change the model; a read-only query uses none of them.
MUTATIONS = re.compile(r"\b(?:Set|Delete|Create|NewFamilyInstance|NewRoom|NewFootPrintRoof|ChangeTypeId"
                       r"|MoveElements?|Activate|set_\w+)\s*\(|\.Name\s*=[^=]")


def data_for(name: str) -> dict:
    return yaml.safe_load((ROOT / f"{name}.yaml").read_text(encoding="utf-8"))


def code_of(name: str) -> str:
    return data_for(name)["code_template"]


@pytest.fixture
def store(tmp_path):
    return ToolStore(user_dir=tmp_path / "user", builtin_dir=ROOT)


def rendered(store: ToolStore, name: str) -> list[str]:
    out = []
    for fixture in data_for(name)["fixtures"]:
        code, errors = store.render(name, fixture["params"])
        assert errors == [] and code, (name, fixture["name"], errors)
        out.append(code)
    return out


def blocks_after(code: str, keyword: str) -> list[str]:
    """The brace-delimited body following each ``keyword (...)`` in ``code``."""
    bodies = []
    for match in re.finditer(rf"\b{keyword}\s*\(", code):
        start = code.index("{", match.end())
        depth, index = 0, start
        while True:
            depth += {"{": 1, "}": -1}.get(code[index], 0)
            if depth == 0:
                break
            index += 1
        bodies.append(code[start:index + 1])
    return bodies


# -- the listing and the format ----------------------------------------------------------------

def test_the_listing_shows_the_fourteen_packs_next_to_the_eight_earlier_ones():
    store = ToolStore()                       # the package's own built-in directory
    listed = {tool.name for tool in store.list_tools()}
    assert set(NAMES) <= listed and set(PREEXISTING) <= listed
    assert len(listed) == len(NAMES) + len(PREEXISTING)
    for name in NAMES:
        assert store.path_of(name) == store.builtin_dir / f"{name}.yaml"


@pytest.mark.parametrize("name", NAMES)
def test_pack_is_valid_v1_and_every_fixture_renders_without_placeholders(store, name):
    data = data_for(name)
    assert validate_pack(data) == []
    assert data["schema_version"] == 1 and data["version"] == "1.0.0" and data["name"] == name
    loaded = store.load(name)
    assert loaded is not None and loaded.code_template == data["code_template"]
    assert data["fixtures"], name
    for code in rendered(store, name):
        assert PLACEHOLDER.search(code) is None
        assert review(code) == (True, [])


@pytest.mark.parametrize("name", NAMES)
def test_every_value_enters_the_code_inside_a_string_literal(name):
    """Numbers too: the snippet parses them, so an odd value is a refusal, not C#."""
    code = code_of(name)
    for param in data_for(name)["parameters"]:
        token = "{" + param["name"] + "}"
        assert code.count(token) >= 1, (name, token)
        assert code.count(token) == code.count('"' + token + '"'), (name, token)


@pytest.mark.parametrize("name", [n for n in NAMES if any(p["type"] == "string" for p in data_for(n)["parameters"])])
def test_text_with_quotes_backslashes_and_line_breaks_stays_one_string_literal(store, name):
    data = data_for(name)
    for param in data["parameters"]:
        if param["type"] != "string":
            continue
        values = {**data["fixtures"][0]["params"], param["name"]: HOSTILE_TEXT}
        code, errors = store.render(name, values)
        assert errors == [] and code
        escaped = '"' + escape_param_value(HOSTILE_TEXT) + '"'
        assert escaped in STRING_LITERAL.findall(code), (name, param["name"])
        assert json.loads(escaped) == HOSTILE_TEXT
        assert PLACEHOLDER.search(code) is None


def test_odd_numbers_stay_inside_literals_and_non_numbers_are_refused_before_revit(store):
    params = {"name": "B", "start_x": " 12 ", "start_y": "1e3", "end_x": -0.5, "end_y": 3000.0}
    code, errors = store.render("create_grid", params)
    assert errors == []
    for text in ('" 12 "', '"1e3"', '"-0.5"', '"3000.0"'):
        assert text in code
    code, errors = store.render("create_grid", {**params, "end_y": '1"); document.Delete(x); //'})
    assert code is None and errors


# -- refusals throw ---------------------------------------------------------------------------

LOCAL_FUNCTION = re.compile(r"^[A-Za-z_][\w<>,\[\]]*\s+[A-Z]\w*\([^;]*\)\s*\n\{", re.MULTILINE)


def helper_spans(code: str) -> list[tuple[int, int]]:
    """Where the snippet's lambdas and local functions are: a ``return`` there returns to the snippet."""
    spans = []
    starts = [m.end() - 1 for m in LOCAL_FUNCTION.finditer(code)]
    starts += [m.end() - 1 for m in re.finditer(r"=>\s*\n\s*\{", code)]
    for start in starts:
        depth, index = 0, start
        while True:
            depth += {"{": 1, "}": -1}.get(code[index], 0)
            if depth == 0:
                break
            index += 1
        spans.append((start, index))
    return spans


@pytest.mark.parametrize("name", NAMES)
def test_every_refusal_throws_and_the_snippet_returns_only_its_result(name):
    code = code_of(name)
    assert THROW in code
    assert 'Status = "Error"' not in code
    spans = helper_spans(code)
    returns = [m.start() for m in re.finditer(r"\breturn\b", code)
               if not any(start < m.start() < end for start, end in spans)]
    assert len(returns) == 1, (name, [code[r:r + 40] for r in returns])
    assert code[returns[0]:].startswith("return new") and code[:returns[0]].endswith("\n"), name
    statuses = re.findall(r'Status = "(\w+)"', code)
    assert len(statuses) == 1 and statuses[0] in {"Created", "Changed", "Moved", "Deleted", "Ok"}, (name, statuses)


@pytest.mark.parametrize("name", NAMES)
def test_refusals_carry_the_marker_and_failures_do_not(name):
    """Refusal: nothing changed, the designer can fix the input. Failure: the pack tried and the
    outcome is wrong. The classification of every throw site is listed in FAILURES."""
    code = code_of(name)
    refusals, failures = 0, []
    for match in re.finditer(re.escape(THROW), code):
        argument = code[match.end():]
        if argument.startswith('"' + MARKER):
            refusals += 1
        else:
            failures.append(next((f for f in FAILURES.get(name, []) if argument.startswith(f)), argument[:60]))
    assert refusals >= 1, name
    assert failures == FAILURES.get(name, []), (name, failures)
    # The marker opens a message and is never built in the middle of one (the header comment names it once).
    assert code.count(MARKER) == refusals + 1, name
    assert '"REFUSED: "' in "\n".join(code.split("\n")[:4]), name


@pytest.mark.parametrize("name", NAMES)
def test_no_catch_swallows_an_error(name):
    """Only Revit's own refusals of designer input are caught, and each is thrown on as a refusal."""
    code = code_of(name)
    for caught in re.findall(r"\bcatch\s*\(([^)]*)\)", code):
        assert caught.split()[0] in CAUGHT, (name, caught)
    for body in blocks_after(code, "catch"):
        assert body.strip("{} \n").startswith(THROW + '"' + MARKER), (name, body)
        assert body.count(";") == 1, (name, body)                  # nothing but the rethrow
    assert not re.search(r"\bcatch\s*\{", code)


@pytest.mark.parametrize("name, call, caught", [
    # API docs: MoveElements throws ArgumentException and InvalidOperationException for a selection it
    # cannot move; Delete documents ArgumentException ("cannot be deleted") - its ModificationForbidden-
    # Exception stays a failure; Parameter.Set throws InvalidOperationException (read-only) and, for a
    # double, ArgumentException. ArgumentNullException (a null argument: a pack bug) is let through.
    ("move_elements", "ElementTransformUtils.MoveElements(", ["ArgumentException", "InvalidOperationException"]),
    ("delete_selected", "document.Delete(", ["ArgumentException"]),
    ("set_parameter", "step.Item2.Set(", ["ArgumentException", "InvalidOperationException"]),
])
def test_revit_refusing_the_designers_input_is_a_refusal(name, call, caught):
    code = code_of(name)
    tries = [m.end() for m in re.finditer(r"\btry\s*\n\s*\{", code)]
    assert len(tries) == 1, name
    after = code[tries[0]:]
    assert call in after[:after.index("catch")], name
    assert re.findall(r"catch \(Autodesk\.Revit\.Exceptions\.(\w+) ex\)", after) == caught, name
    for match in re.finditer(r"catch \(Autodesk\.Revit\.Exceptions\.ArgumentException ex\)", after):
        assert after[match.end():].startswith(" when (!(ex is Autodesk.Revit.Exceptions.ArgumentNullException))"), name


def test_change_type_refuses_before_revit_2024():
    code = code_of("change_type")
    guard = code.index("document.Application.VersionNumber")
    assert guard < code.index("new UIDocument(document)") and guard < code.index("ChangeTypeId(")
    assert "revitYear < 2024" in code
    assert THROW + '"REFUSED: This tool needs Revit 2024 or later; this is Revit " + revitYear' in code


@pytest.mark.parametrize("name", NAMES)
def test_no_transaction_and_no_ui(name):
    code = code_of(name)
    assert not re.search(r"\b(?:Transaction|SubTransaction|TransactionGroup)\s*\(", code)
    assert "TaskDialog" not in code and "PickObject" not in code and "MessageBox" not in code


# -- units, ids and Revit years ------------------------------------------------------------------

@pytest.mark.parametrize("name", NAMES)
def test_lengths_are_mm_and_converted_with_the_unit_api(name):
    data = data_for(name)
    code = data["code_template"]
    doubles = [p for p in data["parameters"] if p["type"] == "double"]
    for param in doubles:
        assert param["unit"] == "mm" and "(mm)" in param["description"], (name, param["name"])
    if doubles:
        assert "UnitUtils.ConvertToInternalUnits(" in code and "UnitTypeId.Millimeters" in code
    assert "304.8" not in code and "DisplayUnitType" not in code and "ParameterType" not in code


@pytest.mark.parametrize("name", NAMES)
def test_element_ids_compile_on_every_declared_year(name):
    """``ElementId.Value`` and ``IntegerValue`` each exist on only part of 2021-2027: ids are read with
    ``ToString`` and built from an ``int`` between the CS0618 pragmas."""
    code = code_of(name)
    assert not re.search(r"\.Value\b", code) and "IntegerValue" not in code
    lines = code.splitlines()
    for index, line in enumerate(lines):
        if "new ElementId(" in line and "new ElementId(category)" not in line:
            assert lines[index - 1].strip() == "#pragma warning disable 618", (name, line)
            assert lines[index + 1].strip() == "#pragma warning restore 618", (name, line)


@pytest.mark.parametrize("name", NAMES)
def test_declared_revit_years(name):
    data = data_for(name)
    first = FIRST_YEAR.get(name, "2021")
    assert data["revit_versions"] == [year for year in ALL_YEARS if year >= first]
    assert "2022" not in data["revit_versions"]                    # the add-in has no 2022 build
    if name in FIRST_YEAR:
        said = f"Revit {first} or later"
        assert said in data["description"], name
        assert any(said in item.get("text", "") for item in data["preconditions"]), name


@pytest.mark.parametrize("name", NAMES)
def test_heights_against_a_level_use_its_project_elevation(name):
    """``Level.Elevation`` follows the level type's elevation base; a comparison with model coordinates
    or a placement uses ``ProjectElevation``. ``Elevation`` is only reported (``ElevationMm``)."""
    for line in code_of(name).splitlines():
        if re.search(r"\b\w+\.Elevation\b", line):
            assert "ElevationMm = " in line, (name, line)


@pytest.mark.parametrize("name", NAMES)
def test_messages_name_no_internal_enum_and_parameter_types_are_spelled_once(name):
    data = data_for(name)
    assert not re.search(r"\+\s*[\w.]*(?:Kind|PlacementType|StorageType)\s*\+", data["code_template"]), name
    # integer, as the earlier packs spell it (delete_elements_by_category, modify_wall_height)
    assert {p["type"] for p in data["parameters"]} <= {"string", "double", "integer"}, name


@pytest.mark.parametrize("name", ("create_door", "create_window", "create_room", "create_ceiling"))
def test_a_created_element_is_null_checked_before_use(name):
    code = code_of(name)
    created = re.search(r"^[\w.]+ (\w+) = (?=document\.Create\.NewFamilyInstance\(|document\.Create\.NewRoom\(|Ceiling\.Create\()",
                        code, re.MULTILINE)
    assert created, name
    after = code[created.end():]
    statement_end = after.index(";")
    assert after[statement_end:].lstrip(";\n ").startswith(f"if ({created.group(1)} == null)"), name


# -- parameters from the model ---------------------------------------------------------------------

def test_parameters_that_pick_from_the_model_offer_real_choices():
    expected = {
        "create_door": {"wall_id": "elements:OST_Walls", "type_name": "family_types:OST_Doors"},
        "create_window": {"wall_id": "elements:OST_Walls", "type_name": "family_types:OST_Windows"},
        "create_room": {"level_name": "levels"},
        "create_roof_by_footprint": {"level_name": "levels", "type_name": "family_types:OST_Roofs"},
        "create_ceiling": {"level_name": "levels", "type_name": "family_types:OST_Ceilings"},
        "elements_in_link": {"link_id": "elements:OST_RvtLinks"},
    }
    for name in NAMES:
        for param in data_for(name)["parameters"]:
            wanted = expected.get(name, {}).get(param["name"])
            if wanted:
                assert param["choices_from"] == wanted and param["source"].startswith("tool:"), (name, param)
            elif param["name"].endswith(("_id", "level_name", "type_name")):
                # The planner's ruling: change_type.type_id is the designer's id from list_types.
                assert (name, param["name"]) == ("change_type", "type_id"), (name, param["name"])
            else:
                assert "choices_from" not in param, (name, param["name"])


# -- validators ------------------------------------------------------------------------------------

def test_validators_prove_creation_type_change_and_read_only():
    for name, category in CREATES.items():
        assert data_for(name)["validator"] == {"kind": "count_delta", "category": category, "expected": 1}
    assert data_for("change_type")["validator"] == {
        "kind": "param_equals", "category": "{category}",
        "checks": [{"param_name": "ELEM_TYPE_PARAM", "spec_param": "type_id"}],
    }
    for name in QUERIES:
        validator = data_for(name)["validator"]
        assert validator["kind"] == "count_delta" and validator["expected"] == 0
    # No built-in validator proves an arbitrary parameter, a move or a mixed deletion (planner: open item).
    for name in ("set_parameter", "move_elements", "delete_selected"):
        data = data_for(name)
        assert data["validator"] is None
        assert "there is no validator" in data["description"]


# -- selections and queries --------------------------------------------------------------------------

@pytest.mark.parametrize("name", SELECTION)
def test_selection_tools_refuse_an_empty_or_large_selection_and_report_ids(name):
    code = code_of(name)
    assert "new UIDocument(document).Selection.GetElementIds()" in code
    assert "if (selected.Count == 0)\n    throw new InvalidOperationException(\"REFUSED: Nothing is selected in Revit." in code
    assert "const int MaxSelected = 100;" in code and "if (selected.Count > MaxSelected)" in code
    assert "ElementIds =" in code and "Count =" in code
    # every element is checked before the first change
    assert code.index("problems.Count > 0") < min(
        code.index(call) for call in (".Set(", "MoveElements(", "ChangeTypeId(", "document.Delete(") if call in code)


def test_selection_tools_refuse_what_would_reach_beyond_the_selection():
    # A type would change or delete every element of it; a level would take everything on it along.
    for name, refused in (("set_parameter", ("element is ElementType",)),
                          ("delete_selected", ("element is ElementType", "element is View", "element is Level")),
                          ("move_elements", ("element is ElementType", "element is View", "element is Level"))):
        code = code_of(name)
        for check in refused:
            assert f"if ({check})" in code, (name, check)
    assert "element is Grid" not in code_of("move_elements")         # grids move, in plan


def test_move_asks_for_every_distance():
    for param in data_for("move_elements")["parameters"]:
        assert param["source"] == "designer" and param["required"] is True and "default" not in param
    assert 'REFUSED: The move is zero' in code_of("move_elements")


def test_a_window_sill_at_or_above_the_wall_top_is_refused_without_the_type_height():
    code = code_of("create_window")
    assert "if (wallBox != null && bottom >= wallBox.Max.Z - tolerance)" in code


@pytest.mark.parametrize("name", QUERIES)
def test_queries_are_read_only_bounded_and_report_truncation(store, name):
    data = data_for(name)
    limit = next(p for p in data["parameters"] if p["name"] == "limit")
    assert limit["default"] == 100 and limit["required"] is False
    code = data["code_template"]
    assert "const int MaxRows = 200;" in code and "limit = Math.Min(limit, MaxRows);" in code
    assert "Truncated =" in code
    assert not MUTATIONS.search(code), MUTATIONS.search(code)


def test_creation_and_change_tools_do_change_the_model():
    for name in tuple(CREATES) + SELECTION:
        assert MUTATIONS.search(code_of(name)), name


# -- the earlier packs ---------------------------------------------------------------------------------

def test_the_eight_earlier_packs_still_load_and_render(store):
    for name in PREEXISTING:
        data = data_for(name)
        assert validate_pack(data) == []
        assert store.load(name).code_template == data["code_template"]
        for fixture in data["fixtures"]:
            code, errors = store.render(name, fixture["params"])
            assert errors == [] and code


# -- through the execution flow with a fake add-in ----------------------------------------------------

TAKEN = 'A level named "L3" already exists. Choose another name.'
LEVEL = {"name": "L3", "elevation": 6000}


def token_for(gate: Gate, tool: str, params: dict) -> str:
    spec = TaskSpec(task=tool, action=Action(kind="run_tool", tool=tool),
                    parameters=[ParamBinding(name=k, value=v, source=Source.answer, evidence=f"q_{k}")
                                for k, v in params.items()])
    return gate.issue(spec).token


def fake_addin(pack_reply, counts=(4, 5)):
    """A fake add-in with one document; the pack's code gets ``pack_reply(request_id)``."""
    state = {"ran": False}

    def handler(request):
        rid, method = request["id"], request["method"]
        if method != "send_code_to_revit":
            return FakeRevit.default_handler(request)
        code = request["params"]["code"]
        if code == execution.DOCUMENT_PROBE:
            return FakeRevit.code_result(rid, {"Title": "Project1", "RevitVersion": "2026"})
        if code == LEVELS_CODE:
            return FakeRevit.code_result(rid, [{"Id": 1, "Name": "L1", "ElevationMm": 0.0}])
        if "CategoryNames" in code:                                  # the snapshot block
            return FakeRevit.code_result(rid, {
                "Document": {"Title": "Project1", "RevitVersion": "2026", "IsWorkshared": False},
                "Levels": [{"Id": 1, "Name": "L1", "ElevationMm": 0.0}], "CategoryNames": {}, "Warnings": []})
        if 'whose message starts with "REFUSED: "' in code:         # the pack itself
            state["ran"] = True
            return pack_reply(rid)
        if "GetElementCount" in code:                                # count_delta's probe
            return FakeRevit.code_result(rid, counts[1] if state["ran"] else counts[0])
        raise AssertionError(f"unexpected code: {code[:80]}")
    return handler


class Host:
    """One host's store, gate and ledger, kept across runs."""

    def __init__(self, tmp_path):
        self.store = ToolStore(user_dir=tmp_path / "user", builtin_dir=ROOT)
        self.gate, self.ledger = Gate(tmp_path / "pending"), Ledger(tmp_path / "evidence")

    def run(self, tool: str, params: dict, handler):
        token = token_for(self.gate, tool, params)

        async def go():
            async with FakeRevit(handler) as fake:
                client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
                try:
                    return await execution.run_pack(store=self.store, gate=self.gate, ledger=self.ledger, client=client,
                                                    name=tool, params=dict(params), token=token, host="web")
                finally:
                    await client.disconnect()
        return asyncio.run(go()), token


def thrown(exception: str, message: str):
    return lambda rid: FakeRevit.thrown(rid, exception, message)


def test_a_refusal_is_refused_not_failed_and_leaves_the_counters_alone(tmp_path):
    host = Host(tmp_path)
    result, token = host.run("create_level", LEVEL, fake_addin(thrown("System.InvalidOperationException", MARKER + TAKEN)))
    assert result.success is False and result.refused is True
    assert result.error == TAKEN                                      # the text after the marker, for the designer
    assert result.hint is None and result.validation is None and result.result is None
    assert host.gate.peek(token).used_at                              # it reached Revit
    tool = host.store.load("create_level")
    assert tool.failure_count == 0 and tool.execution_count == 0
    line = host.ledger.get(result.evidence_id)
    assert line["refused"] is True and line["success"] is False and line["error"] == TAKEN
    assert result.model_dump()["refused"] is True


def test_three_refusals_in_a_row_do_not_make_the_pack_unhealthy(tmp_path):
    host = Host(tmp_path)
    for _ in range(3):
        result, _ = host.run("create_level", LEVEL, fake_addin(thrown("System.InvalidOperationException", MARKER + TAKEN)))
        assert result.refused is True
    assert host.store.load("create_level").failure_count == 0
    assert host.store.health_check("create_level")["recommendation"] == "use_tool"
    created = lambda rid: FakeRevit.code_result(rid, {"Status": "Created", "ElementId": 4242, "Name": "L3"})
    result, _ = host.run("create_level", LEVEL, fake_addin(created))  # the pack still runs
    assert result.success is True and result.validation.passed is True
    assert host.store.load("create_level").execution_count == 1


@pytest.mark.parametrize("exception, message", [
    ("System.InvalidOperationException", "Revit did not place the door in wall 101; nothing was created."),
    ("System.InvalidOperationException", "Sequence contains no elements " + MARKER + TAKEN),   # marker not at the start
    ("Autodesk.Revit.Exceptions.InvalidOperationException", MARKER + TAKEN),                # not the pack's exception
    ("System.ArgumentException", MARKER + TAKEN),
])
def test_any_other_exception_still_counts_as_a_failure(tmp_path, exception, message):
    host = Host(tmp_path)
    result, _ = host.run("create_level", LEVEL, fake_addin(thrown(exception, message)))
    assert result.success is False and result.refused is False and "refused" not in result.model_dump()
    assert result.error == FakeRevit.THROWN_PREFIX + exception + ": " + message      # as before
    assert result.hint is not None and execution.RETRY_HINT in result.hint
    assert host.store.load("create_level").failure_count == 1
    assert "refused" not in host.ledger.get(result.evidence_id)


def test_a_created_level_passes_its_count_delta(tmp_path):
    host = Host(tmp_path)
    created = lambda rid: FakeRevit.code_result(rid, {"Status": "Created", "ElementId": 4242, "Name": "L3"})
    result, _ = host.run("create_level", LEVEL, fake_addin(created))
    assert result.success is True and result.error is None and "refused" not in result.model_dump()
    assert result.validation.validator == "count_delta" and result.validation.passed is True
    assert host.store.load("create_level").execution_count == 1


def test_an_empty_selection_is_a_refusal_for_a_pack_without_validator(tmp_path):
    message = "Nothing is selected in Revit. Select the elements first, then run the tool again."
    host = Host(tmp_path)
    result, _ = host.run("delete_selected", {}, fake_addin(thrown("System.InvalidOperationException", MARKER + message)))
    assert result.refused is True and result.error == message and result.validation is None
    assert host.store.load("delete_selected").failure_count == 0

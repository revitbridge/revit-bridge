"""Model queries shared by the MCP server and the web host: choices, units."""
from __future__ import annotations

import asyncio

from revit_bridge.capabilities.store import ToolStore, escape_param_value
from revit_bridge.revit.client import RevitClient
from revit_bridge.snapshot.query import RevitQueryExecutor, detect_length_unit

from tests.fake_revit import FakeRevit


def _run(handler, scenario):
    async def go():
        async with FakeRevit(handler) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            try:
                return await scenario(RevitQueryExecutor(client), fake)
            finally:
                await client.disconnect()
    return asyncio.run(go())


def test_tool_choices_cover_every_source_kind():
    def handler(request):
        rid, method = request["id"], request["method"]
        if method == "send_code_to_revit":
            code = request["params"]["code"]
            if "typeof(Level)" in code:
                return FakeRevit.code_result(rid, [{"Id": 1, "Name": "L1", "ElevationMm": 0.0}])
            if "typeof(FloorType)" in code:
                return FakeRevit.code_result(rid, [{"Name": "Generic 150mm", "Id": 7}])
            if "OST_Walls" in code:
                return FakeRevit.code_result(rid, [{"Id": 99, "Name": "Basic Wall"}])
        return FakeRevit.default_handler(request)

    async def scenario(executor, fake):
        return await executor.get_tool_choices([
            {"name": "level", "choices_from": "levels"},
            {"name": "col", "choices_from": "family_types:OST_StructuralColumns"},
            {"name": "floor", "choices_from": "floor_types"},
            {"name": "wall", "choices_from": "elements:OST_Walls"},
            {"name": "bad", "choices_from": "elements:OST_Walls; DROP"},
            {"name": "unknown", "choices_from": "nothing"},
        ])

    choices = _run(handler, scenario)
    assert choices["level"] == [{"label": "L1 (0.0mm)", "value": "L1"}]
    assert choices["col"] == [{"label": "OST_StructuralColumns-TypeA", "value": "OST_StructuralColumns-TypeA"}]
    assert choices["floor"] == [{"label": "Generic 150mm", "value": "Generic 150mm"}]
    assert choices["wall"] == [{"label": "Basic Wall (ID: 99)", "value": 99}]
    assert choices["bad"] == []       # category failed the OST_ regex: never interpolated
    assert choices["unknown"] == []


def test_project_units_detection():
    def handler(request):
        if request["method"] == "send_code_to_revit":
            return FakeRevit.code_result(request["id"], {
                "LengthUnit": "autodesk.unit.unit:millimeters-1.0.1",
                "DisplayName": "Millimeters",
            })
        return FakeRevit.default_handler(request)

    units = _run(handler, lambda ex, fake: ex.get_project_units())
    assert units == {
        "revit_unit": "autodesk.unit.unit:millimeters-1.0.1",
        "display_name": "Millimeters",
        "detected": "mm",
    }

    def failing(request):
        return FakeRevit.code_result(request["id"], None, success=False, error="boom")

    assert _run(failing, lambda ex, fake: ex.get_project_units()) == {"error": "boom"}

    assert detect_length_unit("autodesk.unit.unit:meters-1.0.1") == "m"
    assert detect_length_unit("autodesk.unit.unit:feetFractionalInches-1.0.1") == "feet"
    assert detect_length_unit("", "Millimeters") == "mm"


def test_rendered_parameters_cannot_break_out_of_string_literals(tmp_path):
    assert escape_param_value('a"b\\c\nd') == 'a\\"b\\\\c\\nd'
    assert escape_param_value(3000) == "3000"

    store = ToolStore(tmp_path)
    store.solidify(
        name="probe",
        code='var name = "{level_name}"; return name;',
        parameters=[{"name": "level_name", "type": "string", "source": "ask_user"}],
    )
    code = store.render_code("probe", {"level_name": 'L1"; System.IO.File.Delete("x"); var z = "'})
    assert code == 'var name = "L1\\"; System.IO.File.Delete(\\"x\\"); var z = \\""; return name;'

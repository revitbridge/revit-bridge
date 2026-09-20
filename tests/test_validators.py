"""The three built-in validators against the fake add-in."""
from __future__ import annotations

import asyncio

import pytest

from revit_bridge.revit.client import RevitClient
from revit_bridge.spec.models import Action, ParamBinding, Source, TaskSpec
from revit_bridge.validators.base import ValidatorError, extract_ids, resolve
from revit_bridge.validators.builtin import BUILTIN_VALIDATORS, CountDelta, CreatedIds, ParamEquals, get_validator

from tests.fake_revit import FakeRevit


def spec(**values) -> TaskSpec:
    return TaskSpec(task="t", action=Action(kind="run_tool", tool="probe"),
                    parameters=[ParamBinding(name=k, value=v, source=Source.answer, evidence="q") for k, v in values.items()])


def run(handler, coro_factory):
    async def go():
        async with FakeRevit(handler) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            try:
                return await coro_factory(client), fake
            finally:
                await client.disconnect()
    return asyncio.run(go())


# -- helpers ----------------------------------------------------------------------------

def test_extract_ids_and_param_references():
    assert extract_ids({"ElementId": 4242, "Status": "Created"}) == [4242]
    assert extract_ids({"Ids": [1, "2", 2, True, -3, 0], "nested": {"ElementIds": [7]}, "Id": 99}) == [1, 2, 7]
    assert extract_ids([{"ElementId": 5}, {"ElementId": 6}]) == [5, 6]
    assert extract_ids({"ids": [8]}) == [8]                       # the ledger summary key
    assert extract_ids("nothing") == [] and extract_ids(None) == []

    s = spec(quantity=3, category="OST_Doors")
    assert resolve("{quantity}", s) == 3 and resolve("{category}", s) == "OST_Doors"
    assert resolve(4, s) == 4 and resolve("OST_Walls", s) == "OST_Walls"
    with pytest.raises(ValidatorError, match="does not bind"):
        resolve("{missing}", s)
    assert set(BUILTIN_VALIDATORS) == {"created_ids", "count_delta", "param_equals"}
    with pytest.raises(ValidatorError, match="unknown validator kind"):
        get_validator("exists")


# -- count_delta ------------------------------------------------------------------------------

def counting(before: int, after: int):
    calls = {"n": 0}

    def handler(request):
        if request["method"] == "send_code_to_revit" and "GetElementCount" in request["params"]["code"]:
            calls["n"] += 1
            return FakeRevit.code_result(request["id"], before if calls["n"] == 1 else after)
        return FakeRevit.default_handler(request)
    return handler


def test_count_delta_passes_when_the_count_moved_as_expected():
    validator = CountDelta()
    cfg = {"kind": "count_delta", "category": "OST_Walls", "expected": 1}

    async def go(client):
        before = await validator.before(client, spec(), cfg)
        assert before == {"category": "OST_Walls", "count": 3}
        return await validator.after(client, spec(), cfg, before, {"Status": "Created", "ElementId": 1})

    report, fake = run(counting(3, 4), go)
    assert report.passed is True and report.validator == "count_delta"
    assert report.checks[0].detail == "OST_Walls: before 3, after 4, delta 1, expected 1"
    assert report.before == {"category": "OST_Walls", "count": 3}
    assert report.after == {"category": "OST_Walls", "count": 4, "delta": 1}
    probes = [r["params"]["code"] for r in fake.requests]
    assert all('Enum.Parse(typeof(BuiltInCategory), "OST_Walls")' in c for c in probes) and len(probes) == 2


def test_count_delta_fails_when_nothing_was_created():
    validator = CountDelta()
    cfg = {"kind": "count_delta", "category": "OST_Walls", "expected": 1}

    async def go(client):
        before = await validator.before(client, spec(), cfg)
        return await validator.after(client, spec(), cfg, before, {"Status": "Created", "ElementId": 1})

    report, _ = run(counting(3, 3), go)
    assert report.passed is False
    assert report.checks == [report.checks[0]] and report.checks[0].passed is False
    assert report.checks[0].detail == "OST_Walls: before 3, after 3, delta 0, expected 1"


def test_count_delta_resolves_param_references_and_reports_probe_failures():
    validator = CountDelta()
    cfg = {"kind": "count_delta", "category": "{category}", "expected": "{delta}"}
    s = spec(category="OST_Doors", delta=-2)

    async def go(client):
        before = await validator.before(client, s, cfg)
        return await validator.after(client, s, cfg, before, {"Status": "Deleted", "Count": 2})

    report, fake = run(counting(5, 3), go)
    assert report.passed is True and report.after["delta"] == -2
    assert '"OST_Doors"' in fake.requests[0]["params"]["code"]

    def failing(request):
        return FakeRevit.code_result(request["id"], None, success=False, error="no document")

    async def before_fails(client):
        return await validator.before(client, spec(), {"category": "OST_Walls", "expected": 1})

    with pytest.raises(ValidatorError, match="no document"):
        run(failing, before_fails)

    async def missing_before(client):
        return await validator.after(client, spec(), {"category": "OST_Walls", "expected": 1}, {}, {})

    report, _ = run(counting(1, 1), missing_before)
    assert report.passed is False and "no count was sampled" in report.checks[0].detail

    with pytest.raises(ValidatorError, match="OST_"):
        run(counting(1, 1), lambda c: validator.before(c, spec(), {"category": "Walls"}))


# -- created_ids --------------------------------------------------------------------------------

def rows_handler(rows):
    def handler(request):
        if request["method"] == "send_code_to_revit" and "Matches" in request["params"]["code"]:
            return FakeRevit.code_result(request["id"], rows)
        return FakeRevit.default_handler(request)
    return handler


def test_created_ids_checks_existence_and_category():
    validator = CreatedIds()
    cfg = {"kind": "created_ids", "category": "OST_Walls"}
    result = {"Status": "Created", "ElementId": 10, "Extra": {"Ids": [11]}}

    async def go(client):
        assert await validator.before(client, spec(), cfg) == {}
        return await validator.after(client, spec(), cfg, {}, result)

    rows = [{"Id": 10, "Exists": True, "Category": "Walls", "Matches": True},
            {"Id": 11, "Exists": True, "Category": "Walls", "Matches": True}]
    report, fake = run(rows_handler(rows), go)
    assert report.passed is True and [c.name for c in report.checks] == ["id 10", "id 11"]
    assert report.after == {"ids": [10, 11], "category": "OST_Walls"}
    assert "new long[] { 10L, 11L }" in fake.requests[-1]["params"]["code"]

    rows = [{"Id": 10, "Exists": False, "Category": None, "Matches": False},
            {"Id": 11, "Exists": True, "Category": "Doors", "Matches": False}]
    report, _ = run(rows_handler(rows), go)
    assert report.passed is False
    assert report.checks[0].detail == "id 10 does not exist"
    assert report.checks[1].detail == "id 11 exists, category 'Doors' (expected OST_Walls)"

    async def no_ids(client):
        return await validator.after(client, spec(), cfg, {}, {"Status": "Created"})

    report, fake = run(rows_handler([]), no_ids)
    assert report.passed is False and "reports no ElementId" in report.checks[0].detail
    assert fake.requests == []                                    # nothing to ask Revit


# -- param_equals -------------------------------------------------------------------------------

def test_param_equals_reads_back_values_with_length_tolerance():
    validator = ParamEquals()
    cfg = {"kind": "param_equals", "category": "OST_Walls",
           "checks": [{"param_name": "WALL_USER_HEIGHT_PARAM", "spec_param": "height"},
                      {"param_name": "Comments", "spec_param": "note"}]}
    s = spec(height=3000, note="n1")

    def handler(rows):
        def h(request):
            if request["method"] == "send_code_to_revit" and "LookupParameter" in request["params"]["code"]:
                return FakeRevit.code_result(request["id"], rows)
            return FakeRevit.default_handler(request)
        return h

    async def go(client):
        return await validator.after(client, s, cfg, {}, {"ElementId": 7, "Status": "Modified"})

    rows = [{"Id": 7, "Name": "WALL_USER_HEIGHT_PARAM", "Kind": "length_mm", "Value": 3000.4},
            {"Id": 7, "Name": "Comments", "Kind": "string", "Value": "n1"}]
    report, fake = run(handler(rows), go)
    assert report.passed is True
    assert report.checks[0].detail == "got 3000.4 mm, expected 3000 mm"
    code = fake.requests[-1]["params"]["code"]
    assert '"WALL_USER_HEIGHT_PARAM", "Comments"' in code and "Enum.TryParse(name, out bip)" in code

    rows = [{"Id": 7, "Name": "WALL_USER_HEIGHT_PARAM", "Kind": "length_mm", "Value": 3001.0},
            {"Id": 7, "Name": "Comments", "Kind": "none", "Value": None}]
    report, _ = run(handler(rows), go)
    assert report.passed is False
    assert report.checks[0].detail == "got 3001 mm, expected 3000 mm"
    assert report.checks[1].detail == "parameter not found on the element"

    with pytest.raises(ValidatorError, match="no parameter 'height'"):
        run(handler([]), lambda c: validator.after(c, spec(), cfg, {}, {"ElementId": 7}))

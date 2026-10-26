"""P9 read-back verification: plan_verification before an execute_code, verify_execution after it.

The fake add-in below keeps a tiny model (elements with a category and
parameters, and levels) and answers the bridge's fixed probes from it: the
document probe and its levels read, the count probe of count_delta, the
existence probe of created_ids and the read-back probe of param_equals. Any
other code is "the execution": it adds the elements (and levels) the
scenario says it creates.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

import revit_bridge.execution as execution
import revit_bridge.mcp_server as server
from revit_bridge.evidence.ledger import OPTIONAL_FIELDS, RECORD_FIELDS, Ledger
from revit_bridge.revit.client import RevitClient
from revit_bridge.revit.pool import RevitClientPool
from revit_bridge.snapshot.project import LevelInfo, fingerprint_of
from revit_bridge.snapshot.query import LEVELS_CODE
from revit_bridge.validators.builtin import _compare
from revit_bridge.verification import VerificationStore, check_template, plan, verify
from revit_bridge.verification.store import TTL_SECONDS, iso

from tests.fake_revit import FakeRevit
from tests.test_gate import (  # noqa: F401
    PROJECT1, WALL, _acall, _call, code_spec, counting_handler, isolated_store, revit_env)

WALLS_CODE = 'return new { Status = "Created", Ids = new long[] { 501, 502 } };'


class Model:
    """The fake add-in's model and handler (see module doc)."""

    def __init__(self, title: str = "Project1", existing: int = 3, creates: dict | None = None,
                 result=None, fail_execution: bool = False):
        self.title = title
        self.levels = [{"Id": 1, "Name": "L1", "ElevationMm": 0.0}]
        self.creates_levels: list[dict] = []
        self.elements = {100 + i: {"category": "OST_Walls", "params": {}} for i in range(existing)}
        self.creates = creates if creates is not None else {
            501: {"category": "OST_Walls", "params": {"WALL_USER_HEIGHT_PARAM": ("length_mm", 3000.0),
                                                       "Comments": ("string", "A")}},
            502: {"category": "OST_Walls", "params": {"WALL_USER_HEIGHT_PARAM": ("length_mm", 3000.0),
                                                       "Comments": ("string", "A")}},
        }
        self.result = result if result is not None else {"Status": "Created", "Ids": [501, 502]}
        self.fail_execution = fail_execution
        self.executions = 0

    def count(self, category: str) -> int:
        return sum(1 for e in self.elements.values() if e["category"] == category)

    def handler(self, request):
        rid, method = request["id"], request["method"]
        if method != "send_code_to_revit":
            return FakeRevit.default_handler(request)
        code = request["params"]["code"]
        category = re.search(r'typeof\(BuiltInCategory\), "(OST_\w+)"', code)
        ids = [int(x) for x in re.findall(r"(\d+)L", code)]
        if code == execution.DOCUMENT_PROBE:
            return FakeRevit.code_result(rid, {"Title": self.title, "RevitVersion": "2026"})
        if code == LEVELS_CODE:
            return FakeRevit.code_result(rid, self.levels)
        if "CategoryNames" in code:
            return FakeRevit.code_result(rid, {
                "Document": {"Title": self.title, "RevitVersion": "2026", "IsWorkshared": False},
                "Levels": self.levels, "CategoryNames": {}, "Warnings": []})
        if "GetElementCount" in code and category:
            return FakeRevit.code_result(rid, self.count(category.group(1)))
        if "Matches = matches" in code and category:
            rows = []
            for i in ids:
                e = self.elements.get(i)
                rows.append({"Id": i, "Exists": e is not None, "Category": e and e["category"],
                             "Matches": bool(e) and e["category"] == category.group(1)})
            return FakeRevit.code_result(rid, rows)
        if "Kind = kind" in code:
            names = re.findall(r'"([^"]*)"', code.split("new string[]", 1)[1].split("}", 1)[0])
            rows = []
            for i in ids:
                for name in names:
                    e = self.elements.get(i)
                    if e is None:
                        rows.append({"Id": i, "Name": name, "Kind": "missing", "Value": None})
                    elif name not in e["params"]:
                        rows.append({"Id": i, "Name": name, "Kind": "none", "Value": None})
                    else:
                        kind, value = e["params"][name]
                        rows.append({"Id": i, "Name": name, "Kind": kind, "Value": value})
            return FakeRevit.code_result(rid, rows)
        self.executions += 1                                    # the execution itself
        if self.fail_execution:
            return FakeRevit.code_result(rid, None, success=False, error="CS0103: boom")
        self.elements.update(copy.deepcopy(self.creates))
        self.levels = self.levels + copy.deepcopy(self.creates_levels)
        return FakeRevit.code_result(rid, self.result)


async def _execute(code: str = WALLS_CODE) -> dict:
    token = (await _acall("confirm_spec", spec=code_spec(code)))["token"]
    return await _acall("execute_code", code=code, token=token)


def with_model(model: Model, revit_env, scenario):
    async def go():
        async with FakeRevit(model.handler) as fake:
            revit_env(fake.port)
            try:
                return await scenario(fake)
            finally:
                await RevitClientPool.disconnect()
    return asyncio.run(go())


def plans_on_disk() -> list:
    directory = server._verifications.directory
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


# -- the acceptance scenario: count_delta -------------------------------------------------

def test_count_delta_two_walls_verifies_and_three_fails(revit_env, isolated_store):
    model = Model()

    async def scenario(fake):
        planned = await _acall("plan_verification", template="count_delta",
                               params={"category": "OST_Walls", "expected": 2})
        assert planned["template"] == "count_delta" and planned["params"] == {"category": "OST_Walls", "expected": 2}
        assert planned["baseline"] == {"count": 3} and model.executions == 0     # sampled, nothing ran
        assert planned["document"] == PROJECT1                     # with the snapshot's fingerprint
        assert re.fullmatch(r"v_[0-9a-f]{16}", planned["verification_id"])
        created = datetime.fromisoformat(planned["created_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(planned["expires_at"].replace("Z", "+00:00"))
        assert expires - created == timedelta(seconds=TTL_SECONDS) == timedelta(minutes=30)
        three = await _acall("plan_verification", template="count_delta",
                             params=json.dumps({"category": "OST_Walls", "expected": 3}))   # JSON text ok
        out = await _execute()
        assert out["success"] is True and out["validation"] is None             # execute_code unchanged
        ok = await _acall("verify_execution", evidence_id=out["evidence_id"],
                          verification_id=planned["verification_id"])
        bad = await _acall("verify_execution", evidence_id=out["evidence_id"],
                           verification_id=three["verification_id"])
        return planned, out, ok, bad

    planned, out, ok, bad = with_model(model, revit_env, scenario)
    assert set(ok) == {"verified", "template", "checks", "before", "after", "evidence_id", "verification_id",
                       "verification_evidence_id", "error"}
    assert ok["verified"] is True and ok["error"] is None and ok["template"] == "count_delta"
    assert ok["before"]["count"] + 2 == ok["after"]["count"] == 5
    assert ok["checks"] == [{"name": "count_delta", "passed": True,
                             "detail": "OST_Walls: before 3, after 5, delta 2, expected 2"}]
    assert ok["evidence_id"] == out["evidence_id"] and ok["verification_id"] == planned["verification_id"]
    assert bad["verified"] is False and bad["error"] is None
    assert bad["checks"][0]["detail"] == "OST_Walls: before 3, after 5, delta 2, expected 3"
    assert bad["before"] == {"category": "OST_Walls", "count": 3} and bad["after"]["delta"] == 2


def test_the_verify_line_in_the_ledger(revit_env, isolated_store):
    model = Model()

    async def scenario(fake):
        planned = await _acall("plan_verification", template="created_ids", params={"category": "OST_Walls"})
        out = await _execute()
        ok = await _acall("verify_execution", evidence_id=out["evidence_id"],
                          verification_id=planned["verification_id"])
        listed = await _acall("evidence", limit=5)
        only = await _acall("evidence", tool="verify_execution")
        return planned, out, ok, listed, only

    planned, out, ok, listed, only = with_model(model, revit_env, scenario)
    line = server._ledger.get(ok["verification_evidence_id"])
    assert list(line) == list(RECORD_FIELDS) + list(OPTIONAL_FIELDS)
    assert line["action"] == "verify_execution" and line["tool"] == "verify_execution"
    assert line["evidence_id"] == out["evidence_id"] and line["verification_id"] == planned["verification_id"]
    assert line["success"] is True and line["error"] is None and line["scope"] == "local" and line["host"] == "mcp"
    assert line["validation"]["validator"] == "created_ids" and line["validation"]["passed"] is True
    assert line["params"] == {"template": "created_ids", "params": {"category": "OST_Walls"}}
    assert line["document"] == PROJECT1
    executed = server._ledger.get(out["evidence_id"])
    assert list(executed) == list(RECORD_FIELDS)                               # the execution line is unchanged
    assert line["spec_hash"] == executed["spec_hash"] and line["code_sha256"] == executed["code_sha256"]
    assert line["token_prefix"] is None and line["code_head"] is None
    assert [r["id"] for r in listed] == [ok["verification_evidence_id"], out["evidence_id"]]
    assert [r["tool"] for r in listed] == ["verify_execution", None]
    assert [r["id"] for r in only] == [ok["verification_evidence_id"]]
    # validate() on either line behaves as before: only run_tool lines carry a pack validator
    for record_id in (out["evidence_id"], ok["verification_evidence_id"]):
        assert _call("validate", evidence_id=record_id) == {
            "error": "no_validator", "message": "only run_tool executions carry a validator"}
    # the plan is spent and remembers what it checked
    saved = json.loads(plans_on_disk()[0].read_text(encoding="utf-8"))
    assert saved["used_at"] and saved["evidence_id"] == out["evidence_id"]


# -- created_ids and param_equals -------------------------------------------------------------

def test_created_ids_passes_and_fails(revit_env, isolated_store):
    def run(model: Model, category: str = "OST_Walls"):
        async def scenario(fake):
            planned = await _acall("plan_verification", template="created_ids", params={"category": category})
            assert planned["baseline"] is None
            out = await _execute()
            return await _acall("verify_execution", evidence_id=out["evidence_id"],
                                verification_id=planned["verification_id"])
        return with_model(model, revit_env, scenario)

    ok = run(Model())
    assert ok["verified"] is True and [c["name"] for c in ok["checks"]] == ["id 501", "id 502"]
    assert ok["after"] == {"ids": [501, 502], "category": "OST_Walls"} and ok["before"] == {}
    wrong = run(Model(), "OST_Floors")
    assert wrong["verified"] is False and wrong["error"] is None
    assert wrong["checks"][0]["detail"] == "id 501 exists, category 'OST_Walls' (expected OST_Floors)"
    missing = run(Model(creates={501: {"category": "OST_Walls", "params": {}}}))     # 502 never made it
    assert missing["verified"] is False and missing["checks"][1]["detail"] == "id 502 does not exist"
    no_ids = run(Model(result={"Status": "Created"}))
    assert no_ids["verified"] is False
    assert no_ids["checks"] == [{"name": "validator", "passed": False,
                                 "detail": "the result reports no ElementId / Ids / ElementIds"}]


def test_param_equals_passes_and_fails_with_units_and_tolerance(revit_env, isolated_store):
    def run(checks, model: Model | None = None):
        async def scenario(fake):
            planned = await _acall("plan_verification", template="param_equals",
                                   params={"category": "OST_Walls", "checks": checks})
            assert "error" not in planned, planned
            out = await _execute()
            return planned, await _acall("verify_execution", evidence_id=out["evidence_id"],
                                         verification_id=planned["verification_id"])
        return with_model(model or Model(), revit_env, scenario)

    planned, ok = run([{"param": "WALL_USER_HEIGHT_PARAM", "expected": 3, "unit": "m"},
                       {"param": "Comments", "expected": "A"}])
    assert planned["params"]["checks"] == [
        {"param": "WALL_USER_HEIGHT_PARAM", "expected": 3, "unit": "m", "tolerance": None},
        {"param": "Comments", "expected": "A", "unit": None, "tolerance": None}]
    assert ok["verified"] is True and len(ok["checks"]) == 4
    assert ok["checks"][0] == {"name": "id 501 WALL_USER_HEIGHT_PARAM", "passed": True,
                               "detail": "got 3000 mm, expected 3 m = 3000 mm"}
    _, off = run([{"param": "WALL_USER_HEIGHT_PARAM", "expected": 3002}])
    assert off["verified"] is False and off["checks"][0]["detail"] == "got 3000 mm, expected 3002 mm"
    _, near = run([{"param": "WALL_USER_HEIGHT_PARAM", "expected": 3002, "unit": "mm", "tolerance": 5}])
    assert near["verified"] is True and near["checks"][0]["detail"] == "got 3000 mm, expected 3002 mm +/- 5 mm"
    _, absent = run([{"param": "Mark", "expected": "W1"}])
    assert absent["verified"] is False and absent["checks"][0]["detail"] == "parameter not found on the element"


def test_compare_without_a_tolerance_is_what_packs_always_had():
    length = {"Kind": "length_mm", "Value": 3000.4}
    assert _compare(length, 3000, "mm") == (True, "got 3000.4 mm, expected 3000 mm")
    assert _compare(length, 3, "m") == (True, "got 3000.4 mm, expected 3 m = 3000 mm")
    assert _compare({"Kind": "length_mm", "Value": 3001.0}, 3000) == (False, "got 3001 mm, expected 3000 mm")
    assert _compare({"Kind": "int", "Value": 2}, 2) == (True, "got 2, expected 2")
    assert _compare({"Kind": "double", "Value": 1.5}, 1.4, None, 0.2) == (True, "got 1.5, expected 1.4 +/- 0.2")
    assert _compare({"Kind": "length_mm", "Value": 3001.0}, 3, "m", 0.002) == (
        True, "got 3001 mm, expected 3 m = 3000 mm +/- 2 mm")


# -- invalid_template -------------------------------------------------------------------------

@pytest.mark.parametrize("template,params,problem", [
    ("count", {"category": "OST_Walls"}, "template must be one of created_ids, count_delta, param_equals"),
    ("created_ids", {}, "params.category is required"),
    ("created_ids", {"category": "Walls"}, 'params.category must be a BuiltInCategory OST_* name'),
    ("created_ids", {"category": "OST_Walls", "expected": 1}, "params.expected is not a key here"),
    ("count_delta", {"category": "OST_Walls"}, "params.expected is required"),
    ("count_delta", {"category": "OST_Walls", "expected": 1.5}, "params.expected must be an integer"),
    ("count_delta", {"category": "OST_Walls", "expected": "2"}, "params.expected must be an integer"),
    ("count_delta", {"category": "OST_Walls", "expected": True}, "params.expected must be an integer"),
    ("param_equals", {"category": "OST_Walls", "checks": []}, "params.checks must be a non-empty list"),
    ("param_equals", {"category": "OST_Walls", "checks": [{"param": "A"}]}, "params.checks[0].expected is required"),
    ("param_equals", {"category": "OST_Walls", "checks": [{"param": 'A" }); x(); //', "expected": 1}]},
     "params.checks[0].param must not contain a double quote"),
    ("param_equals", {"category": "OST_Walls", "checks": [{"param": "A\\B", "expected": 1}]},
     "params.checks[0].param must not contain a double quote, a backslash"),
    ("param_equals", {"category": "OST_Walls", "checks": [{"param": "A", "expected": 1},
                                                          {"param": "A", "expected": 2}]},
     "params.checks[1].param repeats 'A'"),
    ("param_equals", {"category": "OST_Walls", "checks": [{"param": "A", "expected": 1, "unit": "cm"}]},
     "params.checks[0].unit must be one of mm, m, feet or null"),
    ("param_equals", {"category": "OST_Walls", "checks": [{"param": "A", "expected": 1, "tolerance": -1}]},
     "params.checks[0].tolerance must be a number >= 0 or null"),
    ("param_equals", {"category": "OST_Walls", "checks": [{"param": "A", "expected": [1]}]},
     "params.checks[0].expected must be a string, a number or a boolean"),
    ("param_equals", {"category": "OST_Walls", "checks": [{"param": "A", "expected": 1, "spec_param": "x"}]},
     "params.checks[0].spec_param is not a key here"),
])
def test_invalid_templates_are_refused_and_nothing_is_written(template, params, problem, monkeypatch):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")       # nothing listens: the check needs no Revit
    out = _call("plan_verification", template=template, params=params)
    assert out["error"] == "invalid_template" and any(p.startswith(problem) for p in out["problems"]), out
    assert set(out) == {"error", "problems"} and plans_on_disk() == []


def test_every_problem_at_once_and_integral_floats():
    normal, problems = check_template("param_equals", {"category": "Walls", "checks": [{"param": ""}], "x": 1})
    assert normal is None and len(problems) == 4
    assert check_template("created_ids", []) == (None, ["params must be an object with category"])
    assert check_template("count_delta", {"category": "OST_Walls", "expected": -2.0}) == (
        {"category": "OST_Walls", "expected": -2}, [])


def test_plan_without_revit_writes_nothing(monkeypatch):
    monkeypatch.setenv("REVIT_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "1")
    out = _call("plan_verification", template="created_ids", params={"category": "OST_Walls"})
    assert out["error"] == "revit_unreachable" and out["message"] and plans_on_disk() == []


def test_a_failing_probe_at_plan_time_writes_nothing(revit_env, isolated_store):
    def broken(request):
        if request["method"] == "send_code_to_revit" and "GetElementCount" in request["params"]["code"]:
            return FakeRevit.code_result(request["id"], None, success=False, error="no document")
        return Model().handler(request)

    async def scenario():
        async with FakeRevit(broken) as fake:
            revit_env(fake.port)
            try:
                return await _acall("plan_verification", template="count_delta",
                                    params={"category": "OST_Walls", "expected": 1})
            finally:
                await RevitClientPool.disconnect()

    out = asyncio.run(scenario())
    assert out == {"error": "probe_failed", "message": "ValidatorError: no document"} and plans_on_disk() == []


# -- the preconditions of verify_execution ------------------------------------------------------

def test_verify_refusals_keep_the_plan(revit_env, isolated_store):
    model = Model()

    async def scenario(fake):
        planned = await _acall("plan_verification", template="count_delta",
                               params={"category": "OST_Walls", "expected": 2})
        vid = planned["verification_id"]
        refusals = {
            "unknown": await _acall("verify_execution", evidence_id="ev_20000101T000000_ffffff", verification_id=vid),
            "bad_id": await _acall("verify_execution", evidence_id="nope", verification_id=vid),
        }
        model.fail_execution = True
        failed = await _execute('return "x";')
        assert failed["success"] is False and failed["evidence_id"]
        refusals["not_executed"] = await _acall("verify_execution", evidence_id=failed["evidence_id"],
                                                verification_id=vid)
        model.fail_execution = False
        out = await _execute()
        refusals["no_plan"] = await _acall("verify_execution", evidence_id=out["evidence_id"],
                                           verification_id="v_0000000000000000")
        refusals["path"] = await _acall("verify_execution", evidence_id=out["evidence_id"],
                                        verification_id="../evidence/x")
        ok = await _acall("verify_execution", evidence_id=out["evidence_id"], verification_id=vid)
        refusals["used"] = await _acall("verify_execution", evidence_id=out["evidence_id"], verification_id=vid)
        refusals["verify_line"] = await _acall("verify_execution", evidence_id=ok["verification_evidence_id"],
                                               verification_id=vid)
        return out, ok, refusals

    out, ok, refusals = with_model(model, revit_env, scenario)
    assert {k: v["error"] for k, v in refusals.items()} == {
        "unknown": "unknown_evidence", "bad_id": "unknown_evidence", "not_executed": "not_executed",
        "no_plan": "unknown_verification", "path": "unknown_verification", "used": "verification_used",
        "verify_line": "unknown_evidence"}
    for reply in refusals.values():
        assert reply["verified"] is False and reply["checks"] == [] and reply["verification_evidence_id"] is None
    assert refusals["used"]["template"] == "count_delta"
    assert ok["verified"] is True                     # the refusals before it did not consume the plan
    assert len(server._ledger.recent(tool="verify_execution")) == 1


def test_an_expired_plan_is_refused(revit_env, isolated_store, monkeypatch):
    model = Model()
    later = datetime.now(timezone.utc) + timedelta(minutes=31)

    async def scenario(fake):
        planned = await _acall("plan_verification", template="count_delta",
                               params={"category": "OST_Walls", "expected": 2})
        out = await _execute()
        monkeypatch.setattr(server._verifications, "clock", lambda: later)
        expired = await _acall("verify_execution", evidence_id=out["evidence_id"],
                               verification_id=planned["verification_id"])
        return expired

    expired = with_model(model, revit_env, scenario)
    assert expired["verified"] is False and expired["error"] == "verification_expired"
    assert expired["template"] == "count_delta" and server._ledger.recent(tool="verify_execution") == []


def test_another_document_is_document_changed(revit_env, isolated_store):
    model = Model(title="Project1")

    async def scenario(fake):
        before_switch = await _acall("plan_verification", template="created_ids", params={"category": "OST_Walls"})
        model.title = "Project2"                          # the designer switched documents before running
        out = await _execute()
        ran_elsewhere = await _acall("verify_execution", evidence_id=out["evidence_id"],
                                     verification_id=before_switch["verification_id"])
        planned = await _acall("plan_verification", template="created_ids", params={"category": "OST_Walls"})
        out = await _execute()
        model.title = "Project3"                          # ... or after
        reads_elsewhere = await _acall("verify_execution", evidence_id=out["evidence_id"],
                                       verification_id=planned["verification_id"])
        model.title = "Project2"
        back = await _acall("verify_execution", evidence_id=out["evidence_id"],
                            verification_id=planned["verification_id"])
        return ran_elsewhere, reads_elsewhere, back

    ran_elsewhere, reads_elsewhere, back = with_model(model, revit_env, scenario)
    assert ran_elsewhere["error"] == "document_changed" and reads_elsewhere["error"] == "document_changed"
    assert back["verified"] is True                   # document_changed did not consume the plan


def test_same_title_other_levels_is_another_document(revit_env, isolated_store):
    """The fingerprint (title, version, levels) tells two documents named Project1 apart."""
    model = Model(title="Project1")

    async def scenario(fake):
        planned = await _acall("plan_verification", template="created_ids", params={"category": "OST_Walls"})
        model.levels = [{"Id": 7, "Name": "Level 1", "ElevationMm": 0.0}]     # another Project1 is active
        out = await _execute()
        other = await _acall("verify_execution", evidence_id=out["evidence_id"],
                             verification_id=planned["verification_id"])
        replanned = await _acall("plan_verification", template="created_ids", params={"category": "OST_Walls"})
        out = await _execute()
        same = await _acall("verify_execution", evidence_id=out["evidence_id"],
                            verification_id=replanned["verification_id"])
        return planned, out, other, same

    planned, out, other, same = with_model(model, revit_env, scenario)
    executed = server._ledger.get(out["evidence_id"])["document"]
    assert planned["document"]["title"] == executed["title"] == "Project1"
    assert planned["document"]["fingerprint"] != executed["fingerprint"]
    assert executed["fingerprint"] == fingerprint_of("Project1", "2026", [LevelInfo(id=7, name="Level 1",
                                                                                     elevation_mm=0.0)])
    assert other["verified"] is False and other["error"] == "document_changed"
    assert same["verified"] is True and same["error"] is None


def test_code_that_adds_a_level_still_verifies(revit_env, isolated_store):
    """The document read after the execution is compared by title and version: the code may move levels."""
    model = Model()
    model.creates_levels = [{"Id": 2, "Name": "L2", "ElevationMm": 3000.0}]

    async def scenario(fake):
        planned = await _acall("plan_verification", template="count_delta",
                               params={"category": "OST_Walls", "expected": 2})
        out = await _execute()
        ok = await _acall("verify_execution", evidence_id=out["evidence_id"],
                          verification_id=planned["verification_id"])
        return planned, ok

    planned, ok = with_model(model, revit_env, scenario)
    line = server._ledger.get(ok["verification_evidence_id"])
    assert line["document"]["fingerprint"] != planned["document"]["fingerprint"]       # L2 is new
    assert ok["verified"] is True and ok["error"] is None


def test_records_and_plans_without_a_fingerprint_compare_by_title_and_version(tmp_path):
    """Lines written before the fingerprint existed (or whose levels read failed) still verify,
    and so does a plan made after the execution; a fingerprint taken before both still decides."""
    other_levels = fingerprint_of("Project1", "2026", [LevelInfo(id=9, name="X", elevation_mm=0.0)])
    cases = {
        "old": ({"title": "Project1", "revit_version": "2026"}, None),
        "null": ({"title": "Project1", "revit_version": "2026", "fingerprint": None}, None),
        "other_title": ({"title": "Project2", "revit_version": "2026"}, None),
        "planned_after": (dict(PROJECT1, fingerprint=other_levels), "2026-01-01T00:00:00Z"),
        "planned_before": (dict(PROJECT1, fingerprint=other_levels), "2999-01-01T00:00:00Z"),
    }

    async def go():
        model = Model()
        model.elements.update(copy.deepcopy(model.creates))
        async with FakeRevit(model.handler) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            store, ledger = VerificationStore(tmp_path / "plans"), Ledger(tmp_path / "evidence")
            replies = {}
            for name, (document, ts) in cases.items():
                planned = await plan(store, client, "created_ids", {"category": "OST_Walls"})
                assert planned["document"] == PROJECT1
                record = {"action": "execute_code", "success": True, "document": document,
                          "result_summary": {"ids": [501, 502]}}
                ran = ledger.append(dict(record, ts=ts) if ts else record)
                replies[name] = await verify(store, ledger, client, ran, planned["verification_id"])
            await client.disconnect()
            return replies
    replies = asyncio.run(go())
    assert {k: (v["verified"], v["error"]) for k, v in replies.items()} == {
        "old": (True, None), "null": (True, None), "other_title": (False, "document_changed"),
        "planned_after": (True, None), "planned_before": (False, "document_changed")}


def test_same_document_rules():
    from revit_bridge.verification.flow import same_document
    a = {"title": "P", "revit_version": "2026", "fingerprint": "aaaa"}
    assert same_document(a, dict(a)) and not same_document(a, dict(a, fingerprint="bbbb"))
    assert same_document(a, dict(a, fingerprint="bbbb"), by_fingerprint=False)
    assert same_document(a, {"title": "P", "revit_version": "2026"})                 # an old record
    assert same_document(a, dict(a, fingerprint=None))                                # a failed levels read
    assert not same_document(a, {"title": "P", "revit_version": "2027"})
    assert not same_document(a, {"title": "Q", "revit_version": "2026", "fingerprint": "aaaa"}, by_fingerprint=False)
    assert not same_document({"title": None, "revit_version": None, "fingerprint": None}, a)
    assert not same_document(None, a) and not same_document(a, {})


def test_an_old_ledger_line_still_lists_and_validates(revit_env, isolated_store):
    """evidence() and validate() read lines without document.fingerprint as before."""
    old = {"host": "mcp", "action": "run_tool", "tool": "create_wall", "tool_version": "1.0.0",
           "params": dict(WALL), "document": {"title": "Project1", "revit_version": "2026"}, "success": True,
           "result_summary": {"ids": [4242]},
           "validation": {"validator": "count_delta", "passed": True, "checks": [],
                          "before": {"category": "OST_Walls", "count": 1}, "after": {}}}
    evidence_id = server._ledger.append(old)

    async def scenario():
        async with FakeRevit(counting_handler(2, 2)) as fake:            # the wall is there now
            revit_env(fake.port)
            try:
                return await _acall("evidence", limit=5), await _acall("validate", evidence_id=evidence_id)
            finally:
                await RevitClientPool.disconnect()

    listed, again = asyncio.run(scenario())
    assert [r["id"] for r in listed] == [evidence_id]
    assert listed[0]["document"] == {"title": "Project1", "revit_version": "2026"}
    assert again["evidence_id"] == evidence_id and again["passed"] is True
    assert again["checks"][0]["detail"] == "OST_Walls: before 1, after 2, delta 1, expected 1"


def test_count_delta_planned_after_the_execution_cannot_pass(tmp_path):
    """A baseline taken after the code ran says nothing about what the code did."""
    async def go():
        model = Model()
        async with FakeRevit(model.handler) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            store, ledger = VerificationStore(tmp_path / "plans"), Ledger(tmp_path / "evidence")
            ran = ledger.append({"action": "execute_code", "success": True, "ts": "2026-01-01T00:00:00Z",
                                 "id": "ev_20260101T000000_000001", "result_summary": {"ids": [501]},
                                 "document": {"title": "Project1", "revit_version": "2026"}})
            planned = await plan(store, client, "count_delta", {"category": "OST_Walls", "expected": 0})
            out = await verify(store, ledger, client, ran, planned["verification_id"])
            await client.disconnect()
            return out
    out = asyncio.run(go())
    assert out["verified"] is False and out["error"] is None
    assert [c["name"] for c in out["checks"]] == ["count_delta", "order"]
    assert out["checks"][0]["passed"] is True and out["checks"][1]["passed"] is False
    assert out["checks"][1]["detail"].startswith("the execution (2026-01-01T00:00:00Z) ran before the baseline")


def test_the_store_is_one_time_and_prunes_old_plans(tmp_path):
    now = [datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)]
    store = VerificationStore(tmp_path, clock=lambda: now[0])
    first = store.create("created_ids", {"category": "OST_Walls"}, None, {"title": "P", "revit_version": "2026"})
    assert first["created_at"] == "2026-10-26T09:00:00Z" and first["expires_at"] == "2026-10-26T09:30:00Z"
    assert store.state(store.get(first["verification_id"])) is None
    assert store.consume(first["verification_id"], "ev_x")[1] is None
    assert store.consume(first["verification_id"], "ev_y")[1] == "verification_used"
    assert store.get(first["verification_id"])["evidence_id"] == "ev_x"
    second = store.create("created_ids", {"category": "OST_Walls"}, None, {})
    now[0] += timedelta(minutes=30)
    assert store.state(store.get(second["verification_id"])) == "verification_expired"
    assert store.consume(second["verification_id"], "ev_z")[1] == "verification_expired"
    assert store.get("v_bad") is None and store.state(None) == "unknown_verification"
    now[0] += timedelta(days=2)
    third = store.create("count_delta", {"category": "OST_Walls", "expected": 1}, {"count": 0}, {})
    assert [p.stem for p in sorted(tmp_path.glob("*.json"))] == [third["verification_id"]]
    assert iso(now[0]) == "2026-10-28T09:30:00Z"


# -- what stays as it was -------------------------------------------------------------------------

def test_run_tool_still_uses_the_pack_validator(revit_env, isolated_store):
    async def scenario():
        async with FakeRevit(counting_handler(1, 2)) as fake:
            revit_env(fake.port)
            try:
                from tests.test_gate import spec_for
                token = (await _acall("confirm_spec", spec=spec_for("create_wall", **WALL)))["token"]
                return await _acall("run_tool", name="create_wall", params=json.dumps(WALL), token=token)
            finally:
                await RevitClientPool.disconnect()

    out = asyncio.run(scenario())
    assert out["success"] is True and out["validation"]["validator"] == "count_delta"
    assert out["validation"]["checks"][0]["detail"] == "OST_Walls: before 1, after 2, delta 1, expected 1"
    assert plans_on_disk() == [] and server._ledger.recent(tool="verify_execution") == []


def test_the_tools_are_read_only_and_execute_code_keeps_its_signature():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("plan_verification", "verify_execution"):
        assert tools[name].annotations.model_dump(by_alias=True)["readOnlyHint"] is True, name
    assert set(tools["plan_verification"].input_schema["properties"]) == {"template", "params"}
    assert tools["plan_verification"].input_schema["required"] == ["template", "params"]
    assert set(tools["verify_execution"].input_schema["properties"]) == {"evidence_id", "verification_id"}
    for term in ("created_ids", "count_delta", "param_equals", "OST_", "tolerance", "30 minutes"):
        assert term in tools["plan_verification"].description, term
    for term in ("executed and verified", "verification failed", "not verified", "document_changed"):
        assert term in tools["verify_execution"].description, term
    execute = tools["execute_code"]
    assert set(execute.input_schema["properties"]) == {"code", "parameters", "token"}
    assert execute.annotations.model_dump(by_alias=True)["readOnlyHint"] is False

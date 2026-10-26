"""E3, the host path: an exception thrown in Revit points the model at the fix lessons for it.

The add-in reports code that threw as "<prefix><Type>: <message>" (``FakeRevit.thrown``);
``exception_types`` finds the type names, the failed execution's ``hint`` names
``get_guidance(stage="fix", exceptions=[...])``, and get_guidance finds the seed lessons
whose ``triggers.exceptions`` hold them. The reply keeps its keys; a success, a refusal
and a failure without a type name keep the hint they had.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import revit_bridge.mcp_server as server
from revit_bridge.execution import EXCEPTION_HINT, RETRY_HINT, failure_hint, run_code, run_pack
from revit_bridge.guidance.scan import exception_types
from revit_bridge.revit.pool import RevitClientPool

from tests.fake_revit import FakeRevit
from tests.test_execution import code_token, host, wall_token, with_fake  # noqa: F401
from tests.test_gate import WALL, _acall, code_spec, counting_handler, isolated_store, revit_env  # noqa: F401

FULL = "Autodesk.Revit.Exceptions.ArgumentException"
ACCEPTANCE = 'get_guidance(stage="fix", exceptions=["Autodesk.Revit.Exceptions.ArgumentException"])'
SEEDS = {"revit-families/activate-symbol-before-placement", "revit-elements/floor-ceiling-type-and-level-ids"}
REPLY_KEYS = {"success", "tool", "result", "error", "validation", "evidence_id", "warnings",
              "preconditions_failed", "hint", "refusal"}
THROWS = 'var symbol = (FamilySymbol)null;\nreturn new { Placed = true };'


def throwing(exception: str, message: str = "The symbol is not active.", when: str = THROWS):
    """counting_handler, except that the code containing ``when`` throws ``exception``."""
    counting = counting_handler(0, 0)

    def handler(request):
        if request["method"] == "send_code_to_revit" and when in request["params"]["code"]:
            return FakeRevit.thrown(request["id"], exception, message)
        return counting(request)
    return handler


# -- exception_types ---------------------------------------------------------------------------

@pytest.mark.parametrize("error,expected", [
    (f"{FULL}: The symbol is not active.", [FULL]),
    (FakeRevit.THROWN_PREFIX + "ArgumentException: The symbol is not active.\r\nParameter name: symbol",
     ["ArgumentException"]),                                            # what the add-in reports
    ("System.Reflection.TargetInvocationException: Exception has been thrown by the target of an invocation. "
     "---> Autodesk.Revit.Exceptions.InvalidOperationException: Starting a new transaction is not permitted.",
     ["System.Reflection.TargetInvocationException", "Autodesk.Revit.Exceptions.InvalidOperationException"]),
    ("InvalidOperationException: a; then InvalidOperationException: b; ArgumentNullException",
     ["InvalidOperationException", "ArgumentNullException"]),          # in order, each once
    ("(Autodesk.Revit.Exceptions.ModificationForbiddenException) at the end.",
     ["Autodesk.Revit.Exceptions.ModificationForbiddenException"]),
    ("System.InvalidOperationException and InvalidOperationException", ["System.InvalidOperationException",
                                                                        "InvalidOperationException"]),
])
def test_exception_types_finds_full_and_short_names(error, expected):
    assert exception_types(error) == expected


@pytest.mark.parametrize("error", [
    None, "", 42, "declined_on_device", "validation_failed", "CS0103: The name 'doc' does not exist",
    "Exception: boom", "System.Exception: boom",                        # the bare word names no type
    "Line 2: The type or namespace name 'FooException' could not be found",   # quoted: a mention
    'a "BarException" in quotes', "a `BazException` in backticks",
    "the Autodesk.Revit.Exceptions namespace", "IOExceptions", "NotAnExceptionType", "lowercaseexception",
])
def test_exception_types_ignores_what_is_no_thrown_type(error):
    assert exception_types(error) == []


def test_failure_hint_names_the_types_or_keeps_the_hint():
    assert failure_hint("validation_failed") is None
    assert failure_hint("CS0103: boom", RETRY_HINT) == RETRY_HINT
    hint = failure_hint(f"{FULL}: x")
    assert hint == EXCEPTION_HINT.format(exceptions=json.dumps([FULL])) and ACCEPTANCE in hint
    assert hint.startswith("call get_guidance(") and "check_code" in hint and "confirmation" in hint
    assert failure_hint(f"{FULL}: x", RETRY_HINT) == f"{hint} {RETRY_HINT}"


# -- the execution path ---------------------------------------------------------------------------

def test_a_failed_run_code_points_at_the_lessons_for_its_exception(host):
    token = code_token(host["gate"], THROWS)
    out = with_fake(throwing(FULL), lambda c, f: run_code(
        gate=host["gate"], ledger=host["ledger"], client=c, code=THROWS, parameters=None, token=token, host="web"))
    assert out.success is False and out.error == f"{FakeRevit.THROWN_PREFIX}{FULL}: The symbol is not active."
    assert ACCEPTANCE in out.hint and set(out.model_dump()) == REPLY_KEYS
    assert out.refusal is None and out.validation is None
    record = host["ledger"].get(out.evidence_id)
    assert record["success"] is False and record["error"] == out.error          # the ledger is unchanged
    # the short name the add-in really reports
    token = code_token(host["gate"], THROWS)
    short = with_fake(throwing("ArgumentException"), lambda c, f: run_code(
        gate=host["gate"], ledger=host["ledger"], client=c, code=THROWS, parameters=None, token=token, host="web"))
    assert 'get_guidance(stage="fix", exceptions=["ArgumentException"])' in short.hint


def test_success_refusals_and_failures_without_a_type_keep_their_hint(host):
    ok = with_fake(counting_handler(0, 0), lambda c, f: run_code(
        gate=host["gate"], ledger=host["ledger"], client=c, code="return 1;", parameters=None,
        token=code_token(host["gate"], "return 1;"), host="web"))
    assert ok.success is True and ok.hint is None
    refused = with_fake(throwing(FULL), lambda c, f: run_code(
        gate=host["gate"], ledger=host["ledger"], client=c, code=THROWS, parameters=None, token="", host="web"))
    assert refused.error == "confirmation_required" and "confirm_spec" in refused.hint and ACCEPTANCE not in refused.hint
    blocked_code = 'System.IO.File.Delete("x"); // ArgumentException'
    blocked = with_fake(throwing(FULL), lambda c, f: run_code(
        gate=host["gate"], ledger=host["ledger"], client=c, code=blocked_code, parameters=None,
        token=code_token(host["gate"], blocked_code), host="web"))
    assert blocked.error == "blocked" and blocked.hint is None
    plain = with_fake(throwing("", message="no document"), lambda c, f: run_code(
        gate=host["gate"], ledger=host["ledger"], client=c, code=THROWS, parameters=None,
        token=code_token(host["gate"], THROWS), host="web"))
    assert plain.success is False and plain.hint is None


def test_a_failed_pack_names_the_exception_before_its_retry_advice(host):
    ok = with_fake(counting_handler(1, 2), lambda c, f: run_pack(
        client=c, name="create_wall", params=dict(WALL), token=wall_token(host["gate"]), host="web", **host))
    assert ok.success is True and ok.hint is None                               # first: failures count against the pack
    failed = with_fake(throwing(FULL, "The curve argument is not valid for wall creation.", when="Wall.Create"),
                       lambda c, f: run_pack(client=c, name="create_wall", params=dict(WALL),
                                             token=wall_token(host["gate"]), host="web", **host))
    assert failed.success is False and failed.tool == "create_wall"
    assert failed.hint == f"{EXCEPTION_HINT.format(exceptions=json.dumps([FULL]))} {RETRY_HINT}"
    assert set(failed.model_dump()) == REPLY_KEYS
    plain = with_fake(throwing("", message="no document", when="Wall.Create"),
                      lambda c, f: run_pack(client=c, name="create_wall", params=dict(WALL),
                                            token=wall_token(host["gate"]), host="web", **host))
    assert plain.success is False and plain.hint == RETRY_HINT                  # as before


# -- acceptance: through the MCP tools ------------------------------------------------------------------

def test_execute_code_hint_reaches_the_new_seed_lessons_through_get_guidance(revit_env, isolated_store):
    async def scenario():
        async with FakeRevit(throwing(FULL)) as fake:
            revit_env(fake.port)
            try:
                token = (await _acall("confirm_spec", spec=code_spec(THROWS)))["token"]
                failed = await _acall("execute_code", code=THROWS, token=token)
                token = (await _acall("confirm_spec", spec=code_spec("return 1;")))["token"]
                return failed, await _acall("execute_code", code="return 1;", token=token)
            finally:
                await RevitClientPool.disconnect()

    out, ok = asyncio.run(scenario())
    # execute_code keeps its keys: the ExecutionResult fields but refusal, hint only when there is one
    assert set(out) == REPLY_KEYS - {"refusal"} and set(ok) == REPLY_KEYS - {"refusal", "hint"}
    assert ok["success"] is True
    assert out["success"] is False and ACCEPTANCE in out["hint"]

    def guidance(**args):
        reply = asyncio.run(server.mcp.call_tool("get_guidance", {"mode": "recommend", **args}))
        return json.loads(reply.content[0].text)

    for given in ([FULL], ["ArgumentException"]):                     # the full name and the add-in's short one
        found = guidance(stage="fix", exceptions=given)
        assert found["error"] is None and SEEDS <= {x["id"] for x in found["items"]}, given
        for item in found["items"]:
            assert f"exception:{FULL}" in item["why"] and item["body"].startswith("## Scenario")
    # a .NET exception of the same short name with another namespace is another type
    assert not SEEDS & {x["id"] for x in guidance(stage="fix", exceptions=["System.ArgumentException"])["items"]}
    # InvalidOperationException: the transaction lesson and the placement lesson
    ids = {x["id"] for x in guidance(stage="fix", exceptions=["InvalidOperationException"])["items"]}
    assert {"revit-core/transaction-owned-by-bridge", "revit-families/activate-symbol-before-placement"} <= ids

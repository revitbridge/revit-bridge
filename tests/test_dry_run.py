"""dry_run_code: run C# in Revit and always roll it back - no token, no gate, one ledger line.

A fake add-in that offers ``dry_run_code_in_revit`` answers with the reply the
add-in returns (work plan 2026-11-12, 4.2); the tests check what the bridge
makes of it: passed through when ``rolled_back`` is true, never a success
otherwise, ``no_dry_run`` for an add-in without it, and nothing sent to Revit
for code the sandbox refuses.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import revit_bridge.execution as execution
import revit_bridge.mcp_server as server
from revit_bridge import dry_run
from revit_bridge.evidence.ledger import RECORD_FIELDS, Ledger
from revit_bridge.execution import EXCEPTION_HINT, failure_hint
from revit_bridge.revit.capabilities import Capabilities
from revit_bridge.revit.pool import RevitClientPool

from tests.fake_revit import FakeRevit
from tests.test_check_code import CAPS, CS1061, _tool, _with_client, revit_env  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
DRY_CAPS = dict(CAPS, methods=CAPS["methods"] + ["dry_run_code_in_revit"],
                features={**CAPS["features"], "dry_run": True})
CODE = ("var line = Line.CreateBound(XYZ.Zero, new XYZ(10, 0, 0));\n"
        "var wall = Wall.Create(document, line, new ElementId(311L), false);\n"
        "return new { ElementId = wall.Id.Value };")
CHANGES = {
    "added": {"count": 3, "by_category": {"OST_Walls": 1, "OST_Doors": 2}, "ids": [9001, 9002, 9003]},
    "modified": {"count": 1, "by_category": {"OST_Walls": 1}, "ids": [311]},
    "deleted": {"count": 0, "ids": []},
}
FAILURES = [{"severity": "Warning", "text": "Highlighted walls overlap.", "element_ids": [9001, 311]}]
OK_REPLY = {"success": True, "rolled_back": True, "result": json.dumps({"ElementId": 9001}), "error": "",
            "diagnostics": [], "changes": CHANGES, "failures": FAILURES, "fingerprint": "sha256:abc"}
REPLY_KEYS = {"success", "rolled_back", "result", "error", "diagnostics", "changes", "failures", "fingerprint",
              "evidence_id", "hint", "warnings"}
# Work plan 2026-11-12, 5.2: the tool description, verbatim.
DESCRIPTION = """\
Run C# in the open Revit model and always roll it back: nothing is kept, the undo
stack is untouched. Needs no confirmation and no token. Use it to find out whether
code works on this model before delivering it or before asking the designer to
confirm the real run. Returns what the code returned, what it would have added,
modified and deleted (counts by category), and every Revit warning or error it
raised. It does not replace execute_code: a dry run changes nothing."""
# The include paragraph appended to it (planner, package A extension), verbatim.
INCLUDE_PARAGRAPH = (
    "include: paths of C# source files to compile together with the code, so the code can call into them. Use it "
    "to dry-run the add-in's own core-logic file unchanged - the same file you then build into the add-in. Each "
    "file must be complete (its own usings and namespace) and must not do file, process or network work.")
THROWN ="Autodesk.Revit.Exceptions.ArgumentException: The curve is too short."


def dry_addin(answer=OK_REPLY, caps=DRY_CAPS, respond=None):
    """A handler for an add-in with ``dry_run_code_in_revit``: ``answer`` as its reply, or ``respond(request)``."""
    def handler(request):
        method, rid = request["method"], request["id"]
        if method == "get_bridge_capabilities":
            return FakeRevit.ok(rid, caps)
        if method == "dry_run_code_in_revit":
            return respond(request) if respond is not None else FakeRevit.ok(rid, answer)
        return FakeRevit.default_handler(request)
    return handler


def dry(handler, revit_env, *calls) -> tuple[list[dict], list[dict]]:
    """``dry_run_code`` through the MCP server, once per ``calls`` item (its arguments)."""
    return asyncio.run(_tool(handler, revit_env, [("dry_run_code", args) for args in calls]))


def methods(requests) -> list[str]:
    return [r["method"] for r in requests]


# -- the happy path: no token, the reply as the add-in sent it -----------------------------------

def test_dry_run_code_needs_no_token_and_passes_the_reply_through(revit_env, monkeypatch):
    monkeypatch.delenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", raising=False)
    replies, requests = dry(dry_addin(), revit_env, {"code": CODE, "parameters": [1, "a"]})
    out = replies[0]
    assert set(out) == REPLY_KEYS
    assert out["success"] is True and out["rolled_back"] is True and out["error"] == ""
    assert out["result"] == {"ElementId": 9001}                     # decoded, as execute_code's result is
    assert out["changes"] == CHANGES and out["failures"] == FAILURES and out["diagnostics"] == []
    assert out["fingerprint"] == "sha256:abc" and out["hint"] is None and out["warnings"] == []
    assert out["evidence_id"].startswith("ev_")
    # the capability once, then the dry run: no confirm field, no document probe through
    # send_code_to_revit (its committed transaction would leave an undo entry)
    assert methods(requests) == ["get_bridge_capabilities", "dry_run_code_in_revit"]
    assert requests[1]["params"] == {"code": CODE, "parameters": [1, "a"]}
    assert "token" not in requests[1]


def test_any_other_key_of_the_reply_passes_through_and_json_text_is_read(revit_env):
    extra = dict(OK_REPLY, note="added ids are gone after the rollback", result=json.dumps("text"))
    replies, _ = dry(dry_addin(extra), revit_env, {"code": "return \"text\";"})
    assert replies[0]["note"] == extra["note"] and set(replies[0]) == REPLY_KEYS | {"note"}
    assert replies[0]["result"] == "text" and replies[0]["success"] is True
    # a reply sent as JSON text, and a result that is not JSON, are taken as they come
    as_text = dry_addin(respond=lambda request: FakeRevit.ok(request["id"], json.dumps(dict(OK_REPLY, result="n/a"))))
    replies, _ = dry(as_text, revit_env, {"code": "return 1;"})
    assert replies[0]["success"] is True and replies[0]["result"] == "n/a" and replies[0]["changes"] == CHANGES


def test_the_gate_takes_no_part(revit_env, monkeypatch):
    """No gate_refusal, no Gate, and REVIT_BRIDGE_ALLOW_UNCONFIRMED changes nothing."""
    class NoGate:
        def __getattr__(self, name):
            raise AssertionError(f"the gate was asked for {name}")

    def refuse(*args, **kwargs):
        raise AssertionError("gate_refusal was called")

    monkeypatch.setattr(server, "_gate", NoGate())
    for module in (server, execution):
        monkeypatch.setattr(module, "gate_refusal", refuse)
        monkeypatch.setattr(module, "unconfirmed_allowed", refuse)
    outcomes = []
    for flag in (None, "1", "0"):
        if flag is None:
            monkeypatch.delenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", raising=False)
        else:
            monkeypatch.setenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", flag)
        replies, _ = dry(dry_addin(), revit_env, {"code": CODE})
        outcomes.append({k: v for k, v in replies[0].items() if k != "evidence_id"})
    assert outcomes[0]["success"] is True and outcomes[0] == outcomes[1] == outcomes[2]
    # and the module's code names none of it (its docstring may)
    import ast

    tree = ast.parse((ROOT / "revit_bridge" / "dry_run.py").read_text(encoding="utf-8"))
    docstring = tree.body[0].value
    used, texts = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            used.add(node.module or "")
            used.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node is not docstring:
            texts.append(node.value)
    for name in ("gate_refusal", "unconfirmed_allowed", "ENV_ALLOW_UNCONFIRMED", "env_flag", "Gate",
                 "revit_bridge.spec.gate", "revit_bridge.revit.settings", "environ", "getenv"):
        assert name not in used, name
    assert not any("ALLOW_UNCONFIRMED" in text for text in texts)


def test_the_tool_schema_has_no_token_and_the_planned_description():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    tool = tools["dry_run_code"]
    assert set(tool.input_schema["properties"]) == {"code", "parameters", "include"}
    assert tool.input_schema["required"] == ["code"]
    assert tool.description == DESCRIPTION + "\n\n" + INCLUDE_PARAGRAPH
    assert tool.annotations.model_dump(by_alias=True)["readOnlyHint"] is True


# -- the sandbox: refused code never reaches Revit -----------------------------------------------

class _Untouchable:
    """A client that must not be used."""

    def __init__(self):
        self.calls = 0

    async def ensure_connected(self):
        self.calls += 1
        raise AssertionError("ensure_connected must not be reached")

    async def send_command(self, method, params=None):
        self.calls += 1
        raise AssertionError("send_command must not be reached")


def test_code_the_sandbox_refuses_never_reaches_the_client(revit_env, tmp_path, monkeypatch):
    code = 'return System.IO.File.ReadAllText("c:/secret.txt");'
    replies, requests = dry(dry_addin(), revit_env, {"code": code})
    out = replies[0]
    assert set(out) == REPLY_KEYS and out["success"] is False and out["error"] == "blocked"
    assert any("System.IO" in w for w in out["warnings"]) and out["evidence_id"] is None
    assert out["rolled_back"] is None and out["changes"] is None           # nothing ran
    assert requests == [] and server._ledger.recent() == []
    # the package API refuses before it touches the client
    client = _Untouchable()
    ledger = Ledger(tmp_path / "evidence")
    out = asyncio.run(dry_run.dry_run_code(client, code, ledger=ledger))
    assert out["error"] == "blocked" and client.calls == 0 and ledger.recent() == []
    # the very review execute_code uses: what it refuses, the dry run refuses
    monkeypatch.setattr(execution.sandbox, "review", lambda code: (False, ["Blocked pattern: test"]))
    out = asyncio.run(dry_run.dry_run_code(client, "return 1;", ledger=ledger))
    assert out == dry_run.reply(error="blocked", warnings=["Blocked pattern: test"]) and client.calls == 0


# -- an add-in without the dry run ----------------------------------------------------------------

@pytest.mark.parametrize("caps", [
    CAPS,                                                                  # check_code only
    dict(DRY_CAPS, features={**DRY_CAPS["features"], "dry_run": False}),   # it says no, whatever its methods
], ids=["no_feature_no_method", "feature_false"])
def test_an_addin_without_the_dry_run_answers_no_dry_run(revit_env, caps):
    replies, requests = dry(dry_addin(caps=caps), revit_env, {"code": "return 1;"})
    out = replies[0]
    assert out == dry_run.reply(error="no_dry_run", hint=dry_run.NO_DRY_RUN_HINT) and set(out) == REPLY_KEYS
    assert "check_code" in out["hint"] and "not dry-run" in out["hint"]
    assert methods(requests) == ["get_bridge_capabilities"]
    assert server._ledger.recent() == []


def test_an_old_addin_is_no_dry_run_and_the_method_list_is_the_fallback(revit_env):
    replies, requests = dry(FakeRevit.default_handler, revit_env, {"code": "return 1;"})   # -32601 for everything new
    assert replies[0]["error"] == "no_dry_run" and methods(requests) == ["get_bridge_capabilities"]
    # no `dry_run` feature, but the method is listed: it is asked
    listed = dict(DRY_CAPS, features={k: v for k, v in DRY_CAPS["features"].items() if k != "dry_run"})
    replies, requests = dry(dry_addin(caps=listed), revit_env, {"code": "return 1;"})
    assert replies[0]["success"] is True and methods(requests)[-1] == "dry_run_code_in_revit"


def test_a_method_the_capability_promised_but_the_addin_lacks_is_no_dry_run_and_asked_again(revit_env):
    missing = dry_addin(respond=lambda request: FakeRevit.error(
        request["id"], -32601, "Method 'dry_run_code_in_revit' not found"))
    replies, requests = dry(missing, revit_env, {"code": "return 1;"}, {"code": "return 1;"})
    assert [r["error"] for r in replies] == ["no_dry_run", "no_dry_run"]
    assert all(r["evidence_id"] is None for r in replies) and server._ledger.recent() == []
    assert methods(requests) == ["get_bridge_capabilities", "dry_run_code_in_revit"] * 2   # the cache was dropped


def test_capabilities_can_dry_run():
    assert Capabilities("supported", info=DRY_CAPS).can_dry_run is True
    assert Capabilities("supported", info=CAPS).can_dry_run is False
    assert Capabilities("supported", info=dict(CAPS, features={}, methods=["dry_run_code_in_revit"])).can_dry_run
    assert not Capabilities("supported", info=dict(DRY_CAPS, features={"dry_run": "yes"})).can_dry_run
    assert not Capabilities("unsupported", reason="Method not found").can_dry_run
    assert not Capabilities("unknown", info=DRY_CAPS).can_dry_run


# -- rolled_back must be true ---------------------------------------------------------------------

@pytest.mark.parametrize("answer", [
    dict(OK_REPLY, rolled_back=False),
    {k: v for k, v in OK_REPLY.items() if k != "rolled_back"},
    dict(OK_REPLY, rolled_back=None),
    dict(OK_REPLY, rolled_back="true"),
], ids=["false", "missing", "null", "text"])
def test_a_reply_without_rolled_back_true_is_never_a_success(revit_env, answer):
    replies, _ = dry(dry_addin(answer), revit_env, {"code": CODE})
    out = replies[0]
    assert set(out) == REPLY_KEYS
    assert out["success"] is False and out["error"] == "dry_run_not_rolled_back"
    assert out["hint"] == dry_run.NOT_ROLLED_BACK_HINT and "defect" in out["hint"]
    # the add-in's reply comes along: its keys as sent, its own verdict in the warnings
    assert out["rolled_back"] == answer.get("rolled_back")
    assert out["changes"] == CHANGES and out["failures"] == FAILURES and out["result"] == {"ElementId": 9001}
    rolled = json.dumps(answer.get("rolled_back"))
    assert out["warnings"] == [f'add-in reply: success true, rolled_back {rolled}, error ""']
    record = server._ledger.get(out["evidence_id"])
    assert record["action"] == "dry_run" and record["success"] is False
    assert record["error"] == "dry_run_not_rolled_back" and record["result_summary"]["rolled_back"] is False


@pytest.mark.parametrize("raw", ["garbage", [1, 2], None, 7])
def test_a_reply_that_is_not_an_object_is_not_rolled_back(revit_env, raw):
    replies, _ = dry(dry_addin(respond=lambda request: FakeRevit.ok(request["id"], raw)), revit_env, {"code": CODE})
    out = replies[0]
    assert set(out) == REPLY_KEYS and out["success"] is False and out["error"] == "dry_run_not_rolled_back"
    assert out["warnings"] == [f"unexpected reply to dry_run_code_in_revit: {raw}"]
    record = server._ledger.get(out["evidence_id"])
    assert record["error"] == "dry_run_not_rolled_back" and record["result_summary"]["rolled_back"] is None


# -- failures in Revit -----------------------------------------------------------------------------

def test_a_dry_run_that_threw_points_at_the_fix_lessons(revit_env):
    empty = {"added": {"count": 0, "by_category": {}, "ids": []},
             "modified": {"count": 0, "by_category": {}, "ids": []}, "deleted": {"count": 0, "ids": []}}
    answer = dict(OK_REPLY, success=False, result=None, error=THROWN, changes=empty, failures=[])
    replies, _ = dry(dry_addin(answer), revit_env, {"code": CODE})
    out = replies[0]
    assert set(out) == REPLY_KEYS and out["success"] is False and out["rolled_back"] is True
    assert out["error"] == THROWN and out["changes"] == empty and out["result"] is None
    names = json.dumps(["Autodesk.Revit.Exceptions.ArgumentException"])
    assert out["hint"] == dry_run.failure_hint(THROWN) == dry_run.DRY_RUN_EXCEPTION_HINT.format(exceptions=names)
    assert out["hint"].startswith('call get_guidance(stage="fix", exceptions=')
    # execute_code's call, but the changed code is dry-run again, not confirmed again
    executed = failure_hint(THROWN)
    assert executed == EXCEPTION_HINT.format(exceptions=names)                   # execute_code's own hint as before
    assert out["hint"].split("; ")[0] == executed.split("; ")[0]
    assert out["hint"].endswith("the changed code needs check_code and another dry run.")
    assert "confirmation" not in out["hint"] and executed.endswith("a new confirmation.")
    record = server._ledger.get(out["evidence_id"])
    assert record["success"] is False and record["error"] == THROWN
    assert record["result_summary"]["changes"] == {"added": 0, "modified": 0, "deleted": 0}


def test_a_compile_failure_is_reported_as_the_addin_sent_it(revit_env):
    answer = {"success": False, "rolled_back": True, "result": None, "error": "", "diagnostics": [CS1061],
              "fingerprint": "sha256:abc"}                       # no changes, no failures: nothing ran
    replies, _ = dry(dry_addin(answer), revit_env, {"code": CODE})
    out = replies[0]
    assert set(out) == REPLY_KEYS and out["success"] is False and out["diagnostics"] == [CS1061]
    assert out["changes"] is None and out["failures"] == [] and out["hint"] is None and out["error"] == ""
    summary = server._ledger.get(out["evidence_id"])["result_summary"]
    # no value is not zero: the counts the add-in did not give stay null
    assert summary == {"ids": [], "rolled_back": True, "changes": {"added": None, "modified": None, "deleted": None},
                       "failures": None, "diagnostics": 1, "sources": []}


@pytest.mark.parametrize("respond,error", [
    (lambda request: FakeRevit.DROP, "Revit add-in closed connection"),
    (lambda request: [], "Timeout after 0.5s"),
    (lambda request: FakeRevit.error(request["id"], -32603, "Internal error: System.NullReferenceException: x"),
     "Internal error: System.NullReferenceException: x"),
], ids=["dropped", "silent", "rpc_error"])
def test_no_dry_run_reply_is_recorded_with_the_rollback_unconfirmed(tmp_path, respond, error):
    ledger = Ledger(tmp_path / "evidence")

    async def scenario(client, fake):
        return await dry_run.dry_run_code(client, CODE, [3], ledger=ledger, host="web")

    out = asyncio.run(_with_client(dry_addin(respond=respond), scenario, timeout=0.5))
    assert set(out) == REPLY_KEYS and out["success"] is False and out["error"] == error
    assert out["rolled_back"] is None and out["hint"] == dry_run.failure_hint(error, dry_run.NO_REPLY_HINT)
    assert out["hint"].endswith(dry_run.NO_REPLY_HINT) and "confirmation" not in out["hint"]
    record = ledger.get(out["evidence_id"])                   # it may have reached Revit: recorded
    assert record["action"] == "dry_run" and record["host"] == "web" and record["error"] == error
    assert record["params"] == {"parameters": [3]} and record["result_summary"]["rolled_back"] is None


def test_without_revit_nothing_runs_and_nothing_is_recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "1")
    out = json.loads(asyncio.run(server.mcp.call_tool("dry_run_code", {"code": "return 1;"})).content[0].text)
    assert set(out) == REPLY_KEYS and out["success"] is False and out["error"].startswith("unreachable: ")
    assert out["evidence_id"] is None and server._ledger.recent() == []
    # a refused token on the add-in side is no "no_dry_run" either
    ledger = Ledger(tmp_path / "evidence")

    async def scenario(client, fake):
        return await dry_run.dry_run_code(client, "return 1;", ledger=ledger)

    out = asyncio.run(_with_client(dry_addin(), scenario, token="pre-shared"))
    assert out["error"].startswith("auth: Unauthorized") and ledger.recent() == []


# -- the ledger --------------------------------------------------------------------------------------

def test_the_dry_run_leaves_a_ledger_line_without_any_confirmation(revit_env, isolated_data_dir):
    replies, _ = dry(dry_addin(), revit_env, {"code": CODE, "parameters": [7]})
    out = replies[0]
    record = server._ledger.get(out["evidence_id"])
    assert list(record) == list(RECORD_FIELDS)
    assert record["action"] == "dry_run" and record["host"] == "mcp" and record["scope"] == "local"
    for field in ("tool", "tool_version", "spec_hash", "projection_hash", "token_prefix", "confirmed_by",
                  "channel", "validation"):
        assert record[field] is None, field
    assert record["code_sha256"] == hashlib.sha256(CODE.encode("utf-8")).hexdigest() and record["code_head"] == CODE
    assert record["params"] == {"parameters": [7]}
    assert record["document"] == {"title": None, "revit_version": "2026", "fingerprint": None}
    assert record["success"] is True and record["error"] is None
    assert record["result_summary"] == {"ids": [9001], "rolled_back": True,
                                        "changes": {"added": 3, "modified": 1, "deleted": 0},
                                        "failures": 1, "diagnostics": 0, "sources": []}
    assert record["preconditions_failed"] == [] and record["warnings"] == []
    assert [r["id"] for r in json.loads(asyncio.run(server.mcp.call_tool("evidence", {})).content[0].text)] == [
        out["evidence_id"]]
    # no token was issued or spent, and a dry run is not an execution to verify or validate
    assert list((isolated_data_dir / "evidence" / "pending").glob("*.json")) == []

    def call(name, **args):
        return json.loads(asyncio.run(server.mcp.call_tool(name, args)).content[0].text)

    assert call("verify_execution", evidence_id=out["evidence_id"],
                verification_id="v_0000000000000000")["error"] == "unknown_evidence"
    assert call("validate", evidence_id=out["evidence_id"])["error"] == "no_validator"


def test_another_host_records_its_own_host_and_scope(tmp_path):
    ledger = Ledger(tmp_path / "evidence")

    async def scenario(client, fake):
        return await dry_run.dry_run_code(client, CODE, None, ledger=ledger, host="web", scope="dev_abcdefghijkl")

    out = asyncio.run(_with_client(dry_addin(), scenario))
    assert out["success"] is True
    record = ledger.get(out["evidence_id"])
    assert record["host"] == "web" and record["scope"] == "dev_abcdefghijkl" and record["params"] == {"parameters": []}


def test_a_ledger_that_cannot_be_written_costs_a_warning(tmp_path):
    class Full(Ledger):
        def append(self, record):
            raise OSError(28, "No space left on device")

    async def scenario(client, fake):
        return await dry_run.dry_run_code(client, CODE, ledger=Full(tmp_path / "evidence"))

    out = asyncio.run(_with_client(dry_addin(), scenario))
    assert out["success"] is True and out["evidence_id"] is None
    assert out["warnings"] == ["evidence not recorded: [Errno 28] No space left on device"]


# -- regression: execute_code and the hook ---------------------------------------------------------

def test_execute_code_without_a_token_is_still_refused_where_a_dry_run_is_offered(revit_env, monkeypatch):
    monkeypatch.delenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", raising=False)

    async def scenario():
        async with FakeRevit(dry_addin()) as fake:
            revit_env(fake.port)
            try:
                result = await server.mcp.call_tool("execute_code", {"code": CODE})
                return json.loads(result.content[0].text), fake.requests
            finally:
                await RevitClientPool.disconnect()

    out, requests = asyncio.run(scenario())
    assert out["success"] is False and out["error"] == "confirmation_required" and requests == []


def _hook():
    import importlib.util

    path = ROOT / "plugin" / "hooks" / "spec_gate.py"
    module_spec = importlib.util.spec_from_file_location("spec_gate", path)
    hook = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(hook)
    return hook


DRY_NAMES = ("mcp__revit-bridge__dry_run_code", "mcp__plugin_revit-bridge_revit-bridge__dry_run_code")
GATED = ("mcp__revit-bridge__execute_code", "mcp__plugin_revit-bridge_revit-bridge__execute_code",
         "mcp__revit-bridge__run_tool", "mcp__plugin_revit-bridge_revit-bridge__run_tool")


def test_the_hook_denies_execute_code_without_a_token_and_lets_dry_run_code_pass():
    hook = _hook()
    for name in GATED:
        assert hook.decide(name, {"code": CODE}) == hook.REASON, name
    for name in DRY_NAMES:
        assert hook.decide(name, {"code": CODE}) is None, name
        assert hook.decide(name, {}) is None, name
    matcher = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))[
        "hooks"]["PreToolUse"][0]["matcher"]
    assert all(re.search(matcher, name) is None for name in DRY_NAMES)       # the hook is not even started
    assert all(re.search(matcher, name) for name in GATED)


@pytest.mark.parametrize("name,denied", [(DRY_NAMES[0], False), (DRY_NAMES[1], False),
                                         (GATED[0], True), (GATED[1], True)])
def test_the_hook_script_itself(name, denied):
    """The script as the host runs it: JSON on stdin, a deny decision on stdout or nothing."""
    event = {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": {"code": CODE}}
    run = subprocess.run([sys.executable, "-I", str(ROOT / "plugin" / "hooks" / "spec_gate.py")],
                         input=json.dumps(event).encode("utf-8"), capture_output=True, check=True)
    if denied:
        decision = json.loads(run.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny" and "confirm_spec" in decision["permissionDecisionReason"]
    else:
        assert run.stdout == b"" and run.stderr == b""


# -- the skill texts (work plan 2026-11-12, 6) --------------------------------------------------

SKILLS = ROOT / "plugin" / "skills"


def test_the_bridge_skill_has_the_dry_run_section_after_the_flow():
    text = (SKILLS / "revit-bridge" / "SKILL.md").read_text(encoding="utf-8")
    heading = "## Dry run: try it without changing anything"
    assert text.count(heading) == 1
    assert text.index("**A run that threw.**") < text.index(heading) < text.index("## Parameter source protocol")
    section = text[text.index(heading):text.index("## Parameter source protocol")]
    for phrase in ("`dry_run_code(code, parameters)` runs the code in the open model and always rolls it back.",
                   "It needs no confirmation and no token, because it keeps nothing.",
                   "Use it after `check_code` passes and before you show the card",
                   "(`Dry run: +1 OST_Walls, +2 OST_Doors, no warnings`)",
                   'Report "dry run passed, not applied" - never "done".',
                   "Read `changes` before you trust the code.",
                   "A dry run is not an execution. Ids it returns are gone after the rollback",
                   "If the add-in answers `no_dry_run`, go on without it and say so."):
        assert phrase in section, phrase
    # steps 6 and 7 keep their words: the real run still needs the designer and the token
    assert ('wait for an explicit "yes / confirm / go ahead". A silent designer has not confirmed.' in text)
    assert "A hook denies both calls without a token. Never invent a token." in text


def test_the_addin_dev_skill_dry_runs_the_core_logic():
    text = (SKILLS / "revit-addin-dev" / "SKILL.md").read_text(encoding="utf-8")
    assert ("| Try an algorithm in the current model | Run the core logic with dry_run_code: it executes in the open "
            "model and rolls back, so it needs no confirmation. Read what it would change and what Revit warned "
            "about. Keep the confirmation protocol for runs that must stay. |") in text
    assert ('5. **Core logic runs in the open model before delivery** when a bridge reaches a Revit: dry-run it '
            '(dry_run_code needs no confirmation) on the real elements the command will meet, and fix what it '
            'shows. Say "not verified in Revit" only when no bridge reaches a Revit or the add-in has no dry run.'
            ) in text
    assert "within the existing confirmation protocol" not in text and "who is not there" not in text
    reference = (SKILLS / "revit-addin-dev" / "references" / "bridge-development.md").read_text(encoding="utf-8")
    heading = "## Prototype with the existing contract\n\n"
    start = reference.index(heading) + len(heading)
    assert reference[start:].startswith(
        "For trying logic, use `dry_run_code` first: it runs in the open model and rolls back, with no TaskSpec, "
        "no confirmation and no token. The contract below is for a run whose result must stay in the model.\n\n"
        "A normal source/build task needs neither a TaskSpec nor a live prototype.")


# The rule every text now follows: dry-run with dry_run_code (no confirmation); "not verified in Revit" only
# when no bridge reaches a Revit or the add-in has no dry run; a run whose result must stay is confirmed.
NOT_VERIFIED_ONLY = "no bridge reaches a Revit or the add-in has no dry run"


@pytest.mark.parametrize("path,old,new", [
    ("revit-task-brief/SKILL.md", 'needs a human and nobody is present, the report says "not verified in Revit: '
     'no confirmation available"', "names the prototype to dry-run there with `dry_run_code`, which needs no "
     "confirmation; a run whose result must stay still needs the designer's confirmation"),
    ("revit-task-brief/references/brief-template.md", "If the prototype needs a confirmation nobody can give",
     "dry-run with dry_run_code (no confirmation)"),
    ("revit-addin-dev/references/addin-development.md", "a prototype through the confirmation protocol) before",
     "then a dry run with `dry_run_code`, which needs no confirmation; a run whose result must stay goes through "
     "the confirmation protocol"),
    ("revit-addin-dev/SKILL.md", "run a confirmed prototype before porting it",
     "dry-run it with `dry_run_code` (no confirmation, always rolled back) before porting it"),
])
def test_no_skill_text_still_waits_for_a_confirmation_to_verify(path, old, new):
    text = (SKILLS / path).read_text(encoding="utf-8")
    assert old not in text and new in text
    if path != "revit-addin-dev/SKILL.md":
        assert NOT_VERIFIED_ONLY in text


def test_the_bridge_skill_says_what_the_dry_run_needs_and_the_readme_stays_true():
    text = (SKILLS / "revit-bridge" / "SKILL.md").read_text(encoding="utf-8")
    intro = text[text.index("# revit-bridge"):text.index("## The bridge flow")]
    assert ("`dry_run_code` needs a server that has the tool and an add-in that reports `features.dry_run`; "
            "without them, go on without a dry run and say so)") in intro
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert ("refuses to run anything the designer has not confirmed - except a dry run, which is always rolled "
            "back and keeps nothing.") in readme


# -- include: the add-in's own source files, dry-run as they are ---------------------------------------

SRC_CAPS = dict(DRY_CAPS, features={**DRY_CAPS["features"], "dry_run_sources": True})
CORE = ("using Autodesk.Revit.DB;\n\nnamespace MyAddin\n{\n    public static class Core\n    {\n"
        "        public static object Run(Document document, ElementId id) => new { ElementId = Geometry.Id(id) };\n"
        "    }\n}\n")
HELPERS = ("using Autodesk.Revit.DB;\n\nnamespace MyAddin\n{\n    public static class Geometry\n    {\n"
           "        public static long Id(ElementId id) => id.Value; // 墙\n    }\n}\n")
CALL = "return MyAddin.Core.Run(document, new ElementId(long.Parse(parameters[0].ToString())));"


def write(path: Path, text: str = CORE, bom: bool = False, data: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if data is not None else (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return path


def test_include_sends_each_file_by_its_base_name_and_records_its_hash(revit_env, tmp_path):
    core = write(tmp_path / "src" / "Core.cs", CORE, bom=True)
    helpers = write(tmp_path / "src" / "Geometry" / "Helpers.cs", HELPERS)
    replies, requests = dry(dry_addin(caps=SRC_CAPS), revit_env,
                            {"code": CALL, "parameters": ["311"], "include": [str(core), str(helpers)]})
    out = replies[0]
    assert set(out) == REPLY_KEYS and out["success"] is True and out["warnings"] == []
    assert methods(requests) == ["get_bridge_capabilities", "dry_run_code_in_revit"]
    # the text as written (the BOM dropped, UTF-8 kept), under the base name
    assert requests[1]["params"] == {"code": CALL, "parameters": ["311"],
                                     "sources": [{"name": "Core.cs", "text": CORE},
                                                 {"name": "Helpers.cs", "text": HELPERS}]}
    record = server._ledger.get(out["evidence_id"])
    assert list(record) == list(RECORD_FIELDS)                                    # the schema as before
    # the hash of the bytes on disk, BOM included: it matches the file the add-in is built from
    assert record["result_summary"]["sources"] == [
        {"name": "Core.cs", "sha256": hashlib.sha256(core.read_bytes()).hexdigest()},
        {"name": "Helpers.cs", "sha256": hashlib.sha256(helpers.read_bytes()).hexdigest()}]
    assert record["code_head"] == CALL and "public static class" not in json.dumps(record)   # names, not texts


@pytest.mark.parametrize("include", [None, []], ids=["none", "empty"])
def test_without_include_the_request_is_as_before(revit_env, include):
    for caps in (DRY_CAPS, SRC_CAPS):                     # with or without dry_run_sources
        args = {"code": CODE} if include is None else {"code": CODE, "include": include}
        replies, requests = dry(dry_addin(caps=caps), revit_env, args)
        assert replies[0]["success"] is True
        assert requests[-1]["params"] == {"code": CODE, "parameters": []}         # no sources key at all
        assert server._ledger.get(replies[0]["evidence_id"])["result_summary"]["sources"] == []


def test_a_diagnostic_in_an_included_file_names_it(revit_env, tmp_path):
    core = write(tmp_path / "Core.cs")
    answer = {"success": False, "rolled_back": True, "result": None, "error": "",
              "diagnostics": [dict(CS1061, file="Core.cs")], "fingerprint": "sha256:abc"}
    replies, _ = dry(dry_addin(answer, caps=SRC_CAPS), revit_env, {"code": CALL, "include": [str(core)]})
    assert replies[0]["diagnostics"] == [dict(CS1061, file="Core.cs")] and replies[0]["success"] is False


def _many(tmp_path: Path, count: int) -> list[str]:
    return [str(write(tmp_path / f"F{i}.cs")) for i in range(count)]


INCLUDE_REFUSALS = {
    "missing": (lambda tmp: [str(tmp / "Missing.cs")], "include_not_found",
                lambda tmp: [f"{tmp / 'Missing.cs'}: no such file"]),
    "directory": (lambda tmp: [str((tmp / "Folder.cs").mkdir() or tmp / "Folder.cs")], "include_not_found",
                  lambda tmp: [f"{tmp / 'Folder.cs'}: no such file"]),
    "not_cs": (lambda tmp: [str(write(tmp / "Core.cs")), str(write(tmp / "Notes.txt"))], "include_not_cs",
               lambda tmp: [f"{tmp / 'Notes.txt'}: not a .cs file"]),
    "same_name": (lambda tmp: [str(write(tmp / "a" / "Core.cs")), str(write(tmp / "b" / "core.cs"))],
                  "include_duplicate_name",
                  lambda tmp: [f"Core.cs: {tmp / 'a' / 'Core.cs'}, {tmp / 'b' / 'core.cs'}"]),
    "too_many": (lambda tmp: _many(tmp, 21), "include_too_many", lambda tmp: ["21 files given, at most 20"]),
    "too_large": (lambda tmp: [str(write(tmp / "A.cs", "// " + "x" * 200_000)),
                               str(write(tmp / "B.cs", "// " + "y" * 199_998))], "include_too_large",
                  lambda tmp: ["400004 bytes in all, at most 400000"]),
    "not_utf8": (lambda tmp: [str(write(tmp / "Core.cs", data=b"// \xff\xfe broken\n"))], "include_unreadable",
                 None),
    "blocked": (lambda tmp: [str(write(tmp / "Core.cs")),
                             str(write(tmp / "Io.cs", 'class Io { string R() => System.IO.File.ReadAllText("x"); }'))],
                "include_blocked",
                lambda tmp: ["Io.cs: Blocked pattern: System.IO (file operations)",
                             "Io.cs: Blocked pattern: File I/O operations"]),
}


@pytest.mark.parametrize("case", list(INCLUDE_REFUSALS))
def test_include_is_refused_before_the_addin_is_asked(revit_env, tmp_path, case):
    paths, error, warnings = INCLUDE_REFUSALS[case]
    include = paths(tmp_path)
    replies, requests = dry(dry_addin(caps=SRC_CAPS), revit_env, {"code": CALL, "include": include})
    out = replies[0]
    assert set(out) == REPLY_KEYS and out["success"] is False and out["error"] == error
    assert out["hint"] == dry_run.INCLUDE_HINTS[error] and out["evidence_id"] is None
    if warnings is not None:
        assert out["warnings"] == warnings(tmp_path)
    else:
        assert len(out["warnings"]) == 1 and out["warnings"][0].startswith(f"{tmp_path / 'Core.cs'}: UnicodeDecodeError")
    assert requests == [] and server._ledger.recent() == []                      # not even the capability


def test_the_size_limit_is_inclusive_and_counts_utf8_bytes(tmp_path):
    exact = write(tmp_path / "Exact.cs", "// " + "x" * (dry_run.MAX_SOURCE_BYTES - 3))
    refused, sources = dry_run.read_sources([str(exact)])
    assert refused is None and len(sources[0]["text"].encode("utf-8")) == dry_run.MAX_SOURCE_BYTES
    wide = write(tmp_path / "Wide.cs", "// " + "墙" * ((dry_run.MAX_SOURCE_BYTES - 3) // 3 + 1))   # 3 bytes each
    assert dry_run.read_sources([str(wide)])[0]["error"] == "include_too_large"
    assert dry_run.read_sources(None) == (None, []) and dry_run.read_sources([]) == (None, [])
    assert dry_run.read_sources([str(exact)] * 2)[0]["error"] == "include_duplicate_name"   # the same file twice


def test_include_needs_an_addin_that_compiles_sources(revit_env, tmp_path):
    core = write(tmp_path / "Core.cs")
    replies, requests = dry(dry_addin(caps=DRY_CAPS), revit_env, {"code": CALL, "include": [str(core)]})
    out = replies[0]
    assert out == dry_run.reply(error="no_dry_run_include", hint=dry_run.NO_DRY_RUN_INCLUDE_HINT)
    assert "paste the code the snippet needs" in out["hint"] and "say so in the report" in out["hint"]
    assert methods(requests) == ["get_bridge_capabilities"] and server._ledger.recent() == []
    # an add-in without any dry run says so first
    replies, _ = dry(dry_addin(caps=CAPS), revit_env, {"code": CALL, "include": [str(core)]})
    assert replies[0]["error"] == "no_dry_run"
    # a refused snippet is refused before its files are read
    replies, requests = dry(dry_addin(caps=SRC_CAPS), revit_env,
                            {"code": "System.IO.File.Delete(\"x\");", "include": [str(tmp_path / "Missing.cs")]})
    assert replies[0]["error"] == "blocked" and requests == []


def test_capabilities_can_dry_run_sources():
    assert Capabilities("supported", info=SRC_CAPS).can_dry_run_sources is True
    assert Capabilities("supported", info=DRY_CAPS).can_dry_run_sources is False
    assert not Capabilities("supported", info=dict(SRC_CAPS, features={"dry_run": False,
                                                                        "dry_run_sources": True})).can_dry_run_sources
    assert not Capabilities("supported", info=dict(SRC_CAPS, features={"dry_run": True,
                                                                        "dry_run_sources": "yes"})).can_dry_run_sources
    assert not Capabilities("unknown", info=SRC_CAPS).can_dry_run_sources

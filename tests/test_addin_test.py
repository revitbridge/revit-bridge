"""test_addin_command, zoom_view, capture_view against a fake add-in (work plan 2026-11-12 addin-autotest, 5)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

import pytest

import revit_bridge.mcp_server as server
from revit_bridge import addin_test
from revit_bridge.evidence.ledger import RECORD_FIELDS, Ledger
from revit_bridge.revit.capabilities import Capabilities
from revit_bridge.revit.client import RevitResponse

from tests.fake_revit import FakeRevit
from tests.test_check_code import CAPS, _tool, revit_env  # noqa: F401
from tests.test_dry_run import CHANGES, FAILURES, _hook

ROOT = Path(__file__).resolve().parents[1]
TEST_CAPS = dict(CAPS, features={**CAPS["features"], "dry_run": True, "addin_test": True})
TEST_KEYS = {"loaded", "load_error", "returned", "command_message", "result_file", "changes", "failures",
             "readback", "picks_done", "restore", "rolled_back", "undone", "restored_clean", "timed_out",
             "foreground_ok", "screenshot", "dll_sha256", "error", "evidence_id", "hint", "warnings"}
PICKS = [{"element_id": 1664030, "fraction": 0.5}, {"key": "Escape"}]
INPUT = {"layer_mm": 10, "door_mark": "M08"}
NAMES = ("mcp__revit-bridge__test_addin_command", "mcp__plugin_revit-bridge_revit-bridge__test_addin_command",
         "mcp__revit-bridge__zoom_view", "mcp__plugin_revit-bridge_revit-bridge__zoom_view",
         "mcp__revit-bridge__capture_view", "mcp__plugin_revit-bridge_revit-bridge__capture_view")


@pytest.fixture
def dll(tmp_path) -> Path:
    path = tmp_path / "bin" / "WallKit.dll"
    path.parent.mkdir()
    path.write_bytes(b"MZ\x90\x00 not really a PE file")
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ran(path: Path, **overrides) -> dict:
    """The add-in's reply to a test that ran and restored the model (work plan 4.3)."""
    return {"loaded": True, "load_error": "", "returned": "Succeeded", "command_message": "",
            "result_file": {"result": "Succeeded", "message": "1 door, 1 window"}, "changes": CHANGES,
            "failures": FAILURES, "readback": json.dumps({"Width": 210}),
            "picks_done": [{"requested": 1664030, "screen": [812, 440], "attempts": 1}],
            "restore": "group_rollback", "rolled_back": True, "undone": 0, "restored_clean": True,
            "timed_out": False, "foreground_ok": True, "screenshot": "", "dll_sha256": sha(path), **overrides}


def addin(answer=None, caps=TEST_CAPS, respond=None, view=None):
    def handler(request):
        method, rid = request["method"], request["id"]
        if method == "get_bridge_capabilities":
            return FakeRevit.ok(rid, caps)
        if method == "test_addin_command_in_revit":
            return respond(request) if respond is not None else FakeRevit.ok(rid, answer)
        if method in ("zoom_view_in_revit", "capture_view_in_revit"):
            return FakeRevit.ok(rid, view or {"view": "Level 1", "rect": [0, 0, 1600, 900], "path": "C:/x.png",
                                              "foreground_ok": True})
        return FakeRevit.default_handler(request)
    return handler


def call(handler, revit_env, *calls):
    return asyncio.run(_tool(handler, revit_env, list(calls)))


def methods(requests):
    return [r["method"] for r in requests]


# -- test_addin_command ------------------------------------------------------------------------

def test_a_test_needs_no_token_and_passes_the_reply_through(revit_env, dll):
    replies, requests = call(addin(ran(dll)), revit_env, ("test_addin_command", {
        "dll": str(dll), "command_class": "WallKit.Command", "picks": PICKS, "input": INPUT,
        "readback_code": "return 1;", "zoom_to": 1664030}))
    out = replies[0]
    assert set(out) == TEST_KEYS and out["error"] is None and out["hint"] is None and out["warnings"] == []
    assert out["returned"] == "Succeeded" and out["changes"] == CHANGES and out["restored_clean"] is True
    assert methods(requests) == ["get_bridge_capabilities", "test_addin_command_in_revit"]
    assert requests[1]["params"] == {"dll": str(dll.resolve()), "class": "WallKit.Command", "timeout_seconds": 120,
                                     "picks": PICKS, "input": INPUT, "readback_code": "return 1;",
                                     "zoom_to": 1664030}                      # no restore: the add-in chooses
    record = server._ledger.get(out["evidence_id"])
    assert list(record) == list(RECORD_FIELDS) and record["action"] == "addin_test"
    for field in ("tool", "spec_hash", "projection_hash", "token_prefix", "confirmed_by", "channel"):
        assert record[field] is None, field
    assert record["params"] == {"dll": str(dll.resolve()), "dll_sha256": sha(dll), "class": "WallKit.Command",
                                "picks": PICKS, "input": INPUT, "zoom_to": 1664030, "timeout_seconds": 120,
                                "restore": None}
    assert record["code_head"] == "return 1;" and record["success"] is True and record["error"] is None
    assert record["result_summary"] == {"loaded": True, "returned": "Succeeded", "timed_out": False,
                                        "changes": {"added": 3, "modified": 1, "deleted": 0}, "failures": 1,
                                        "restore": "group_rollback", "restored_clean": True,
                                        "dll_sha256": sha(dll)}


def test_restore_is_passed_through_only_when_given(revit_env, dll):
    replies, requests = call(addin(ran(dll, restore="undo", undone=1)), revit_env, ("test_addin_command", {
        "dll": str(dll), "command_class": "WallKit.Command", "restore": "undo", "timeout_seconds": 10}))
    assert replies[0]["error"] is None and replies[0]["undone"] == 1
    assert requests[1]["params"] == {"dll": str(dll.resolve()), "class": "WallKit.Command", "timeout_seconds": 10,
                                     "restore": "undo"}


class _Recording:
    """A client that answers like a test add-in and records each request's timeout."""

    def __init__(self, path: Path):
        self.path, self.calls = path, []

    async def send_command(self, method, params=None, timeout=None):
        self.calls.append((method, timeout))
        if method == "get_bridge_capabilities":
            return RevitResponse(success=True, result=TEST_CAPS)
        return RevitResponse(success=True, result=ran(self.path))


@pytest.mark.parametrize("seconds", [120, 10, 600])
def test_the_wait_is_the_test_timeout_plus_30_seconds(tmp_path, dll, seconds):
    client = _Recording(dll)
    out = asyncio.run(addin_test.test_addin_command(client, str(dll), "WallKit.Command", timeout_seconds=seconds,
                                                    ledger=Ledger(tmp_path / "evidence")))
    assert out["error"] is None
    assert client.calls == [("get_bridge_capabilities", None), ("test_addin_command_in_revit", seconds + 30)]


@pytest.mark.parametrize("args,error,warning", [
    ({"dll": "{missing}"}, "dll_not_found", "{missing}: no such file"),
    ({"dll": "{folder}"}, "dll_not_found", "{folder}: no such file"),
    ({"command_class": " "}, "invalid_args", "command_class: the full name of the IExternalCommand class is required"),
    ({"timeout_seconds": 0}, "invalid_args", "timeout_seconds: a positive whole number of seconds, not 0"),
    ({"restore": "commit"}, "invalid_args", "restore: one of group_rollback, undo or none, not 'commit'"),
    ({"readback_code": 'return System.IO.File.ReadAllText("x");'}, "blocked",
     "readback_code: Blocked pattern: System.IO (file operations)"),
], ids=["missing", "folder", "class", "timeout", "restore", "readback"])
def test_local_refusals_never_reach_the_addin(revit_env, dll, args, error, warning):
    missing, folder = str(dll.parent / "Gone.dll"), str(dll.parent)
    given = {"dll": str(dll), "command_class": "WallKit.Command", **args}
    given = {k: (v.format(missing=missing, folder=folder) if isinstance(v, str) and k == "dll" else v)
             for k, v in given.items()}
    replies, requests = call(addin(ran(dll)), revit_env, ("test_addin_command", given))
    out = replies[0]
    assert set(out) == TEST_KEYS and out["error"] == error and out["evidence_id"] is None
    assert out["warnings"][0] == warning.format(missing=missing, folder=folder)
    assert requests == [] and server._ledger.recent() == []


@pytest.mark.parametrize("answer", [
    lambda path: ran(path, restored_clean=False),
    lambda path: {k: v for k, v in ran(path).items() if k != "restored_clean"},
    lambda path: ran(path, restored_clean="true"),
    lambda path: "garbage",
], ids=["false", "missing", "text", "not_an_object"])
def test_a_test_that_did_not_restore_the_model_is_reported_at_once(revit_env, dll, answer):
    replies, _ = call(addin(answer(dll)), revit_env, ("test_addin_command", {
        "dll": str(dll), "command_class": "WallKit.Command"}))
    out = replies[0]
    assert set(out) == TEST_KEYS and out["error"] == "addin_test_not_restored"
    assert out["hint"] == addin_test.NOT_RESTORED_HINT and "tell the designer now" in out["hint"]
    record = server._ledger.get(out["evidence_id"])
    assert record["success"] is False and record["error"] == "addin_test_not_restored"


def test_a_dll_the_addin_hashed_otherwise_is_an_error(revit_env, dll):
    replies, _ = call(addin(ran(dll, dll_sha256="0" * 64)), revit_env, ("test_addin_command", {
        "dll": str(dll), "command_class": "WallKit.Command"}))
    out = replies[0]
    assert out["error"] == "dll_hash_mismatch" and out["hint"] == addin_test.HASH_MISMATCH_HINT
    assert out["warnings"] == [f"dll_sha256: the server hashed {sha(dll)}, the add-in reports '{'0' * 64}'"]
    # an upper-case hash is the same hash; a DLL that did not load has nothing to compare
    replies, _ = call(addin(ran(dll, dll_sha256=sha(dll).upper())), revit_env, ("test_addin_command", {
        "dll": str(dll), "command_class": "WallKit.Command"}))
    assert replies[0]["error"] is None
    replies, _ = call(addin(ran(dll, loaded=False, load_error="class not found", dll_sha256="")), revit_env,
                      ("test_addin_command", {"dll": str(dll), "command_class": "WallKit.Nope"}))
    assert replies[0]["error"] is None and replies[0]["load_error"] == "class not found"


def test_no_reply_is_recorded_with_the_restore_unconfirmed(revit_env, dll):
    replies, _ = call(addin(respond=lambda request: FakeRevit.error(request["id"], -32603, "a test is running")),
                      revit_env, ("test_addin_command", {"dll": str(dll), "command_class": "WallKit.Command"}))
    out = replies[0]
    assert out["error"] == "a test is running" and out["hint"] == addin_test.NO_REPLY_HINT
    summary = server._ledger.get(out["evidence_id"])["result_summary"]
    assert summary["restored_clean"] is None and summary["changes"] == {"added": None, "modified": None,
                                                                       "deleted": None}


# -- the capability ---------------------------------------------------------------------------------

@pytest.mark.parametrize("caps", [CAPS, dict(TEST_CAPS, features={**TEST_CAPS["features"], "addin_test": False})],
                         ids=["absent", "false"])
def test_an_addin_without_the_feature_answers_no_addin_test(revit_env, dll, caps):
    replies, requests = call(addin(ran(dll), caps=caps), revit_env,
                             ("test_addin_command", {"dll": str(dll), "command_class": "WallKit.Command"}),
                             ("zoom_view", {"element_id": 1664030}), ("capture_view", {}))
    test, zoom, capture = replies
    assert test == addin_test.test_reply(error="no_addin_test", hint=addin_test.NO_ADDIN_TEST_HINT)
    assert zoom == addin_test.view_reply(error="no_addin_test", hint=addin_test.NO_ADDIN_TEST_HINT)
    assert capture == addin_test.capture_reply(error="no_addin_test", hint=addin_test.NO_ADDIN_TEST_HINT)
    assert methods(requests) == ["get_bridge_capabilities"] and server._ledger.recent() == []


def test_capabilities_can_addin_test():
    assert Capabilities("supported", info=TEST_CAPS).can_addin_test is True
    assert Capabilities("supported", info=CAPS).can_addin_test is False
    assert not Capabilities("supported", info=dict(CAPS, features={"addin_test": "yes"})).can_addin_test
    assert not Capabilities("unknown", info=TEST_CAPS).can_addin_test


# -- zoom_view, capture_view ------------------------------------------------------------------------

def test_zoom_and_capture_pass_through_and_are_recorded(revit_env):
    replies, requests = call(addin(), revit_env, ("zoom_view", {"element_id": 1664030}), ("zoom_view", {}),
                             ("capture_view", {"element_id": 1664030}), ("capture_view", {"margin_mm": 900}))
    assert [r["params"] for r in requests[1:]] == [{"element_id": 1664030, "margin_mm": 2500}, {"fit": True},
                                                   {"element_id": 1664030, "margin_mm": 1500}, {}]
    zoom, fit, capture, plain = replies
    assert zoom["error"] is None and zoom["view"] == "Level 1" and {"error", "evidence_id", "hint", "warnings"} <= set(zoom)
    assert capture["path"] == "C:/x.png" and capture["foreground_ok"] is True and capture["error"] is None
    lines = server._ledger.recent(10)
    assert [line["action"] for line in lines] == ["capture_view", "capture_view", "zoom_view", "zoom_view"]
    assert lines[1]["params"] == {"element_id": 1664030, "margin_mm": 1500}
    assert lines[1]["result_summary"] == {"path": "C:/x.png", "foreground_ok": True}


# -- regression: the gate and the hook ---------------------------------------------------------------

def test_execute_code_without_a_token_is_still_refused_and_the_hook_lets_the_new_tools_pass(revit_env, monkeypatch):
    monkeypatch.delenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", raising=False)
    replies, requests = call(addin(), revit_env, ("execute_code", {"code": "return 1;"}))
    assert replies[0]["error"] == "confirmation_required" and requests == []
    hook = _hook()
    matcher = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))[
        "hooks"]["PreToolUse"][0]["matcher"]
    for name in NAMES:
        assert hook.decide(name, {"dll": "x.dll", "command_class": "A.B"}) is None, name
        assert re.search(matcher, name) is None, name
    assert hook.decide("mcp__revit-bridge__execute_code", {}) == hook.REASON


def test_the_tools_have_no_token_and_the_planned_descriptions():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools["test_addin_command"].input_schema["properties"]) == {
        "dll", "command_class", "picks", "input", "readback_code", "zoom_to", "timeout_seconds", "restore"}
    assert set(tools["zoom_view"].input_schema["properties"]) == {"element_id", "margin_mm"}
    assert set(tools["capture_view"].input_schema["properties"]) == {"element_id", "margin_mm"}
    assert tools["test_addin_command"].description == (
        "Run a compiled add-in command in the open Revit model without installing it and\n"
        "without keeping anything: the command runs on the real project, its picks are\n"
        "clicked for it on the elements you name by id, its inputs come from\n"
        "<name>.input.json, and when it returns the model is restored. Needs no\n"
        "confirmation and no token. Returns what the command returned, the result file it\n"
        "wrote, what it added, modified and deleted (counts by category), every Revit\n"
        "warning or error the command left to Revit (one the command deleted or resolved\n"
        "itself is not seen), and what your read-back code saw before the restore. It takes\n"
        "over the mouse and needs Revit in the foreground while it runs. Plan views only.")
    assert tools["zoom_view"].description == (
        "Zoom the active view to an element (with a margin) or to fit. Changes only the view.")
    assert tools["capture_view"].description == (
        "Save a picture of the active view, optionally zoomed to an element first, and\n"
        "return its path. Use it when the data cannot settle a question; it is not a test.")


# -- the skill texts (work plan 6) --------------------------------------------------------------------

def test_the_skill_texts_are_in_place():
    skills = ROOT / "plugin" / "skills"
    dev = (skills / "revit-addin-dev" / "SKILL.md").read_text(encoding="utf-8")
    section = "## Make the command testable by script"
    assert dev.index("## Non-negotiables in every command") < dev.index(section) < dev.index("## Verify and hand over")
    assert ("When a file named `<AssemblyName>.input.json` sits next to the add-in DLL, read the values from it"
            in dev and "environment variable" not in dev[dev.index(section):dev.index("## Verify and hand over")])
    assert 'a command that was never run on a model is reported as "built, not run".' in dev
    test = (skills / "revit-addin-test" / "SKILL.md").read_text(encoding="utf-8")
    assert "the bridge does not run the add-in's commands" not in test
    assert "`test_addin_command` runs a built DLL in that Revit without installing it" in test
    assert test.index("## Test through the bridge") < test.index("## One session")
    assert "6. `restored_clean` must be true after every run." in test
    bridge = (skills / "revit-bridge" / "SKILL.md").read_text(encoding="utf-8")
    dry = bridge[bridge.index("## Dry run: try it without changing anything"):bridge.index("## Parameter source")]
    assert dry.rstrip().endswith("None of the three needs a confirmation; none of them keeps anything in the model.")

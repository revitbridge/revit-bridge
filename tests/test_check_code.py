"""check_code, capability discovery and the environment, against new and old fake add-ins.

A "new" add-in answers ``get_bridge_capabilities`` and ``check_code_in_revit``;
an "old" one answers both with JSON-RPC -32601 (the FakeRevit default).
"""
from __future__ import annotations

import asyncio
import json

import pytest

import revit_bridge.mcp_server as server
from revit_bridge import checking
from revit_bridge.guidance import code_symbols, recommend
from revit_bridge.guidance import index as guidance_index
from revit_bridge.guidance import load as load_guidance
from revit_bridge.paths import guidance_dir
from revit_bridge.revit import capabilities as caps_mod
from revit_bridge.revit.capabilities import Capabilities, discover, error_kind
from revit_bridge.revit.client import RevitClient, RevitResponse
from revit_bridge.revit.environment import ENV_PROBE, get_environment, version_label
from revit_bridge.revit.pool import RevitClientPool
from revit_bridge.revit.settings import RevitSettings

from tests.fake_revit import FakeRevit
from tests.test_guidance import lesson, make_root

CAPS = {
    "protocol": 2,
    "addin_version": "0.2.0",
    "methods": ["send_code_to_revit", "check_code_in_revit", "get_bridge_capabilities"],
    "features": {"check_code": True, "structured_diagnostics": True, "kit": False},
    "revit": {"version_number": "2026", "version_build": "20260401_1515(x64)",
              "version_name": "Autodesk Revit 2026.5"},
    "runtime": {"framework": ".NET 8.0.11", "clr": "8.0.11"},
    "kit": None,
}
CS1061 = {
    "id": "CS1061", "severity": "error", "line": 3, "column": 18,
    "message": "'Wall' does not contain a definition for 'LocationCurve' and no accessible extension "
               "method 'LocationCurve' accepting a first argument of type 'Wall' could be found (are you "
               "missing a using directive or an assembly reference?)",
    "symbol": "LocationCurve", "in_wrapper": False,
}
FAILED_CHECK = {"compiled": False, "diagnostics": [CS1061], "truncated": 0,
                "fingerprint": "sha256:abc", "wrapper_version": 1, "errorMessage": ""}
CLEAN_CHECK = {"compiled": True, "diagnostics": [], "truncated": 0,
               "fingerprint": "sha256:def", "wrapper_version": 1, "errorMessage": ""}
PROBE_REPLY = {"version_number": "2024", "version_build": "20230308_1635(x64)",
               "version_name": "Autodesk Revit 2024", "clr": "4.0.30319.42000"}
REPLY_KEYS = {"schema_version", "checked", "compiled", "blocked", "capability", "reason", "diagnostics",
              "truncated", "fingerprint", "hints", "guidance", "environment", "warnings"}
ENV_KEYS = {"source", "revit", "runtime", "kit", "capability", "error"}


def new_addin(check_reply=FAILED_CHECK, caps=CAPS):
    """A handler for an add-in that has both new methods."""
    def handler(request):
        method, rid = request["method"], request["id"]
        if method == "get_bridge_capabilities":
            return FakeRevit.ok(rid, caps)
        if method == "check_code_in_revit":
            return FakeRevit.ok(rid, check_reply)
        return FakeRevit.default_handler(request)
    return handler


def old_addin(probe=lambda rid: FakeRevit.code_result(rid, PROBE_REPLY)):
    """A handler for an add-in from before the new methods: -32601 for them, the probe answered."""
    def handler(request):
        if request["method"] == "send_code_to_revit" and request["params"]["code"] == ENV_PROBE:
            return probe(request["id"])
        return FakeRevit.default_handler(request)
    return handler


def _settings(fake: FakeRevit, **overrides) -> RevitSettings:
    base = {"host": "127.0.0.1", "port": fake.port, "timeout": 2.0, "connect_timeout": 1.0}
    base.update(overrides)
    return RevitSettings(**base)


async def _with_client(handler, operation, token=None, **settings):
    """Run ``operation(client, fake)`` on a fresh client connected to a fake add-in."""
    async with FakeRevit(handler, token=token) as fake:
        client = RevitClient(settings=_settings(fake, **settings))
        try:
            await client.connect()
            return await operation(client, fake)
        finally:
            await client.disconnect()


@pytest.fixture
def revit_env(monkeypatch):
    """Direct the connection pool at a fake add-in."""
    def apply(port: int):
        monkeypatch.setenv("REVIT_BRIDGE_HOST", "127.0.0.1")
        monkeypatch.setenv("REVIT_BRIDGE_PORT", str(port))
        monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "2")
        monkeypatch.delenv("REVIT_BRIDGE_TOKEN", raising=False)
    return apply


async def _tool(handler, revit_env, calls):
    """Call MCP tools ``[(name, args), ...]`` against a fake add-in; (replies, requests)."""
    async with FakeRevit(handler) as fake:
        revit_env(fake.port)
        try:
            replies = []
            for name, args in calls:
                result = await server.mcp.call_tool(name, args)
                replies.append(json.loads(result.content[0].text))
            return replies, fake.requests
        finally:
            await RevitClientPool.disconnect()


# -- capability discovery -------------------------------------------------------------------

def test_a_new_addin_is_supported_and_cached_per_connection():
    async def scenario(client, fake):
        first = await discover(client)
        second = await discover(client)
        asked_once = [r["method"] for r in fake.requests]
        await client.disconnect()
        await client.connect()                           # a new connection asks again
        third = await discover(client)
        return first, second, third, asked_once, [r["method"] for r in fake.requests]

    first, second, third, asked_once, asked = asyncio.run(_with_client(new_addin(), scenario))
    assert first.state == "supported" and first.reason is None and second is first
    assert first.can_check_code and first.revit["version_name"] == "Autodesk Revit 2026.5"
    assert first.as_dict()["protocol"] == 2 and first.as_dict()["addin_version"] == "0.2.0"
    assert asked_once == ["get_bridge_capabilities"]
    assert third.state == "supported" and asked == ["get_bridge_capabilities"] * 2


def test_the_capability_request_carries_no_confirm_and_no_params():
    async def scenario(client, fake):
        await discover(client)
        return fake.requests[0]

    request = asyncio.run(_with_client(new_addin(), scenario))
    assert request["method"] == "get_bridge_capabilities" and request["params"] == {}


def test_an_old_addin_is_unsupported_with_the_original_reason():
    async def scenario(client, fake):
        return await discover(client), await discover(client), [r["method"] for r in fake.requests]

    caps, again, asked = asyncio.run(_with_client(old_addin(), scenario))
    assert caps.state == "unsupported" and caps.error_code == -32601
    assert "get_bridge_capabilities" in caps.reason and "not found" in caps.reason
    assert not caps.can_check_code and again is caps and asked == ["get_bridge_capabilities"]


def test_no_connection_is_unknown_never_unsupported():
    async def scenario():
        async with FakeRevit() as fake:
            port = fake.port
        client = RevitClient(settings=RevitSettings(host="127.0.0.1", port=port, timeout=1.0, connect_timeout=0.5))
        return await discover(client)

    caps = asyncio.run(scenario())
    assert caps.state == "unknown" and caps.error_kind == "unreachable" and caps.reason


def test_auth_timeout_and_other_rpc_errors_are_unknown_and_not_cached():
    async def wrong_token(client, fake):
        return await discover(client)

    caps = asyncio.run(_with_client(new_addin(), wrong_token, token="pre-shared"))
    assert caps.state == "unknown" and caps.error_kind == "auth" and "Unauthorized" in caps.reason

    async def hangs(client, fake):
        return await discover(client)

    caps = asyncio.run(_with_client(lambda request: [], hangs, timeout=0.3))
    assert caps.state == "unknown" and caps.error_kind == "timeout"

    failures = [1]

    def flaky(request):
        if request["method"] == "get_bridge_capabilities" and failures:
            failures.pop()
            return FakeRevit.error(request["id"], -32603, "Internal error: boom")
        return new_addin()(request)

    async def twice(client, fake):
        return await discover(client), await discover(client), len(fake.requests)

    first, second, asked = asyncio.run(_with_client(flaky, twice))
    assert first.state == "unknown" and first.error_kind == "rpc_error" and first.error_code == -32603
    assert first.reason == "Internal error: boom"
    assert second.state == "supported" and asked == 2          # unknown was asked again


def test_a_malformed_reply_is_unknown():
    caps = caps_mod.from_response(RevitResponse(success=True, result={"success": False, "errorMessage": "x"}))
    assert caps.state == "unknown" and caps.error_kind == "bad_reply"
    caps = caps_mod.from_response(RevitResponse(success=False, error="Timeout after 2.0s"))
    assert caps.state == "unknown" and caps.error_kind == "timeout"


def test_error_kinds_cover_the_addin_messages():
    assert error_kind("Unauthorized: invalid or missing token") == "auth"
    assert error_kind("Timeout after 60.0s") == "timeout"
    assert error_kind("x " + "".join(map(chr, (0x8D85, 0x65F6)))) == "timeout"   # the add-in's Chinese text
    assert error_kind("Object reference not set to an instance of an object.") == "no_document"
    assert error_kind("Starting a new transaction is not permitted. ... read-only mode") == "read_only"
    assert error_kind("The document is read-only") == "read_only"
    assert error_kind("runtime outdated") is None and error_kind(None) is None


# -- the environment -----------------------------------------------------------------------------

def test_environment_from_the_capabilities_needs_no_probe():
    async def scenario(client, fake):
        return await get_environment(client), [r["method"] for r in fake.requests]

    env, asked = asyncio.run(_with_client(new_addin(), scenario))
    assert set(env) == ENV_KEYS and env["source"] == "capabilities" and env["error"] is None
    assert env["revit"] == CAPS["revit"] and env["runtime"] == CAPS["runtime"] and env["kit"] is None
    assert env["capability"]["state"] == "supported"
    assert asked == ["get_bridge_capabilities"]                 # no send_code_to_revit, no transaction
    assert version_label(env) == "Revit 2026.5"


def test_an_old_addin_falls_back_to_the_probe_snippet():
    async def scenario(client, fake):
        return await get_environment(client), fake.requests

    env, requests = asyncio.run(_with_client(old_addin(), scenario))
    assert [r["method"] for r in requests] == ["get_bridge_capabilities", "send_code_to_revit"]
    assert requests[1]["params"]["code"] == ENV_PROBE and "confirm" not in requests[1]["params"]
    assert "app.VersionNumber" in ENV_PROBE and "Environment.Version.ToString()" in ENV_PROBE
    assert env["source"] == "probe" and env["error"] is None and env["kit"] is None
    assert env["revit"] == {k: PROBE_REPLY[k] for k in ("version_number", "version_build", "version_name")}
    assert env["runtime"] == {"framework": None, "clr": "4.0.30319.42000"}
    assert env["capability"]["state"] == "unsupported"
    assert version_label(env) == "Revit 2024"


@pytest.mark.parametrize("message,kind", [
    ("Object reference not set to an instance of an object.", "no_document"),
    ("InvalidOperationException: Starting a new transaction is not permitted (read-only mode)", "read_only"),
])
def test_probe_failures_are_named_not_reported_as_no_kit(message, kind):
    probe = lambda rid: FakeRevit.code_result(rid, None, success=False, error=message)   # noqa: E731

    async def scenario(client, fake):
        return await get_environment(client)

    env = asyncio.run(_with_client(old_addin(probe), scenario))
    assert env["source"] == "probe" and env["error"] == f"{kind}: {message}"
    assert env["revit"]["version_number"] is None and env["kit"] is None
    assert "kit" not in env["error"].lower()


def test_probe_timeout_auth_and_no_connection_are_distinct_errors():
    async def scenario(client, fake):
        return await get_environment(client)

    env = asyncio.run(_with_client(old_addin(lambda rid: []), scenario, timeout=0.3))
    assert env["source"] == "probe" and env["error"].startswith("timeout: ")

    async def asked(client, fake):
        return await get_environment(client), [r["method"] for r in fake.requests]

    env, methods = asyncio.run(_with_client(old_addin(), asked, token="pre-shared"))
    assert env["source"] is None and env["error"].startswith("auth: Unauthorized")
    assert methods == ["get_bridge_capabilities"]               # unknown: no probe after a refusal

    env = asyncio.run(get_environment(None, Capabilities("unknown", reason="refused", error_kind="unreachable")))
    assert env["error"] == "unreachable: refused" and env["capability"]["state"] == "unknown"
    assert env["source"] is None and env["kit"] is None


# -- check_code ------------------------------------------------------------------------------------

def test_check_code_on_a_new_addin_compiles_without_running(revit_env):
    code = "var wall = (Wall)document.GetElement(new ElementId(1));\nreturn wall.LocationCurve;"
    replies, requests = asyncio.run(_tool(new_addin(), revit_env, [
        ("check_code", {"code": code, "parameters": [1, "a"]}),
        ("check_code", {"code": code}),
    ]))
    out = replies[0]
    assert set(out) == REPLY_KEYS and out["schema_version"] == 1
    assert out["checked"] is True and out["compiled"] is False and out["blocked"] is False
    assert out["capability"] == "supported" and out["reason"] is None
    assert out["diagnostics"] == [CS1061] and out["truncated"] == 0 and out["fingerprint"] == "sha256:abc"
    assert out["hints"] == [{
        "id": "CS1061", "symbol": "LocationCurve",
        "text": "Revit 2026.5: 'Wall' has no 'LocationCurve'. If an API lookup tool (search_revit_api) is "
                "connected, query 'Wall LocationCurve' before changing the code.",
    }]
    # the code names ElementId and Wall: the fix lessons in category order (core first), bodies inline
    assert [g["id"] for g in out["guidance"]] == [
        "revit-core/elementid-value", "revit-geometry/wall-length-from-location"]
    assert all(g["body"] and g["applicability"] == "verified" for g in out["guidance"])
    assert out["warnings"] == []
    assert out["environment"]["source"] == "capabilities" and out["environment"]["revit"] == CAPS["revit"]
    # compile only: the capability once per connection, then two checks; nothing ran in Revit
    methods = [r["method"] for r in requests]
    assert methods == ["get_bridge_capabilities", "check_code_in_revit", "check_code_in_revit"]
    assert requests[1]["params"] == {"code": code, "parameters": [1, "a"]}
    assert requests[2]["params"] == {"code": code, "parameters": []}


def test_check_code_reports_a_clean_compile(revit_env):
    replies, _ = asyncio.run(_tool(new_addin(CLEAN_CHECK), revit_env, [("check_code", {"code": "return 1;"})]))
    out = replies[0]
    assert out["checked"] is True and out["compiled"] is True and out["diagnostics"] == [] and out["hints"] == []
    assert out["fingerprint"] == "sha256:def"


def test_check_code_on_an_old_addin_is_not_checked_and_runs_nothing(revit_env):
    replies, requests = asyncio.run(_tool(old_addin(), revit_env, [("check_code", {"code": "return 1;"})]))
    out = replies[0]
    assert set(out) == REPLY_KEYS
    assert out["checked"] is False and out["compiled"] is None and out["capability"] == "unsupported"
    assert out["reason"].startswith("method_not_found: ") and "not found" in out["reason"]
    assert out["environment"] is None and out["hints"] == [] and out["guidance"] == []
    assert [r["method"] for r in requests] == ["get_bridge_capabilities"]   # no send_code_to_revit


def test_check_code_without_revit_is_unknown(monkeypatch):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "1")
    out = json.loads(asyncio.run(server.mcp.call_tool("check_code", {"code": "return 1;"})).content[0].text)
    assert set(out) == REPLY_KEYS
    assert out["checked"] is False and out["capability"] == "unknown" and out["compiled"] is None
    assert out["reason"].startswith("unreachable: ")


def test_check_code_blocks_locally_before_asking_revit(revit_env):
    code = 'return System.IO.File.ReadAllText("c:/secret.txt");'
    replies, requests = asyncio.run(_tool(new_addin(), revit_env, [("check_code", {"code": code})]))
    out = replies[0]
    assert out["checked"] is True and out["compiled"] is False and out["blocked"] is True
    assert out["capability"] is None and any("System.IO" in w for w in out["warnings"])
    assert requests == []                                       # never reached the add-in


def test_check_code_without_the_feature_or_with_a_compiler_failure_is_not_checked():
    no_feature = dict(CAPS, features={"check_code": False}, methods=["send_code_to_revit", "get_bridge_capabilities"])

    async def scenario(client, fake):
        return await checking.check_code(client, "return 1;", None), [r["method"] for r in fake.requests]

    out, methods = asyncio.run(_with_client(new_addin(caps=no_feature), scenario))
    assert out["checked"] is False and out["capability"] == "supported"
    assert out["reason"].startswith("no_check_code: ") and methods == ["get_bridge_capabilities"]
    assert out["environment"]["source"] == "capabilities"

    crashed = {"compiled": False, "diagnostics": [], "truncated": 0, "fingerprint": "sha256:0",
               "wrapper_version": 1, "errorMessage": "Roslyn threw"}
    out, _ = asyncio.run(_with_client(new_addin(crashed), scenario))
    assert out["checked"] is False and out["compiled"] is None and out["reason"] == "compiler_error: Roslyn threw"

    def missing_method(request):
        if request["method"] == "check_code_in_revit":
            return FakeRevit.error(request["id"], -32601, "Method 'check_code_in_revit' not found")
        return new_addin()(request)

    out, _ = asyncio.run(_with_client(missing_method, scenario))
    assert out["checked"] is False and out["reason"].startswith("method_not_found: ")


# -- hints ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("diag_id,message,symbol,text", [
    ("CS0117", "'Wall' does not contain a definition for 'Create'", "Create",
     "Revit 2026.5: 'Wall' has no 'Create'. If an API lookup tool (search_revit_api) is connected, "
     "query 'Wall Create' before changing the code."),
    ("CS0246", "The type or namespace name 'FilteredElementColector' could not be found (are you missing "
               "a using directive or an assembly reference?)", "FilteredElementColector",
     "Revit 2026.5: type or namespace 'FilteredElementColector' is not found with the snippet's usings "
     "(System, System.Linq, System.Collections.Generic, Autodesk.Revit.DB, Autodesk.Revit.UI) and "
     "references (base library, RevitAPI, RevitAPIUI). If an API lookup tool (search_revit_api) is "
     "connected, query 'FilteredElementColector' before changing the code."),
    ("CS0103", "The name 'doc' does not exist in the current context", "doc",
     "Revit 2026.5: 'doc' does not exist here; the snippet has only `document` and `parameters` in scope."),
    ("CS1501", "No overload for method 'Create' takes 3 arguments", "Create",
     "Revit 2026.5: no overload of 'Create' takes 3 arguments. If an API lookup tool (search_revit_api) "
     "is connected, query 'Create' before changing the code."),
    ("CS1503", "Argument 2: cannot convert from 'double' to 'Autodesk.Revit.DB.XYZ'", "XYZ",
     "Revit 2026.5: argument 2 is 'double' but must be 'XYZ'. If an API lookup tool (search_revit_api) "
     "is connected, query 'XYZ' before changing the code."),
    ("CS7036", "There is no argument given that corresponds to the required parameter 'level' of "
               "'Wall.Create(Document, Curve, ElementId, bool)'", "Wall.Create",
     "Revit 2026.5: 'Wall.Create' needs an argument for its parameter 'level'. If an API lookup tool "
     "(search_revit_api) is connected, query 'Wall Create' before changing the code."),
])
def test_hints_for_the_listed_ids(diag_id, message, symbol, text):
    hint = checking.hint_for({"id": diag_id, "severity": "error", "message": message, "symbol": None},
                             "Revit 2026.5")
    assert hint == {"id": diag_id, "symbol": symbol, "text": text}


def test_hints_only_for_the_listed_ids_once_each_and_without_a_version_when_unknown():
    other = {"id": "CS0161", "severity": "error", "message": "'Execute': not all code paths return a value"}
    warning = {"id": "CS0168", "severity": "warning", "message": "The variable 'x' is declared but never used"}
    hints = checking.hints_for([CS1061, other, warning, dict(CS1061, line=9)])
    assert [h["id"] for h in hints] == ["CS1061"]
    assert hints[0]["text"].startswith("'Wall' has no 'LocationCurve'.")
    upper = checking.hint_for({"id": "CS0103", "message": "The name 'UnitTypeId' does not exist in the current context"})
    assert upper["text"].endswith("query 'UnitTypeId' before changing the code.")
    assert checking.hint_for({"id": "CS1061", "message": "no quotes here"}) is None
    assert set(checking.HINT_IDS) == {"CS1061", "CS0117", "CS0246", "CS0103", "CS1501", "CS1503", "CS7036"}


# -- guidance: lessons in the reply ----------------------------------------------------------------

WALL = "revit-geometry/wall-length-from-location"
COLLECTOR = "revit-collect/collector-needs-filter"
ITEM_HEAD = {"id", "revision", "hash", "layer", "category", "title", "kind", "stage", "revit", "applicability", "why"}
BODY_ITEM = ITEM_HEAD | {"body", "bytes", "truncated"}
POINTER_ITEM = ITEM_HEAD | {"summary"}
WALL_CODE = "var wall = document.GetElement(parameters[0] as string) as Wall;\nreturn wall.LocationCurve;"
# Names that hit five fix lessons: internal units, ElementId, the collector, the wall and curve length.
BUSY_CODE = (
    "var ids = new FilteredElementCollector(document).OfClass(typeof(Wall)).ToElementIds();\n"
    "var wall = document.GetElement(ids.First()) as Wall;\n"
    "double feet = wall.LocationCurve.Curve.Length;\n"
    "return UnitUtils.ConvertFromInternalUnits(feet, UnitTypeId.Meters) + new ElementId(1L).Value;"
)


@pytest.fixture(scope="module")
def seed_library():
    return load_guidance(guidance_dir())


def failed_check(*diagnostics) -> dict:
    return dict(FAILED_CHECK, diagnostics=list(diagnostics))


def _checked(handler, code: str, library) -> dict:
    """``checking.check_code`` against a fake add-in with this guidance library."""
    async def scenario(client, fake):
        return await checking.check_code(client, code, None, library=library)
    return asyncio.run(_with_client(handler, scenario))


def _never_loaded(calls: list):
    def library():
        calls.append("load")
        raise AssertionError("the guidance library was asked for")
    return library


def test_compile_errors_inline_the_wall_lesson(revit_env):
    replies, _ = asyncio.run(_tool(new_addin(), revit_env, [("check_code", {"code": WALL_CODE})]))
    out = replies[0]
    assert set(out) == REPLY_KEYS and out["compiled"] is False and out["warnings"] == []
    assert out["hints"] == checking.hints_for([CS1061], "Revit 2026.5")             # hints unchanged
    assert 1 <= len(out["guidance"]) <= 2
    first = out["guidance"][0]
    assert first["id"] == WALL and set(first) == BODY_ITEM
    assert first["body"].startswith("## Scenario") and first["truncated"] is False
    assert first["bytes"] == len(first["body"].encode("utf-8")) <= 2400
    assert first["applicability"] == "verified" and first["layer"] == "framework"
    assert first["why"][0] == "stage:fix"
    error_hits = [w for w in first["why"] if w.startswith("error:CS1061+symbol:")]
    assert len(error_hits) == 1 and "LocationCurve" in error_hits[0].split("+symbol:")[1].split(",")
    assert all(set(g) == BODY_ITEM and g["bytes"] <= 2400 for g in out["guidance"])


def test_an_error_without_any_lesson_symbol_gets_no_guidance(seed_library):
    other = dict(CS1061, message="'Foo' does not contain a definition for 'Bar'", symbol="Bar")
    out = _checked(new_addin(failed_check(other)), "var foo = new Foo();\nreturn foo.Bar;", seed_library)
    assert out["checked"] is True and out["compiled"] is False
    assert out["guidance"] == [] and out["warnings"] == []
    assert [h["symbol"] for h in out["hints"]] == ["Bar"]


def test_more_than_two_fix_lessons_inline_only_the_first_two(seed_library):
    symbols = ["LocationCurve"] + code_symbols(BUSY_CODE)
    every = recommend(seed_library, "fix", errors=["CS1061"], symbols=symbols)["items"]
    assert len(every) >= 3
    out = _checked(new_addin(), BUSY_CODE, seed_library)
    assert set(out) == REPLY_KEYS                                   # no "omitted": the reply keys are fixed
    assert [g["id"] for g in out["guidance"]] == [x["id"] for x in every[:2]]
    assert [g["id"] for g in out["guidance"]] == ["revit-core/internal-units-feet", "revit-core/elementid-value"]


def test_long_bodies_are_cut_to_2400_bytes_with_the_read_line(tmp_path):
    long_body = "## Scenario\n\n" + "".join(f"line {i:04d} of a long lesson about walls\n" for i in range(200))
    wide_body = "## Scenario\n\n" + chr(0x5899) * 1200 + "\n"                    # 3 UTF-8 bytes each
    library = load_guidance(make_root(tmp_path, {
        "revit-geometry/lessons/long.md": lesson("revit-geometry/long", body=long_body, order="1",
                                                 triggers="\n  symbols: [Wall]\n  errors: [CS1061]"),
        "revit-geometry/lessons/wide.md": lesson("revit-geometry/wide", body=wide_body, order="2"),
    }))
    out = _checked(new_addin(), WALL_CODE, library)
    assert [g["id"] for g in out["guidance"]] == ["revit-geometry/long", "revit-geometry/wide"]
    for item in out["guidance"]:
        assert item["truncated"] is True
        assert item["bytes"] == len(item["body"].encode("utf-8")) <= 2400
        assert item["body"].rstrip("\n").splitlines()[-1] == f'[truncated: get_guidance(mode="read", ids=["{item["id"]}"])]'
    assert out["guidance"][0]["body"].startswith(long_body[:100])
    assert "error:CS1061+symbol:Wall" in out["guidance"][0]["why"]


def test_a_clean_compile_points_at_write_lessons_without_bodies(seed_library):
    code = "var walls = new FilteredElementCollector(document).OfClass(typeof(Wall)).ToElements();\nreturn walls.Count;"
    out = _checked(new_addin(CLEAN_CHECK), code, seed_library)
    assert out["compiled"] is True and out["warnings"] == [] and set(out) == REPLY_KEYS
    ids = [g["id"] for g in out["guidance"]]
    assert COLLECTOR in ids and 1 <= len(ids) <= 5
    summaries = {x["id"]: x["summary"] for x in guidance_index(seed_library)["items"]}
    for item in out["guidance"]:
        assert set(item) == POINTER_ITEM and "body" not in item
        assert item["summary"] == summaries[item["id"]] and item["why"][0] == "stage:write"
        assert "write" in item["stage"]
    # six write lessons named: still at most budget.items (5); names in comments do not count
    code = BUSY_CODE + "\nvar picked = new UIDocument(document).Selection.GetElementIds(); // LookupParameter"
    chosen = recommend(seed_library, "write", symbols=code_symbols(code))
    assert len(chosen["items"]) + len(chosen["omitted"]) == 6
    busy = _checked(new_addin(CLEAN_CHECK), code, seed_library)
    assert [g["id"] for g in busy["guidance"]] == [x["id"] for x in chosen["items"][:5]]
    assert all(set(g) == POINTER_ITEM for g in busy["guidance"])
    # nothing that names a lesson: no pointers
    assert _checked(new_addin(CLEAN_CHECK), "return 1;", seed_library)["guidance"] == []


def test_unchecked_and_blocked_replies_have_no_guidance_and_never_load_it():
    calls: list = []
    library = _never_loaded(calls)
    out = _checked(old_addin(), "return 1;", library)                               # unsupported
    assert out["checked"] is False and out["guidance"] == []
    no_feature = dict(CAPS, features={"check_code": False}, methods=["send_code_to_revit", "get_bridge_capabilities"])
    out = _checked(new_addin(caps=no_feature), WALL_CODE, library)                  # supported, cannot check
    assert out["checked"] is False and out["guidance"] == []
    crashed = dict(FAILED_CHECK, diagnostics=[], errorMessage="Roslyn threw")
    out = _checked(new_addin(crashed), WALL_CODE, library)                          # compiler_error
    assert out["checked"] is False and out["guidance"] == []
    out = _checked(new_addin(), 'return System.IO.File.ReadAllText("c:/x");', library)   # blocked locally
    assert out["blocked"] is True and out["guidance"] == []
    warning = dict(CS1061, id="CS0168", severity="warning", symbol=None)
    out = _checked(new_addin(failed_check(warning)), WALL_CODE, library)            # no error diagnostic
    assert out["checked"] is True and out["compiled"] is False and out["guidance"] == []
    assert calls == [] and out["warnings"] == []
    out = _checked(new_addin(), WALL_CODE, None)                                    # no library given
    assert out["guidance"] == [] and out["warnings"] == []


def test_check_code_without_revit_has_no_guidance(monkeypatch):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "1")
    out = json.loads(asyncio.run(server.mcp.call_tool("check_code", {"code": WALL_CODE})).content[0].text)
    assert out["checked"] is False and out["guidance"] == [] and set(out) == REPLY_KEYS


def test_a_broken_library_gives_no_guidance_and_a_warning(tmp_path, monkeypatch, revit_env):
    bad = make_root(tmp_path, {"revit-geometry/lessons/a.md": lesson("revit-geometry/b")})
    monkeypatch.setattr(server, "guidance_dir", lambda: bad)
    monkeypatch.setattr(server, "_guidance_library", None)
    replies, _ = asyncio.run(_tool(new_addin(), revit_env, [("check_code", {"code": WALL_CODE})]))
    out = replies[0]
    assert set(out) == REPLY_KEYS and out["guidance"] == []
    assert out["checked"] is True and out["compiled"] is False and out["diagnostics"] == [CS1061]
    assert out["hints"] == checking.hints_for([CS1061], "Revit 2026.5")
    assert len(out["warnings"]) == 1 and out["warnings"][0].startswith("guidance_unavailable: ")
    assert "does not match its path" in out["warnings"][0]
    replies, _ = asyncio.run(_tool(new_addin(CLEAN_CHECK), revit_env, [("check_code", {"code": BUSY_CODE})]))
    assert replies[0]["compiled"] is True and replies[0]["guidance"] == []
    assert replies[0]["warnings"][0].startswith("guidance_unavailable: ")

    def exploding():
        raise RuntimeError("disk gone")
    out = _checked(new_addin(), WALL_CODE, exploding)
    assert out["guidance"] == [] and out["warnings"] == ["guidance_unavailable: RuntimeError: disk gone"]


def test_the_same_input_gives_the_same_guidance(revit_env, seed_library):
    replies, _ = asyncio.run(_tool(new_addin(), revit_env, [
        ("check_code", {"code": BUSY_CODE}), ("check_code", {"code": BUSY_CODE})]))
    assert json.dumps(replies[0]) == json.dumps(replies[1]) and replies[0]["guidance"]
    fresh = load_guidance(guidance_dir())
    for check in (FAILED_CHECK, CLEAN_CHECK):
        once = _checked(new_addin(check), BUSY_CODE, seed_library)
        again = _checked(new_addin(check), BUSY_CODE, fresh)
        assert json.dumps(once["guidance"]) == json.dumps(again["guidance"]) and once["guidance"]


@pytest.mark.parametrize("revit,expected", [
    ({"version_name": "Autodesk Revit 2026.5", "version_number": "2026"}, "2026.5"),
    ({"version_name": "Autodesk Revit 2026", "version_number": "2026"}, "2026"),
    ({"version_name": "Autodesk Revit 2024.3", "version_number": "2024"}, "2024"),
    ({"version_name": "Autodesk Revit 2025.6"}, "2025.5"),
    ({"version_name": None, "version_number": "2027"}, "2027"),
    ({"version_name": "Autodesk Revit", "version_number": "2025"}, "2025"),
    ({"version_name": None, "version_number": None}, None),
    ({}, None),
])
def test_the_lesson_version_comes_from_the_environment(revit, expected):
    assert checking.lesson_version({"revit": revit}) == expected


def test_lesson_version_edge_cases():
    assert checking.lesson_version(None) is None and checking.lesson_version({"revit": None}) is None


def test_the_revit_version_filters_and_verifies_lessons(seed_library):
    old = dict(CAPS, revit={"version_number": "2023", "version_build": "x", "version_name": "Autodesk Revit 2023"})
    assert _checked(new_addin(caps=old), WALL_CODE, seed_library)["guidance"] == []     # >=2024 <2028 only
    unknown = dict(CAPS, revit={"version_number": None, "version_build": None, "version_name": None})
    out = _checked(new_addin(caps=unknown), WALL_CODE, seed_library)
    assert out["guidance"][0]["id"] == WALL and out["guidance"][0]["applicability"] == "unknown"


# -- the tool list, the status resource and `revit-bridge check` -----------------------------------

def test_tool_names_and_the_check_code_schema():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == {
        "get_project_snapshot", "query", "missing_params", "reconcile", "confirm_spec", "check_code",
        "execute_code", "solidify_tool", "list_tools", "get_tool_choices", "run_tool", "evidence", "validate",
        "get_guidance",
    }
    check = tools["check_code"]
    assert check.annotations.model_dump(by_alias=True)["readOnlyHint"] is True
    assert set(check.input_schema["properties"]) == {"code", "parameters"} and check.input_schema["required"] == ["code"]
    assert "checked: false" in check.description and "not pre-compiled" in check.description
    for name in ("get_project_snapshot", "query"):
        assert "OST_" in tools[name].description and "OST_Walls" in tools[name].description, name


def test_the_confirmation_hook_leaves_check_code_alone():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "spec_gate.py"
    module_spec = importlib.util.spec_from_file_location("spec_gate", path)
    hook = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(hook)
    assert hook.decide("mcp__plugin_revit-bridge_revit-bridge__check_code", {"code": "return 1;"}) is None


def test_check_connection_reports_capability_and_environment():
    async def status(handler):
        async with FakeRevit(handler) as fake:
            return await server.check_connection(_settings(fake), environment=True), fake.requests

    new, requests = asyncio.run(status(new_addin()))
    assert new["reachable"] is True and new["document"] == "Project1"
    assert new["capability"]["state"] == "supported" and new["environment"]["source"] == "capabilities"
    assert [r["method"] for r in requests] == ["send_code_to_revit", "get_bridge_capabilities"]

    old, requests = asyncio.run(status(old_addin()))
    assert old["capability"]["state"] == "unsupported" and old["environment"]["source"] == "probe"
    assert old["environment"]["revit"]["version_name"] == "Autodesk Revit 2024"

    async def default_call(handler):
        async with FakeRevit(handler) as fake:
            return await server.check_connection(_settings(fake)), fake.requests

    plain, requests = asyncio.run(default_call(new_addin()))       # the web host's call is unchanged
    assert set(plain) == set(new) - {"capability", "environment"}
    assert [r["method"] for r in requests] == ["send_code_to_revit"]


def test_connection_status_resource_carries_the_capability(revit_env):
    async def scenario():
        async with FakeRevit(new_addin()) as fake:
            revit_env(fake.port)
            return json.loads(await server.connection_status())

    status = asyncio.run(scenario())
    assert status["capability"]["state"] == "supported" and status["environment"]["revit"] == CAPS["revit"]


def test_main_check_without_revit_prints_an_unknown_capability(monkeypatch, capsys):
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")
    monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "1")
    assert server.main(["check"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "disconnected"
    assert printed["capability"]["state"] == "unknown" and printed["capability"]["reason"]
    assert printed["environment"]["source"] is None and printed["environment"]["error"].startswith("unreachable: ")

"""revit_bridge.execution called directly, the way a host other than the MCP server would."""
from __future__ import annotations

import asyncio

import pytest

from revit_bridge.capabilities.store import ToolStore
from revit_bridge.evidence.ledger import Ledger
from revit_bridge.execution import ExecutionResult, revalidate, run_code, run_pack
from revit_bridge.revit.client import RevitClient
from revit_bridge.spec.gate import Gate
from revit_bridge.spec.models import Action, ParamBinding, Source, TaskSpec

from tests.fake_revit import FakeRevit
from tests.test_gate import WALL, counting_handler


@pytest.fixture
def host(tmp_path):
    """A host's own dependencies: private store, gate and ledger."""
    return {
        "store": ToolStore(user_dir=tmp_path / "user"),
        "gate": Gate(tmp_path / "pending"),
        "ledger": Ledger(tmp_path / "evidence"),
    }


def wall_token(gate: Gate, **overrides) -> str:
    params = {**WALL, **overrides}
    spec = TaskSpec(task="wall", action=Action(kind="run_tool", tool="create_wall"),
                    parameters=[ParamBinding(name=k, value=v, unit="mm" if isinstance(v, (int, float)) else None,
                                             source=Source.answer, evidence=f"q_{k}") for k, v in params.items()])
    return gate.issue(spec).token


def code_token(gate: Gate, code: str) -> str:
    spec = TaskSpec(task="code", action=Action(kind="execute_code", code=code), parameters=[])
    return gate.issue(spec).token


def with_fake(handler, scenario):
    async def go():
        async with FakeRevit(handler) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            try:
                return await scenario(client, fake)
            finally:
                await client.disconnect()
    return asyncio.run(go())


# -- run_pack ----------------------------------------------------------------------------------

def test_run_pack_from_a_web_host_writes_its_own_ledger(host):
    token = wall_token(host["gate"])

    async def scenario(client, fake):
        return await run_pack(client=client, name="create_wall", params=dict(WALL), token=token, host="web", **host)

    result = with_fake(counting_handler(1, 2), scenario)
    assert isinstance(result, ExecutionResult)
    assert result.success is True and result.error is None and result.refusal is None
    assert result.result == {"Status": "Created", "ElementId": 4242}
    assert result.validation.validator == "count_delta" and result.validation.passed is True
    assert result.tool == "create_wall" and result.warnings == [] and result.preconditions_failed == []
    record = host["ledger"].get(result.evidence_id)
    assert record["host"] == "web" and record["action"] == "run_tool" and record["success"] is True
    assert record["token_prefix"] == token[:6] and record["confirmed_by"] == "designer"
    assert record["document"] == {"title": "Project1", "revit_version": "2026"}
    assert host["store"].load("create_wall").execution_count == 1
    assert host["gate"].peek(token).used_at                          # consumed
    # the result is the MCP JSON shape
    assert set(result.model_dump()) == {"success", "tool", "result", "error", "validation", "evidence_id",
                                        "warnings", "preconditions_failed", "hint", "refusal"}


def test_run_pack_refuses_without_a_token_and_never_touches_revit(host):
    async def scenario(client, fake):
        out = await run_pack(client=client, name="create_wall", params=dict(WALL), token="", host="web", **host)
        assert fake.requests == []
        bad = await run_pack(client=client, name="create_wall", params=dict(WALL), token="nope", host="web", **host)
        assert fake.requests == []
        return out, bad

    out, bad = with_fake(counting_handler(1, 2), scenario)
    assert out.success is False and out.error == "confirmation_required" and out.evidence_id is None
    assert out.refusal == {"success": False, "error": "confirmation_required", "hint": out.hint}
    assert "confirm_spec" in out.hint
    assert bad.error == "confirmation_invalid" and bad.refusal["reason"] == "unknown"
    assert host["ledger"].recent() == []

    # no env means no bypass, whatever the process environment says
    import os
    os.environ["REVIT_BRIDGE_ALLOW_UNCONFIRMED"] = "1"
    try:
        out = with_fake(counting_handler(1, 2), lambda c, f: run_pack(
            client=c, name="create_wall", params=dict(WALL), token="", host="web", **host))
        assert out.error == "confirmation_required"
        out = with_fake(counting_handler(1, 2), lambda c, f: run_pack(
            client=c, name="create_wall", params=dict(WALL), token="", host="web",
            env={"REVIT_BRIDGE_ALLOW_UNCONFIRMED": "1"}, **host))
        assert out.success is True
        assert host["ledger"].get(out.evidence_id)["confirmed_by"] == "host_bypass"
    finally:
        del os.environ["REVIT_BRIDGE_ALLOW_UNCONFIRMED"]


def test_run_pack_reports_validation_failed_with_the_result_attached(host):
    token = wall_token(host["gate"])
    result = with_fake(counting_handler(1, 1), lambda c, f: run_pack(
        client=c, name="create_wall", params=dict(WALL), token=token, host="web", **host))
    assert result.success is False and result.error == "validation_failed"
    assert result.result == {"Status": "Created", "ElementId": 4242}
    assert result.validation.passed is False
    assert result.validation.checks[0].detail == "OST_Walls: before 1, after 1, delta 0, expected 1"
    record = host["ledger"].get(result.evidence_id)
    assert record["host"] == "web" and record["error"] == "validation_failed"
    assert host["store"].load("create_wall").failure_count == 1


def test_run_pack_records_an_execution_that_died_after_consume(host):
    token = wall_token(host["gate"])
    counting = counting_handler(1, 2)

    def dies_on_execute(request):
        if request["method"] == "send_code_to_revit" and "Wall.Create" in request["params"]["code"]:
            return FakeRevit.DROP
        return counting(request)

    result = with_fake(dies_on_execute, lambda c, f: run_pack(
        client=c, name="create_wall", params=dict(WALL), token=token, host="web", **host))
    assert result.success is False and "closed connection" in result.error
    assert result.evidence_id and result.validation is None
    record = host["ledger"].get(result.evidence_id)
    assert record["success"] is False and record["error"] == result.error and record["token_prefix"] == token[:6]
    assert host["gate"].peek(token).used_at                          # consumed, so not retried by accident


def test_run_pack_refusals_before_consume_are_recorded_and_keep_the_token(host):
    token = wall_token(host["gate"])
    counting = counting_handler(1, 2)

    def no_levels(request):
        if request["method"] == "send_code_to_revit" and "CategoryNames" in request["params"]["code"]:
            return FakeRevit.code_result(request["id"], {
                "Document": {"Title": "Empty", "RevitVersion": "2026", "IsWorkshared": False},
                "Levels": [], "CategoryNames": {}, "Warnings": []})
        return counting(request)

    result = with_fake(no_levels, lambda c, f: run_pack(
        client=c, name="create_wall", params=dict(WALL), token=token, host="web", **host))
    assert result.success is False and result.error == "preconditions_failed"
    assert result.preconditions_failed == ["levels_min 1: the model has 0 level(s)"]
    assert host["ledger"].get(result.evidence_id)["preconditions_failed"] == result.preconditions_failed
    assert host["gate"].peek(token).used_at is None

    # a Revit that cannot be reached at all: nothing recorded, token untouched
    async def unreachable(client, fake):
        dead = RevitClient(host="127.0.0.1", port=1, timeout=1, connect_timeout=0.5)
        return await run_pack(client=dead, name="create_wall", params=dict(WALL), token=token, host="web", **host)

    result = with_fake(counting, unreachable)
    assert result.success is False and result.evidence_id is None and result.refusal is None
    assert host["gate"].peek(token).used_at is None
    assert len(host["ledger"].recent()) == 1


def test_run_pack_early_refusals_carry_the_payload(host):
    """The gate comes first; then the pack checks refuse with their 0.1 payloads, token kept."""
    token = wall_token(host["gate"], height="tall")
    nope = host["gate"].issue(TaskSpec(task="x", action=Action(kind="run_tool", tool="nope"), parameters=[])).token

    async def scenario(client, fake):
        missing = await run_pack(client=client, name="nope", params={}, token=nope, host="web", **host)
        bad = await run_pack(client=client, name="create_wall", params={**WALL, "height": "tall"},
                             token=token, host="web", **host)
        not_dict = await run_pack(client=client, name="create_wall", params="x", token=token, host="web", **host)
        assert fake.requests == []
        return missing, bad, not_dict

    missing, bad, not_dict = with_fake(counting_handler(1, 2), scenario)
    assert missing.refusal == {"success": False, "error": "Tool 'nope' not found."}
    assert "expects double" in bad.error and bad.refusal["error"] == bad.error
    assert not_dict.error == "params must be a JSON object"
    assert host["gate"].peek(token).used_at is None and host["gate"].peek(nope).used_at is None


# -- run_code ----------------------------------------------------------------------------------

def test_run_code_from_a_web_host(host):
    code = "return 1;"
    token = code_token(host["gate"], code)
    result = with_fake(counting_handler(0, 0), lambda c, f: run_code(
        gate=host["gate"], ledger=host["ledger"], client=c, code=code, parameters=None, token=token, host="web"))
    assert result.success is True and result.result == {"Status": "Created", "ElementId": 4242}
    assert result.validation is None and result.tool is None
    record = host["ledger"].get(result.evidence_id)
    assert record["host"] == "web" and record["action"] == "execute_code" and record["code_head"] == code
    assert record["document"]["title"] == "Project1"

    refused = with_fake(counting_handler(0, 0), lambda c, f: run_code(
        gate=host["gate"], ledger=host["ledger"], client=c, code=code, parameters=None, token="", host="web"))
    assert refused.error == "confirmation_required" and refused.evidence_id is None
    dangerous = 'System.IO.File.Delete("x");'
    blocked = with_fake(counting_handler(0, 0), lambda c, f: run_code(
        gate=host["gate"], ledger=host["ledger"], client=c, code=dangerous, parameters=None,
        token=code_token(host["gate"], dangerous), host="web"))
    assert blocked.error == "blocked" and any("System.IO" in w for w in blocked.warnings)

    token = code_token(host["gate"], code)

    def dies(request):
        if request["method"] == "send_code_to_revit" and request["params"]["code"] == code:
            return FakeRevit.DROP
        return counting_handler(0, 0)(request)

    died = with_fake(dies, lambda c, f: run_code(
        gate=host["gate"], ledger=host["ledger"], client=c, code=code, parameters=None, token=token, host="web"))
    assert died.success is False and "closed connection" in died.error
    assert host["ledger"].get(died.evidence_id)["success"] is False


# -- revalidate --------------------------------------------------------------------------------

def test_revalidate_reruns_the_recorded_assertion(host):
    token = wall_token(host["gate"])
    counting = counting_handler(1, 2)

    async def scenario(client, fake):
        run = await run_pack(client=client, name="create_wall", params=dict(WALL), token=token, host="web", **host)
        again = await revalidate(store=host["store"], ledger=host["ledger"], client=client, evidence_id=run.evidence_id)
        fake.handler = counting_handler(1, 1)
        gone = await revalidate(store=host["store"], ledger=host["ledger"], client=client, evidence_id=run.evidence_id)
        unknown = await revalidate(store=host["store"], ledger=host["ledger"], client=client,
                                   evidence_id="ev_20000101T000000_ffffff")
        return run, again, gone, unknown

    run, again, gone, unknown = with_fake(counting, scenario)
    assert run.success is True
    assert again["evidence_id"] == run.evidence_id and again["tool"] == "create_wall" and again["passed"] is True
    assert again["before"] == {"category": "OST_Walls", "count": 1}
    assert gone["passed"] is False and "delta 0, expected 1" in gone["checks"][0]["detail"]
    assert unknown == {"error": "unknown_evidence", "evidence_id": "ev_20000101T000000_ffffff"}

    # a transport failure propagates for the host to map
    async def unreachable(client, fake):
        dead = RevitClient(host="127.0.0.1", port=1, timeout=1, connect_timeout=0.5)
        with pytest.raises(OSError):
            await revalidate(store=host["store"], ledger=host["ledger"], client=dead, evidence_id=run.evidence_id)

    with_fake(counting, unreachable)


# -- review 6.0-1: the connection is opened before any timed probe ------------------------------

class _NeverConnects:
    """A client whose connect outlives the probe budget and then fails - or fails outright."""

    def __init__(self, delay: float, exc: Exception):
        self.delay, self.exc = delay, exc
        self.sends = 0

    async def ensure_connected(self):
        await asyncio.sleep(self.delay)
        raise self.exc

    async def send_code(self, code, parameters=None):
        self.sends += 1
        raise AssertionError("send_code must not be reached")

    async def send_command(self, method, params=None):
        self.sends += 1
        raise AssertionError("send_command must not be reached")


@pytest.mark.parametrize("exc", [ConnectionError("connect to 10.0.0.9:18080 timed out after 5.0s"),
                                 ValueError("REVIT_BRIDGE_PORT must be an integer, got 'abc'")])
def test_a_connection_that_never_opens_consumes_nothing(host, monkeypatch, exc):
    import revit_bridge.execution as execution

    monkeypatch.setattr(execution, "PRECONDITION_SNAPSHOT_TIMEOUT", 0.2)
    client = _NeverConnects(delay=0.4, exc=exc)        # slower than the probe budget

    token = wall_token(host["gate"])
    result = asyncio.run(run_pack(client=client, name="create_wall", params=dict(WALL), token=token, host="web", **host))
    assert result.success is False and result.error == str(exc) and result.refusal is None
    assert result.evidence_id is None and result.warnings == []
    assert host["gate"].peek(token).used_at is None
    assert host["ledger"].recent() == [] and client.sends == 0
    assert host["store"].load("create_wall").failure_count == 1

    token = code_token(host["gate"], "return 1;")
    result = asyncio.run(run_code(gate=host["gate"], ledger=host["ledger"], client=client, code="return 1;",
                                  parameters=None, token=token, host="web"))
    assert result.success is False and result.error == str(exc)
    assert result.evidence_id is None and host["gate"].peek(token).used_at is None
    assert host["ledger"].recent() == [] and client.sends == 0


def test_revit_client_ensure_connected_opens_the_socket_once():
    async def scenario():
        async with FakeRevit() as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            try:
                assert client.connected is False
                await client.ensure_connected()
                assert client.connected is True
                await client.ensure_connected()                 # idempotent
                return await client.ping()
            finally:
                await client.disconnect()

    assert asyncio.run(scenario()) is True
    dead = RevitClient(host="127.0.0.1", port=1, timeout=1, connect_timeout=0.5)
    with pytest.raises(OSError):
        asyncio.run(dead.ensure_connected())


# -- phase 7: scope - the device an execution belongs to ----------------------------------------

DEVICE = "dev_abcdefghijkl"


def test_scope_binds_the_token_and_labels_the_ledger_line(host):
    """A token issued for a device runs only on that device; the line says which."""
    spec = TaskSpec(task="wall", action=Action(kind="run_tool", tool="create_wall"),
                    parameters=[ParamBinding(name=k, value=v, unit="mm" if isinstance(v, (int, float)) else None,
                                             source=Source.answer, evidence=f"q_{k}") for k, v in WALL.items()])
    remote = host["gate"].issue(spec, scope=DEVICE).token
    local = host["gate"].issue(spec).token

    async def scenario(client, fake):
        wrong = await run_pack(client=client, name="create_wall", params=dict(WALL), token=remote, host="web", **host)
        other = await run_pack(client=client, name="create_wall", params=dict(WALL), token=remote, host="web",
                               scope="dev_zzzzzzzzzzzz", **host)
        crossed = await run_pack(client=client, name="create_wall", params=dict(WALL), token=local, host="web",
                                 scope=DEVICE, **host)
        assert fake.requests == []                                   # refused before Revit, tokens kept
        right = await run_pack(client=client, name="create_wall", params=dict(WALL), token=remote, host="web",
                               scope=DEVICE, **host)
        return wrong, other, crossed, right

    wrong, other, crossed, right = with_fake(counting_handler(1, 2), scenario)
    for refused in (wrong, other, crossed):
        assert refused.error == "confirmation_invalid" and refused.refusal["reason"] == "mismatch"
        assert refused.refusal["message"] == "the execution does not match the confirmed spec"
        assert refused.evidence_id is None
    assert right.success is True
    record = host["ledger"].get(right.evidence_id)
    assert record["scope"] == DEVICE and record["host"] == "web" and record["token_prefix"] == remote[:6]
    assert host["gate"].peek(remote).used_at and host["gate"].peek(local).used_at is None
    assert [r["id"] for r in host["ledger"].recent(scope=DEVICE)] == [right.evidence_id]
    assert host["ledger"].recent(scope="local") == []

    # the default scope is local: a token issued with the defaults runs with the defaults
    plain = with_fake(counting_handler(1, 2), lambda c, f: run_pack(
        client=c, name="create_wall", params=dict(WALL), token=local, host="web", **host))
    assert plain.success is True and host["ledger"].get(plain.evidence_id)["scope"] == "local"
    assert [r["id"] for r in host["ledger"].recent(scope="local")] == [plain.evidence_id]
    assert [r["id"] for r in host["ledger"].recent()] == [plain.evidence_id, right.evidence_id]

    # run_code: the same binding, and a refusal after the probe still carries the scope
    code = "return 1;"
    remote_code = host["gate"].issue(TaskSpec(task="code", action=Action(kind="execute_code", code=code),
                                              parameters=[]), scope=DEVICE).token
    mismatch = with_fake(counting_handler(0, 0), lambda c, f: run_code(
        gate=host["gate"], ledger=host["ledger"], client=c, code=code, parameters=None, token=remote_code, host="web"))
    assert mismatch.refusal["reason"] == "mismatch" and host["gate"].peek(remote_code).used_at is None
    ran = with_fake(counting_handler(0, 0), lambda c, f: run_code(
        gate=host["gate"], ledger=host["ledger"], client=c, code=code, parameters=None, token=remote_code,
        host="web", scope=DEVICE))
    assert ran.success is True and host["ledger"].get(ran.evidence_id)["scope"] == DEVICE
    # the host bypass has no token to bind, but the line still says where it ran
    bypassed = with_fake(counting_handler(0, 0), lambda c, f: run_code(
        gate=host["gate"], ledger=host["ledger"], client=c, code=code, parameters=None, token="", host="web",
        scope=DEVICE, env={"REVIT_BRIDGE_ALLOW_UNCONFIRMED": "1"}))
    assert bypassed.success is True
    assert host["ledger"].get(bypassed.evidence_id)["scope"] == DEVICE
    assert host["ledger"].get(bypassed.evidence_id)["confirmed_by"] == "host_bypass"


def test_revalidate_refuses_a_record_from_another_scope(host):
    spec = TaskSpec(task="wall", action=Action(kind="run_tool", tool="create_wall"),
                    parameters=[ParamBinding(name=k, value=v, unit="mm" if isinstance(v, (int, float)) else None,
                                             source=Source.answer, evidence=f"q_{k}") for k, v in WALL.items()])
    token = host["gate"].issue(spec, scope=DEVICE).token

    async def scenario(client, fake):
        run = await run_pack(client=client, name="create_wall", params=dict(WALL), token=token, host="web",
                             scope=DEVICE, **host)
        assert run.success is True
        same = await revalidate(store=host["store"], ledger=host["ledger"], client=client,
                                evidence_id=run.evidence_id, scope=DEVICE)
        other = await revalidate(store=host["store"], ledger=host["ledger"], client=client,
                                 evidence_id=run.evidence_id, scope="dev_zzzzzzzzzzzz")
        local = await revalidate(store=host["store"], ledger=host["ledger"], client=client,
                                 evidence_id=run.evidence_id, scope="local")
        admin = await revalidate(store=host["store"], ledger=host["ledger"], client=client,
                                 evidence_id=run.evidence_id)                       # no scope: any record
        unknown = await revalidate(store=host["store"], ledger=host["ledger"], client=client,
                                   evidence_id="ev_20000101T000000_ffffff", scope=DEVICE)
        return run, same, other, local, admin, unknown

    run, same, other, local, admin, unknown = with_fake(counting_handler(1, 2), scenario)
    assert same["passed"] is True and same["evidence_id"] == run.evidence_id
    assert other == {"error": "scope_mismatch", "evidence_id": run.evidence_id}
    assert local == {"error": "scope_mismatch", "evidence_id": run.evidence_id}
    assert admin["passed"] is True
    assert unknown == {"error": "unknown_evidence", "evidence_id": "ev_20000101T000000_ffffff"}

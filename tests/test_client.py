"""TCP client, connection pool and settings against a fake add-in."""
from __future__ import annotations

import asyncio
import json

from revit_bridge.revit.client import RevitClient
from revit_bridge.revit.pool import RevitClientPool
from revit_bridge.revit.settings import RevitSettings

from tests.fake_revit import FakeRevit


def _settings(server: FakeRevit, **overrides) -> RevitSettings:
    base = {"host": "127.0.0.1", "port": server.port, "timeout": 2.0, "connect_timeout": 1.0}
    base.update(overrides)
    return RevitSettings(**base)


def test_settings_from_env_defaults_and_overrides():
    defaults = RevitSettings.from_env({})
    assert (defaults.host, defaults.port, defaults.token, defaults.timeout) == ("127.0.0.1", 18080, None, 60.0)

    custom = RevitSettings.from_env({
        "REVIT_BRIDGE_HOST": "10.0.0.5",
        "REVIT_BRIDGE_PORT": "19000",
        "REVIT_BRIDGE_TOKEN": "s3cret",
        "REVIT_BRIDGE_TIMEOUT": "7.5",
    })
    assert (custom.host, custom.port, custom.token, custom.timeout) == ("10.0.0.5", 19000, "s3cret", 7.5)
    assert custom.describe()["token"] == "set"
    assert "s3cret" not in str(custom.describe())


def test_ping_and_send_code_unwraps_inner_result():
    async def scenario():
        async with FakeRevit() as server:
            client = RevitClient(settings=_settings(server))
            try:
                assert await client.ping() is True
                probe = server.requests[-1]
                assert probe["method"] == "send_code_to_revit"          # not say_hello (a dialog)
                assert probe["params"]["code"] == "return document.Title;"
                resp = await client.send_code("return 1;")
                assert resp.success is True
                assert resp.result == {"Status": "Created", "ElementId": 4242}
                sent = server.requests[-1]
                assert sent["method"] == "send_code_to_revit"
                assert sent["params"]["code"] == "return 1;"
                assert "token" not in sent
            finally:
                await client.disconnect()

    asyncio.run(scenario())


def test_token_is_sent_and_required_by_addin():
    async def scenario():
        async with FakeRevit(token="pre-shared") as server:
            good = RevitClient(settings=_settings(server, token="pre-shared"))
            bad = RevitClient(settings=_settings(server, token=None))
            try:
                assert await good.ping() is True
                assert server.requests[-1]["token"] == "pre-shared"
                resp = await bad.send_command("say_hello", {"message": "ping"})
                assert resp.success is False
                assert "Unauthorized" in resp.error
            finally:
                await good.disconnect()
                await bad.disconnect()

    asyncio.run(scenario())


def test_execution_failure_is_reported_not_hidden():
    def handler(request):
        if request["method"] == "send_code_to_revit":
            return FakeRevit.code_result(request["id"], None, success=False,
                                         error="CS1002: ; expected")
        return FakeRevit.default_handler(request)

    async def scenario():
        async with FakeRevit(handler) as server:
            client = RevitClient(settings=_settings(server))
            try:
                resp = await client.send_code("var x = 1")
                assert resp.success is False
                assert resp.error == "CS1002: ; expected"
            finally:
                await client.disconnect()

    asyncio.run(scenario())


def test_stale_response_is_discarded():
    def handler(request):
        rid = request["id"]
        stale = FakeRevit.ok("stale-id", {"message": "late reply"})
        return [stale, FakeRevit.ok(rid, {"message": "fresh"})]

    async def scenario():
        async with FakeRevit(handler) as server:
            client = RevitClient(settings=_settings(server))
            try:
                resp = await client.send_command("say_hello")
                assert resp.success and resp.result == {"message": "fresh"}
            finally:
                await client.disconnect()

    asyncio.run(scenario())


def test_timeout_returns_error_response():
    def handler(request):
        return []  # never answer

    async def scenario():
        async with FakeRevit(handler) as server:
            client = RevitClient(settings=_settings(server, timeout=0.2))
            try:
                resp = await client.send_command("say_hello")
                assert resp.success is False
                assert "Timeout" in resp.error
            finally:
                await client.disconnect()

    asyncio.run(scenario())


def _drops_the_first_request():
    """A handler: the first request closes the connection unanswered (Revit restarted), every
    later one is answered."""
    seen = []

    def handler(request):
        seen.append(request["id"])
        if len(seen) == 1:
            return FakeRevit.DROP
        return FakeRevit.default_handler(request)
    return handler


def test_a_connection_the_add_in_closed_is_replaced_by_the_next_call():
    """A1: the failed call says why; the next one opens a new connection instead of failing again."""
    async def scenario():
        async with FakeRevit(_drops_the_first_request()) as server:
            client = RevitClient(settings=_settings(server))
            try:
                try:
                    await client.send_code("return document.Title;")
                    raise AssertionError("the first call must fail")
                except ConnectionError as exc:
                    first = str(exc)
                assert client.connected is False                  # dropped, not kept for the next call
                second = await client.send_code("return document.Title;")
                third = await client.send_code("return document.Title;")
                return first, second, third, server.connections
            finally:
                await client.disconnect()

    first, second, third, connections = asyncio.run(scenario())
    assert first == "Revit add-in closed connection"
    assert second.success is True and second.result == "Project1"
    assert third.success is True
    assert connections == 2                                        # one new connection, then kept


def test_the_pool_hands_out_a_new_connection_after_the_add_in_closed_one():
    async def scenario():
        async with FakeRevit(_drops_the_first_request()) as server:
            try:
                results = []
                for _ in range(3):                                 # what _PooledClient does per call
                    client = await RevitClientPool.get_client(settings=_settings(server))
                    try:
                        results.append((await client.send_code("return document.Title;")).success)
                    except ConnectionError as exc:
                        results.append(str(exc))
                return results, server.connections
            finally:
                await RevitClientPool.disconnect()

    results, connections = asyncio.run(scenario())
    assert results == ["Revit add-in closed connection", True, True]
    assert connections == 2


def test_an_idle_connection_the_add_in_closed_reads_as_not_connected():
    """The add-in answered and then went away while the client was idle: the next call reconnects
    without a failed call first."""
    def handler(request):
        if len(server_requests) == 0:
            server_requests.append(request["id"])
            return FakeRevit.closing(FakeRevit.default_handler(request))
        return FakeRevit.default_handler(request)

    server_requests: list = []

    async def scenario():
        async with FakeRevit(handler) as server:
            client = RevitClient(settings=_settings(server))
            try:
                first = await client.send_code("return document.Title;")
                for _ in range(50):                                # let the loop see the close
                    if not client.connected:
                        break
                    await asyncio.sleep(0.02)
                closed_seen = client.connected is False
                second = await client.send_code("return document.Title;")
                return first, closed_seen, second, server.connections
            finally:
                await client.disconnect()

    first, closed_seen, second, connections = asyncio.run(scenario())
    assert first.success is True and closed_seen is True
    assert second.success is True and connections == 2


def test_a_timed_out_request_cannot_leave_half_a_reply_in_front_of_the_next_one():
    """A reply cut off by the timeout: its tail must never be read as the start of the next answer.
    The connection is dropped at the timeout, so the next request starts on a clean stream."""
    late = {}

    def handler(request):
        rid = request["id"]
        if "first" not in late:
            late["first"] = json.dumps(FakeRevit.ok(rid, {"message": "late"})).encode("utf-8")
            return [late["first"][:20]]                            # half a reply, then nothing
        if server_ref[0].connections == 1:                         # still the first connection
            return [late["first"][20:], FakeRevit.ok(rid, {"message": "fresh"})]
        return FakeRevit.ok(rid, {"message": "fresh"})

    server_ref: list = []

    async def scenario():
        async with FakeRevit(handler) as server:
            server_ref.append(server)
            client = RevitClient(settings=_settings(server, timeout=0.3))
            try:
                timed_out = await client.send_command("say_hello")
                dropped = client.connected is False
                client.timeout = 2.0
                fresh = await client.send_command("say_hello")
                return timed_out, dropped, fresh
            finally:
                await client.disconnect()

    timed_out, dropped, fresh = asyncio.run(scenario())
    assert timed_out.success is False and "Timeout" in timed_out.error
    assert dropped is True
    assert fresh.success is True and fresh.result == {"message": "fresh"}


def test_a_caller_s_own_timeout_drops_the_connection_too():
    """discover() and the execution probes wrap calls in asyncio.wait_for: the abandoned request
    leaves the stream in an unknown state, so the connection goes with it."""
    answered = []

    def handler(request):
        if not answered:
            answered.append(False)
            return []                                              # never answers the first request
        return FakeRevit.default_handler(request)

    async def scenario():
        async with FakeRevit(handler) as server:
            client = RevitClient(settings=_settings(server))
            try:
                try:
                    await asyncio.wait_for(client.send_command("say_hello"), timeout=0.2)
                    raise AssertionError("must time out")
                except asyncio.TimeoutError:
                    pass
                dropped = client.connected is False
                again = await client.send_command("say_hello")
                return dropped, again, server.connections
            finally:
                await client.disconnect()

    dropped, again, connections = asyncio.run(scenario())
    assert dropped is True and again.success is True and connections == 2


def test_pool_reuses_connection_and_survives_new_loop():
    async def scenario(server):
        first = await RevitClientPool.get_client(settings=_settings(server))
        second = await RevitClientPool.get_client(settings=_settings(server))
        assert first is second
        assert await RevitClientPool.ping(settings=_settings(server)) is True

    async def run_all():
        async with FakeRevit() as server:
            await scenario(server)
            await RevitClientPool.disconnect()

    asyncio.run(run_all())
    asyncio.run(run_all())  # a second event loop must not trip over the old lock


def test_probe_lives_in_revit_probe_and_stays_importable_from_the_server():
    """Phase 6.0: check_connection moved; the 0.1 web host imports it from mcp_server."""
    from revit_bridge import mcp_server, revit
    from revit_bridge.revit import probe

    assert probe.PING_PROBE == "return document.Title;" and probe.CHECK_PROBE == probe.PING_PROBE
    assert revit.check_connection is probe.check_connection
    assert mcp_server.check_connection is probe.check_connection
    assert revit.PING_PROBE is probe.PING_PROBE

    async def scenario():
        async with FakeRevit() as server:
            return await probe.check_connection(_settings(server))

    status = asyncio.run(scenario())
    assert status["reachable"] is True and status["document"] == "Project1"

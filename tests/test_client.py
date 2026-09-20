"""TCP client, connection pool and settings against a fake add-in."""
from __future__ import annotations

import asyncio

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

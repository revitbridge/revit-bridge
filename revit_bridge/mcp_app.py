"""Mount the bridge's MCP handlers on Streamable HTTP with host-routed clients."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import cache
from hashlib import sha256
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request

from revit_bridge import __version__
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.evidence.ledger import Ledger
from revit_bridge.jsonfile import LockedJsonFile
from revit_bridge.mcp_server import SERVER_INSTRUCTIONS, _register_handlers, _request_runtime, _Runtime
from revit_bridge.paths import data_root
from revit_bridge.revit.client import RevitClient
from revit_bridge.spec.gate import Confirmation, Gate, GateError
from revit_bridge.verification import VerificationStore


class _DeviceGate(Gate):
    """Several web workers can verify the same token. Only its atomic unlink authorizes dispatch."""

    def _persist(self, conf: Confirmation) -> None:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        LockedJsonFile(self._path(conf.token)).write(conf.model_dump())

    def _retire(self, conf: Confirmation) -> None:
        try:
            self._path(conf.token).unlink()
        except FileNotFoundError:
            raise GateError("used", "confirmation already redeemed") from None
        except OSError as exc:
            raise GateError("used", "the confirmation could not be retired on disk; confirm again") from exc


def scope_directory(scope: str, data_dir: Path | str | None = None) -> Path:
    """Root of a paired device's capabilities, workflows, evidence and verifications."""
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("expected a nonempty authenticated device scope")
    root = Path(data_dir) if data_dir is not None else data_root() / "mcp"
    return root / sha256(scope.encode("utf-8")).hexdigest()


def build_asgi_app(
    get_client: Callable[[Request], Awaitable[tuple[str, RevitClient]]],
    *,
    channel: str = "mcp",
    data_dir: Path | str | None = None,
    streamable_http_path: str = "/mcp",
    transport_security: TransportSecuritySettings | None = None,
) -> Starlette:
    """Serve the same handlers as stdio. The host authenticates before dispatch.

    For each MCP request, get_client returns (stable authenticated device scope,
    connected client). A scope identifies one device; never use an unverified
    request argument or a bearer secret as its name. The host owns the client.
    Device data lives under scope_directory(scope, data_dir). Evidence records
    use channel for their host field. Remote executions always require a token.
    Stage host-side C# includes and add-in DLLs under the device's files directory.

    When mounting, enter app.router.lifespan_context(app) in the parent lifespan.
    The endpoint path is relative to the mount point. For a non-loopback host
    supply the SDK transport security settings for the host's public origin.
    """
    if not isinstance(channel, str) or not channel.strip():
        raise ValueError("channel must be a nonempty string")

    @cache
    def device_state(scope: str) -> _Runtime:
        root = scope_directory(scope, data_dir)
        evidence = root / "evidence"
        return _Runtime(None, ToolStore(root / "capabilities"), _DeviceGate(evidence / "pending"),
                        Ledger(evidence), VerificationStore(root / "verifications"), channel, scope, {})

    async def bind_request(ctx, call_next):
        if not isinstance(ctx.request, Request):
            raise ValueError("missing HTTP request for Revit device routing")
        scope, client = await get_client(ctx.request)
        if client is None:
            raise ValueError("no paired Revit client for this request")
        token = _request_runtime.set(replace(device_state(scope), client=client))
        try:
            return await call_next(ctx)
        finally:
            _request_runtime.reset(token)

    server = MCPServer("revit-bridge", version=__version__, instructions=SERVER_INSTRUCTIONS,
                       middleware=[bind_request])
    _register_handlers(server)
    return server.streamable_http_app(streamable_http_path=streamable_http_path, json_response=True,
                                      stateless_http=True, transport_security=transport_security)

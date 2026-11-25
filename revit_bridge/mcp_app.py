"""Mount the bridge's MCP handlers on Streamable HTTP with host-routed clients."""
from __future__ import annotations

import inspect
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import cache, wraps
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request

from revit_bridge import __version__
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.evidence.ledger import Ledger
from revit_bridge.jsonfile import LockedJsonFile
from revit_bridge.mcp_server import SERVER_INSTRUCTIONS, _http_request, _register_handlers, _request_runtime, _Runtime
from revit_bridge.paths import data_root
from revit_bridge.revit.client import RevitClient
from revit_bridge.spec.gate import Confirmation, Gate, GateError
from revit_bridge.verification import VerificationStore


# Paths can also occur inside diagnostics (including quoted OS errors).
# A slash inside a URI, a MIME type or a relative filename is not a path root.
_ABSOLUTE_PATH = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[/\\]|file://)|\\\\[^\s\\]+\\|(?<![\w:./])/(?:[^\s/*]|/[^/\s]+/)",
    re.IGNORECASE,
)


def _public_text(text: str, root: Path) -> str | None:
    if not _ABSOLUTE_PATH.search(text) and text != "/":
        return text
    # Lexical only: a reply can contain paths from another OS or a remote
    # machine. Never touch the filesystem to render a response.
    if Path(text).is_absolute():
        path = Path(os.path.normpath(text))
        if path.is_relative_to(root):
            relative = path.relative_to(root).as_posix()
            if not _ABSOLUTE_PATH.search(relative):
                return relative
    if PureWindowsPath(text).is_absolute() or PurePosixPath(text).is_absolute():
        return None
    # Strip our storage prefix from diagnostic text. Omit a diagnostic that
    # still embeds an absolute path outside the device's directory.
    for prefix in (str(root) + os.sep, root.as_posix() + "/"):
        text = text.replace(prefix, "")
    return "filesystem path omitted" if _ABSOLUTE_PATH.search(text) else text


def _public_value(value, root: Path):
    if isinstance(value, str):
        return _public_text(value, root)
    if isinstance(value, dict):
        public = {}
        for key, item in value.items():
            key = _public_text(key, root) if isinstance(key, str) else key
            if key is not None:
                public[key] = _public_value(item, root)
        return public
    if isinstance(value, list):
        return [_public_value(item, root) for item in value]
    return value


def _public_reply(reply, root: Path, *, yaml_resource: bool):
    if isinstance(reply, list):  # capture_view_image also carries an MCP Image
        return [_public_reply(item, root, yaml_resource=False) for item in reply]
    if not isinstance(reply, str):
        return reply
    # Keep the author's YAML formatting, or the handler's original JSON, if
    # there are no paths. Backslashes can introduce YAML or JSON escapes.
    if not _ABSOLUTE_PATH.search(reply) and "\\" not in reply:
        return reply
    if yaml_resource:
        try:
            # Work on scalar events, without expanding aliases or imposing
            # the workflow import limits on ordinary capability pack files.
            events = list(yaml.parse(reply, Loader=yaml.SafeLoader))
            changed = False
            for event in events:
                if isinstance(event, yaml.ScalarEvent):
                    value = _public_text(event.value, root)
                    if value != event.value:
                        event.value = value or ""
                        event.style = "'"
                        changed = True
            if changed or _ABSOLUTE_PATH.search(reply):  # emission also drops private paths in comments
                return yaml.emit(events, allow_unicode=True)
        except yaml.YAMLError:
            return json.dumps({"error": "invalid_resource"})
        return reply
    try:
        payload = json.loads(reply)
    except ValueError:
        return _public_text(reply, root) or ""
    return json.dumps(_public_value(payload, root), ensure_ascii=False, indent=2)


def _device_handler(fn):
    @wraps(fn)
    async def dispatch(*args, **kwargs):
        runtime = _request_runtime.get()
        if runtime is None:
            raise RuntimeError("missing authenticated device context for HTTP handler")
        try:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except OSError as exc:
            raise RuntimeError(f"device operation failed ({type(exc).__name__})") from None
        return _public_reply(result, runtime.tools.user_dir.parent,
                             yaml_resource=fn.__name__ in {"tool_resource", "workflow_resource"})
    return dispatch


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
    resolve_scope_dir: Callable[[str], Path | None] | None = None,
    streamable_http_path: str = "/mcp",
    transport_security: TransportSecuritySettings | None = None,
) -> Starlette:
    """Serve the same handlers as stdio. The host authenticates before dispatch.

    For each MCP request, get_client returns (stable authenticated device scope,
    connected client). A scope identifies one device; never use an unverified
    request argument or a bearer secret as its name. The host owns the client.
    A single-user host can map its device to its existing data root with
    resolve_scope_dir(scope). A multi-user host must never map two devices to
    one directory. Returning None selects scope_directory(scope, data_dir).
    Evidence records use channel for their host field. Remote executions always require a token.
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
        if resolve_scope_dir is not None:
            mapped = resolve_scope_dir(scope)
            if mapped is not None:
                root = mapped
        root = root.resolve()
        evidence = root / "evidence"
        return _Runtime(None, ToolStore(root / "capabilities"), _DeviceGate(evidence / "pending"),
                        Ledger(evidence), VerificationStore(root / "verifications"), channel, scope, {})

    async def bind_request(ctx, call_next):
        if not isinstance(ctx.request, Request):
            raise ValueError("missing HTTP request for Revit device routing")
        http_token = _http_request.set(True)
        token = _request_runtime.set(None)
        try:
            scope, client = await get_client(ctx.request)
            if client is None:
                raise ValueError("no paired Revit client for this request")
            _request_runtime.set(replace(device_state(scope), client=client))
            return await call_next(ctx)
        finally:
            _request_runtime.reset(token)
            _http_request.reset(http_token)

    server = MCPServer("revit-bridge", version=__version__, instructions=SERVER_INSTRUCTIONS,
                       middleware=[bind_request])
    _register_handlers(server, adapt=_device_handler)
    return server.streamable_http_app(streamable_http_path=streamable_http_path, json_response=True,
                                      stateless_http=True, transport_security=transport_security)

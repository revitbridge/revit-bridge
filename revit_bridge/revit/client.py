"""
Revit TCP Client - JSON-RPC 2.0 over raw TCP socket to the Revit add-in.

Protocol (from the add-in's SocketService.cs):
- Transport: TcpListener / TcpClient (NOT WebSocket)
- Port: 18080 by default (8080 conflicts with AdskLicensingAgent)
- Message format: JSON-RPC 2.0, UTF-8, no delimiter (raw read)
- Buffer: 8192 bytes per read on the add-in side
- Optional pre-shared token: sent as a top-level "token" field; the add-in
  rejects the request when its configured token does not match
- send_code_to_revit timeout: 60s (add-in side RaiseAndWaitForCompletion)

Connection settings default to the ``REVIT_BRIDGE_*`` environment variables
(see :mod:`revit_bridge.revit.settings`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass

from revit_bridge.revit.settings import RevitSettings

_log = logging.getLogger("revit_bridge.revit.client")


@dataclass
class RevitResponse:
    """Structured response from Revit execution."""
    success: bool
    result: dict | list | str | None = None
    error: str | None = None
    raw: str = ""


_DECODER = json.JSONDecoder()


def _pop_json_object(buf: bytes) -> tuple[dict | None, bytes]:
    """Split the first complete JSON object off ``buf``.

    Returns ``(obj, rest)``; ``(None, buf)`` when the buffer holds no complete
    object yet. Leading whitespace is skipped. Non-object JSON (a bare number,
    a list) is dropped so a corrupt frame cannot wedge the loop.
    """
    try:
        text = buf.decode("utf-8")
    except UnicodeDecodeError:
        return None, buf  # multi-byte character split across reads
    stripped = text.lstrip()
    if not stripped:
        return None, b""
    try:
        obj, end = _DECODER.raw_decode(stripped)
    except json.JSONDecodeError:
        return None, buf  # incomplete, keep reading
    rest = stripped[end:].encode("utf-8")
    if not isinstance(obj, dict):
        return _pop_json_object(rest)
    return obj, rest


class RevitClient:
    """Async TCP client that speaks JSON-RPC 2.0 to the Revit add-in."""

    def __init__(self, host: str | None = None, port: int | None = None,
                 timeout: float | None = None, connect_timeout: float | None = None,
                 token: str | None = None, settings: RevitSettings | None = None):
        base = settings or RevitSettings.from_env()
        self.host = host or base.host
        self.port = port or base.port
        self.timeout = timeout if timeout is not None else base.timeout
        self.connect_timeout = (
            connect_timeout if connect_timeout is not None else base.connect_timeout
        )
        self.token = token if token is not None else base.token
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()  # prevent concurrent command interleaving

    # -- connection lifecycle --------------------------------------------------

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.connect_timeout,
        )

    async def disconnect(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    # -- send command ----------------------------------------------------------

    @staticmethod
    def _make_id() -> str:
        return f"{int(time.time() * 1000)}{random.randint(100000, 999999)}"

    def _build_payload(self, method: str, params: dict | None, request_id: str) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": request_id,
        }
        if self.token:
            payload["token"] = self.token
        return payload

    async def send_command(self, method: str, params: dict | None = None) -> RevitResponse:
        """Send a JSON-RPC 2.0 command and wait for the response.

        Uses a lock to prevent concurrent commands from interleaving on the
        same TCP connection.  Validates that the response id matches the
        request id - stale responses from previous commands (e.g. a late
        health-check reply) are discarded automatically.
        """
        async with self._lock:
            if not self.connected:
                await self.connect()

            request_id = self._make_id()
            payload = self._build_payload(method, params, request_id)

            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._writer.write(data)
            await self._writer.drain()

            # Read response - accumulate until one complete JSON object arrived.
            # Add-in sends raw UTF-8 JSON with no delimiter, reads up to 8192 bytes.
            # Several objects may share one chunk (e.g. a late reply followed by
            # ours); the leftover bytes stay in `buf` for the next iteration.
            # Loop to skip stale responses whose id doesn't match request_id.
            deadline = time.monotonic() + self.timeout
            buf = b""
            while True:
                try:
                    while True:
                        resp, buf = _pop_json_object(buf)
                        if resp is not None:
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return RevitResponse(success=False, error=f"Timeout after {self.timeout}s")
                        chunk = await asyncio.wait_for(
                            self._reader.read(8192),
                            timeout=remaining,
                        )
                        if not chunk:
                            raise ConnectionError("Revit add-in closed connection")
                        buf += chunk
                except asyncio.TimeoutError:
                    return RevitResponse(success=False, error=f"Timeout after {self.timeout}s")

                # Validate response id matches our request
                resp_id = resp.get("id")
                if resp_id != request_id:
                    _log.warning(
                        f"[send_command] Discarding stale response: "
                        f"expected id={request_id}, got id={resp_id}"
                    )
                    continue  # discard and read the next response

                # Parse JSON-RPC response
                if "error" in resp and resp["error"]:
                    err = resp["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    return RevitResponse(success=False, error=msg, raw=json.dumps(resp))

                return RevitResponse(success=True, result=resp.get("result"), raw=json.dumps(resp))

    # -- high-level: send code -------------------------------------------------

    async def send_code(self, code: str, parameters: list | None = None) -> RevitResponse:
        """Send C# code to Revit for dynamic compilation and execution.

        Maps to the send_code_to_revit command. The add-in wraps user code in:
            public static object Execute(Document document, object[] parameters)
        and compiles it with Roslyn. A Transaction is already active - user code
        must NOT create its own Transaction.

        The add-in returns: {"success": bool, "result": "JSON string", "errorMessage": ""}
        We unwrap this nested structure so callers get parsed data directly.
        """
        resp = await self.send_command("send_code_to_revit", {
            "code": code,
            "parameters": parameters or [],
        })

        _log.debug(f"[send_code] resp.success={resp.success} result_type={type(resp.result).__name__}")

        if resp.success and isinstance(resp.result, dict):
            inner = resp.result
            if "success" in inner:
                inner_result_raw = inner.get("result", "")
                if not inner.get("success"):
                    _log.error(f"[send_code] EXECUTION FAILED - errorMessage: {inner.get('errorMessage', '(none)')}")

                inner_result = inner_result_raw

                # The result field is often a JSON string - parse it
                if isinstance(inner_result, str) and inner_result.strip():
                    try:
                        parsed = json.loads(inner_result)
                        # json.loads("null") -> None, keep original string in that case
                        inner_result = parsed if parsed is not None else inner_result
                    except (json.JSONDecodeError, ValueError) as e:
                        _log.warning(f"[send_code] JSON parse failed: {e}, keeping raw string")
                        inner_result = {"raw_output": inner_result}

                # If result is truly empty/None, provide minimal feedback
                is_success = bool(inner.get("success", False))
                if is_success and inner_result is None:
                    inner_result = {"Status": "Success", "Message": "Code executed (no return statement in code)"}
                    _log.warning("[send_code] C# code returned null - code likely missing 'return' statement")
                elif is_success and inner_result == "":
                    inner_result = {"Status": "Success", "Message": "Code executed (empty return value)"}
                    _log.warning("[send_code] C# code returned empty string")

                error_msg = inner.get("errorMessage") or None
                return RevitResponse(
                    success=is_success,
                    result=inner_result,
                    error=error_msg if error_msg else resp.error,
                    raw=resp.raw,
                )

        return resp

    async def ping(self) -> bool:
        """Quick connectivity check via say_hello command."""
        try:
            resp = await self.send_command("say_hello", {"message": "ping"})
            return resp.success
        except Exception:
            return False


async def with_revit_connection(operation, settings: RevitSettings | None = None):
    """Run ``operation(client)`` on a fresh connection, closing it afterwards."""
    client = RevitClient(settings=settings)
    try:
        await client.connect()
        return await operation(client)
    finally:
        await client.disconnect()

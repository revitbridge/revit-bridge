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
- Optional "confirm" field in the send_code_to_revit params (phase 7): the
  add-in shows the designer a Yes/No dialog first and answers a declined
  run with JSON-RPC error -32001; such a request waits at least
  CONFIRM_TIMEOUT_SECONDS for the designer

Connection settings default to the ``REVIT_BRIDGE_*`` environment variables
(see :mod:`revit_bridge.revit.settings`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass

from revit_bridge.revit.settings import RevitSettings

from revit_bridge.revit.probe import PING_PROBE

_log = logging.getLogger("revit_bridge.revit.client")

# A request that carries ``confirm`` waits for a person: never less than this.
CONFIRM_TIMEOUT_SECONDS = 180.0
# The add-in's JSON-RPC error code when the designer answered No on the device.
DECLINED_ON_DEVICE = -32001


@dataclass
class RevitResponse:
    """Structured response from Revit execution."""
    success: bool
    result: dict | list | str | None = None
    error: str | None = None
    raw: str = ""
    error_code: int | None = None    # the JSON-RPC error code, when the add-in answered with an error


# Bytes that matter outside a JSON string.
_STRUCTURAL = re.compile(rb'[{}\[\]"]')
# The rest of a JSON string, from inside it: plain runs and escape pairs, possessive so the
# scan is linear. It stops at the closing quote, at a lone backslash whose escaped byte has
# not arrived yet, or at the end of the data.
_STRING_REST = re.compile(rb'(?:[^"\\]++|\\.)*+', re.DOTALL)
READ_SIZE = 65536


class _Framer:
    """Cuts the add-in's undelimited byte stream into JSON objects, in linear time.

    The add-in writes raw JSON with no delimiter, so a reply is complete when
    its outermost brace closes. Each byte is scanned once, as it arrives:
    outside a string only braces, brackets and quotes count; inside one, a
    regex runs to the closing quote. Only a complete object is decoded and
    parsed, once, so a multi-byte character or an escape split across reads
    is never decoded in halves (UTF-8 lead and continuation bytes are >= 0x80
    and never look like one of those ASCII bytes), and a ``}`` that happens to
    end a read inside a string costs nothing. Whatever lies between objects
    at the top level - whitespace, a bare value, junk - is skipped, and a
    complete value that is not an object is dropped, so a corrupt frame
    cannot wedge the loop.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pos = 0              # the next byte to scan
        self._start = 0            # where the open top-level value began
        self._depth = 0
        self._in_string = False

    def feed(self, data: bytes) -> None:
        self._buf += data

    def pop(self) -> dict | None:
        """The next complete object, or None until more data arrives."""
        buf = self._buf
        while True:
            if self._in_string:
                end = _STRING_REST.match(buf, self._pos).end()
                if end >= len(buf) or buf[end] != 0x22:          # not closed yet
                    self._pos = end
                    return None
                self._pos, self._in_string = end + 1, False
                continue
            found = _STRUCTURAL.search(buf, self._pos)
            if found is None:
                self._pos = len(buf)
                if self._depth == 0:                              # only skipped bytes: forget them
                    buf.clear()
                    self._pos = 0
                return None
            at = found.start()
            byte = buf[at]
            self._pos = at + 1
            if byte == 0x22:                                      # "
                self._in_string = True
            elif byte in (0x7B, 0x5B):                            # { [
                if self._depth == 0:
                    self._start = at
                self._depth += 1
            elif self._depth > 0:                                 # } ] (a stray one at the top is junk)
                self._depth -= 1
                if self._depth == 0:
                    frame = bytes(buf[self._start:self._pos])
                    del buf[:self._pos]
                    self._pos = 0
                    obj = _parse_frame(frame)
                    if isinstance(obj, dict):
                        return obj


def _parse_frame(frame: bytes):
    try:
        return json.loads(frame.decode("utf-8", errors="replace"))
    except ValueError:
        _log.warning("[send_command] Dropping an unparseable frame of %d bytes", len(frame))
        return None


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
        # Counts successful connects: what is cached per connection (the
        # add-in's capabilities) is stale once this changes.
        self.connection_generation = 0

    # -- connection lifecycle --------------------------------------------------

    async def connect(self) -> None:
        self._drop()                     # a connection the add-in closed is not leaked
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.connect_timeout,
            )
        except asyncio.TimeoutError as exc:
            # A connect that never completes is a connection failure, not a
            # command timeout: callers distinguish the two.
            raise ConnectionError(
                f"connect to {self.host}:{self.port} timed out after {self.connect_timeout}s") from exc
        self.connection_generation += 1

    async def ensure_connected(self) -> None:
        """Connect now if not connected (the execution flow calls this before its timed probes)."""
        if not self.connected:
            await self.connect()

    async def disconnect(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    def _drop(self) -> None:
        """Forget the connection at once (no await, so it also runs while a cancellation unwinds)."""
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()

    @property
    def connected(self) -> bool:
        """An open connection the add-in has not closed. A socket the add-in closed while the
        client was idle (Revit restarted) stays "not closing" on the client side; its reader
        at end of file is what tells."""
        return (self._writer is not None and not self._writer.is_closing()
                and self._reader is not None and not self._reader.at_eof())

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

    async def send_command(self, method: str, params: dict | None = None,
                           timeout: float | None = None) -> RevitResponse:
        """Send a JSON-RPC 2.0 command and wait for the response.

        Uses a lock to prevent concurrent commands from interleaving on the
        same TCP connection.  Validates that the response id matches the
        request id - stale responses from previous commands (e.g. a late
        health-check reply) are discarded automatically. ``timeout`` (seconds)
        replaces the client's timeout for this one request.

        An exchange that does not complete - the add-in closed the connection,
        it was reset, no reply within ``timeout``, or the caller cancelled the
        call (``asyncio.wait_for`` around it) - leaves the stream in an unknown
        state: a reply may still be on its way, or be half read. The connection
        is dropped before the error is raised or the timeout reply returned, so
        the next call opens a new one and starts on a message boundary. The
        request itself is never sent again: Revit may have run it.
        """
        timeout = self.timeout if timeout is None else timeout
        async with self._lock:
            if not self.connected:
                await self.connect()

            request_id = self._make_id()
            payload = self._build_payload(method, params, request_id)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                resp = await self._exchange(data, request_id, timeout)
            except BaseException:
                self._drop()
                raise
            if resp is None:
                self._drop()
                return RevitResponse(success=False, error=f"Timeout after {timeout}s")
            return resp

    async def _exchange(self, data: bytes, request_id: str, timeout: float) -> RevitResponse | None:
        """Write one request and read until the reply with ``request_id``; None on timeout."""
        self._writer.write(data)
        await self._writer.drain()

        # Read the response: the add-in sends raw UTF-8 JSON with no delimiter.
        # Several objects may share one read (e.g. a late reply followed by
        # ours); the framer keeps what follows an object for the next pop.
        # Loop to skip stale responses whose id doesn't match request_id. The
        # deadline is checked before every read, and scanning a read costs
        # time in proportion to its size, so a long reply cannot outlast it.
        deadline = time.monotonic() + timeout
        frames = _Framer()
        while True:
            try:
                while (resp := frames.pop()) is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    chunk = await asyncio.wait_for(self._reader.read(READ_SIZE), timeout=remaining)
                    if not chunk:
                        raise ConnectionError("Revit add-in closed connection")
                    frames.feed(chunk)
            except asyncio.TimeoutError:
                return None

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
                code = err.get("code") if isinstance(err, dict) else None
                return RevitResponse(success=False, error=msg, raw=json.dumps(resp),
                                     error_code=code if isinstance(code, int) else None)

            return RevitResponse(success=True, result=resp.get("result"), raw=json.dumps(resp))

    # -- high-level: send code -------------------------------------------------

    async def send_code(self, code: str, parameters: list | None = None,
                        confirm: dict | None = None) -> RevitResponse:
        """Send C# code to Revit for dynamic compilation and execution.

        Maps to the send_code_to_revit command. The add-in wraps user code in:
            public static object Execute(Document document, object[] parameters)
        and compiles it with Roslyn. A Transaction is already active - user code
        must NOT create its own Transaction.

        ``confirm`` (``{kind, title, message}``) asks the add-in to show the
        designer a Yes/No dialog before running; the request then waits
        ``max(timeout, CONFIRM_TIMEOUT_SECONDS)`` and a No comes back as
        ``error_code == DECLINED_ON_DEVICE``. Without it nothing is shown.

        The add-in returns: {"success": bool, "result": "JSON string", "errorMessage": ""}
        We unwrap this nested structure so callers get parsed data directly.
        """
        params: dict = {"code": code, "parameters": parameters or []}
        timeout = None
        if confirm is not None:
            params["confirm"] = confirm
            timeout = max(self.timeout, CONFIRM_TIMEOUT_SECONDS)
        resp = await self.send_command("send_code_to_revit", params, timeout=timeout)

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
        """Quick connectivity check: run the read-only title probe.

        ``say_hello`` is not used because the add-in answers it with a
        TaskDialog in Revit.
        """
        try:
            resp = await self.send_code(PING_PROBE)
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

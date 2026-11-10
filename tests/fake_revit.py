"""A minimal stand-in for the Revit add-in's TCP JSON-RPC listener.

Speaks the same wire format as SocketService.cs: one raw UTF-8 JSON request
per read, one raw JSON response written back, no delimiters. Enough to test
the client, the pool and the MCP tools without Revit.
"""
from __future__ import annotations

import asyncio
import json


class _Closing:
    """Responses to send before the server closes the connection (``FakeRevit.closing``)."""

    def __init__(self, responses):
        self.responses = list(responses)


class FakeRevit:
    """Async TCP server; ``handler(request: dict) -> dict | list[dict]``.

    The handler returns the JSON-RPC response object. Returning a list sends
    several responses back-to-back (used to simulate stale replies); an item
    that is ``bytes`` is written as is (a reply cut in pieces), a float item
    pauses that many seconds first (so the pieces arrive as separate reads).
    When ``token`` is set the server rejects requests that do not carry it,
    like the add-in does. ``connections`` counts the connections accepted.
    """

    DROP = object()   # a handler returns this to close the connection without answering

    def __init__(self, handler=None, token: str | None = None):
        self.handler = handler or self.default_handler
        self.token = token
        self.requests: list[dict] = []
        self.connections = 0          # accepted so far
        self.open_connections = 0     # accepted and not closed by either side yet
        self._server: asyncio.AbstractServer | None = None
        self.port: int = 0

    @staticmethod
    def closing(*responses) -> _Closing:
        """A handler result: send ``responses``, then close the connection (an add-in that went away)."""
        return _Closing(responses)

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.connections += 1
        self.open_connections += 1
        try:
            while True:
                data = await reader.read(8192)
                if not data:
                    break
                request = json.loads(data.decode("utf-8"))
                self.requests.append(request)
                close = False
                if self.token and request.get("token") != self.token:
                    responses = [self.error(request.get("id"), -32600,
                                            "Unauthorized: invalid or missing token")]
                else:
                    result = self.handler(request)
                    if result is self.DROP:
                        break   # like an add-in that died mid-request
                    if isinstance(result, _Closing):
                        result, close = result.responses, True
                    responses = result if isinstance(result, list) else [result]
                for resp in responses:
                    if isinstance(resp, float):
                        await asyncio.sleep(resp)
                        continue
                    writer.write(resp if isinstance(resp, bytes) else json.dumps(resp).encode("utf-8"))
                    await writer.drain()
                if close:
                    break
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.open_connections -= 1
            writer.close()

    # -- response helpers ------------------------------------------------------

    @staticmethod
    def ok(request_id, result) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def error(request_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @classmethod
    def code_result(cls, request_id, payload, success: bool = True, error: str = "") -> dict:
        """Shape of a send_code_to_revit reply: the inner result is a JSON string."""
        inner = {
            "success": success,
            "result": json.dumps(payload) if payload is not None else None,
            "errorMessage": error,
        }
        return cls.ok(request_id, inner)

    # The add-in's execute handler reports code that threw as its prefix
    # ("execution failed: ", in Chinese), the exception's type name and its
    # message. The add-in names the inner exception's short type
    # (``ArgumentException``); tests pass full names (``Autodesk.Revit...``) too.
    THROWN_PREFIX = "\u6267\u884c\u5931\u8d25: "

    @classmethod
    def thrown(cls, request_id, exception: str, message: str) -> dict:
        """A send_code_to_revit reply for code that threw ``exception`` while it ran."""
        return cls.code_result(request_id, None, success=False, error=f"{cls.THROWN_PREFIX}{exception}: {message}")

    @classmethod
    def default_handler(cls, request: dict) -> dict:
        method = request.get("method")
        rid = request.get("id")
        if method == "say_hello":
            return cls.ok(rid, {"message": "Hello from fake Revit"})
        if method == "send_code_to_revit":
            code = request.get("params", {}).get("code", "")
            if code == "return document.Title;":
                return cls.code_result(rid, "Project1")
            return cls.code_result(rid, {"Status": "Created", "ElementId": 4242})
        if method == "get_available_family_types":
            cats = request.get("params", {}).get("categoryList", [])
            return cls.ok(rid, [{"name": f"{c}-TypeA"} for c in cats])
        return cls.error(rid, -32601, f"Method '{method}' not found")

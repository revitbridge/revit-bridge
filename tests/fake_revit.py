"""A minimal stand-in for the Revit add-in's TCP JSON-RPC listener.

Speaks the same wire format as SocketService.cs: one raw UTF-8 JSON request
per read, one raw JSON response written back, no delimiters. Enough to test
the client, the pool and the MCP tools without Revit.
"""
from __future__ import annotations

import asyncio
import json


class FakeRevit:
    """Async TCP server; ``handler(request: dict) -> dict | list[dict]``.

    The handler returns the JSON-RPC response object. Returning a list sends
    several responses back-to-back (used to simulate stale replies). When
    ``token`` is set the server rejects requests that do not carry it, like
    the add-in does.
    """

    def __init__(self, handler=None, token: str | None = None):
        self.handler = handler or self.default_handler
        self.token = token
        self.requests: list[dict] = []
        self._server: asyncio.AbstractServer | None = None
        self.port: int = 0

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                data = await reader.read(8192)
                if not data:
                    break
                request = json.loads(data.decode("utf-8"))
                self.requests.append(request)
                if self.token and request.get("token") != self.token:
                    responses = [self.error(request.get("id"), -32600,
                                            "Unauthorized: invalid or missing token")]
                else:
                    result = self.handler(request)
                    responses = result if isinstance(result, list) else [result]
                for resp in responses:
                    writer.write(json.dumps(resp).encode("utf-8"))
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
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

    @classmethod
    def default_handler(cls, request: dict) -> dict:
        method = request.get("method")
        rid = request.get("id")
        if method == "say_hello":
            return cls.ok(rid, {"message": "Hello from fake Revit"})
        if method == "send_code_to_revit":
            return cls.code_result(rid, {"Status": "Created", "ElementId": 4242})
        if method == "get_available_family_types":
            cats = request.get("params", {}).get("categoryList", [])
            return cls.ok(rid, [{"name": f"{c}-TypeA"} for c in cats])
        return cls.error(rid, -32601, f"Method '{method}' not found")

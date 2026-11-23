"""A minimal stand-in for the Revit add-in's TCP JSON-RPC listener.

Speaks the same wire format as SocketService.cs: one raw UTF-8 JSON request
per read, one raw JSON response written back, no delimiters. Enough to test
the client, the pool and the MCP tools without Revit.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re


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


# -- a small model behind list_model_items and its fallbacks ------------------------------

def _type(type_id, family, name, display, builtin):
    """A ``types`` row: ``Category`` is the display name, ``BuiltInCategory`` the OST_* name (or None)."""
    return {"FamilyTypeId": type_id, "UniqueId": f"uid-{type_id}", "FamilyName": family, "TypeName": name,
            "Category": display, "BuiltInCategory": builtin}


# Made-up names and ids. Host model: three levels, walls, doors, framing and pipes (pipes only
# reach the bridge through list_model_items: the old type command cannot list them), and three
# link instances - a loaded structural model, a rotated loaded one and an unloaded one.
DEFAULT_MODEL = {
    "levels": [{"Id": 311, "Name": "F1", "ElevationMm": 0.0}, {"Id": 312, "Name": "F2", "ElevationMm": 3000.0},
               {"Id": 313, "Name": "Roof", "ElevationMm": 9000.0}],
    "types": {
        "OST_Walls": [_type(401, "Basic Wall", "Generic - 200mm", "Walls", "OST_Walls"),
                      _type(402, "Basic Wall", "Generic - 300mm", "Walls", "OST_Walls")],
        "OST_Doors": [_type(411, "Single-Flush", "915 x 2134mm", "Doors", "OST_Doors")],
        "OST_StructuralFraming": [_type(421, "W Shapes", "W310x60", "Structural Framing", "OST_StructuralFraming")],
        "OST_PipeCurves": [_type(431, "Pipe Types", "Standard", "Pipes", "OST_PipeCurves")],
    },
    "category_names": {"OST_Walls": "Walls", "OST_Doors": "Doors", "OST_StructuralFraming": "Structural Framing",
                       "OST_PipeCurves": "Pipes"},
    "old_command_categories": ["OST_Walls", "OST_Doors", "OST_StructuralFraming"],
    "counts": {"OST_Walls": 12, "OST_Doors": 3, "OST_StructuralFraming": 0},
    "links": [
        {"Id": 501, "Name": "Structure.rvt : 1 : location <Not Shared>", "TypeId": 500, "TypeName": "Structure.rvt",
         "Loaded": True, "DocumentTitle": "Structure", "Origin": {"X": 0.0, "Y": 0.0, "Z": 0.0}, "Rotated": False},
        {"Id": 502, "Name": "Site.rvt : 1 : location <Not Shared>", "TypeId": 510, "TypeName": "Site.rvt",
         "Loaded": True, "DocumentTitle": "Site", "Origin": {"X": 12000.0, "Y": -500.0, "Z": 0.0}, "Rotated": True},
        {"Id": 503, "Name": "MEP.rvt : 1 : location <Not Shared>", "TypeId": 520, "TypeName": "MEP.rvt",
         "Loaded": False, "DocumentTitle": None, "Origin": {"X": 0.0, "Y": 0.0, "Z": 0.0}, "Rotated": False},
    ],
    "linked": {
        501: {"levels": [{"Id": 11, "Name": "S-B1", "ElevationMm": -3500.0},
                         {"Id": 12, "Name": "S-F1", "ElevationMm": -100.0}],
              "types": {"OST_StructuralFraming": [_type(901, "Concrete-Rectangular Beam", "300x600mm",
                                                        "Structural Framing", "OST_StructuralFraming")]},
              "counts": {"OST_StructuralFraming": 42, "OST_Walls": 0},
              "elements": {"OST_StructuralFraming": [
                  {"Id": 9001, "Name": "300x600mm", "Category": "Structural Framing", "Type": "300x600mm",
                   "Level": "S-F1"}]}},
        502: {"levels": [{"Id": 21, "Name": "Ground", "ElevationMm": 0.0}], "types": {}, "counts": {},
              "elements": {}},
    },
}


_CATEGORY_SHAPE = re.compile(r"^OST_[A-Za-z0-9_]+$")
_ID_KEYS = {"levels": "Id", "links": "Id", "types": "FamilyTypeId"}
_NAME_KEYS = {"levels": ("Name",), "links": ("Name", "TypeName", "DocumentTitle"),
              "types": ("FamilyName", "TypeName"), "type_categories": ("Category", "DisplayName")}


class FakeModel:
    """A handler for :class:`FakeRevit` that answers from a small model.

    ``list_model_items=True`` reports the method in ``get_bridge_capabilities`` and answers it as
    the add-in does (the reply shape frozen in section 2.6, "planning addendum"): always a
    successful JSON-RPC result ``{Success, Kind, Items, Total, Truncated, Warnings, Error}``,
    a failure as ``Success: false`` with ``Error: {Code, Message}``; ``ids`` for levels, links
    and types; ``types`` needs ``category`` or ``ids``; ``link_id`` for levels, types and
    type_categories, refused with links. False is an add-in without the method (capability
    supported, method missing), ``capabilities="unsupported"`` an add-in from before
    ``get_bridge_capabilities``. The fallbacks are answered too: ``send_code_to_revit`` blocks
    (levels, links, type categories, counts, elements, and their linked variants) and
    ``get_available_family_types`` (like the old command, not every category).
    ``advertise_only`` lists the method but answers -32601; ``honour_ids=False`` is an add-in
    that ignores ``ids`` (and sends every row); ``fail`` makes every call fail with that code;
    ``warnings`` are added to every successful reply.
    """

    def __init__(self, model: dict | None = None, *, list_model_items: bool = True, capabilities: str = "supported",
                 advertise_only: bool = False, honour_ids: bool = True, fail: str | None = None,
                 warnings: list[str] | None = None):
        self.model = copy.deepcopy(model or DEFAULT_MODEL)
        self.list_model_items = list_model_items
        self.capabilities = capabilities
        self.advertise_only = advertise_only
        self.honour_ids = honour_ids
        self.fail = fail
        self.warnings = list(warnings or [])

    def __call__(self, request: dict) -> dict:
        rid, method = request.get("id"), request.get("method")
        params = request.get("params") or {}
        if method == "get_bridge_capabilities":
            if self.capabilities == "unsupported":
                return FakeRevit.error(rid, -32601, "Method 'get_bridge_capabilities' not found")
            methods = ["get_bridge_capabilities", "send_code_to_revit", "get_available_family_types"]
            if self.list_model_items:
                methods.append("list_model_items")
            return FakeRevit.ok(rid, {"protocol": 2, "addin_version": "0.0.0-test", "methods": methods,
                                      "features": {}})
        if method == "list_model_items":
            if not self.list_model_items or self.advertise_only:
                return FakeRevit.error(rid, -32601, "Method 'list_model_items' not found")
            return self._list_model_items(rid, params)
        if method == "send_code_to_revit":
            return self._send_code(rid, params.get("code", ""))
        if method == "get_available_family_types":
            # the released command: no BuiltInCategory, and not every category
            cats = params.get("categoryList") or self.model["old_command_categories"]
            rows = [{k: v for k, v in t.items() if k != "BuiltInCategory"}
                    for c in cats if c in self.model["old_command_categories"] for t in self.model["types"].get(c, [])]
            return FakeRevit.ok(rid, rows)
        return FakeRevit.default_handler(request)

    # -- list_model_items ----------------------------------------------------------

    @staticmethod
    def _failed(rid, kind, code, message):
        return FakeRevit.ok(rid, {"Success": False, "Kind": kind, "Items": [], "Total": 0, "Truncated": False,
                                  "Warnings": [], "Error": {"Code": code, "Message": message}})

    def _type_categories(self, doc):
        names = self.model["category_names"]
        rows = [{"Category": c, "DisplayName": names.get(c, c), "Count": len(rows)}
                for c, rows in doc["types"].items() if rows]
        return sorted(rows, key=lambda r: (r["DisplayName"].lower(), r["Category"]))

    def _list_model_items(self, rid, params):
        kind = params.get("kind")
        if kind not in ("levels", "links", "types", "type_categories"):
            return self._failed(rid, kind, "unknown_kind", "kind must be one of levels, links, types, type_categories.")
        if self.fail:
            return self._failed(rid, kind, self.fail, f"fake failure {self.fail}")
        # the add-in's parameter checks and messages (ListModelItemsCommand.cs, ModelItemsRequest.Parse)
        category, link_id, ids = params.get("category"), params.get("link_id"), params.get("ids")
        if category is not None:
            if kind != "types":
                return self._failed(rid, kind, "bad_params", "category is used with kind types only.")
            if not isinstance(category, str) or not _CATEGORY_SHAPE.match(category) or category == "OST_Nope":
                return self._failed(rid, kind, "bad_category", "category must be the name of a BuiltInCategory, "
                                                               f"such as OST_Walls; got {json.dumps(category)}.")
        if link_id is not None and kind == "links":
            return self._failed(rid, kind, "bad_params", "link_id is not used with kind links.")
        if ids is not None:
            if kind == "type_categories":
                return self._failed(rid, kind, "bad_params", "ids is not used with kind type_categories.")
            if (not isinstance(ids, list) or len(ids) > 200
                    or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
                return self._failed(rid, kind, "bad_params", "ids must be an array of at most 200 integer element ids.")
        if kind == "types" and category is None and ids is None:
            return self._failed(rid, kind, "bad_params", "kind types needs category or ids.")
        doc = self.model
        if link_id is not None:
            link = next((l for l in self.model["links"] if l["Id"] == link_id), None)
            if link is None:
                return self._failed(rid, kind, "unknown_link",
                                    f"link_id {link_id} is not a Revit link instance in the open document.")
            if not link["Loaded"] or link.get("DocumentTitle") is None:      # a closed workset: type loaded, no document
                return self._failed(rid, kind, "link_not_loaded", f"The link {link_id} ({link['Name']}) is not loaded.")
            doc = self.model["linked"][link_id]
        if kind == "levels":
            rows = doc["levels"]
        elif kind == "links":
            rows = self.model["links"]
        elif kind == "types":
            # ids: any category, in the order of ids; with category too, only that category's (the
            # intersection). An add-in that ignores ids lists the category, or every type.
            rows = (doc["types"].get(category, []) if category is not None
                    else [t for group in doc["types"].values() for t in group])
            if ids is not None and self.honour_ids:
                by_id = {t["FamilyTypeId"]: t for t in rows}
                rows = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
        else:
            rows = self._type_categories(doc)
        if ids is not None and self.honour_ids and kind != "types":
            rows = [r for r in rows if r[_ID_KEYS[kind]] in ids]
        q = (params.get("q") or "").strip().lower()
        if q:
            rows = [r for r in rows if any(q in str(r.get(k) or "").lower() for k in _NAME_KEYS[kind])]
        total = len(rows)
        limit = params.get("limit")
        items = copy.deepcopy(rows[:limit] if limit else rows)
        return FakeRevit.ok(rid, {"Success": True, "Kind": kind, "Items": items, "Total": total,
                                  "Truncated": len(items) < total, "Warnings": list(self.warnings), "Error": None})

    # -- the send_code fallbacks -----------------------------------------------------

    def _send_code(self, rid, code: str):
        doc = self.model
        linked = re.search(r"l\.Id\.Value == (\d+)\);", code)
        if linked and "LinkError" in code:
            link_id = int(linked.group(1))
            link = next((l for l in self.model["links"] if l["Id"] == link_id), None)
            if link is None:
                return FakeRevit.code_result(rid, {"LinkError": "unknown_link"})
            if not link["Loaded"] or link.get("DocumentTitle") is None:
                return FakeRevit.code_result(rid, {"LinkError": "link_not_loaded"})
            doc = self.model["linked"][link_id]
        elif "typeof(RevitLinkInstance)" in code:
            return FakeRevit.code_result(rid, {"Success": True, "Items": copy.deepcopy(self.model["links"]),
                                               "Warnings": []})
        if "typeof(FamilySymbol)" in code and "DisplayName" in code:
            old = {c: rows for c, rows in doc["types"].items() if c in self.model["old_command_categories"]}
            return FakeRevit.code_result(rid, {"Success": True, "Items": self._type_categories({"types": old}),
                                               "Warnings": []})
        if "typeof(Level)" in code:
            return FakeRevit.code_result(rid, copy.deepcopy(doc["levels"]))
        if "new string[]" in code:
            names = re.findall(r'"(OST_[A-Za-z]+)"', code)
            return FakeRevit.code_result(rid, [{"Category": n, "Count": doc["counts"].get(n, 0)} for n in names])
        if "OfCategory(bic)" in code and "Take(" in code:
            category = re.search(r'"(OST_[A-Za-z]+)"', code).group(1)
            items = doc.get("elements", {}).get(category, [])
            return FakeRevit.code_result(rid, {"Total": doc["counts"].get(category, len(items)),
                                               "Items": copy.deepcopy(items)})
        return FakeRevit.code_result(rid, {"Status": "Created", "ElementId": 4242})

"""Read current type inventories and share complete rows only for clean documents."""
from collections import OrderedDict
from threading import Lock
from time import monotonic
from typing import NamedTuple

from revit_bridge.revit.client import RevitClient
from revit_bridge.snapshot.query import RevitQueryError

# Document.GetHashCode() identifies an opening of a document, including an
# unsaved one. A new save or dirty-state transition also invalidates the rows.
DOCUMENT_STAMP = """
using (var version = Document.GetDocumentVersion(document)) {
    return new { document = document.GetHashCode() + ":" + version.VersionGUID + ":" + version.NumberOfSaves,
                 modified = document.IsModified };
}
"""

# The normal category command takes one category at a time. An unsaved
# document cannot reuse those rows on the next keystroke. Read a broad search
# from one bounded collector using the same classes and family names as the
# add-in's FamilyTypeCollector. The legacy query covers six type classes.
DIRTY_TYPES = """
var timer = System.Diagnostics.Stopwatch.StartNew();
var rows = new List<object>();
var warnings = new List<string>();
bool modern = (bool)parameters[0], truncated = false;
var categories = new List<BuiltInCategory>();
foreach (var p in parameters.Skip(1))
    if (Enum.TryParse((string)p, out BuiltInCategory category)) categories.Add(category);
if (parameters.Length > 1 && categories.Count == 0) return new { rows, warnings, truncated };
using (var filter = categories.Count > 0 ? new ElementMulticategoryFilter(categories) : null)
using (var collector = new FilteredElementCollector(document)) {
    collector.WhereElementIsElementType();
    if (filter != null) collector.WherePasses(filter);
    foreach (Element element in collector) {
        if (timer.ElapsedMilliseconds >= 200) {
            warnings.Add("Type search reached its read budget. Choose a category to narrow the search.");
            truncated = true;
            break;
        }
        if (!(element is ElementType t)) continue;
        if (!modern && !(t is FamilySymbol || t is WallType || t is FloorType || t is RoofType ||
                         t is CeilingType || t is CurtainSystemType)) continue;
        string key = null;
        try {
            Category cat = t.Category;
            if (cat == null) continue;
            if (int.TryParse(cat.Id.ToString(), out int value))
                key = Enum.GetName(typeof(BuiltInCategory), (BuiltInCategory)value);
            if (key == null || !key.StartsWith("OST_")) continue;
            string family = t is FamilySymbol symbol ? symbol.FamilyName :
                (t.get_Parameter(BuiltInParameter.SYMBOL_FAMILY_NAME_PARAM)?.AsString()
                 ?? t.GetType().Name.Replace("Type", ""));
            rows.Add(new { id = t.Id.ToString(), name = t.Name, family, category = key, shown = cat.Name });
        } catch (Exception ex) {
            truncated = true;
            if (warnings.Count < 100) warnings.Add((key ?? "type") + " " + t.Id + ": " + ex.Message);
        }
    }
}
return new { rows, warnings, truncated };
"""

_TTL = 30
_sessions = OrderedDict()
_lock = Lock()


class DocumentStamp(NamedTuple):
    scope: tuple
    document: str
    modified: bool


def device_scope(client):
    # A WebSocket host can construct a fresh adapter for each request. Its
    # scope identifies the selected device and current relay connection.
    scope = getattr(client, "mention_cache_scope", None)
    if scope is not None:
        return "host", scope
    if isinstance(client, RevitClient):
        return "tcp", client.host, client.port, client.connection_generation
    return None


async def request_scope(client):
    if isinstance(client, RevitClient):
        await client.ensure_connected()
    return device_scope(client)


async def document_stamp(client, scope):
    if scope is None:
        return None
    response = await client.send_code(DOCUMENT_STAMP)
    if device_scope(client) != scope:
        raise RevitQueryError("The device connection changed; pick the type again.")
    data = response.result
    if (not response.success or not isinstance(data, dict) or not isinstance(data.get("document"), str)
            or not data["document"] or not isinstance(data.get("modified"), bool)):
        # Some hosts permit list_model_items but disable fixed code queries.
        return None
    return DocumentStamp(scope, data["document"], data["modified"])


async def read_unsaved_types(executor, matched):
    if matched == set():
        return {"rows": [], "warnings": [], "truncated": False}
    # Checking an empty id list reads no model elements and preserves the
    # picker's legacy fallback if a device advertises an unavailable method.
    modern = await executor._offered("types", ids=[]) is not None
    response = await executor.client.send_code(DIRTY_TYPES, parameters=[modern, *sorted(matched or ())])
    data = response.result
    if not response.success or not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise RevitQueryError(response.error or "Invalid type search result")
    return data


def inventories(stamp):
    """Keep only the active document for each add-in session."""
    session, document, modified = stamp
    with _lock:
        if modified:
            _sessions.pop(session, None)
            return {}
        cached = _sessions.get(session)
        if cached is None or cached[0] != document:
            cached = _sessions[session] = (document, {})
        _sessions.move_to_end(session)
        if len(_sessions) > 32:
            _sessions.popitem(last=False)
        rows = cached[1]
        now = monotonic()
        for key in [k for k, (expires, _) in rows.items() if expires <= now]:
            del rows[key]
        return rows


def cached(inventory, category):
    with _lock:
        entry = inventory.get(category)
        return entry[1] if entry is not None and monotonic() < entry[0] else None


def pending(rows):
    return monotonic() + _TTL, rows


def publish(inventory, additions):
    # A concurrent document switch replaces this mapping in the registry.
    # A delayed request can only publish to its own document's mapping.
    with _lock:
        inventory.update(additions)

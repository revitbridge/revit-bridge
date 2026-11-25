"""Read current type inventories and share complete rows only for clean documents."""
from collections import OrderedDict
from threading import Lock
from time import monotonic
from typing import NamedTuple
from weakref import ref

from revit_bridge.revit.client import RevitClient
from revit_bridge.snapshot.query import RevitQueryError

# Document.GetHashCode() identifies an opening of a document, including an
# unsaved one. A new save or dirty-state transition also invalidates the rows.
DOCUMENT_STAMP = """
using (var version = Document.GetDocumentVersion(document)) {
    return new { document = document.GetHashCode().ToString(), version = version.VersionGUID + ":" + version.NumberOfSaves,
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
string source = document.GetHashCode().ToString();
var categories = new List<BuiltInCategory>();
foreach (var p in parameters.Skip(1))
    if (Enum.TryParse((string)p, out BuiltInCategory category)) categories.Add(category);
if (parameters.Length > 1 && categories.Count == 0) return new { source, rows, warnings, truncated };
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
return new { source, rows, warnings, truncated };
"""

_TTL = 30
_sessions = OrderedDict()
_lock = Lock()


class DocumentStamp(NamedTuple):
    scope: tuple
    document: str
    version: str
    modified: bool


def device_scope(client):
    # A WebSocket host can construct a fresh adapter for each request. Its
    # scope identifies the selected device and current relay connection.
    scope = getattr(client, "mention_cache_scope", None)
    if scope is not None:
        return "host", scope
    if isinstance(client, RevitClient) and client.connected:
        # Generations count connects per client. Different clients may share
        # an endpoint and generation; a weak key keeps their sockets separate.
        return "tcp", ref(client), client.connection_generation
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
            or not data["document"] or not isinstance(data.get("version"), str) or not isinstance(data.get("modified"), bool)):
        # Some hosts permit list_model_items but disable fixed code queries.
        return None
    return DocumentStamp(scope, data["document"], data["version"], data["modified"])


async def read_unsaved_types(executor, matched, stamp):
    if matched == set():
        return {"rows": [], "warnings": [], "truncated": False}
    # Checking an empty id list reads no model elements and preserves the
    # picker's legacy fallback if a device advertises an unavailable method.
    modern = await executor._offered("types", ids=[]) is not None
    response = await executor.client.send_code(DIRTY_TYPES, parameters=[modern, *sorted(matched or ())])
    data = response.result
    if not response.success or not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise RevitQueryError(response.error or "Invalid type search result")
    if stamp.document != data.get("source"):
        raise RevitQueryError("The active document changed; pick the type again.")
    return data


def _prune(now):
    for key, (_, entries) in list(_sessions.items()):
        for category in [c for c, (expires, _) in entries.items() if expires <= now]:
            del entries[category]
        if not entries:
            del _sessions[key]


def inventories(stamp):
    """Keep only the active document for each add-in session."""
    session, document, version, modified = stamp
    with _lock:
        _prune(monotonic())
        cached = _sessions.get(session)
        if modified or cached is None or cached[0] != (document, version):
            _sessions.pop(session, None)
            return {}
        _sessions.move_to_end(session)
        return cached[1]


def discard(scope):
    with _lock:
        _sessions.pop(scope, None)


def cached(inventory, category):
    with _lock:
        entry = inventory.get(category)
        return entry[1] if entry is not None and monotonic() < entry[0] else None


def pending(rows):
    return monotonic() + _TTL, rows


def publish(stamp, additions):
    # Register complete rows only after the second stamp confirms the clean
    # document. Another connection's read cannot evict an in-flight fill.
    with _lock:
        now = monotonic()
        _prune(now)
        fresh = {category: entry for category, entry in additions.items() if entry[0] > now}
        if not fresh:
            return
        marker = stamp.document, stamp.version
        cached = _sessions.get(stamp.scope)
        if cached is None or cached[0] != marker:
            cached = _sessions[stamp.scope] = (marker, {})
        cached[1].update(fresh)
        _sessions.move_to_end(stamp.scope)
        if len(_sessions) > 32:
            _sessions.popitem(last=False)

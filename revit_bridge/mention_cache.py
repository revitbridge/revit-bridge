"""Read current type inventories and share complete rows only for clean documents."""
from collections import OrderedDict
from threading import Lock
from time import monotonic
from typing import NamedTuple
from weakref import WeakKeyDictionary, ref

from revit_bridge.revit.client import RevitClient
from revit_bridge.snapshot.query import RevitQueryError, _type_name, type_category_items

# Document.GetHashCode() identifies an opening of a document, including an
# unsaved one. A new save or dirty-state transition also invalidates the rows.
DOCUMENT_STAMP = """
using (var version = Document.GetDocumentVersion(document)) {
    return new { document = document.GetHashCode().ToString(), version = version.VersionGUID + ":" + version.NumberOfSaves,
                 modified = document.IsModified };
}
"""

# The normal category command takes one category at a time. Read a cold
# cross-category inventory in one callback with the same classes and names as the
# add-in's FamilyTypeCollector. The legacy query covers six type classes.
_INVENTORY_BODY = """
var timer = System.Diagnostics.Stopwatch.StartNew();
var rows = new List<object>();
var warnings = new List<string>();
bool modern = (bool)parameters[0], truncated = false, timedOut = false;
long copiedChars = 0;
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
        if (timer.ElapsedMilliseconds >= 1500) {
            warnings.Add("Type search reached its read budget. Choose a category to narrow the search.");
            truncated = timedOut = true;
            break;
        }
        if (rows.Count >= 8192) {
            warnings.Add("Type search reached its row limit. Choose a category to narrow the search.");
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
            string id = t.Id.ToString(), name = t.Name, shown = cat.Name;
            copiedChars += id.Length + (name?.Length ?? 0) + (family?.Length ?? 0) + key.Length + (shown?.Length ?? 0);
            if (copiedChars > 1048576) {
                warnings.Add("Type search reached its response budget. Choose a category to narrow the search.");
                truncated = true;
                break;
            }
            rows.Add(new { id, name, family, category = key, shown });
        } catch (Exception ex) {
            truncated = true;
            if (warnings.Count < 100) warnings.Add((key ?? "type") + ": " + ex.Message.Substring(0, Math.Min(240, ex.Message.Length)));
        }
    }
}
"""
TYPE_INVENTORY = _INVENTORY_BODY + "return new { source, rows, warnings, truncated, timedOut };"
# The repeat is an independent API callback. Read its closing document stamp
# there so a cold search including the repeat still uses at most five RPCs.
_RETRY_INVENTORY = _INVENTORY_BODY + """
using (var version = Document.GetDocumentVersion(document)) {
    return new { source, rows, warnings, truncated, timedOut, document = source,
                 version = version.VersionGUID + ":" + version.NumberOfSaves, modified = document.IsModified };
}
"""

_TTL = 30
_sessions = OrderedDict()
_tcp_reads = WeakKeyDictionary()
_host_readers = {}
_next_host_check = 0.0
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


def _first_inventory(client, scope):
    """Mark the first valid inventory response on a connection, independently of cached rows."""
    if scope is None:
        return False
    if scope[0] == "tcp":
        with _lock:
            generation = scope[2]
            if _tcp_reads.get(client, -1) >= generation:
                return False
            _tcp_reads[client] = generation
            return True
    # A request-local relay adapter can still observe its underlying device
    # after this request ends. A third-party adapter with a static key cannot.
    observes_connection = (isinstance(getattr(type(client), "mention_cache_scope", None), property)
                           and isinstance(getattr(type(client), "connected", None), property))
    global _next_host_check
    with _lock:
        first = scope not in _host_readers
        if first or (observes_connection and _host_readers[scope] is None):
            _host_readers[scope] = client if observes_connection else None
        now = monotonic()
        if now < _next_host_check:
            return first
        _next_host_check = now + _TTL
        candidates = [(key, adapter) for key, adapter in _host_readers.items() if adapter is not None]
    for other, adapter in candidates:
        try:
            retired = not adapter.connected or device_scope(adapter) != other
        except Exception:
            # Keep the consumed marker if its lifetime cannot be checked, but
            # release the faulty adapter. Foreign properties run outside the lock.
            with _lock:
                if _host_readers.get(other) is adapter:
                    _host_readers[other] = None
        else:
            if retired:
                with _lock:
                    if _host_readers.get(other) is adapter:
                        del _host_readers[other]
    return first


def _stamp(data, scope):
    if (not isinstance(data, dict) or not isinstance(data.get("document"), str) or not data["document"]
            or not isinstance(data.get("version"), str) or not isinstance(data.get("modified"), bool)):
        return None
    return DocumentStamp(scope, data["document"], data["version"], data["modified"])


async def document_stamp(client, scope):
    if scope is None:
        return None
    response = await client.send_code(DOCUMENT_STAMP)
    if device_scope(client) != scope:
        raise RevitQueryError("The device connection changed; pick the type again.")
    # Some hosts permit list_model_items but disable fixed code queries.
    return _stamp(response.result, scope) if response.success else None


async def read_type_inventory(executor, matched, stamp):
    # An empty id list verifies the advertised method without reading types.
    # If a host denied the identity query, ask for categories instead: they
    # provide a bounded native fallback if it also denies the inventory.
    client = executor.client
    scope = device_scope(client)
    if stamp is None:
        probe = await executor._offered("type_categories", definite=True, limit=200)
    else:
        probe = await executor._offered("types", definite=True, ids=[], limit=1)
    if device_scope(client) != scope or (stamp and scope != stamp.scope):
        raise RevitQueryError("The device connection changed; pick the type again.")
    parameters = [probe is not None, *sorted(matched or ())]
    response = await client.send_code(TYPE_INVENTORY, parameters=parameters)
    if device_scope(client) != scope:
        raise RevitQueryError("The device connection changed; pick the type again.")
    if not response.success and stamp is None and probe is not None:
        return await _native_subset(executor, probe, matched), None
    data = _inventory_result(response, stamp)
    first = _first_inventory(client, scope)
    if not (first and data["truncated"] and data.get("timedOut") is True):
        return data, None
    response = await client.send_code(_RETRY_INVENTORY if stamp else TYPE_INVENTORY, parameters=parameters)
    repeated = _inventory_result(response, stamp)
    if device_scope(client) != scope or data["source"] != repeated["source"]:
        raise RevitQueryError("The document or connection changed; pick the type again.")
    current = _stamp(repeated, scope) if stamp else None
    if stamp and (not current or current.document != repeated["source"]):
        raise RevitQueryError("Invalid document identity on repeated type search")
    return repeated, current


def _inventory_result(response, stamp):
    data = response.result
    if (not response.success or not isinstance(data, dict) or not isinstance(data.get("rows"), list)
            or not isinstance(data.get("source"), str)):
        raise RevitQueryError(response.error or "Invalid type search result")
    if stamp and stamp.document != data["source"]:
        raise RevitQueryError("The active document changed; pick the type again.")
    if (not isinstance(data.get("warnings"), list) or not all(isinstance(w, str) for w in data["warnings"])
            or not isinstance(data.get("truncated"), bool)):
        raise RevitQueryError("Invalid type search result")
    return data


async def _native_subset(executor, probe, matched):
    # One request remains. A single alias can name a category beyond the
    # probe's page. Otherwise read the most populated eligible category.
    categories, warnings = native_categories(probe)
    if matched and len(matched) == 1:
        key = next(iter(matched))
        incomplete = False
    else:
        eligible = [row for row in categories if matched is None or row["category"] in matched]
        eligible.sort(key=lambda row: (-(row["count"] or 0), row["category"]))
        key = eligible[0]["category"] if eligible else None
        incomplete = bool(warnings) or len(eligible) > 1
    rows = []
    if key:
        page = await executor.list_model_items("types", category=key, limit=200)
        data = native_types(page, key)
        rows = data["rows"]
        warnings.extend(data["warnings"])
        incomplete = incomplete or data["truncated"]
    if incomplete:
        scope = f" Only the first {len(rows)} types of {key} were read." if key else ""
        warnings.append(f"Cross-category query unavailable.{scope} Choose a category for a full search.")
    return {"rows": rows, "warnings": warnings, "truncated": bool(incomplete or warnings)}


def _coverage_warnings(data):
    if data.truncated or (data.total is not None and data.total != len(data.rows)):
        return ["The add-in returned an incomplete list."]
    if data.truncated is None and data.total is None:
        return ["The add-in did not confirm that its list was complete."]
    return []


def native_types(data, category):
    rows = [dict(id=t.get("FamilyTypeId"), name=_type_name(t), family=t.get("FamilyName"),
                 shown=t.get("Category"), category=t.get("BuiltInCategory") or category) for t in data.rows]
    warnings = data.warnings + _coverage_warnings(data)
    return {"rows": rows, "warnings": warnings, "truncated": bool(warnings)}


def native_categories(data):
    rows, warnings = type_category_items(data)
    return rows, warnings + _coverage_warnings(data)


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

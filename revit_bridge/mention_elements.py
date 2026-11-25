"""Fixed, read-only queries for picking elements and rechecking their ids."""
import asyncio

from revit_bridge.mention_search import facets, rank_items
from revit_bridge.snapshot.query import RevitQueryError, element_id_or_none

SELECTION_LIMIT = 50

# The index carries only the fields needed for facets and ranking. Cache
# repeated category/type reads within this callback. Only the requested page
# goes on to read levels and curves. Yield control on an unusually slow scan.
_READ = """
var items = new List<object>();
var warnings = new List<string>();
void Warn(string text) {
    if (warnings.Count < 100) warnings.Add(text.Length > 500 ? text.Substring(0, 500) : text);
    else if (warnings.Count == 100) warnings.Add("Additional selection field warnings omitted.");
}
var categories = new Dictionary<ElementId, string[]>();
var types = new Dictionary<ElementId, string[]>();
int remainingChars = 1024 * 1024;
foreach (ElementId id in ids) {
    if (!details && (timer.ElapsedMilliseconds >= 200 || items.Count >= maxRows))
        break;
    Element e = document.GetElement(id);
    if (e == null) {
        if (!details) throw new InvalidOperationException("The selection changed; pick the selection again.");
        continue;
    }
    string name = null, family = null, category = null, shown = null, level = null;
    double? length = null;
    if (names) try {
        ElementId typeId = e is ElementType ? id : e.GetTypeId();
        if (!types.TryGetValue(typeId, out var pair)) {
            ElementType type = e as ElementType ?? document.GetElement(typeId) as ElementType;
            types[typeId] = pair = new string[] {type?.Name, type?.FamilyName};
        }
        name = pair[0]; family = pair[1];
    } catch (Exception ex) { Warn(id + " type: " + ex.Message); }
    try {
        Category cat = e.Category;
        if (cat != null) {
            if (!categories.TryGetValue(cat.Id, out var pair)) {
                int value;
                string key = int.TryParse(cat.Id.ToString(), out value)
                    ? Enum.GetName(typeof(BuiltInCategory), (BuiltInCategory)value) : null;
                categories[cat.Id] = pair = new string[] {key, cat.Name};
            }
            category = pair[0]; shown = pair[1];
        }
    } catch (Exception ex) { Warn(id + " category: " + ex.Message); }
    if (details) try { level = document.GetElement(e.LevelId)?.Name; }
    catch (Exception ex) { Warn(id + " level: " + ex.Message); }
    if (details) try {
        LocationCurve line = e.Location as LocationCurve;
        if (line?.Curve != null) length = line.Curve.Length * 304.8;
    } catch (Exception ex) { Warn(id + " length: " + ex.Message); }
    if (!details) {
        remainingChars -= 96 + (name?.Length ?? 0) + (family?.Length ?? 0)
                             + (category?.Length ?? 0) + (shown?.Length ?? 0);
        if (remainingChars < 0) break;
    }
    if (details) items.Add(new { id = id.ToString(), name, family, category, shown, level, length });
    else if (names) items.Add(new { id = id.ToString(), name, family, category, shown });
    else items.Add(new { id = id.ToString(), category, shown });
}
"""

_VIEW = """
string viewId = document.ActiveView?.UniqueId;
if (viewId == null) throw new InvalidOperationException("No active view.");
string view = document.GetHashCode().ToString() + ":" + viewId;
"""
_TIMER = "var timer = System.Diagnostics.Stopwatch.StartNew();\n"
_SNAPSHOT = """
var ids = new UIDocument(document).Selection.GetElementIds();
int total = ids.Count;
// A native selection is already a snapshot. Bulk-copy its handles only if
// the collection has no indexer. Neither deep paging nor a budget expiry
// needs model lookups for the elements preceding the requested page.
var page = new List<string>();
var positions = parameters.Select(p => Convert.ToInt32(p)).Where(i => i < total).ToArray();
if (positions.Length > 0) {
    IList<ElementId> ordered = ids as IList<ElementId>;
    if (ordered == null) {
        int end = positions.Max() + 1;
        ordered = total >= 1024 && end >= total - total / 8 ? ids.ToArray() : ids.Take(end).ToArray();
    }
    foreach (int i in positions) page.Add(ordered[i].ToString());
}
var members = new List<string>();
foreach (var id in ids) {
    if (timer.ElapsedMilliseconds >= memberBudget || members.Count >= 65536) break;
    members.Add(id.ToString());
}
"""
_CHANGED = "The active view or selection changed; pick the selection again."
_PARTIAL = ("The selection read or response-size budget was reached. Category facets and text matches cover only "
            "the verified rows; total is the full selection count.")


async def _read(executor, prelude, parameters=(), *, selection=False):
    result = "view, items, warnings" + (", members, total, page" if selection
                                        else ", document_id = document.GetHashCode().ToString()")
    response = await executor.client.send_code(prelude + _READ + "return new { " + result + " };",
                                               parameters=list(parameters))
    if not response.success or not isinstance(response.result, dict) or not isinstance(response.result.get("items"), list):
        raise RevitQueryError(response.error or "Invalid element query result")
    return response.result


def _items(rows):
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        number = element_id_or_none(row.get("id"))
        if number is None:
            continue
        name, shown, level = row.get("name"), row.get("shown"), row.get("level")
        details = [shown or name or f"#{number}"]
        if isinstance(row.get("length"), (int, float)):
            details.append(f"{row['length']:.0f} mm")
        if level:
            details.append(level)
        summary = ", ".join(" ".join(str(s).split()) for s in details)
        items.append(dict(kind="element", id=number, name=name, family=row.get("family"),
                          category=row.get("category"), category_label=shown, level=level, summary=summary,
                          label=name or f"#{number}", detail=summary))
    return items


def _snapshot(data, positions):
    if (not isinstance(data, dict) or not isinstance(data.get("view"), str) or
            type(data.get("total")) is not int or data["total"] < 0 or
            not isinstance(data.get("members"), list) or not isinstance(data.get("page"), list)):
        raise RevitQueryError("Invalid selection snapshot")
    members = [element_id_or_none(value) for value in data["members"]]
    page = [element_id_or_none(value) for value in data["page"]]
    if (None in members or None in page or len(set(members)) != len(members) or
            len(members) > data["total"] or len(page) != sum(i < data["total"] for i in positions)):
        raise RevitQueryError("Invalid ids in selection snapshot")
    return data["view"], data["total"], members, page


async def _by_ids(executor, numbers, *, view=None):
    constructors = ", ".join(f"new ElementId({n})" for n in numbers)
    prelude = (_VIEW + 'if (view != (string)parameters[0]) throw new InvalidOperationException("The active view changed.");\n'
               if view is not None else "string view = null;\n")
    page = await _read(executor, prelude + _TIMER + f"""
#pragma warning disable 618
var ids = new ElementId[] {{ {constructors} }};
#pragma warning restore 618
bool names = true, details = true;
int maxRows = 50;
""", [view] if view is not None else [])
    document_id = page.get("document_id")
    if not isinstance(document_id, str) or not document_id:
        raise RevitQueryError("Missing document identity in element query")
    return [dict(item, document_id=document_id) for item in _items(page["items"])], page.get("warnings") or []


async def selected_items(executor, offset, limit, terms):
    """One lightweight index, at most one detail page, then verify the live selection."""
    positions = [] if terms else list(range(offset, min(offset + limit, 2**31 - 1)))
    # Reserve half of the per-callback time for model fields after copying
    # scalar ids. A separate size cap bounds the double-encoded RPC reply.
    prelude = (_VIEW + _TIMER + "int memberBudget = 100;\n" + _SNAPSHOT
               + f"int maxRows = members.Count;\nbool names = {str(bool(terms)).lower()}, details = false;\n")
    index = await _read(executor, prelude, positions, selection=True)
    view, total, members, page_ids = _snapshot(index, positions)
    items = _items(index["items"])
    if len(items) != len(index["items"]) or [i["id"] for i in items] != members[:len(items)]:
        raise RevitQueryError("Invalid selection index")
    ranked = await asyncio.to_thread(rank_items, items, terms) if terms else items
    if terms:
        page_ids = [item["id"] for item in ranked[offset:offset + limit]]
        index_of = {number: i for i, number in enumerate(members)}
        positions = [index_of[number] for number in page_ids]
    warnings = list(index.get("warnings") or [])
    rows = []
    if page_ids:
        rows, notes = await _by_ids(executor, page_ids, view=view)
        if [item["id"] for item in rows] != page_ids:
            raise RevitQueryError(_CHANGED)
        warnings.extend(notes)
    recheck = _VIEW + _TIMER + "int memberBudget = 200;\n" + _SNAPSHOT
    response = await executor.client.send_code(recheck + "return new { view, total, members, page };", parameters=positions)
    if not response.success:
        raise RevitQueryError(response.error or "Could not recheck the selection")
    last_view, last_total, last_members, last_page = _snapshot(response.result, positions)
    if view != last_view or total != last_total:
        raise RevitQueryError(_CHANGED)
    checked = min(len(members), len(last_members))
    same_members = (set(members) == set(last_members) if checked == total
                    else members[:checked] == last_members[:checked])
    if not same_members or ((not terms or checked < total) and page_ids != last_page):
        raise RevitQueryError(_CHANGED)
    partial = len(items) < total or checked < total
    if partial:
        warnings.append(_PARTIAL)
        if checked < total:
            warnings.append("Selection membership beyond the checked prefix and returned page could not be verified.")
        if checked < len(items):
            verified = set(last_members[:checked]) | set(last_page)
            ranked = [item for item in ranked if item["id"] in verified]
            if terms:
                wanted = {item["id"] for item in ranked[offset:offset + limit]}
                rows = [item for item in rows if item["id"] in wanted]
        if not terms:
            indexed = {item["id"] for item in ranked}
            ranked.extend(item for item in rows if item["id"] not in indexed)
    return {"kind": "element", "items": rows, "offset": offset, "total": total if partial else len(ranked),
            "truncated": partial or len(ranked) > offset + limit, "category_matched": [], "facets": facets(ranked),
            **({"facets_partial": True} if partial else {}),
            **({"warnings": list(dict.fromkeys(warnings))} if warnings else {})}


async def items_by_id(executor, ids):
    """The bound element can be deselected. Check the open document directly."""
    numbers = [element_id_or_none(value) for value in ids]
    if len(ids) > SELECTION_LIMIT or None in numbers:
        raise ValueError("Invalid element ids")
    # Small integer literals select the supported constructor on all years.
    # Revit 2024 introduced 64-bit ids. An older document cannot contain one.
    if any(n > 2147483647 for n in numbers):
        year = await executor.client.send_code("return document.Application.VersionNumber;")
        if not year.success or not str(year.result).isdigit():
            raise RevitQueryError(year.error or "Unknown Revit version")
        if int(year.result) < 2024:
            numbers = [n for n in numbers if n <= 2147483647]
    if not numbers:
        return [], []
    return await _by_ids(executor, numbers)

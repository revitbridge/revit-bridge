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
var categories = new Dictionary<ElementId, string[]>();
var types = new Dictionary<ElementId, string[]>();
foreach (ElementId id in ids) {
    if (!details && timer.ElapsedMilliseconds >= 200)
        throw new InvalidOperationException("Selection read took too long. Select fewer elements and try again.");
    Element e = document.GetElement(id);
    if (e == null) continue;
    string name = null, family = null, category = null, shown = null, level = null;
    double? length = null;
    if (names) try {
        ElementId typeId = e is ElementType ? id : e.GetTypeId();
        if (!types.TryGetValue(typeId, out var pair)) {
            ElementType type = e as ElementType ?? document.GetElement(typeId) as ElementType;
            types[typeId] = pair = new string[] {type?.Name, type?.FamilyName};
        }
        name = pair[0]; family = pair[1];
    } catch (Exception ex) { warnings.Add(id + " type: " + ex.Message); }
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
    } catch (Exception ex) { warnings.Add(id + " category: " + ex.Message); }
    if (details) try { level = document.GetElement(e.LevelId)?.Name; }
    catch (Exception ex) { warnings.Add(id + " level: " + ex.Message); }
    if (details) try {
        LocationCurve line = e.Location as LocationCurve;
        if (line?.Curve != null) length = line.Curve.Length * 304.8;
    } catch (Exception ex) { warnings.Add(id + " length: " + ex.Message); }
    if (details) items.Add(new { id = id.ToString(), name, family, category, shown, level, length });
    else if (names) items.Add(new { id = id.ToString(), name, family, category, shown });
    else items.Add(new { id = id.ToString(), category, shown });
}
return new { view, items, warnings };
"""

_VIEW = """
string viewId = document.ActiveView?.UniqueId;
if (viewId == null) throw new InvalidOperationException("No active view.");
string view = document.GetHashCode().ToString() + ":" + viewId;
"""
_TIMER = "var timer = System.Diagnostics.Stopwatch.StartNew();\n"
_SELECTED_IDS = "var ids = new UIDocument(document).Selection.GetElementIds();\n"
_MEMBERSHIP = _VIEW + _TIMER + _SELECTED_IDS + """
var members = new List<string>(ids.Count);
foreach (var id in ids) {
    if (timer.ElapsedMilliseconds >= 200)
        throw new InvalidOperationException("Selection read took too long. Select fewer elements and try again.");
    members.Add(id.ToString());
}
return new { view, ids = members };
"""
_CHANGED = "The active view or selection changed; pick the selection again."


async def _read(executor, prelude, parameters=()):
    response = await executor.client.send_code(prelude + _READ, parameters=list(parameters))
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


async def _selection(executor):
    response = await executor.client.send_code(_MEMBERSHIP)
    data = response.result
    if (not response.success or not isinstance(data, dict) or not isinstance(data.get("ids"), list)
            or not isinstance(data.get("view"), str)):
        raise RevitQueryError(response.error or "Invalid selection snapshot")
    ids = [element_id_or_none(value) for value in data["ids"]]
    if None in ids or len(set(ids)) != len(ids):
        raise RevitQueryError("Invalid ids in selection snapshot")
    return data["view"], ids


async def _by_ids(executor, numbers, *, view=None):
    constructors = ", ".join(f"new ElementId({n})" for n in numbers)
    prelude = (_VIEW + 'if (view != (string)parameters[0]) throw new InvalidOperationException("The active view changed.");\n'
               if view is not None else "string view = null;\n")
    page = await _read(executor, prelude + _TIMER + f"""
#pragma warning disable 618
var ids = new ElementId[] {{ {constructors} }};
#pragma warning restore 618
bool names = true, details = true;
""", [view] if view is not None else [])
    return _items(page["items"]), page.get("warnings") or []


async def selected_items(executor, offset, limit, terms):
    """One lightweight index, at most one detail page, then verify the live selection."""
    prelude = _VIEW + _TIMER + _SELECTED_IDS + f"bool names = {str(bool(terms)).lower()}, details = false;\n"
    index = await _read(executor, prelude)
    view = index.get("view")
    items = _items(index["items"])
    members = {item["id"] for item in items}
    if not isinstance(view, str) or len(members) != len(index["items"]):
        raise RevitQueryError("Invalid selection index")
    ranked = await asyncio.to_thread(rank_items, items, terms) if terms else items
    page_ids = [item["id"] for item in ranked[offset:offset + limit]]
    warnings = list(index.get("warnings") or [])
    rows = []
    if page_ids:
        rows, notes = await _by_ids(executor, page_ids, view=view)
        if [item["id"] for item in rows] != page_ids:
            raise RevitQueryError(_CHANGED)
        warnings.extend(notes)
    last_view, last_ids = await _selection(executor)
    if view != last_view or members != set(last_ids):
        raise RevitQueryError(_CHANGED)
    return {"kind": "element", "items": rows, "offset": offset, "total": len(ranked),
            "truncated": len(ranked) > offset + limit, "category_matched": [], "facets": facets(ranked),
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

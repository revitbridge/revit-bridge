"""Fixed, read-only queries for picking elements and rechecking their ids."""
from revit_bridge.snapshot.query import RevitQueryError, element_id_or_none

SELECTION_LIMIT = 50

# Each callback reads at most SELECTION_LIMIT elements. The older dedicated
# get_selected_elements command materializes *every* element before applying
# its limit and returns neither the total nor the type or level.
_READ = """
var items = new List<object>();
var warnings = new List<string>();
foreach (ElementId id in ids) {
    Element e = document.GetElement(id);
    if (e == null) continue;
    string name = null, family = null, category = null, shown = null, level = null;
    double? length = null;
    if (details) try {
        ElementType type = e as ElementType ?? document.GetElement(e.GetTypeId()) as ElementType;
        if (type != null) { name = type.Name; family = type.FamilyName; }
    } catch (Exception ex) { warnings.Add(id + " type: " + ex.Message); }
    try {
        Category cat = e.Category;
        if (cat != null) {
            shown = cat.Name;
            int value;
            if (int.TryParse(cat.Id.ToString(), out value))
                category = Enum.GetName(typeof(BuiltInCategory), (BuiltInCategory)value);
        }
    } catch (Exception ex) { warnings.Add(id + " category: " + ex.Message); }
    if (details) try { level = document.GetElement(e.LevelId)?.Name; }
    catch (Exception ex) { warnings.Add(id + " level: " + ex.Message); }
    if (details) try {
        LocationCurve line = e.Location as LocationCurve;
        if (line?.Curve != null) length = line.Curve.Length * 304.8;
    } catch (Exception ex) { warnings.Add(id + " length: " + ex.Message); }
    items.Add(new { id = id.ToString(), name, family, category, shown, level, length });
}
return new { items, warnings };
"""

_VIEW = 'string view = document.GetHashCode().ToString() + ":" + document.ActiveView.UniqueId;\n'
_SELECTION = _VIEW + """
var ids = new UIDocument(document).Selection.GetElementIds();
return new { view, ids = ids.Select(id => id.ToString()).ToList() };
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
    # Copy just the scalar ids of one native selection snapshot. All slicing,
    # comparison and ranking of that snapshot take place outside Revit.
    response = await executor.client.send_code(_SELECTION)
    data = response.result
    if (not response.success or not isinstance(data, dict) or not isinstance(data.get("ids"), list)
            or not isinstance(data.get("view"), str)):
        raise RevitQueryError(response.error or "Invalid selection snapshot")
    ids = [element_id_or_none(value) for value in data["ids"]]
    if None in ids or len(set(ids)) != len(ids):
        raise RevitQueryError("Invalid ids in selection snapshot")
    return data["view"], ids


async def _by_ids(executor, numbers, *, details=True, view=None):
    constructors = ", ".join(f"new ElementId({n})" for n in numbers)
    prelude = (_VIEW + 'if (view != (string)parameters[0]) throw new InvalidOperationException("The active view changed.");\n'
               if view is not None else "")
    page = await _read(executor, prelude + f"""
#pragma warning disable 618
var ids = new ElementId[] {{ {constructors} }};
#pragma warning restore 618
bool details = {str(details).lower()};
""", [view] if view is not None else [])
    return _items(page["items"]), page.get("warnings") or []


async def selected_items(executor, offset, limit, searching):
    """Read the requested detail page and count categories across the complete snapshot."""
    view, ids = await _selection(executor)
    items, warnings = [], []
    # Searching needs all names for ranking. Otherwise only the actual page
    # needs a type, level and location curve; the other ids are for facets.
    groups = [(ids, True)] if searching else [
        (ids[:offset], False), (ids[offset:offset + limit], True), (ids[offset + limit:], False)]
    for group, details in groups:
        for start in range(0, len(group), SELECTION_LIMIT):
            batch = group[start:start + SELECTION_LIMIT]
            rows, notes = await _by_ids(executor, batch, view=view, details=details)
            if len(rows) != len(batch):
                raise RevitQueryError(_CHANGED)
            items.extend(rows)
            warnings.extend(notes)
    if ids:
        last_view, last_ids = await _selection(executor)
        if view != last_view or set(ids) != set(last_ids):
            raise RevitQueryError(_CHANGED)
    return items, warnings


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

"""Fixed, read-only queries for finding elements in the whole model and for a rough 3D shape of one.

``model_items(executor, offset, limit, terms)``
    The designer has nothing selected: search the open model instead. One index read lists
    the model elements that have a place in the model (a built-in model category, not view
    specific, not a view, a camera or a subcategory element, with a bounding box that has a size) as ``{id, name, family, category, shown}``; the terms are
    ranked here, as a selection is (type name, family, category, its display name and the
    bilingual aliases); only the requested page goes on to read levels and lengths. A term
    that is a whole number also finds the element with that id. The index stops at a time
    and row budget: the reply then says ``truncated`` and ``facets_partial`` with a warning,
    and a search covers only the rows read.
``element_preview(executor, element_id, max_triangles=...)``
    A rough shape of one element for a host to draw while the designer chooses: the
    triangles Revit gives at its coarsest tessellation, in whole millimetres from the corner
    of the element's bounding box, never more than ``max_triangles``. It is a picture to
    recognise an element by, not a measurement: nothing here may be used as a dimension. An
    element without geometry, or with more than the budget allows, comes back as its box
    (``shape: "box"``) or as the part that was read (``partial: true``).
"""
from __future__ import annotations

import asyncio

from revit_bridge.mention_elements import SELECTION_LIMIT, _by_ids, _items, items_by_id
from revit_bridge.mention_search import facets, rank_items
from revit_bridge.snapshot.query import RevitQueryError, element_id_or_none

INDEX_ROWS = 20000
INDEX_BUDGET_MS = 1500
PREVIEW_TRIANGLES = 1500
PREVIEW_TRIANGLES_MAX = 4000
PREVIEW_BUDGET_MS = 400

_INCOMPLETE = ("The model is larger than one search reads. Category facets and text matches cover only "
               "the elements read; add a search term or select the elements in Revit.")

_INDEX = """
var timer = System.Diagnostics.Stopwatch.StartNew();
int budget = Convert.ToInt32(parameters[0]), maxRows = Convert.ToInt32(parameters[1]);
var items = new List<object>();
var warnings = new List<string>();
var categories = new Dictionary<ElementId, string[]>();
var types = new Dictionary<ElementId, string[]>();
int remainingChars = 1024 * 1024;
bool complete = true;
foreach (Element e in new FilteredElementCollector(document).WhereElementIsNotElementType()) {
    if (timer.ElapsedMilliseconds >= budget || items.Count >= maxRows || remainingChars < 0) { complete = false; break; }
    string name = null, family = null, category = null, shown = null;
    try {
        Category cat = e.Category;
        // A subcategory element (a pipe's centre line) is part of another element; a camera is a view's.
        if (cat == null || cat.CategoryType != CategoryType.Model || cat.Parent != null
            || e.ViewSpecific || e is View) continue;
        if (!categories.TryGetValue(cat.Id, out var pair)) {
            int value;
            string key = int.TryParse(cat.Id.ToString(), out value)
                ? Enum.GetName(typeof(BuiltInCategory), (BuiltInCategory)value) : null;
            categories[cat.Id] = pair = new string[] {key, cat.Name};
        }
        category = pair[0]; shown = pair[1];
        if (category == null || category == "OST_Cameras") continue;
        // Something with a size: base points, the survey point and the like have none.
        BoundingBoxXYZ box = e.get_BoundingBox(null);
        if (box == null || box.Max.DistanceTo(box.Min) < 0.001) continue;
    } catch (Exception) { continue; }
    try {
        ElementId typeId = e.GetTypeId();
        if (!types.TryGetValue(typeId, out var pair)) {
            ElementType type = document.GetElement(typeId) as ElementType;
            types[typeId] = pair = new string[] {type?.Name, type?.FamilyName};
        }
        name = pair[0]; family = pair[1];
    } catch (Exception ex) {
        if (warnings.Count < 20) warnings.Add(e.Id + " type: " + ex.Message);
    }
    if (name == null) try { name = e.Name; } catch (Exception) { }
    remainingChars -= 96 + (name?.Length ?? 0) + (family?.Length ?? 0) + category.Length + (shown?.Length ?? 0);
    items.Add(new { id = e.Id.ToString(), name, family, category, shown });
}
return new { items, warnings, complete, document_id = document.GetHashCode().ToString() };
"""

_PREVIEW = """
var timer = System.Diagnostics.Stopwatch.StartNew();
int maxTriangles = Convert.ToInt32(parameters[0]), budget = Convert.ToInt32(parameters[1]);
Element e = document.GetElement(id);
if (e == null) return new { found = false };
string name = null, family = null, category = null, shown = null;
try {
    Category cat = e.Category;
    if (cat != null) {
        int value;
        category = int.TryParse(cat.Id.ToString(), out value)
            ? Enum.GetName(typeof(BuiltInCategory), (BuiltInCategory)value) : null;
        shown = cat.Name;
    }
} catch (Exception) { }
try {
    ElementType type = e as ElementType ?? document.GetElement(e.GetTypeId()) as ElementType;
    name = type?.Name; family = type?.FamilyName;
} catch (Exception) { }
if (name == null) try { name = e.Name; } catch (Exception) { }
var points = new List<XYZ>();
var triangles = new List<int>();
bool partial = false;
void Add(Mesh mesh) {
    if (mesh == null || partial) return;
    int count = mesh.NumTriangles;
    if (count == 0) return;
    if (triangles.Count / 3 + count > maxTriangles || timer.ElapsedMilliseconds >= budget) { partial = true; return; }
    int start = points.Count;
    foreach (XYZ p in mesh.Vertices) points.Add(p);
    for (int i = 0; i < count; i++) {
        MeshTriangle t = mesh.get_Triangle(i);
        triangles.Add(start + (int)t.get_Index(0));
        triangles.Add(start + (int)t.get_Index(1));
        triangles.Add(start + (int)t.get_Index(2));
    }
}
void Walk(GeometryElement geometry, int depth) {
    if (geometry == null || depth > 4) return;
    foreach (GeometryObject o in geometry) {
        if (partial) return;
        if (o is Solid solid) {
            foreach (Face face in solid.Faces) {
                if (partial) return;
                try { Add(face.Triangulate(0)); } catch (Exception) { }
            }
        } else if (o is Mesh mesh) Add(mesh);
        else if (o is GeometryInstance instance) {
            try { Walk(instance.GetInstanceGeometry(), depth + 1); } catch (Exception) { }
        }
    }
}
// Coarse first: the fewest triangles. Some families (pipe fittings) have no solid at that
// level, so the fine one is read when the coarse one gave nothing.
foreach (ViewDetailLevel level in new ViewDetailLevel[] { ViewDetailLevel.Coarse, ViewDetailLevel.Fine }) {
    if (triangles.Count > 0 || partial) break;
    try {
        var options = new Options();
        options.DetailLevel = level;
        Walk(e.get_Geometry(options), 0);
    } catch (Exception) { }
}
BoundingBoxXYZ box = null;
try { box = e.get_BoundingBox(null); } catch (Exception) { }
XYZ low = box?.Min, high = box?.Max;
if (points.Count > 0) {
    double x0 = points.Min(p => p.X), y0 = points.Min(p => p.Y), z0 = points.Min(p => p.Z);
    double x1 = points.Max(p => p.X), y1 = points.Max(p => p.Y), z1 = points.Max(p => p.Z);
    if (low == null || partial == false) { low = new XYZ(x0, y0, z0); high = new XYZ(x1, y1, z1); }
    else {
        low = new XYZ(Math.Min(low.X, x0), Math.Min(low.Y, y0), Math.Min(low.Z, z0));
        high = new XYZ(Math.Max(high.X, x1), Math.Max(high.Y, y1), Math.Max(high.Z, z1));
    }
}
if (low == null) return new { found = true, name, family, category, shown, document_id = document.GetHashCode().ToString() };
var vertices = new List<long>();
foreach (XYZ p in points) {
    vertices.Add((long)Math.Round((p.X - low.X) * 304.8));
    vertices.Add((long)Math.Round((p.Y - low.Y) * 304.8));
    vertices.Add((long)Math.Round((p.Z - low.Z) * 304.8));
}
var size = new long[] {
    (long)Math.Round((high.X - low.X) * 304.8), (long)Math.Round((high.Y - low.Y) * 304.8),
    (long)Math.Round((high.Z - low.Z) * 304.8) };
return new { found = true, name, family, category, shown, size, vertices, triangles, partial,
             document_id = document.GetHashCode().ToString() };
"""


async def model_items(executor, offset, limit, terms):
    """Elements of the whole model for the picker: one index read, ranked here, one detail page."""
    limit = min(limit, SELECTION_LIMIT)
    response = await executor.client.send_code(_INDEX, parameters=[INDEX_BUDGET_MS, INDEX_ROWS])
    data = response.result
    if not response.success or not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise RevitQueryError(response.error or "Invalid model element index")
    complete = data.get("complete") is True
    warnings = [str(w) for w in data.get("warnings") or []]
    items = _items(data["items"])
    ranked = await asyncio.to_thread(rank_items, items, terms) if terms else items
    # A whole number may be the element's id: that element comes first, wherever it is.
    numbers = [n for n in (element_id_or_none(t) for t in terms) if n is not None] if len(terms) == 1 else []
    by_id = []
    if numbers and str(numbers[0]) == terms[0]:
        by_id, notes = await items_by_id(executor, numbers)
        warnings.extend(notes)
        found = {item["id"] for item in by_id}
        ranked = [dict(item) for item in by_id] + [item for item in ranked if item["id"] not in found]
    page_ids = [item["id"] for item in ranked[offset:offset + limit]]
    rows = []
    if page_ids:
        details, notes = await _by_ids(executor, page_ids)
        warnings.extend(notes)
        detail_of = {item["id"]: item for item in details}
        rows = [detail_of[number] for number in page_ids if number in detail_of]
    if not complete:
        warnings.append(_INCOMPLETE)
    return {"kind": "element", "source": "model", "items": rows, "offset": offset, "total": len(ranked),
            "truncated": not complete or len(ranked) > offset + limit, "category_matched": [],
            "facets": facets(ranked), **({} if complete else {"facets_partial": True}),
            **({"warnings": list(dict.fromkeys(warnings))} if warnings else {})}


def _whole(values, count=None):
    return (isinstance(values, list) and all(type(v) is int for v in values)
            and (count is None or len(values) == count))


async def element_preview(executor, element_id, max_triangles: int = PREVIEW_TRIANGLES) -> dict:
    """A rough shape of the element: ``{kind, id, document_id, name, family, category,
    category_label, shape, size_mm, vertices, triangles, partial, approximate}``.

    ``vertices`` are whole millimetres ``[x, y, z, ...]`` from the lowest corner, ``triangles``
    index triples into them; ``shape`` is ``mesh``, or ``box`` when only the size is known (both
    lists empty). ``{"error": "invalid_args"}`` for an id that is not one, ``{"error":
    "not_found"}`` when the element is not in the open model or has no place in it. A model that
    could not be read raises RevitQueryError.
    """
    number = element_id_or_none(element_id)
    if number is None:
        return {"error": "invalid_args", "kind": "element", "message": "id must be an element id"}
    try:
        budget = max(1, min(int(max_triangles), PREVIEW_TRIANGLES_MAX))
    except (TypeError, ValueError, OverflowError):
        budget = PREVIEW_TRIANGLES
    # A small integer literal selects the constructor every supported year has.
    code = f"#pragma warning disable 618\nElementId id = new ElementId({number});\n#pragma warning restore 618\n" + _PREVIEW
    response = await executor.client.send_code(code, parameters=[budget, PREVIEW_BUDGET_MS])
    data = response.result
    if not response.success or not isinstance(data, dict):
        raise RevitQueryError(response.error or "Invalid element preview result")
    if data.get("found") is not True or not _whole(data.get("size"), 3):
        return {"error": "not_found", "kind": "element", "id": number}
    size = [max(0, v) for v in data["size"]]
    vertices, triangles = data.get("vertices"), data.get("triangles")
    usable = (_whole(vertices) and _whole(triangles) and len(vertices) % 3 == 0 and len(triangles) % 3 == 0
              and 0 < len(triangles) <= 3 * PREVIEW_TRIANGLES_MAX
              and all(0 <= i < len(vertices) // 3 for i in triangles))
    if not usable:
        vertices, triangles = [], []
    return {"kind": "element", "id": number, "document_id": data.get("document_id"),
            "name": data.get("name"), "family": data.get("family"), "category": data.get("category"),
            "category_label": data.get("shown"), "shape": "mesh" if usable else "box", "size_mm": size,
            "vertices": vertices, "triangles": triangles,
            "partial": bool(usable and data.get("partial") is True), "approximate": True}

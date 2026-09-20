"""Project snapshot: what exists in the model before a request is interpreted.

``take_snapshot(client, categories)`` runs one read-only C# block in Revit
(document, units, active view, levels, grids, selection, links, phases, the
display names of the requested categories) plus one ``get_family_types``
command, and returns a :class:`ProjectSnapshot`. Every part is collected
under its own try/catch, on both sides: a part that fails leaves its field
empty and adds a line to ``warnings``; the snapshot itself is still
returned. Nothing is cached; the host decides how long a snapshot is good
for, and ``fingerprint`` tells whether two snapshots describe the same
model state (title, Revit version, level names and elevations).
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel

from revit_bridge.revit.client import RevitClient
from revit_bridge.snapshot.query import (
    CATEGORY_RE,
    RevitQueryError,
    RevitQueryExecutor,
    _type_name,
    detect_length_unit,
)

DEFAULT_CATEGORIES = [
    "OST_Walls", "OST_StructuralColumns", "OST_StructuralFraming",
    "OST_Floors", "OST_Doors", "OST_Windows",
]
MAX_NAMES = 50          # grid / type names kept per list
MAX_SELECTION = 20      # selected elements kept (the total is selection_count)


# -- models -------------------------------------------------------------------------

class LevelInfo(BaseModel):
    id: int
    name: str
    elevation_mm: float


class GridSummary(BaseModel):
    count: int
    names: list[str]


class TypeSummary(BaseModel):
    category: str
    count: int
    names: list[str]


class SelectedElement(BaseModel):
    id: int
    category: str
    name: str


class LinkInfo(BaseModel):
    name: str
    loaded: bool


class ActiveView(BaseModel):
    name: str
    view_type: str
    level: str | None


class ProjectSnapshot(BaseModel):
    schema_version: Literal[1] = 1
    taken_at: str                       # ISO 8601, UTC
    duration_ms: int
    document: dict                      # {title, revit_version, is_workshared}
    units: dict                         # {"length": "mm"|"m"|"feet", "raw": <UnitTypeId>}
    active_view: ActiveView | None
    levels: list[LevelInfo]
    grids: GridSummary
    family_types: list[TypeSummary]     # only the requested categories
    selection: list[SelectedElement]    # at most MAX_SELECTION; the total is selection_count
    selection_count: int
    links: list[LinkInfo]
    phases: list[str]
    warnings: list[str]                 # partial failures land here
    fingerprint: str                    # sha256(title + revit_version + sorted levels)[:16]


# -- the C# block -----------------------------------------------------------------

# Runs inside the add-in's Execute(Document document, object[] parameters)
# wrapper (usings: System, System.Linq, System.Collections.Generic,
# Autodesk.Revit.DB, Autodesk.Revit.UI). Every part has its own try/catch so
# one failing API leaves the rest intact; failures are returned in Warnings.
# {categories} is replaced by a comma-separated list of quoted OST_* names
# that passed CATEGORY_RE.
SNAPSHOT_CODE = """\
var warnings = new List<string>();
object docInfo = null;
object unitsInfo = null;
object viewInfo = null;
var levels = new List<object>();
object grids = null;
var selection = new List<object>();
int selectionCount = 0;
var links = new List<object>();
var phases = new List<string>();
var categoryNames = new Dictionary<string, string>();

try {
    docInfo = new { Title = document.Title,
                    RevitVersion = document.Application.VersionNumber,
                    IsWorkshared = document.IsWorkshared };
} catch (Exception ex) { warnings.Add("document: " + ex.Message); }

try {
    var formatOptions = document.GetUnits().GetFormatOptions(SpecTypeId.Length);
    var unitTypeId = formatOptions.GetUnitTypeId();
    unitsInfo = new { LengthUnit = unitTypeId.TypeId,
                      DisplayName = LabelUtils.GetLabelForUnit(unitTypeId) };
} catch (Exception ex) { warnings.Add("units: " + ex.Message); }

try {
    var view = document.ActiveView;
    if (view != null) {
        string levelName = null;
        try { levelName = view.GenLevel != null ? view.GenLevel.Name : null; } catch (Exception) { }
        viewInfo = new { Name = view.Name, ViewType = view.ViewType.ToString(), Level = levelName };
    }
} catch (Exception ex) { warnings.Add("active_view: " + ex.Message); }

try {
    levels = new FilteredElementCollector(document).OfClass(typeof(Level)).Cast<Level>()
        .OrderBy(l => l.Elevation)
        .Select(l => (object)new { Id = l.Id.Value, Name = l.Name,
                                   ElevationMm = Math.Round(l.Elevation * 304.8, 1) })
        .ToList();
} catch (Exception ex) { warnings.Add("levels: " + ex.Message); }

try {
    var gridNames = new FilteredElementCollector(document).OfClass(typeof(Grid)).Cast<Grid>()
        .Select(g => g.Name).ToList();
    grids = new { Count = gridNames.Count, Names = gridNames.Take(50).ToList() };
} catch (Exception ex) { warnings.Add("grids: " + ex.Message); }

try {
    var uidoc = new UIDocument(document);
    var ids = uidoc.Selection.GetElementIds().ToList();
    selectionCount = ids.Count;
    foreach (var id in ids.Take(20)) {
        var e = document.GetElement(id);
        if (e == null) continue;
        selection.Add(new { Id = e.Id.Value,
                            Category = e.Category != null ? e.Category.Name : "",
                            Name = e.Name });
    }
} catch (Exception ex) { warnings.Add("selection: " + ex.Message); }

try {
    links = new FilteredElementCollector(document).OfClass(typeof(RevitLinkType)).Cast<RevitLinkType>()
        .Select(t => (object)new { Name = t.Name, Loaded = RevitLinkType.IsLoaded(document, t.Id) })
        .ToList();
} catch (Exception ex) { warnings.Add("links: " + ex.Message); }

try {
    foreach (Phase p in document.Phases) phases.Add(p.Name);
} catch (Exception ex) { warnings.Add("phases: " + ex.Message); }

foreach (var name in new string[] { {categories} }) {
    try {
        var bic = (BuiltInCategory)Enum.Parse(typeof(BuiltInCategory), name);
        var cat = Category.GetCategory(document, bic);
        categoryNames[name] = cat != null ? cat.Name : name;
    } catch (Exception ex) { warnings.Add("category " + name + ": " + ex.Message); }
}

return new { Document = docInfo, Units = unitsInfo, ActiveView = viewInfo, Levels = levels,
             Grids = grids, Selection = selection, SelectionCount = selectionCount,
             Links = links, Phases = phases, CategoryNames = categoryNames, Warnings = warnings };
"""


def snapshot_code(categories: list[str]) -> str:
    """The C# block for these categories (already validated by CATEGORY_RE)."""
    quoted = ", ".join(f'"{c}"' for c in categories)
    return SNAPSHOT_CODE.replace("{categories}", quoted)


def validate_categories(categories: list[str] | None) -> list[str]:
    """The requested categories, or the default set; raises on a bad name."""
    if categories is None:
        return list(DEFAULT_CATEGORIES)
    if not isinstance(categories, list):
        raise ValueError("categories must be a list of OST_* names")
    cleaned: list[str] = []
    for cat in categories:
        if not isinstance(cat, str) or not CATEGORY_RE.fullmatch(cat.strip()):
            raise ValueError(f"invalid category {cat!r}: expected an OST_* name")
        if cat.strip() not in cleaned:
            cleaned.append(cat.strip())
    return cleaned


def fingerprint_of(title: str, revit_version: str, levels: list[LevelInfo]) -> str:
    """sha256 over title, Revit version and the sorted (name, elevation) pairs."""
    parts = [title, revit_version] + [
        f"{lv.name}@{lv.elevation_mm}" for lv in sorted(levels, key=lambda l: (l.name, l.elevation_mm))
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


# -- collection ---------------------------------------------------------------------

async def take_snapshot(client: RevitClient, categories: list[str] | None = None) -> ProjectSnapshot:
    """Collect the snapshot; partial failures become warnings, never exceptions.

    Transport failures (no add-in, connection lost) do propagate: without a
    reply there is nothing to describe.
    """
    started = time.monotonic()
    cats = validate_categories(categories)
    warnings: list[str] = []

    raw: dict = {}
    resp = await client.send_code(snapshot_code(cats))
    if resp.success and isinstance(resp.result, dict):
        raw = resp.result
        warnings.extend(str(w) for w in (raw.get("Warnings") or []))
    else:
        warnings.append(f"snapshot code failed: {resp.error or 'no result'}")

    document = _part(warnings, "document", _document, raw.get("Document"), {"title": "", "revit_version": "", "is_workshared": False})
    units = _part(warnings, "units", _units, raw.get("Units"), {"length": "mm", "raw": ""})
    active_view = _part(warnings, "active_view", _active_view, raw.get("ActiveView"), None)
    levels = _part(warnings, "levels", _levels, raw.get("Levels"), [])
    grids = _part(warnings, "grids", _grids, raw.get("Grids"), GridSummary(count=0, names=[]))
    selection = _part(warnings, "selection", _selection, raw.get("Selection"), [])
    selection_count = _part(warnings, "selection_count", _count, raw.get("SelectionCount"), len(selection))
    links = _part(warnings, "links", _links, raw.get("Links"), [])
    phases = _part(warnings, "phases", _phases, raw.get("Phases"), [])

    category_names = raw.get("CategoryNames") if isinstance(raw.get("CategoryNames"), dict) else {}
    family_types = await _family_types(client, cats, category_names, warnings)

    return ProjectSnapshot(
        taken_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        duration_ms=int((time.monotonic() - started) * 1000),
        document=document,
        units=units,
        active_view=active_view,
        levels=levels,
        grids=grids,
        family_types=family_types,
        selection=selection,
        selection_count=selection_count,
        links=links,
        phases=phases,
        warnings=warnings,
        fingerprint=fingerprint_of(document["title"], document["revit_version"], levels),
    )


def _part(warnings: list[str], field: str, parse, value, empty):
    """Parse one field; on any error keep ``empty`` and record why."""
    if value is None:
        return empty
    try:
        return parse(value)
    except Exception as exc:  # noqa: BLE001 - a bad field must not sink the snapshot
        warnings.append(f"{field}: {type(exc).__name__}: {exc}")
        return empty


def _document(value: dict) -> dict:
    return {
        "title": str(value.get("Title") or ""),
        "revit_version": str(value.get("RevitVersion") or ""),
        "is_workshared": bool(value.get("IsWorkshared", False)),
    }


def _units(value: dict) -> dict:
    unit_id = str(value.get("LengthUnit") or "")
    return {"length": detect_length_unit(unit_id, str(value.get("DisplayName") or "")), "raw": unit_id}


def _active_view(value: dict) -> ActiveView:
    level = value.get("Level")
    return ActiveView(
        name=str(value.get("Name") or ""),
        view_type=str(value.get("ViewType") or ""),
        level=str(level) if level not in (None, "") else None,
    )


def _levels(value: list) -> list[LevelInfo]:
    return [
        LevelInfo(id=int(lv["Id"]), name=str(lv.get("Name") or ""),
                  elevation_mm=float(lv.get("ElevationMm") or 0.0))
        for lv in value
    ]


def _grids(value: dict) -> GridSummary:
    names = [str(n) for n in (value.get("Names") or [])][:MAX_NAMES]
    return GridSummary(count=int(value.get("Count") or 0), names=names)


def _selection(value: list) -> list[SelectedElement]:
    return [
        SelectedElement(id=int(e["Id"]), category=str(e.get("Category") or ""), name=str(e.get("Name") or ""))
        for e in value[:MAX_SELECTION]
    ]


def _count(value: Any) -> int:
    return int(value)


def _links(value: list) -> list[LinkInfo]:
    return [LinkInfo(name=str(l.get("Name") or ""), loaded=bool(l.get("Loaded", False))) for l in value]


def _phases(value: list) -> list[str]:
    return [str(p) for p in value]


async def _family_types(client: RevitClient, categories: list[str],
                        category_names: dict, warnings: list[str]) -> list[TypeSummary]:
    """One ``get_family_types`` call, grouped back onto the requested categories.

    The add-in labels each type with its category's display name (localised),
    so the C# block reports that name per OST_* category. Categories the block
    could not name are queried one by one. A category whose query failed gets
    no summary at all (never ``count: 0``) and a ``family_types`` warning.
    """
    executor = RevitQueryExecutor(client)
    named = [c for c in categories if category_names.get(c)]
    by_display: dict[str, list[str]] | None = None
    if named:
        try:
            by_display = {}
            for item in await executor.get_family_types(named, strict=True):
                display = str(item.get("Category") or "") if isinstance(item, dict) else ""
                by_display.setdefault(display, []).append(_type_name(item))
        except RevitQueryError as exc:
            warnings.append(f"family_types: {exc}")
            by_display = None

    summaries: list[TypeSummary] = []
    for cat in categories:
        display = category_names.get(cat)
        if display:
            if by_display is None:
                continue
            names = by_display.get(str(display), [])
        else:
            try:
                names = [_type_name(t) for t in await executor.get_family_types([cat], strict=True)]
            except RevitQueryError as exc:
                warnings.append(f"family_types {cat}: {exc}")
                continue
        summaries.append(TypeSummary(category=cat, count=len(names), names=names[:MAX_NAMES]))
    return summaries

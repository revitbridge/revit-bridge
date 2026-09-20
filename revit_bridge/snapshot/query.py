"""
Revit model queries — read levels, family types and the current selection.

Only the query half of the former ``interactive`` module lives here; the LLM
intent classifier that used to sit next to it belongs to the host and was
not migrated.
"""
from __future__ import annotations

import logging
import re

from revit_bridge.revit.client import RevitClient

_log = logging.getLogger("revit_bridge.snapshot.query")

# Category values interpolated into C# must match this exact shape (P0-4)
CATEGORY_RE = re.compile(r"OST_[A-Za-z]+")

# ``choices_from`` values a capability pack may declare for a parameter.
CHOICE_SOURCES = ("levels", "family_types:<OST_Category>", "floor_types", "elements:<OST_Category>")


# BuiltInCategory reference — the categories the bridge knows how to query.
# Values are display labels (zh) for hosts that want to show them.
OST_REFERENCE: dict[str, str] = {
    "OST_Walls": "墙", "OST_StructuralColumns": "结构柱", "OST_Columns": "柱",
    "OST_StructuralFraming": "梁/结构框架", "OST_Floors": "楼板",
    "OST_Windows": "窗户", "OST_Doors": "门",
    "OST_Ceilings": "天花板", "OST_Roofs": "屋顶",
    "OST_StairsRailing": "栏杆", "OST_Stairs": "楼梯",
    "OST_Furniture": "家具", "OST_FurnitureSystems": "家具系统",
    "OST_PlumbingFixtures": "卫浴洁具", "OST_LightingFixtures": "灯具",
    "OST_MechanicalEquipment": "机械设备", "OST_ElectricalEquipment": "电气设备",
    "OST_GenericModel": "常规模型",
    "OST_CurtainWallPanels": "幕墙嵌板", "OST_CurtainWallMullions": "幕墙竖梃",
    "OST_Rooms": "房间", "OST_Parking": "停车场",
    "OST_Site": "场地", "OST_Topography": "地形",
    "OST_Casework": "橱柜", "OST_SpecialityEquipment": "专用设备",
    "OST_Entourage": "环境", "OST_Planting": "植物",
}

# Hosted element categories — need a host element (wall, floor, etc.)
HOSTED_CATEGORIES: frozenset[str] = frozenset({"OST_Windows", "OST_Doors"})
ALLOWED_CATEGORIES: frozenset[str] = frozenset(OST_REFERENCE)

# The ``query`` tool: read-only kinds, each with the ``args`` keys it accepts.
MAX_QUERY_LIMIT = 200
DEFAULT_QUERY_LIMIT = 100
QUERY_KINDS: dict[str, tuple[str, ...]] = {
    "levels": (),
    "grids": (),
    "family_types": ("categories",),
    "elements": ("category", "limit"),
    "selection": (),
    "view_elements": ("limit",),
    "units": (),
    "counts": ("categories",),
}


class RevitQueryError(RuntimeError):
    """Revit answered, but with an error (compile failure, no document, timeout)."""


def _clamp_limit(limit) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_QUERY_LIMIT
    return max(1, min(value, MAX_QUERY_LIMIT))


def sanitize_categories(raw_categories) -> list[str]:
    """Keep only known BuiltInCategory names, in order, without duplicates."""
    if not isinstance(raw_categories, list):
        return []
    cleaned: list[str] = []
    for cat in raw_categories:
        if not isinstance(cat, str):
            continue
        value = cat.strip()
        if value in ALLOWED_CATEGORIES and value not in cleaned:
            cleaned.append(value)
    return cleaned


class RevitQueryExecutor:
    """Execute add-in commands / snippets to read model data."""

    def __init__(self, client: RevitClient):
        self.client = client

    async def get_family_types(self, categories: list[str]) -> list[dict]:
        """Query available family types by category via get_available_family_types command."""
        resp = await self.client.send_command(
            "get_available_family_types",
            {"categoryList": categories},
        )
        if resp.success and resp.result:
            return resp.result if isinstance(resp.result, list) else [resp.result]
        return []

    async def get_levels(self) -> list[dict]:
        """Query all levels via send_code_to_revit (no dedicated command for this)."""
        code = (
            'var levels = new FilteredElementCollector(document)\n'
            '    .OfClass(typeof(Level))\n'
            '    .Cast<Level>()\n'
            '    .OrderBy(l => l.Elevation)\n'
            '    .Select(l => new { Id = l.Id.Value, Name = l.Name, '
            'ElevationMm = Math.Round(l.Elevation * 304.8, 1) })\n'
            '    .ToList();\n'
            'return levels;'
        )
        resp = await self.client.send_code(code)
        if resp.success and resp.result:
            return resp.result if isinstance(resp.result, list) else [resp.result]
        return []

    async def trigger_selection(self) -> list[dict]:
        """Trigger Revit interactive pick mode — user clicks an element in Revit.

        Uses UIDocument.Selection.PickObject() which blocks until user picks.
        Falls back to get_selected_elements if PickObject fails.
        """
        # Use PickObject for interactive selection — this shows a Revit prompt
        pick_code = (
            'var uidoc = new UIDocument(document);\n'
            'var reference = uidoc.Selection.PickObject(\n'
            '    Autodesk.Revit.UI.Selection.ObjectType.Element,\n'
            '    "Please select a host element (wall/floor)");\n'
            'var element = document.GetElement(reference);\n'
            'return new {\n'
            '    Id = element.Id.Value,\n'
            '    Name = element.Name,\n'
            '    Category = element.Category != null ? element.Category.Name : ""\n'
            '};\n'
        )
        resp = await self.client.send_code(pick_code)
        if resp.success and resp.result:
            data = resp.result if isinstance(resp.result, list) else [resp.result]
            return data

        # Fallback: return currently selected elements
        _log.warning(f"[trigger_selection] PickObject failed: {resp.error}, trying get_selected_elements")
        resp = await self.client.send_command("get_selected_elements", {})
        if resp.success and resp.result:
            return resp.result if isinstance(resp.result, list) else [resp.result]
        return []

    async def get_selected_elements(self) -> list[dict]:
        """Get currently selected elements without triggering selection mode."""
        resp = await self.client.send_command("get_selected_elements", {})
        if resp.success and resp.result:
            return resp.result if isinstance(resp.result, list) else [resp.result]
        return []

    async def get_project_units(self) -> dict:
        """Read the project's display unit for lengths.

        Returns ``{"revit_unit": <UnitTypeId>, "display_name": <label>,
        "detected": "mm" | "m" | "feet"}`` or ``{"error": <message>}``.
        """
        code = (
            'var units = document.GetUnits();\n'
            'var lengthSpec = Autodesk.Revit.DB.SpecTypeId.Length;\n'
            'var formatOptions = units.GetFormatOptions(lengthSpec);\n'
            'var unitTypeId = formatOptions.GetUnitTypeId();\n'
            'return new {\n'
            '    LengthUnit = unitTypeId.TypeId,\n'
            '    DisplayName = Autodesk.Revit.DB.LabelUtils.GetLabelForUnit(unitTypeId)\n'
            '};\n'
        )
        resp = await self.client.send_code(code)
        if not (resp.success and isinstance(resp.result, dict)):
            return {"error": resp.error or "Failed to query project units"}
        unit_id = str(resp.result.get("LengthUnit", ""))
        display = str(resp.result.get("DisplayName", ""))
        return {
            "revit_unit": unit_id,
            "display_name": display,
            "detected": detect_length_unit(unit_id, display),
        }

    # -- read-only templates behind the ``query`` tool ------------------------------

    async def get_grids(self) -> list[dict]:
        """All grids: ``[{Id, Name}]``."""
        code = (
            'var grids = new FilteredElementCollector(document)\n'
            '    .OfClass(typeof(Grid)).Cast<Grid>()\n'
            '    .Select(g => new { Id = g.Id.Value, Name = g.Name }).ToList();\n'
            'return grids;'
        )
        return await self._code_list(code)

    async def get_elements(self, category: str, limit: int = MAX_QUERY_LIMIT) -> dict:
        """Instances of one category: ``{Total, Items: [{Id, Name, Category, Type, Level}]}``.

        Raises ``ValueError`` for a category that does not look like ``OST_*``
        (the name is interpolated into C#).
        """
        if not CATEGORY_RE.fullmatch(category):
            raise ValueError(f"invalid category {category!r}")
        limit = _clamp_limit(limit)
        code = (
            f'var bic = (BuiltInCategory)Enum.Parse(typeof(BuiltInCategory), "{category}");\n'
            f'var total = new FilteredElementCollector(document).OfCategory(bic)\n'
            f'    .WhereElementIsNotElementType().GetElementCount();\n'
            f'var items = new FilteredElementCollector(document).OfCategory(bic)\n'
            f'    .WhereElementIsNotElementType().Take({limit})\n'
            f'    .Select(e => {{\n'
            f'        var t = document.GetElement(e.GetTypeId());\n'
            f'        var lv = document.GetElement(e.LevelId);\n'
            f'        return new {{ Id = e.Id.Value, Name = e.Name,\n'
            f'                     Category = e.Category != null ? e.Category.Name : "",\n'
            f'                     Type = t != null ? t.Name : "",\n'
            f'                     Level = lv != null ? lv.Name : null }};\n'
            f'    }}).ToList();\n'
            f'return new {{ Total = total, Items = items }};'
        )
        return await self._code_dict(code)

    async def get_view_elements(self, limit: int = MAX_QUERY_LIMIT) -> dict:
        """Elements visible in the active view: ``{View, ViewType, Total, Items}``."""
        limit = _clamp_limit(limit)
        code = (
            f'var view = document.ActiveView;\n'
            f'if (view == null) return new {{ View = (string)null, ViewType = (string)null,\n'
            f'                                Total = 0, Items = new List<object>() }};\n'
            f'var total = new FilteredElementCollector(document, view.Id)\n'
            f'    .WhereElementIsNotElementType().GetElementCount();\n'
            f'var items = new FilteredElementCollector(document, view.Id)\n'
            f'    .WhereElementIsNotElementType().Take({limit})\n'
            f'    .Select(e => (object)new {{ Id = e.Id.Value, Name = e.Name,\n'
            f'        Category = e.Category != null ? e.Category.Name : "" }}).ToList();\n'
            f'return new {{ View = view.Name, ViewType = view.ViewType.ToString(),\n'
            f'             Total = total, Items = items }};'
        )
        return await self._code_dict(code)

    async def get_counts(self, categories: list[str]) -> list[dict]:
        """Instance counts per category: ``[{Category, Count}]`` (``Count`` -1 + ``Error`` on failure)."""
        for cat in categories:
            if not CATEGORY_RE.fullmatch(cat):
                raise ValueError(f"invalid category {cat!r}")
        quoted = ", ".join(f'"{c}"' for c in categories)
        code = (
            f'var result = new List<object>();\n'
            f'foreach (var name in new string[] {{ {quoted} }}) {{\n'
            f'    try {{\n'
            f'        var bic = (BuiltInCategory)Enum.Parse(typeof(BuiltInCategory), name);\n'
            f'        var n = new FilteredElementCollector(document).OfCategory(bic)\n'
            f'            .WhereElementIsNotElementType().GetElementCount();\n'
            f'        result.Add(new {{ Category = name, Count = n }});\n'
            f'    }} catch (Exception ex) {{\n'
            f'        result.Add(new {{ Category = name, Count = -1, Error = ex.Message }});\n'
            f'    }}\n'
            f'}}\n'
            f'return result;'
        )
        return await self._code_list(code)

    async def _code_list(self, code: str) -> list[dict]:
        resp = await self.client.send_code(code)
        if not resp.success:
            raise RevitQueryError(resp.error or "Revit returned no result")
        if resp.result is None:
            return []
        return resp.result if isinstance(resp.result, list) else [resp.result]

    async def _code_dict(self, code: str) -> dict:
        resp = await self.client.send_code(code)
        if not resp.success or not isinstance(resp.result, dict):
            raise RevitQueryError(resp.error or "Revit returned no result")
        return resp.result

    async def get_tool_choices(self, dynamic_params: list[dict]) -> dict[str, list[dict]]:
        """Resolve ``choices_from`` sources of a capability pack against Revit.

        ``dynamic_params`` is what ``ToolStore.get_dynamic_params`` returns:
        ``[{"name": ..., "choices_from": ...}, ...]``. The result maps each
        parameter name to ``[{"label": ..., "value": ...}, ...]``; unknown or
        malformed sources yield an empty list rather than an exception.
        """
        choices: dict[str, list[dict]] = {}
        for param in dynamic_params:
            source = str(param.get("choices_from", ""))
            items: list[dict] = []

            if source == "levels":
                items = [
                    {"label": f"{lv.get('Name', '?')} ({lv.get('ElevationMm', 0)}mm)",
                     "value": lv.get("Name", "")}
                    for lv in await self.get_levels()
                ]
            elif source.startswith("family_types:"):
                category = source.split(":", 1)[1]
                items = [
                    {"label": _type_name(t), "value": _type_name(t)}
                    for t in await self.get_family_types([category])
                ]
            elif source == "floor_types":
                code = (
                    'var types = new FilteredElementCollector(document)\n'
                    '    .OfClass(typeof(FloorType)).Cast<FloorType>()\n'
                    '    .Select(ft => new { Name = ft.Name, Id = ft.Id.Value }).ToList();\n'
                    'return types;'
                )
                resp = await self.client.send_code(code)
                if resp.success and resp.result:
                    data = resp.result if isinstance(resp.result, list) else [resp.result]
                    items = [{"label": _type_name(ft), "value": _type_name(ft)} for ft in data]
            elif source.startswith("elements:"):
                category = source.split(":", 1)[1]
                if not CATEGORY_RE.fullmatch(category):
                    _log.warning(f"[get_tool_choices] invalid category in {source!r}")
                    choices[param["name"]] = []
                    continue
                code = (
                    f'var elems = new FilteredElementCollector(document)\n'
                    f'    .OfCategory(BuiltInCategory.{category})\n'
                    f'    .WhereElementIsNotElementType()\n'
                    f'    .Select(e => new {{ Id = e.Id.Value, Name = e.Name }}).ToList();\n'
                    f'return elems;'
                )
                resp = await self.client.send_code(code)
                if resp.success and resp.result:
                    data = resp.result if isinstance(resp.result, list) else [resp.result]
                    items = [
                        {"label": f"{el.get('Name', '?')} (ID: {el.get('Id', '?')})",
                         "value": el.get("Id", "")}
                        for el in data
                    ]
            else:
                _log.warning(f"[get_tool_choices] unknown choices_from {source!r}")

            choices[param["name"]] = items
        return choices


def _type_name(item) -> str:
    """Type name from a Revit reply - the add-in is not consistent about the key."""
    if isinstance(item, dict):
        return str(
            item.get("TypeName") or item.get("typeName")
            or item.get("name") or item.get("Name") or item
        )
    return str(item)


def detect_length_unit(unit_id: str, display_name: str = "") -> str:
    """Map a Revit ``UnitTypeId`` / label to ``"mm"``, ``"m"`` or ``"feet"``."""
    uid = unit_id.lower()
    label = display_name.lower()
    if "millimeters" in uid or "millimeters" in label:
        return "mm"
    if "meters" in uid and "milli" not in uid:
        return "m"
    if "feet" in uid or "foot" in uid:
        return "feet"
    return "mm"


# -- the ``query`` tool -------------------------------------------------------------

async def run_query(executor: RevitQueryExecutor, kind: str, args: dict | None = None) -> dict:
    """Answer one read-only ``query(kind, args)`` call.

    ``args`` keys are whitelisted per kind (see ``QUERY_KINDS``); categories
    must match ``CATEGORY_RE``; ``limit`` is clamped to 1..MAX_QUERY_LIMIT.
    Errors come back as ``{"error": code, ...}`` rather than exceptions:
    ``unknown_kind`` (with the list of kinds), ``invalid_args`` (with a
    message) and ``revit_error`` (with Revit's message). Transport failures
    (no add-in) propagate to the caller.
    """
    if kind not in QUERY_KINDS:
        return {"error": "unknown_kind", "kind": kind, "kinds": sorted(QUERY_KINDS)}
    args = args or {}
    if not isinstance(args, dict):
        return {"error": "invalid_args", "message": "args must be an object"}
    unknown = sorted(set(args) - set(QUERY_KINDS[kind]))
    if unknown:
        return {"error": "invalid_args", "kind": kind, "message": f"unexpected args {unknown}",
                "allowed": list(QUERY_KINDS[kind])}
    try:
        return await _QUERY_HANDLERS[kind](executor, args)
    except ValueError as exc:
        return {"error": "invalid_args", "kind": kind, "message": str(exc)}
    except RevitQueryError as exc:
        return {"error": "revit_error", "kind": kind, "message": str(exc)}


def _categories_arg(args: dict) -> list[str]:
    raw = args.get("categories")
    if not isinstance(raw, list) or not raw:
        raise ValueError("categories must be a non-empty list of OST_* names")
    cleaned: list[str] = []
    for cat in raw:
        if not isinstance(cat, str) or not CATEGORY_RE.fullmatch(cat.strip()):
            raise ValueError(f"invalid category {cat!r}: expected an OST_* name")
        if cat.strip() not in cleaned:
            cleaned.append(cat.strip())
    return cleaned


def _category_arg(args: dict) -> str:
    cat = args.get("category")
    if not isinstance(cat, str) or not CATEGORY_RE.fullmatch(cat.strip()):
        raise ValueError(f"invalid category {cat!r}: expected an OST_* name")
    return cat.strip()


async def _q_levels(executor: RevitQueryExecutor, args: dict) -> dict:
    items = [
        {"id": lv.get("Id"), "name": lv.get("Name", ""), "elevation_mm": lv.get("ElevationMm", 0.0)}
        for lv in await executor.get_levels()
    ]
    return {"kind": "levels", "items": items}


async def _q_grids(executor: RevitQueryExecutor, args: dict) -> dict:
    items = [{"id": g.get("Id"), "name": g.get("Name", "")} for g in await executor.get_grids()]
    return {"kind": "grids", "count": len(items), "items": items}


async def _q_family_types(executor: RevitQueryExecutor, args: dict) -> dict:
    categories = _categories_arg(args)
    items = [
        {"id": t.get("FamilyTypeId"), "family": t.get("FamilyName", ""),
         "name": _type_name(t), "category": t.get("Category", "")}
        for t in await executor.get_family_types(categories)
        if isinstance(t, dict)
    ]
    return {"kind": "family_types", "categories": categories, "items": items}


async def _q_elements(executor: RevitQueryExecutor, args: dict) -> dict:
    category = _category_arg(args)
    limit = _clamp_limit(args.get("limit", DEFAULT_QUERY_LIMIT))
    data = await executor.get_elements(category, limit)
    items = [
        {"id": e.get("Id"), "name": e.get("Name", ""), "category": e.get("Category", ""),
         "type": e.get("Type", ""), "level": e.get("Level")}
        for e in (data.get("Items") or [])
    ]
    return {"kind": "elements", "category": category, "total": int(data.get("Total") or 0),
            "limit": limit, "items": items}


async def _q_selection(executor: RevitQueryExecutor, args: dict) -> dict:
    items = [
        {"id": e.get("Id"), "name": e.get("Name", ""), "category": e.get("Category") or ""}
        for e in await executor.get_selected_elements()
        if isinstance(e, dict)
    ]
    return {"kind": "selection", "count": len(items), "items": items}


async def _q_view_elements(executor: RevitQueryExecutor, args: dict) -> dict:
    limit = _clamp_limit(args.get("limit", DEFAULT_QUERY_LIMIT))
    data = await executor.get_view_elements(limit)
    items = [
        {"id": e.get("Id"), "name": e.get("Name", ""), "category": e.get("Category", "")}
        for e in (data.get("Items") or [])
    ]
    return {"kind": "view_elements", "view": data.get("View"), "view_type": data.get("ViewType"),
            "total": int(data.get("Total") or 0), "limit": limit, "items": items}


async def _q_units(executor: RevitQueryExecutor, args: dict) -> dict:
    units = await executor.get_project_units()
    if "error" in units:
        raise RevitQueryError(str(units["error"]))
    return {"kind": "units", "length": units["detected"], "raw": units["revit_unit"],
            "display_name": units["display_name"]}


async def _q_counts(executor: RevitQueryExecutor, args: dict) -> dict:
    categories = _categories_arg(args)
    items = []
    for row in await executor.get_counts(categories):
        item = {"category": row.get("Category", ""), "count": int(row.get("Count", -1))}
        if row.get("Error"):
            item["error"] = str(row["Error"])
        items.append(item)
    return {"kind": "counts", "items": items}


_QUERY_HANDLERS = {
    "levels": _q_levels,
    "grids": _q_grids,
    "family_types": _q_family_types,
    "elements": _q_elements,
    "selection": _q_selection,
    "view_elements": _q_view_elements,
    "units": _q_units,
    "counts": _q_counts,
}

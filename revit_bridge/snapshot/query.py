"""
Revit model queries — read levels, family types and the current selection.

Only the query half of the former ``interactive`` module lives here; the LLM
intent classifier that used to sit next to it belongs to the host and was
not migrated.
"""
from __future__ import annotations

import logging

from revit_bridge.revit.client import RevitClient

_log = logging.getLogger("revit_bridge.snapshot.query")


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

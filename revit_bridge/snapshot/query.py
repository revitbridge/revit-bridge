"""
Revit model queries — read levels, family types and the current selection.

Only the query half of the former ``interactive`` module lives here; the LLM
intent classifier that used to sit next to it belongs to the host and was
not migrated.

``list_model_items`` (add-in method, ``{kind, category?, link_id?, ids?, q?,
limit?}`` with ``kind`` levels | links | types | type_categories) is used where
it is needed and the add-in's ``get_bridge_capabilities`` lists it: the
``links`` and ``type_categories`` kinds, every ``link_id`` read, checks by id
(``*_by_id``) and the type picker (``get_type_rows``). No compile, no
transaction. Its reply is always a successful JSON-RPC result ``{Success,
Kind, Items, Total, Truncated, Warnings, Error}``; ``Success`` false carries
``Error: {Code, Message}``, mapped here onto the query's own error replies.
``levels`` and ``family_types`` without ``link_id`` keep their 0.8.2 path on
every add-in (``send_code`` and one ``get_available_family_types`` call), so
their requests and replies do not change. Without the method, ``links`` and
``type_categories`` fall back to a read-only ``send_code`` block and a
``link_id`` is refused with ``unsupported_addin``; a check by id falls back
only when the add-in definitely lacks the method (an unreadable capability
raises). ``elements`` and ``counts`` of a link run a ``send_code`` block
against the link document (the method does not serve them). The bridge asks
for all rows (no ``q``, no ``limit``) and filters itself; rows asked by
``ids`` are filtered by id here too. Transport failures propagate on every
path.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from revit_bridge.revit.capabilities import (
    METHOD_NOT_FOUND, UNKNOWN, Capabilities, MethodUnavailable, discover, forget, send_listed_command,
)
from revit_bridge.revit.client import RevitClient

_log = logging.getLogger("revit_bridge.snapshot.query")
_OMITTED: Any = object()

# Category values interpolated into C# must match this exact shape (P0-4), always with
# ``fullmatch`` (``^...$`` with ``match`` would let a trailing newline through). Letters,
# digits and underscores after ``OST_``: the BuiltInCategory names the add-in accepts.
CATEGORY_RE = re.compile(r"OST_[A-Za-z0-9_]+")

# ``choices_from`` values a capability pack may declare for a parameter.
CHOICE_SOURCES = ("levels", "family_types:<OST_Category>", "floor_types", "elements", "elements:<OST_Category>")

# Unqualified element picks refer to the designer's selection, including for
# comma-separated id parameters. Read at most one small page, without a pick prompt.
_SELECTED_CHOICES = """
var rows = new List<object>();
foreach (ElementId id in new UIDocument(document).Selection.GetElementIds().Take(50)) {
    string name = null;
    try { name = document.GetElement(id)?.Name; } catch (Exception) { }
    rows.Add(new { Id = id.ToString(), Name = name });
}
return rows;
"""


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
    "levels": ("link_id",),
    "grids": (),
    "family_types": ("categories", "link_id"),
    "elements": ("category", "limit", "link_id"),
    "selection": (),
    "view_elements": ("limit",),
    "units": (),
    "counts": ("categories", "link_id"),
    "links": (),
    "type_categories": ("link_id",),
}

# The add-in method behind levels / links / types / type_categories (protocol section 2.6).
LIST_MODEL_ITEMS_METHOD = "list_model_items"
MODEL_ITEM_KINDS = ("levels", "links", "types", "type_categories")
MAX_IDS = 200                       # list_model_items takes at most this many ids per call
# list_model_items error codes, by the query error they become (anything else: revit_error).
_ARG_CODES = frozenset({"unknown_kind", "bad_category", "bad_params"})
_LINK_CODES = frozenset({"unknown_link", "link_not_loaded"})
# What a reply read from a linked model says about its coordinates.
LINK_NOTE = ("read from the linked model: elevations and coordinates are in the link's own coordinate "
             "system; link.origin_mm is where the link's origin sits in the host model, and link.rotated "
             "is true when its placement is more than a translation (rotated or mirrored). Element, "
             "level and type ids read through link_id belong to the linked document: they name no "
             "element of the open model and must not be passed to tools that act on the open model")
_MAX_ELEMENT_ID = 2 ** 63 - 1       # an ElementId is an Int64 (Revit 2024 and later)


# The levels read (``get_levels``, ``query("levels")``, the execution's document
# fingerprint): rows {Id, Name, ElevationMm}, the shape of the snapshot block.
LEVELS_CODE = (
    'var levels = new FilteredElementCollector(document)\n'
    '    .OfClass(typeof(Level))\n'
    '    .Cast<Level>()\n'
    '    .OrderBy(l => l.Elevation)\n'
    '    .Select(l => new { Id = l.Id.Value, Name = l.Name, '
    'ElevationMm = Math.Round(l.Elevation * 304.8, 1) })\n'
    '    .ToList();\n'
    'return levels;'
)

# The fallback for ``links`` (an add-in without list_model_items): the rows
# list_model_items gives (one per RevitLinkInstance, read as the add-in reads
# them: loaded from the link type, DocumentTitle null when not loaded, Origin
# in mm to 0.1, Rotated when the transform is more than a translation), each
# under its own try/catch, in the same envelope.
LINKS_CODE = """\
var warnings = new List<string>();
var items = new List<object>();
foreach (var inst in new FilteredElementCollector(document).OfClass(typeof(RevitLinkInstance))
                         .Cast<RevitLinkInstance>()) {
    try {
        var typeId = inst.GetTypeId();
        var linkType = document.GetElement(typeId);
        bool loaded = RevitLinkType.IsLoaded(document, typeId);
        var linkDoc = loaded ? inst.GetLinkDocument() : null;
        var t = inst.GetTotalTransform();
        items.Add(new { Id = inst.Id.Value, Name = inst.Name, TypeId = typeId.Value,
                        TypeName = linkType != null ? linkType.Name : null,
                        Loaded = loaded,
                        DocumentTitle = linkDoc != null ? linkDoc.Title : null,
                        Origin = new { X = Math.Round(t.Origin.X * 304.8, 1),
                                       Y = Math.Round(t.Origin.Y * 304.8, 1),
                                       Z = Math.Round(t.Origin.Z * 304.8, 1) },
                        Rotated = !t.IsTranslation });
    } catch (Exception ex) { warnings.Add("link " + inst.Id.Value + " skipped: " + ex.Message); }
}
return new { Success = true, Items = items, Warnings = warnings };"""

# The fallback for ``type_categories``: the categories of exactly the types the
# old ``get_available_family_types`` lists (family symbols and five system type
# classes), so every category offered here can be listed by ``family_types``;
# ordered by display name as list_model_items orders them.
#
# The category id is turned into the enum with Enum.ToObject and that enum value
# is what Enum.IsDefined tests (as the add-in's TryGetBuiltInCategoryName does):
# BuiltInCategory is a 32-bit enum before Revit 2024 and a 64-bit one from 2024,
# and IsDefined throws when given a boxed number of the other width (a boxed int
# against the 64-bit enum failed every type on Revit 2026). ``cat.Id.Value``
# keeps the block on Revit 2024 and later, like every other template here.
TYPE_CATEGORIES_CODE = """\
var warnings = new List<string>();
var counts = new Dictionary<string, int>();
var names = new Dictionary<string, string>();
var types = new FilteredElementCollector(document).OfClass(typeof(FamilySymbol)).Cast<ElementType>().ToList();
foreach (var cls in new System.Type[] { typeof(WallType), typeof(FloorType), typeof(RoofType),
                                        typeof(CeilingType), typeof(CurtainSystemType) })
    types.AddRange(new FilteredElementCollector(document).OfClass(cls).Cast<ElementType>());
foreach (var t in types) {
    try {
        var cat = t.Category;
        if (cat == null) continue;
        long raw = cat.Id.Value;
        BuiltInCategory bic = BuiltInCategory.INVALID;
        if (raw >= int.MinValue && raw <= int.MaxValue)
            bic = (BuiltInCategory)Enum.ToObject(typeof(BuiltInCategory), raw);
        if (bic == BuiltInCategory.INVALID || !Enum.IsDefined(typeof(BuiltInCategory), bic)) {
            if (warnings.Count < 100) warnings.Add("category " + raw + " (" + cat.Name + ") is not a built-in category");
            continue;
        }
        string key = bic.ToString();
        int n;
        counts.TryGetValue(key, out n);
        counts[key] = n + 1;
        names[key] = cat.Name;
    } catch (Exception ex) { if (warnings.Count < 100) warnings.Add("type " + t.Id.Value + " skipped: " + ex.Message); }
}
var items = counts.OrderBy(kv => names[kv.Key], StringComparer.OrdinalIgnoreCase)
    .ThenBy(kv => kv.Key, StringComparer.Ordinal)
    .Select(kv => (object)new { Category = kv.Key, DisplayName = names[kv.Key], Count = kv.Value }).ToList();
return new { Success = true, Items = items, Warnings = warnings };"""

# A read (``get_elements``, ``get_counts``) against a linked document: the same
# block with ``document`` renamed, after this prelude finds the link instance.
_LINK_PRELUDE = (
    'var linkInstance = new FilteredElementCollector(document)\n'
    '    .OfClass(typeof(RevitLinkInstance)).Cast<RevitLinkInstance>()\n'
    '    .FirstOrDefault(l => l.Id.Value == {link_id});\n'
    'var linkDoc = linkInstance != null ? linkInstance.GetLinkDocument() : null;\n'
    'if (linkDoc == null) return new {{ LinkError = linkInstance == null ? "unknown_link" : "link_not_loaded" }};\n'
)


def in_link(code: str, link_id: int) -> str:
    """``code`` run against the linked document of RevitLinkInstance ``link_id`` (an element id)."""
    return _LINK_PRELUDE.format(link_id=_checked_id(link_id)) + re.sub(r"\bdocument\b", "linkDoc", code)


def element_id_or_none(value) -> int | None:
    """``value`` as an element id: a whole number with 0 < id <= 2**63-1 (an int, a float with no
    fractional part, or a string of digits); None for anything else - bools, 1.9 (never cut to 1),
    1e30, "9999999999999999999"."""
    number = int_or_none(value)
    return number if number is not None and 0 < number <= _MAX_ELEMENT_ID else None


def _checked_id(value) -> int:
    """An id about to go into a request or into C#: ValueError unless it is an element id."""
    number = element_id_or_none(value)
    if number is None:
        raise ValueError(f"invalid element id {value!r}: expected a whole number in 1..{_MAX_ELEMENT_ID}")
    return number


class RevitQueryError(RuntimeError):
    """Revit answered, but with an error (compile failure, no document, timeout)."""


class ModelItemsMethodMissing(RevitQueryError):
    """``list_model_items`` was advertised but answered "method not found" (the capability is forgotten)."""


class ModelItemsError(RevitQueryError):
    """``list_model_items`` answered ``Success: false``; ``code`` is its ``Error.Code``."""

    def __init__(self, code: str, message: str, warnings: list[str] | None = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.warnings = list(warnings or [])

    def reply(self, kind: str, link_id: int | None = None) -> dict:
        """The query error this becomes: ``unknown_link`` / ``link_not_loaded`` as they are, an
        argument code as ``invalid_args``, anything else (no_document, busy, timeout, failed) as
        ``revit_error``; the add-in's code and message are kept in ``message``."""
        if self.code in _LINK_CODES:
            error = self.code
        elif self.code in _ARG_CODES:
            error = "invalid_args"
        else:
            error = "revit_error"
        out = {"error": error, "kind": kind, "message": str(self)}
        if link_id is not None and self.code in _LINK_CODES:
            out["link_id"] = link_id
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


@dataclass
class ModelItems:
    """One ``list_model_items`` reply (or its fallback): object rows, what the add-in said about them."""

    rows: list[dict]
    total: int | None = None
    truncated: bool | None = None
    warnings: list[str] = field(default_factory=list)


def only_ids(data: ModelItems, ids, key: str) -> ModelItems:
    """``data`` keeping the rows whose ``key`` is one of ``ids``; whatever the add-in did with ``ids``."""
    wanted = set(ids)
    return ModelItems(rows=[r for r in data.rows if int_or_none(r.get(key)) in wanted],
                      warnings=list(data.warnings))


def model_items_offered(caps: Capabilities) -> bool:
    """The add-in lists ``list_model_items`` among its methods."""
    return caps.supported and caps.has_method(LIST_MODEL_ITEMS_METHOD)


async def capabilities_of(client) -> Capabilities:
    """``discover(client)``, except that a transport failure (connection refused, reset or closed)
    is raised as ConnectionError instead of coming back as an ``unknown`` capability: every query
    path lets transport failures propagate, as ``run_query`` says."""
    caps = await discover(client)
    if caps.state == UNKNOWN and caps.error_kind == "unreachable":
        raise ConnectionError(caps.reason or "the Revit add-in is unreachable")
    return caps


def _first(mapping: dict, *keys):
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def int_or_none(value) -> int | None:
    """An element id or a count as an int; None for anything else (never 0 for "no value")."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,19}", value.strip()):
        return int(value.strip())
    return None


def _float_or_none(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _str_or_none(value) -> str | None:
    return None if value is None else str(value)


def parse_model_items(result) -> ModelItems:
    """A ``list_model_items`` result ``{Success, Kind, Items, Total, Truncated, Warnings, Error}`` (also
    what the LINKS_CODE / TYPE_CATEGORIES_CODE blocks return; a JSON string of it is read too).

    ``Success`` false raises :class:`ModelItemsError` with ``Error.Code``; anything that is not
    this envelope raises RevitQueryError. A row that is not an object is dropped with a warning;
    the add-in's ``Warnings`` are kept.
    """
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            raise RevitQueryError(f"unexpected reply to {LIST_MODEL_ITEMS_METHOD}: {result[:200]}") from None
    if not isinstance(result, dict) or not isinstance(result.get("Success"), bool):
        raise RevitQueryError(f"unexpected reply to {LIST_MODEL_ITEMS_METHOD}: {str(result)[:200]}")
    listed = result.get("Warnings")
    warnings = [str(w) for w in listed if w] if isinstance(listed, list) else []
    if not result["Success"]:
        error = result.get("Error") if isinstance(result.get("Error"), dict) else {}
        raise ModelItemsError(str(error.get("Code") or "failed"), str(error.get("Message") or ""), warnings)
    raw = result.get("Items")
    if not isinstance(raw, list):
        raise RevitQueryError(f"unexpected reply to {LIST_MODEL_ITEMS_METHOD}: Items is not a list")
    flag = result.get("Truncated")
    rows = []
    for i, row in enumerate(raw):
        if isinstance(row, dict):
            rows.append(row)
        else:
            warnings.append(f"row {i}: not an object ({type(row).__name__})")
    return ModelItems(rows=rows, total=int_or_none(result.get("Total")),
                      truncated=flag if isinstance(flag, bool) else None, warnings=warnings)


def _clamp_limit(limit) -> int:
    """``limit`` as an int in 1..MAX_QUERY_LIMIT; nonsense falls back to the default."""
    try:
        value = int(limit)
    except OverflowError:                       # 1e400 parses as inf; -inf likewise
        return MAX_QUERY_LIMIT if limit > 0 else 1
    except (TypeError, ValueError):             # None, "lots", nan
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

    # The three 0.1 queries return [] when Revit fails (web callers rely on
    # that); ``strict=True`` raises RevitQueryError instead, so a failure can
    # never read as "the model has none".

    async def _family_type_response(self, categories):
        return await self.client.send_command(
            "get_available_family_types",
            {"categoryList": categories},
        )

    async def get_family_types(self, categories: list[str], strict: bool = False) -> list[dict]:
        """Query available family types by category via get_available_family_types command."""
        return _list_result(await self._family_type_response(categories), strict)

    async def get_levels(self, strict: bool = False) -> list[dict]:
        """Query all levels via send_code_to_revit (no dedicated command for this)."""
        resp = await self.client.send_code(LEVELS_CODE)
        return _list_result(resp, strict)

    async def trigger_selection(self, mode: str = _OMITTED, category: str | None = _OMITTED,
                                prompt: str = _OMITTED, timeout_s: int = _OMITTED) -> list[dict] | dict:
        """With no arguments return the 0.10.1 element list. An explicit option requests mentions.

        Defaults are single pick, no category, "Select in Revit" and 120 seconds.
        A native pick can acknowledge cancellation or timeout. Older add-ins use
        the original single-pick/current-selection path and its original rows.
        """
        from revit_bridge.mention_elements import picked_items

        legacy = all(value is _OMITTED for value in (mode, category, prompt, timeout_s))
        mode = "single" if mode is _OMITTED else mode
        category = None if category is _OMITTED else category
        prompt = "Select in Revit" if prompt is _OMITTED else prompt
        timeout_s = 120 if timeout_s is _OMITTED else timeout_s
        if mode not in ("single", "multiple", "box"):
            raise ValueError("mode must be single, multiple or box")
        if category is not None and (not isinstance(category, str) or not CATEGORY_RE.fullmatch(category)):
            raise ValueError("category must be a BuiltInCategory identifier such as OST_Walls")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty text")
        if type(timeout_s) is not int or timeout_s <= 0:
            raise ValueError("timeout_s must be a positive whole number of seconds")
        params = {"mode": mode, "prompt": prompt, "timeout_s": timeout_s}
        if category is not None:
            params["category"] = category
        try:
            data = await send_listed_command(self.client, "trigger_selection_in_revit", params, timeout=timeout_s + 10)
        except MethodUnavailable as exc:
            if mode == "single" and category is None:
                return await self._legacy_pick()
            raise RevitQueryError(str(exc)) from exc
        except RuntimeError as exc:
            raise RevitQueryError(str(exc)) from exc
        if isinstance(data, dict):
            for flag in ("cancelled", "timed_out"):
                if set(data) == {flag} and data[flag] is True:
                    return [] if legacy else {flag: True}
        return picked_items(data, mode, category, legacy=legacy)

    async def _legacy_pick(self) -> list[dict]:
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
            return _list_result(resp, strict=False)
        _log.warning("[trigger_selection] PickObject failed: %s, trying get_selected_elements", resp.error)
        return await self.get_selected_elements()

    async def get_selected_elements(self, strict: bool = False) -> list[dict]:
        """Get currently selected elements without triggering selection mode."""
        resp = await self.client.send_command("get_selected_elements", {})
        return _list_result(resp, strict)

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

    async def get_elements(self, category: str, limit: int = MAX_QUERY_LIMIT, link_id: int | None = None) -> dict:
        """Instances of one category: ``{Total, Items: [{Id, Name, Category, Type, Level}]}``.

        Raises ``ValueError`` for a category that does not look like ``OST_*``
        (the name is interpolated into C#). With ``link_id`` the same read runs
        on that link's document; ``{LinkError}`` when it has none.
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
        if link_id is not None:
            code = in_link(code, link_id)
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

    async def get_counts(self, categories: list[str], link_id: int | None = None) -> list[dict]:
        """Instance counts per category: ``[{Category, Count}]`` (``Count`` -1 + ``Error`` on failure).
        With ``link_id`` the counts are read in that link's document; ``[{LinkError}]`` when it has none."""
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
        if link_id is not None:
            code = in_link(code, link_id)
        return await self._code_list(code)

    # -- list_model_items, with the fallbacks ------------------------------------------

    async def offers_list_model_items(self) -> bool:
        """The add-in's capability reply lists ``list_model_items`` (cached per connection); a
        transport failure raises."""
        return model_items_offered(await capabilities_of(self.client))

    async def list_model_items(self, kind: str, *, category: str | None = None, link_id: int | None = None,
                               ids: list[int] | None = None, q: str | None = None,
                               limit: int | None = None) -> ModelItems:
        """Call the add-in's ``list_model_items``; RevitQueryError on any failure.

        ``kind`` is one of MODEL_ITEM_KINDS; ``category`` must match CATEGORY_RE;
        ``ids`` (at most MAX_IDS element ids) asks for those rows only. A reply
        with ``Success: false`` raises :class:`ModelItemsError`. A "method not
        found" answer forgets the cached capability and raises
        :class:`ModelItemsMethodMissing`, so the caller can fall back.
        """
        if kind not in MODEL_ITEM_KINDS:
            raise ValueError(f"invalid list_model_items kind {kind!r}")
        params: dict = {"kind": kind}
        if category is not None:
            if not CATEGORY_RE.fullmatch(category):
                raise ValueError(f"invalid category {category!r}")
            params["category"] = category
        if link_id is not None:
            params["link_id"] = _checked_id(link_id)
        if ids is not None:
            id_list = [_checked_id(i) for i in ids]        # the add-in refuses anything outside Int64
            if len(id_list) > MAX_IDS:
                raise ValueError(f"at most {MAX_IDS} ids per list_model_items call, got {len(id_list)}")
            params["ids"] = id_list
        if q is not None:
            params["q"] = str(q)
        if limit is not None:
            params["limit"] = _clamp_limit(limit)
        resp = await self.client.send_command(LIST_MODEL_ITEMS_METHOD, params)
        if not resp.success:
            message = resp.error or "Revit returned no result"
            if resp.error_code == METHOD_NOT_FOUND:
                forget(self.client)              # the capability reply promised a method that is not there
                raise ModelItemsMethodMissing(f"method_not_found: {message}")
            raise RevitQueryError(message)
        return parse_model_items(resp.result)

    async def _offered(self, kind: str, *, definite: bool = False, **params) -> ModelItems | None:
        """``list_model_items`` when the add-in offers it; None for the caller's fallback.

        ``definite``: the fallback may only stand in when the add-in definitely does not offer
        the method; a capability that could not be read (timeout, error) raises RevitQueryError
        instead. Checking a pick by id needs this: the fallback lists fewer types than the
        method, so a pick it does not see would read as "not found". A transport failure
        raises either way.
        """
        caps = await capabilities_of(self.client)
        if definite and caps.state == UNKNOWN:
            raise RevitQueryError(f"the add-in's capabilities could not be read "
                                  f"({caps.error_kind or 'unknown'}: {caps.reason}); {kind} not checked")
        if not model_items_offered(caps):
            return None
        try:
            return await self.list_model_items(kind, **params)
        except ModelItemsMethodMissing:
            return None

    async def get_link_rows(self, ids: list[int] | None = None) -> ModelItems:
        """RevitLinkInstance rows (list_model_items ``links``, else the LINKS_CODE block); with
        ``ids`` only those, and an unreadable capability raises (see ``_offered``)."""
        data = await self._offered("links", definite=ids is not None, ids=ids)
        if data is None:
            data = parse_model_items(await self._code_dict(LINKS_CODE))
        return data if ids is None else only_ids(data, ids, "Id")

    async def get_type_category_rows(self) -> ModelItems:
        """Categories that have types, with counts (list_model_items ``type_categories``, else
        TYPE_CATEGORIES_CODE: the categories ``get_available_family_types`` can list)."""
        data = await self._offered("type_categories")
        if data is None:
            data = parse_model_items(await self._code_dict(TYPE_CATEGORIES_CODE))
            if data.truncated is None:
                data.truncated = False  # the fixed legacy query returns its entire category array
        return data

    async def get_type_rows(self, category: str) -> ModelItems:
        """The types of one category, for a picker: list_model_items ``types`` (rows with
        ``BuiltInCategory``, and the add-in's warnings), else ``get_available_family_types``."""
        if not CATEGORY_RE.fullmatch(category):
            raise ValueError(f"invalid category {category!r}: expected an OST_* name")
        data = await self._offered("types", category=category)
        if data is None:
            response = await self._family_type_response([category])
            if not response.success:
                raise RevitQueryError(response.error or "Revit returned no result")
            data = parse_model_items({"Success": True, "Items": response.result, "Truncated": False})
        return data

    async def get_levels_by_id(self, ids: list[int]) -> ModelItems:
        """The level rows with these ids: list_model_items ``levels`` with ``ids``, else ``get_levels``;
        an unreadable capability raises."""
        data = await self._offered("levels", definite=True, ids=list(ids))
        if data is None:
            data = ModelItems(rows=[r for r in await self.get_levels(strict=True) if isinstance(r, dict)])
        return only_ids(data, ids, "Id")

    async def get_types_by_id(self, ids: list[int]) -> ModelItems:
        """The type rows with these ids, any category: list_model_items ``types`` with ``ids``,
        else (only on an add-in that definitely lacks the method) what ``get_available_family_types``
        lists without a category; an unreadable capability raises."""
        data = await self._offered("types", definite=True, ids=list(ids))
        if data is None:
            data = ModelItems(rows=[t for t in await self.get_family_types([], strict=True) if isinstance(t, dict)])
        return only_ids(data, ids, "FamilyTypeId")

    async def _code_list(self, code: str) -> list[dict]:
        return _list_result(await self.client.send_code(code), strict=True)

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
            elif source == "elements":
                response = await self.client.send_code(_SELECTED_CHOICES)
                items = _element_choices(response, as_text=param.get("type") == "string")
            elif source.startswith("elements:"):
                category = source.split(":", 1)[1]
                if not CATEGORY_RE.fullmatch(category):
                    _log.warning(f"[get_tool_choices] invalid category in {source!r}")
                    choices[param["name"]] = []
                    continue
                # A supplemental picker suggests a page; the designer can still
                # supply any valid id. Existing tool-sourced category lists keep their scope.
                page = ".Take(50)" if param.get("source") in ("designer", "default") else ""
                code = (
                    'var rows = new List<object>();\n'
                    'using (var collector = new FilteredElementCollector(document)) {\n'
                    f'    foreach (var e in collector.OfCategory(BuiltInCategory.{category})\n'
                    f'             .WhereElementIsNotElementType(){page}) {{\n'
                    '        string name = null;\n'
                    '        try { name = e.Name; } catch (Exception) { }\n'
                    '        rows.Add(new { Id = e.Id.ToString(), Name = name });\n'
                    '    }\n'
                    '}\n'
                    'return rows;'
                )
                items = _element_choices(await self.client.send_code(code))
            else:
                _log.warning(f"[get_tool_choices] unknown choices_from {source!r}")

            choices[param["name"]] = items
        return choices


def _element_choices(response, *, as_text: bool = False) -> list[dict]:
    items = []
    for row in _list_result(response, strict=False):
        number = element_id_or_none(row.get("Id")) if isinstance(row, dict) else None
        if number is not None:
            items.append({"label": f"{row.get('Name') or '?'} (ID: {number})",
                          "value": str(number) if as_text else number})
    return items


def _list_result(resp, strict: bool) -> list[dict]:
    """A list reply; ``[]`` on failure, or RevitQueryError when ``strict``."""
    if not resp.success:
        if strict:
            raise RevitQueryError(resp.error or "Revit returned no result")
        return []
    if not resp.result:
        return []
    return resp.result if isinstance(resp.result, list) else [resp.result]


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

    ``args.link_id`` (levels, family_types, elements, counts, type_categories)
    reads the linked model of that RevitLinkInstance; the reply adds ``link``
    (id, name, document_title, origin_mm, rotated), ``coordinates: "link"`` and
    ``note``. It needs an add-in that offers ``list_model_items``: otherwise
    ``unsupported_addin``. ``unknown_link`` / ``link_not_loaded`` when the id is
    not a link instance of the open model or the link is not loaded.
    ``list_model_items`` failures keep the add-in's code in ``message``:
    argument codes become ``invalid_args``, the rest ``revit_error``. The
    add-in's ``Warnings`` reach the reply's ``warnings``.
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
    except ModelItemsError as exc:
        return exc.reply(kind, int_or_none(args.get("link_id")))
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


def _link_id_arg(args: dict) -> int | None:
    """``args.link_id`` as a positive int, None when absent; ValueError for anything else."""
    raw = args.get("link_id")
    if raw is None:
        return None
    value = element_id_or_none(raw)
    if value is None:
        raise ValueError(f"invalid link_id {raw!r}: expected the id of a link instance (query kind links)")
    return value


# -- links: rows, the summary a linked read carries, the refusals ------------------------------

def _origin_mm(value) -> dict | None:
    """``{X, Y, Z}`` (any case) or ``[x, y, z]`` in mm as ``{"x", "y", "z"}``; None when not all three are numbers."""
    if isinstance(value, dict):
        parts = [_float_or_none(_first(value, key, key.lower())) for key in ("X", "Y", "Z")]
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        parts = [_float_or_none(v) for v in value]
    else:
        return None
    if any(p is None for p in parts):
        return None
    return {"x": parts[0], "y": parts[1], "z": parts[2]}


def link_items(data: ModelItems) -> tuple[list[dict], list[str]]:
    """The ``links`` rows as items ``{id, name, type_id, type_name, loaded, document_title, origin_mm,
    rotated}``, and the warnings. A row without an id is dropped with a warning; a value that is
    not there is None (``loaded`` included: unknown is not "not loaded")."""
    items: list[dict] = []
    warnings = list(data.warnings)
    for i, row in enumerate(data.rows):
        link_id = int_or_none(row.get("Id"))
        if link_id is None:
            warnings.append(f"link row {i}: no id")
            continue
        loaded, rotated = row.get("Loaded"), row.get("Rotated")
        items.append({
            "id": link_id,
            "name": str(row.get("Name") or ""),
            "type_id": int_or_none(row.get("TypeId")),
            "type_name": _str_or_none(row.get("TypeName")),
            "loaded": loaded if isinstance(loaded, bool) else None,
            "document_title": _str_or_none(row.get("DocumentTitle")),
            "origin_mm": _origin_mm(_first(row, "Origin", "OriginMm")),
            "rotated": rotated if isinstance(rotated, bool) else None,
        })
    return items, warnings


def link_summary(link: dict) -> dict:
    """What a reply read from a linked model says about the link: id, name, document, transform."""
    return {key: link.get(key) for key in ("id", "name", "document_title", "origin_mm", "rotated")}


def unsupported_addin(kind: str) -> dict:
    """The refusal of a ``link_id`` read on an add-in without ``list_model_items``."""
    return {"error": "unsupported_addin", "kind": kind, "method": LIST_MODEL_ITEMS_METHOD,
            "message": f"the Revit add-in does not offer {LIST_MODEL_ITEMS_METHOD}: reading a linked model "
                       f"(link_id) needs a newer add-in"}


def _link_error(kind: str, code: str, link_id: int, link: dict | None = None,
                warnings: list[str] | None = None) -> dict:
    """``unknown_link`` or ``link_not_loaded`` (the add-in's codes), with the link when it is known."""
    if code == "link_not_loaded":
        name = (link or {}).get("name") or link_id
        reply = {"error": code, "kind": kind, "link_id": link_id,
                 "message": f"link {name!r} is not loaded in Revit: nothing can be read from it"}
        if link is not None:
            reply["link"] = link_summary(link)
    else:
        reply = {"error": "unknown_link", "kind": kind, "link_id": link_id,
                 "message": f"{link_id} is not a link instance of the open model (query kind links lists them)"}
    if warnings:
        reply["warnings"] = list(warnings)
    return reply


async def _link_or_refusal(executor: RevitQueryExecutor, kind: str,
                           link_id: int) -> tuple[dict | None, list[str], dict | None]:
    """``(link summary, warnings, None)`` for a loaded link of the open model, else
    ``(None, [], refusal)``. The link is read with list_model_items ``links`` and ``ids``. A
    transport failure raises (``capabilities_of``); any other unreadable capability is a
    ``revit_error``, never ``unsupported_addin``."""
    caps = await capabilities_of(executor.client)
    if caps.state == UNKNOWN:
        return None, [], {"error": "revit_error", "kind": kind,
                          "message": f"the add-in's capabilities could not be read "
                                     f"({caps.error_kind or 'unknown'}: {caps.reason})"}
    if not model_items_offered(caps):
        return None, [], unsupported_addin(kind)
    try:
        data = only_ids(await executor.list_model_items("links", ids=[link_id]), [link_id], "Id")
    except ModelItemsMethodMissing:
        return None, [], unsupported_addin(kind)
    items, warnings = link_items(data)
    if not items:
        return None, [], _link_error(kind, "unknown_link", link_id, warnings=warnings)
    # readable only when loaded AND its document is there (a closed workset leaves DocumentTitle null)
    if items[0]["loaded"] is not True or items[0]["document_title"] is None:
        return None, [], _link_error(kind, "link_not_loaded", link_id, items[0], warnings)
    return link_summary(items[0]), warnings, None


async def _linked_items(executor: RevitQueryExecutor, kind: str, categories: list[str | None],
                        link_id: int) -> ModelItems | None:
    """``list_model_items`` with ``link_id`` for each category, merged; None when the method
    turned out to be missing. Only for linked reads: the caller has checked the capability, and
    the reads without ``link_id`` keep their 0.8.2 path."""
    merged = ModelItems(rows=[])
    try:
        for category in categories:
            data = await executor.list_model_items(kind, category=category, link_id=link_id)
            merged.rows.extend(data.rows)
            merged.warnings.extend(data.warnings)
            if data.truncated:
                merged.truncated = True
    except ModelItemsMethodMissing:
        return None
    return merged


def _finish(reply: dict, data: ModelItems, link: dict | None, link_warnings: list[str] | None = None) -> dict:
    """``truncated`` / ``warnings`` only when the add-in reported them; the link fields for a linked read."""
    if data.truncated:
        reply["truncated"] = True
    warnings = list(link_warnings or []) + list(data.warnings)
    if warnings:
        reply["warnings"] = warnings
    if link is not None:
        reply.update(link=link, coordinates="link", note=LINK_NOTE)
    return reply


async def _q_levels(executor: RevitQueryExecutor, args: dict) -> dict:
    link_id = _link_id_arg(args)
    if link_id is None:
        # the 0.8.2 path on every add-in: the same request, the same reply
        items = [
            {"id": lv.get("Id"), "name": lv.get("Name", ""), "elevation_mm": lv.get("ElevationMm", 0.0)}
            for lv in await executor.get_levels(strict=True)
        ]
        return {"kind": "levels", "items": items}
    link, link_warnings, refusal = await _link_or_refusal(executor, "levels", link_id)
    if refusal is not None:
        return refusal
    data = await _linked_items(executor, "levels", [None], link_id)
    if data is None:
        return unsupported_addin("levels")
    items = [{"id": lv.get("Id"), "name": lv.get("Name", ""), "elevation_mm": lv.get("ElevationMm")}
             for lv in data.rows]
    return _finish({"kind": "levels", "items": items}, data, link, link_warnings)


async def _q_links(executor: RevitQueryExecutor, args: dict) -> dict:
    data = await executor.get_link_rows()
    items, warnings = link_items(data)
    reply = {"kind": "links", "count": len(items), "items": items, "warnings": warnings}
    if data.truncated:
        reply["truncated"] = True
    return reply


def type_category_items(data: ModelItems) -> tuple[list[dict], list[str]]:
    """``type_categories`` rows as ``{category, display_name, count}``; a category that is not an
    OST_* name (it could not be queried) is dropped with a warning; a missing count is None."""
    items: list[dict] = []
    warnings = list(data.warnings)
    for i, row in enumerate(data.rows):
        category = row.get("Category")
        if not isinstance(category, str) or not CATEGORY_RE.fullmatch(category.strip()):
            warnings.append(f"type category row {i}: {category!r} is not an OST_* name")
            continue
        display = row.get("DisplayName")
        items.append({"category": category.strip(), "display_name": _str_or_none(display),
                      "count": int_or_none(row.get("Count"))})
    return items, warnings


async def _q_type_categories(executor: RevitQueryExecutor, args: dict) -> dict:
    link_id = _link_id_arg(args)
    link, link_warnings = None, []
    if link_id is None:
        data = await executor.get_type_category_rows()
    else:
        link, link_warnings, refusal = await _link_or_refusal(executor, "type_categories", link_id)
        if refusal is not None:
            return refusal
        data = await _linked_items(executor, "type_categories", [None], link_id)
        if data is None:
            return unsupported_addin("type_categories")
    items, warnings = type_category_items(data)
    reply = {"kind": "type_categories", "count": len(items), "items": items, "warnings": link_warnings + warnings}
    if data.truncated:
        reply["truncated"] = True
    if link is not None:
        reply.update(link=link, coordinates="link", note=LINK_NOTE)
    return reply


async def _q_grids(executor: RevitQueryExecutor, args: dict) -> dict:
    items = [{"id": g.get("Id"), "name": g.get("Name", "")} for g in await executor.get_grids()]
    return {"kind": "grids", "count": len(items), "items": items}


def _type_item(t: dict) -> dict:
    return {"id": t.get("FamilyTypeId"), "family": t.get("FamilyName", ""),
            "name": _type_name(t), "category": t.get("Category", "")}


async def _q_family_types(executor: RevitQueryExecutor, args: dict) -> dict:
    categories = _categories_arg(args)
    link_id = _link_id_arg(args)
    if link_id is None:
        # the 0.8.2 path on every add-in: one get_available_family_types call for all categories,
        # its order kept (one list_model_items call per category would reorder a multi-category reply)
        items = [_type_item(t) for t in await executor.get_family_types(categories, strict=True)
                 if isinstance(t, dict)]
        return {"kind": "family_types", "categories": categories, "items": items}
    link, link_warnings, refusal = await _link_or_refusal(executor, "family_types", link_id)
    if refusal is not None:
        return refusal
    data = await _linked_items(executor, "types", list(categories), link_id)
    if data is None:
        return unsupported_addin("family_types")
    reply = {"kind": "family_types", "categories": categories, "items": [_type_item(t) for t in data.rows]}
    return _finish(reply, data, link, link_warnings)


async def _q_elements(executor: RevitQueryExecutor, args: dict) -> dict:
    category = _category_arg(args)
    limit = _clamp_limit(args.get("limit", DEFAULT_QUERY_LIMIT))
    link_id = _link_id_arg(args)
    link, link_warnings = None, []
    if link_id is None:
        data = await executor.get_elements(category, limit)
    else:
        # list_model_items does not serve elements: a send_code block against the link document
        link, link_warnings, refusal = await _link_or_refusal(executor, "elements", link_id)
        if refusal is not None:
            return refusal
        data = await executor.get_elements(category, limit, link_id=link_id)
        if data.get("LinkError"):
            return _link_error("elements", str(data["LinkError"]), link_id)
    items = [
        {"id": e.get("Id"), "name": e.get("Name", ""), "category": e.get("Category", ""),
         "type": e.get("Type", ""), "level": e.get("Level")}
        for e in (data.get("Items") or [])
    ]
    reply = {"kind": "elements", "category": category, "total": int(data.get("Total") or 0),
             "limit": limit, "items": items}
    return _finish(reply, ModelItems(rows=[]), link, link_warnings)


async def _q_selection(executor: RevitQueryExecutor, args: dict) -> dict:
    items = [
        {"id": e.get("Id"), "name": e.get("Name", ""), "category": e.get("Category") or ""}
        for e in await executor.get_selected_elements(strict=True)
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
    link_id = _link_id_arg(args)
    link, link_warnings = None, []
    if link_id is None:
        rows = await executor.get_counts(categories)
    else:
        # list_model_items does not serve counts: a send_code block against the link document
        link, link_warnings, refusal = await _link_or_refusal(executor, "counts", link_id)
        if refusal is not None:
            return refusal
        rows = await executor.get_counts(categories, link_id=link_id)
        if len(rows) == 1 and isinstance(rows[0], dict) and rows[0].get("LinkError"):
            return _link_error("counts", str(rows[0]["LinkError"]), link_id)
    items = []
    for row in rows:
        item = {"category": row.get("Category", ""), "count": int(row.get("Count", -1))}
        if row.get("Error"):
            item["error"] = str(row["Error"])
        items.append(item)
    return _finish({"kind": "counts", "items": items}, ModelItems(rows=[]), link, link_warnings)


_QUERY_HANDLERS = {
    "levels": _q_levels,
    "grids": _q_grids,
    "family_types": _q_family_types,
    "elements": _q_elements,
    "selection": _q_selection,
    "view_elements": _q_view_elements,
    "units": _q_units,
    "counts": _q_counts,
    "links": _q_links,
    "type_categories": _q_type_categories,
}

"""Mentions: what a designer picks in the host (``@level``, ``@type``, ``@link``).

Three functions for a host that lets the designer pick model items while
writing a message (section 2.4 of the hostv2 plan):

``mention_candidates(executor, kind, category=None, q=None, limit=50)``
    The items to offer, ``{kind, items, total, truncated}``; each item is
    ``{kind, id, name, label, detail, category?, family?}``, and an item that
    cannot be picked (a link that is not loaded) adds ``disabled: true`` and
    ``reason``. ``kind`` is ``level``, ``type`` or ``link``; ``type`` without
    ``category`` offers the categories first (items of kind ``category``
    whose ``id`` is the OST_* name). ``q`` keeps the items whose name, label,
    family or category contains it (case-insensitive); ``limit`` (1..200)
    cuts the list, ``total`` counts every match and ``truncated`` says
    whether some were cut; ``warnings`` (only when there are any) are the
    add-in's lines about rows it could not read. Bad input and Revit failures
    come back as ``{"error": code, "kind": kind, ...}`` like ``run_query``; a
    transport failure (no add-in) raises.
``resolve_mentions(executor, mentions)``
    Checks each ``{kind, id}`` by id against the open model now and returns
    ``{"resolved": [item, ...], "not_found": [{kind, id, reason}, ...],
    "warnings": [...]}``;
    ``reason`` is ``not_found``, ``not_loaded`` / ``document_not_available`` /
    ``load_state_unknown`` (a link: one is usable only when it is loaded and
    its document is there) or ``invalid``. A model that could not be read raises
    RevitQueryError: that is never reported as "not found".
``mention_block(resolved, language)``
    The fixed text the host appends to the designer's message, e.g.
    ``Designer's selections (picked in the host, verified against the model):
    level "F2" (id 312, 3000 mm).``

Every read goes through ``run_query`` / the query executor, so the add-in's
``list_model_items`` is used when it offers it and the old paths otherwise;
both give the same candidates for the same model, and a type offered on one
path is resolved on the same path.
"""
from __future__ import annotations

import json
import re
import unicodedata

from revit_bridge.snapshot.query import (
    CATEGORY_RE,
    MAX_QUERY_LIMIT,
    ModelItemsError,
    RevitQueryError,
    RevitQueryExecutor,
    _type_name,
    element_id_or_none,
    int_or_none,
    link_items,
    run_query,
)

MENTION_KINDS = ("level", "type", "link")
MAX_MENTIONS = 20                   # the host's ChatRequest takes at most this many
DEFAULT_MENTION_LIMIT = 50
# A name in the text block is at most this many characters AFTER escaping, quotes not counted
# (so a name full of escapes cannot grow past it); the block is bounded by MAX_MENTIONS items.
_MAX_NAME = 120

__all__ = [
    "DEFAULT_MENTION_LIMIT",
    "MAX_MENTIONS",
    "MENTION_KINDS",
    "mention_block",
    "mention_candidates",
    "resolve_mentions",
]


# -- items ------------------------------------------------------------------------------------

def _mm(value) -> str:
    """An elevation for a label: ``3000 mm``, ``-3500.5 mm``; "" when there is none (never "0 mm")."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return f"{value:g} mm"


def _level_item(row: dict) -> dict | None:
    """A ``levels`` query item as a candidate; one without an id cannot be picked."""
    level_id = int_or_none(row.get("id"))
    if level_id is None:
        return None
    name = str(row.get("name") or "")
    return {"kind": "level", "id": level_id, "name": name, "label": name, "detail": _mm(row.get("elevation_mm"))}


def _type_item(type_id, family, name, shown, category: str | None) -> dict | None:
    """A type as a candidate: ``label`` is ``family: type``, ``detail`` the category as Revit shows it,
    ``category`` the OST_* name when it is known (the category asked for, or the row's
    ``BuiltInCategory``; never guessed from the display name)."""
    type_id = int_or_none(type_id)
    if type_id is None:
        return None
    family, shown = str(family or ""), str(shown or "")
    if category is not None and not CATEGORY_RE.fullmatch(category):
        category = None
    item = {"kind": "type", "id": type_id, "name": str(name or ""),
            "label": f"{family}: {name}" if family else str(name or ""), "detail": shown}
    if category is not None:
        item["category"] = category
    if family:
        item["family"] = family
    return item


def _link_reason(link: dict) -> str | None:
    """Why a link cannot be picked: it is readable only when loaded AND its document is there
    (a loaded type whose document is not available, e.g. a closed workset, answers link_not_loaded)."""
    loaded = link.get("loaded")
    if loaded is False:
        return "not_loaded"
    if loaded is not True:
        return "load_state_unknown"
    if link.get("document_title") is None:
        return "document_not_available"
    return None


def _link_item(link: dict) -> dict:
    """A ``links`` query item as a candidate; one that is not loaded is listed but cannot be picked."""
    item = {"kind": "link", "id": link["id"], "name": link["name"],
            "label": link.get("type_name") or link["name"], "detail": link.get("document_title") or ""}
    reason = _link_reason(link)
    if reason is not None:
        item.update(disabled=True, reason=reason, detail=reason.replace("_", " "))
    return item


def _category_item(row: dict) -> dict:
    count = row.get("count")
    return {"kind": "category", "id": row["category"], "name": row["category"],
            "label": row.get("display_name") or row["category"],
            "detail": f"{count} types" if isinstance(count, int) else ""}


def _matches(item: dict, needle: str) -> bool:
    fields = (item.get("name"), item.get("label"), item.get("family"), item.get("category"))
    return any(needle in str(f).casefold() for f in fields if f)


def _limit(limit) -> int:
    if limit is None:
        return DEFAULT_MENTION_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_MENTION_LIMIT
    return max(1, min(value, MAX_QUERY_LIMIT))


# -- candidates -------------------------------------------------------------------------------

async def mention_candidates(executor: RevitQueryExecutor, kind: str, category: str | None = None,
                             q: str | None = None, limit: int | None = DEFAULT_MENTION_LIMIT) -> dict:
    """What the designer can pick for ``@<kind>``: ``{kind, items, total, truncated}`` (see the module)."""
    if kind not in MENTION_KINDS:
        return {"error": "unknown_kind", "kind": kind, "kinds": list(MENTION_KINDS)}
    if q is not None and not isinstance(q, str):
        return {"error": "invalid_args", "kind": kind, "message": "q must be a string"}
    if category is not None and (kind != "type" or not isinstance(category, str)
                                 or not CATEGORY_RE.fullmatch(category.strip())):
        return {"error": "invalid_args", "kind": kind,
                "message": f"invalid category {category!r}: only type takes one, an OST_* name"}
    limit = _limit(limit)

    if category is not None:
        category = category.strip()
        reply = await _type_candidates(executor, category)
    else:
        reply = await run_query(executor, {"level": "levels", "link": "links", "type": "type_categories"}[kind])
    if "error" in reply:
        return dict(reply, kind=kind)
    if kind == "level":
        items = [_level_item(row) for row in reply["items"]]
    elif kind == "link":
        items = [_link_item(link) for link in reply["items"]]
    elif category is None:
        items = [_category_item(row) for row in reply["items"]]
    else:
        items = [_type_item(t.get("FamilyTypeId"), t.get("FamilyName"), _type_name(t), t.get("Category"), category)
                 for t in reply["items"]]
    items = [item for item in items if item is not None]
    needle = (q or "").strip().casefold()
    if needle:
        items = [item for item in items if _matches(item, needle)]
    out = {"kind": kind, "items": items[:limit], "total": len(items),
           "truncated": len(items) > limit or reply.get("truncated") is True}
    if reply.get("warnings"):
        out["warnings"] = list(reply["warnings"])      # rows the add-in could not read: said, not dropped
    return out


async def _type_candidates(executor: RevitQueryExecutor, category: str) -> dict:
    """The types of one category as raw rows: list_model_items when the add-in offers it (every
    type of the category, with warnings), else ``get_available_family_types``; errors as replies."""
    try:
        data = await executor.get_type_rows(category)
    except ValueError as exc:
        return {"error": "invalid_args", "kind": "type", "message": str(exc)}
    except ModelItemsError as exc:
        return exc.reply("type")
    except RevitQueryError as exc:
        return {"error": "revit_error", "kind": "type", "message": str(exc)}
    reply = {"items": data.rows, "warnings": list(data.warnings)}
    if data.truncated:
        reply["truncated"] = True
    return reply


# -- resolution -------------------------------------------------------------------------------

def _normalise(mention) -> tuple[str | None, int | None]:
    """``(kind, id)``; the id None when it is not an element id (a whole number in 1..2**63-1:
    no bool, no 1.9, no 1e30): such a mention is ``invalid`` and never reaches the add-in."""
    if not isinstance(mention, dict):
        return None, None
    kind = mention.get("kind")
    mention_id = element_id_or_none(mention.get("id"))
    if kind not in MENTION_KINDS or mention_id is None:
        return (kind if isinstance(kind, str) else None), None
    return kind, mention_id


async def resolve_mentions(executor: RevitQueryExecutor, mentions: list) -> dict:
    """Check each ``{kind, id}`` against the open model:
    ``{"resolved": [...], "not_found": [...], "warnings": [...]}``.

    One read per kind mentioned, by id: list_model_items with ``ids`` when the
    add-in offers it, else - only when the add-in definitely lacks the method -
    the old reads (levels and links through send_code, types through
    ``get_available_family_types``). An add-in whose capability could not be
    read raises RevitQueryError rather than reporting a pick "not found"; a
    transport failure raises as it is. A resolved item is the candidate item
    plus what the text block needs: ``elevation_mm`` (level);
    ``document_title``, ``origin_mm``, ``rotated`` (link). An id that is not an
    element id is ``invalid`` and the other mentions are still checked.
    Duplicates are checked once. ``warnings`` are the add-in's lines about rows
    it could not read. More than MAX_MENTIONS raises ValueError.
    """
    if not isinstance(mentions, list):
        raise ValueError("mentions must be a list of {kind, id}")
    if len(mentions) > MAX_MENTIONS:
        raise ValueError(f"at most {MAX_MENTIONS} mentions, got {len(mentions)}")

    wanted: list[tuple[str, int]] = []
    not_found: list[dict] = []
    for mention in mentions:
        kind, mention_id = _normalise(mention)
        if mention_id is None:
            raw_id = mention.get("id") if isinstance(mention, dict) else None
            not_found.append({"kind": kind, "id": raw_id, "reason": "invalid"})
        elif (kind, mention_id) not in wanted:
            wanted.append((kind, mention_id))
    ids = {kind: [i for k, i in wanted if k == kind] for kind in MENTION_KINDS}
    warnings: list[str] = []

    levels: dict[int, dict] = {}
    if ids["level"]:
        data = await executor.get_levels_by_id(ids["level"])
        warnings += data.warnings
        for row in data.rows:
            item = _level_item({"id": row.get("Id"), "name": row.get("Name"), "elevation_mm": row.get("ElevationMm")})
            if item is not None:
                levels[item["id"]] = dict(item, elevation_mm=row.get("ElevationMm"))
    links: dict[int, dict] = {}
    if ids["link"]:
        items, link_warnings = link_items(await executor.get_link_rows(ids["link"]))
        warnings += link_warnings
        links = {link["id"]: link for link in items}
    types: dict[int, dict] = {}
    if ids["type"]:
        data = await executor.get_types_by_id(ids["type"])
        warnings += data.warnings
        for row in data.rows:
            item = _type_item(row.get("FamilyTypeId"), row.get("FamilyName"), _type_name(row),
                              row.get("Category"), row.get("BuiltInCategory"))
            if item is not None:
                types.setdefault(item["id"], item)

    resolved: list[dict] = []
    for kind, mention_id in wanted:
        if kind == "level":
            found = levels.get(mention_id)
        elif kind == "type":
            found = types.get(mention_id)
        else:
            link = links.get(mention_id)
            if link is not None and _link_reason(link) is not None:
                not_found.append({"kind": kind, "id": mention_id, "reason": _link_reason(link)})
                continue
            found = None if link is None else dict(
                _link_item(link), document_title=link.get("document_title"), origin_mm=link.get("origin_mm"),
                rotated=link.get("rotated"))
        if found is None:
            not_found.append({"kind": kind, "id": mention_id, "reason": "not_found"})
        else:
            resolved.append(found)
    return {"resolved": resolved, "not_found": not_found, "warnings": warnings}


# -- the text block ----------------------------------------------------------------------------

_BLOCK = {
    "en": {
        "head": "Designer's selections (picked in the host, verified against the model): ",
        "level": "level {name} (id {id}{elevation})",
        "type": "type {name} ({family}id {id}{category})",
        "link": "link {name} (id {id}; queries about its content carry link_id {id})",
        "family": "family {family}, ",
        "category": ", category {category}",
        "comma": ", ",
        "sep": "; ",
        "end": ".",
    },
    "zh": {
        "head": "设计师的选择（在宿主中选取，已与模型核对）：",
        "level": "标高 {name}（id {id}{elevation}）",
        "type": "类型 {name}（{family}id {id}{category}）",
        "link": "链接 {name}（id {id}；查询其内容时带 link_id {id}）",
        "family": "族 {family}，",
        "category": "，类别 {category}",
        "comma": "，",
        "sep": "；",
        "end": "。",
    },
}


def _escaped(ch: str) -> str:
    """One character as it goes inside a JSON string literal: quote and backslash escaped, and every
    control, format, separator, surrogate, private-use or unassigned character as ``\\uXXXX``."""
    if ch in '"\\':
        return "\\" + ch
    if unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in ("Zl", "Zp"):
        return json.dumps(ch)[1:-1]                  # ensure_ascii: \\uXXXX (a surrogate pair above BMP)
    return ch


def _quote(text) -> str:
    """A name as a JSON string literal that cannot end the block or hide text: runs of whitespace
    (line and paragraph separators included) become one space, then every character that is not
    plain text is escaped; at most _MAX_NAME characters between the quotes, counted after
    escaping, a cut name ending in ``...``."""
    text = re.sub(r"\s+", " ", str(text if text is not None else "")).strip()
    pieces = [_escaped(ch) for ch in text]
    if sum(len(p) for p in pieces) > _MAX_NAME:
        kept, size = [], 0
        for piece in pieces:
            if size + len(piece) > _MAX_NAME - 3:
                break
            kept.append(piece)
            size += len(piece)
        pieces = kept + ["..."]
    return '"' + "".join(pieces) + '"'


def _shown_id(value) -> str:
    """An id for the block: the number, or the value quoted when it is not an element id."""
    number = element_id_or_none(value)
    return str(number) if number is not None else _quote(value)


def mention_block(resolved: list[dict], language: str = "zh") -> str:
    """The fixed text for the designer's verified selections; "" when there are none.

    ``language`` starting with ``zh`` gives the Chinese text, anything else
    the English one. Names are quoted as JSON strings; each item carries its
    id, and a link the ``link_id`` its queries take.
    """
    table = _BLOCK["zh" if str(language or "").lower().startswith("zh") else "en"]
    parts = []
    for item in resolved or []:
        kind = item.get("kind")
        if kind == "level":
            mm = _mm(item.get("elevation_mm"))
            parts.append(table["level"].format(name=_quote(item.get("name")), id=_shown_id(item.get("id")),
                                               elevation=table["comma"] + mm if mm else ""))
        elif kind == "type":
            family = item.get("family")
            category = item.get("category") or item.get("detail")
            parts.append(table["type"].format(
                name=_quote(item.get("name")), id=_shown_id(item.get("id")),
                family=table["family"].format(family=_quote(family)) if family else "",
                category=table["category"].format(category=_quote(category)) if category else ""))
        elif kind == "link":
            parts.append(table["link"].format(name=_quote(item.get("label") or item.get("name")),
                                              id=_shown_id(item.get("id"))))
    if not parts:
        return ""
    return table["head"] + table["sep"].join(parts) + table["end"]

"""Mentions: what a designer picks in the host (levels, types, links and selected elements).

Three functions for a host that lets the designer pick model items while
writing a message (section 2.4 of the hostv2 plan):

``mention_candidates(executor, kind, category=None, q=None, limit=50, offset=0, source=None)``
    The items to offer, ``{kind, items, total, truncated}``; each item is
    ``{kind, id, name, label, detail, category?, family?}``, and an item that
    cannot be picked (a link that is not loaded) adds ``disabled: true`` and
    ``reason``. An empty type search without a category offers categories
    first. Text searches rank all matching types across categories, with
    bilingual aliases and unpaginated category facets. ``element`` requires
    ``source="selection"`` and returns at most 50 items in one page. ``limit``
    and ``offset`` select a page; ``total`` counts every match and ``truncated``
    says whether more follow. ``warnings`` (only when there are any) are the
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

Levels, links and types use the query executor's supported add-in or legacy
path. Element selection and document identity use fixed, read-only queries.
"""
from __future__ import annotations

import asyncio
import json
import re
import unicodedata

from revit_bridge import mention_cache
from revit_bridge.mention_elements import SELECTION_LIMIT, items_by_id, selected_items
from revit_bridge.mention_search import category_terms, facets, rank_items, search_terms
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

MENTION_KINDS = ("level", "type", "link", "element")
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
                             q: str | None = None, limit: int | None = DEFAULT_MENTION_LIMIT,
                             offset: int | None = 0, source: str | None = None) -> dict:
    """What the designer can pick for ``@<kind>``: ``{kind, items, total, truncated}`` (see the module)."""
    if kind not in MENTION_KINDS:
        return {"error": "unknown_kind", "kind": kind, "kinds": list(MENTION_KINDS)}
    if q is not None and not isinstance(q, str):
        return {"error": "invalid_args", "kind": kind, "message": "q must be a string"}
    if (kind == "element" and source != "selection") or (kind != "element" and source is not None):
        return {"error": "invalid_args", "kind": kind, "message": "only element takes source=selection"}
    if offset is None:
        offset = 0
    if int_or_none(offset) is None or int(offset) < 0:
        return {"error": "invalid_args", "kind": kind, "message": "offset must be a non-negative integer"}
    offset = int(offset)
    if category is not None and (kind != "type" or not isinstance(category, str)
                                 or not CATEGORY_RE.fullmatch(category.strip())):
        return {"error": "invalid_args", "kind": kind,
                "message": f"invalid category {category!r}: only type takes one, an OST_* name"}
    limit = _limit(limit)

    try:
        terms = search_terms(q or "")
    except ValueError as exc:
        return {"error": "invalid_args", "kind": kind, "message": str(exc)}
    matched, remaining = category_terms(terms) if kind == "type" else (None, terms)
    category = category and category.strip()
    if kind == "element":
        limit = min(limit, SELECTION_LIMIT)
        try:
            return await selected_items(executor, offset, limit, terms)
        except RevitQueryError as exc:
            return {"error": "revit_error", "kind": kind, "message": str(exc)}
    elif kind == "type":
        scope = await mention_cache.request_scope(executor.client)
        reply = await _type_candidates(executor, scope, category, matched, bool(terms))
        if "error" in reply:
            return reply
        items = reply["items"]
    else:
        reply = await run_query(executor, {"level": "levels", "link": "links"}[kind])
        if "error" in reply:
            return dict(reply, kind=kind)
        convert = _level_item if kind == "level" else _link_item
        items = [item for row in reply["items"] if (item := convert(row)) is not None]
    if remaining:
        items = await asyncio.to_thread(rank_items, items, remaining, name_and_family_only=matched is not None)
    elif matched is not None:
        items.sort(key=lambda item: ((item["name"] or "").casefold(), item["id"]))
    out = {"kind": kind, "items": [dict(i) for i in items[offset:offset + limit]], "total": len(items),
           "truncated": len(items) > offset + limit or reply.get("truncated") is True,
           "offset": offset, "category_matched": sorted(matched or ()),
           "facets": reply["facets"] if "facets" in reply else facets(items)}
    if reply.get("warnings"):
        out["warnings"] = list(reply["warnings"])      # rows the add-in could not read: said, not dropped
    if kind == "type" and scope != mention_cache.device_scope(executor.client):
        return {"error": "revit_error", "kind": kind, "message": "The device connection changed; pick the type again."}
    return out


async def _type_candidates(executor, scope, category, matched, searching):
    """Fetch only the necessary categories, preserving category navigation on an empty query."""
    try:
        stamp = await mention_cache.document_stamp(executor.client, scope)
        inventory = mention_cache.inventories(stamp) if stamp else {}
        additions = {}
        if stamp and stamp.modified and not category and searching:
            data = await mention_cache.read_unsaved_types(executor, matched)
            items = [item for row in data["rows"] if isinstance(row, dict) and (item := _type_item(
                row.get("id"), row.get("family"), row.get("name"), row.get("shown"), row.get("category")))]
            reply = {"items": items, "warnings": data.get("warnings", []), "truncated": data.get("truncated", False)}
        else:
            reply = await _type_inventory(executor, category, matched, searching, inventory, additions)
        if stamp and (additions or stamp.modified):
            current = await mention_cache.document_stamp(executor.client, scope)
            if current != stamp:
                if current:
                    mention_cache.inventories(current)
                raise RevitQueryError("The active document changed; pick the type again.")
            if not stamp.modified:
                mention_cache.publish(inventory, additions)
        return reply
    except ValueError as exc:
        return {"error": "invalid_args", "kind": "type", "message": str(exc)}
    except ModelItemsError as exc:
        return exc.reply("type")
    except RevitQueryError as exc:
        return {"error": "revit_error", "kind": "type", "message": str(exc)}


async def _type_inventory(executor, category, matched, searching, inventory, additions):
    warnings = []
    truncated = False
    if category:
        categories = [category]
    else:
        reply = mention_cache.cached(inventory, None)
        if reply is None:
            reply = await run_query(executor, "type_categories")
            if not any(reply.get(k) for k in ("error", "warnings", "truncated")):
                additions[None] = mention_cache.pending(reply)
        if "error" in reply:
            return dict(reply, kind="type")
        warnings.extend(reply.get("warnings", []))
        truncated = reply.get("truncated", False)
        rows = reply["items"]
        if not searching:
            return {"items": [_category_item(row) for row in rows], "warnings": warnings, "truncated": truncated,
                    "facets": [{"category": r["category"], "label": r["display_name"] or r["category"],
                                "count": r["count"]} for r in rows]}
        categories = [r["category"] for r in rows]
    items = []
    for key in categories:
        if matched is not None and key not in matched:
            continue
        rows = mention_cache.cached(inventory, key)
        if rows is None:
            try:
                data = await executor.get_type_rows(key)
            except (ValueError, RevitQueryError) as exc:
                if category:
                    raise
                warnings.append(f"{key}: {exc}")
                warnings.extend(getattr(exc, "warnings", []))
                truncated = True
                continue
            warnings.extend(data.warnings)
            truncated = truncated or data.truncated is True
            rows = [item for row in data.rows if (item := _type_item(
                row.get("FamilyTypeId"), row.get("FamilyName"), _type_name(row), row.get("Category"), key))]
            if not data.truncated and not data.warnings:
                additions[key] = mention_cache.pending(rows)
        items.extend(rows)
    return {"items": items, "warnings": warnings, "truncated": truncated}


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

    elements = {}
    if ids["element"]:
        rows, element_warnings = await items_by_id(executor, ids["element"])
        elements = {item["id"]: item for item in rows}
        warnings.extend(element_warnings)

    resolved: list[dict] = []
    for kind, mention_id in wanted:
        if kind == "level":
            found = levels.get(mention_id)
        elif kind == "type":
            found = types.get(mention_id)
        elif kind == "element":
            found = elements.get(mention_id)
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
        "element": "element {name} (id {id}, {summary})",
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
        "element": "元素 {name}（id {id}，{summary}）",
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
        elif kind == "element":
            parts.append(table["element"].format(name=_quote(item.get("name")), id=_shown_id(item.get("id")),
                                                 summary=_quote(item.get("summary"))))
        elif kind == "link":
            parts.append(table["link"].format(name=_quote(item.get("label") or item.get("name")),
                                              id=_shown_id(item.get("id"))))
    if not parts:
        return ""
    return table["head"] + table["sep"].join(parts) + table["end"]

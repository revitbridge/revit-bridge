"""Observe the user's normal selection, then read it through the element mention API."""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from revit_bridge import mentions
from revit_bridge.mention_elements import SELECTION_LIMIT
from revit_bridge.mention_search import facets
from revit_bridge.revit.capabilities import MethodUnavailable, send_listed_command
from revit_bridge.snapshot.query import CATEGORY_RE, RevitQueryError


STATE_METHOD = "get_selection_state_in_revit"
MAX_SELECTION = 5000


@dataclass(frozen=True)
class _State:
    document_id: str
    stamp: str
    count: int


async def _state(client, *, focus=False):
    params = {"focus": True, "prompt": "Select elements in Revit."} if focus else {}
    data = await send_listed_command(client, STATE_METHOD, params)
    if (not isinstance(data, dict) or type(data.get("count")) is not int or data["count"] < 0
            or not all(isinstance(data.get(k), str) and data[k] for k in ("document_id", "stamp"))):
        raise RevitQueryError(f"Invalid selection state from {STATE_METHOD}")
    return _State(data["document_id"], data["stamp"], data["count"])


async def _read_mentions(executor, selected, category):
    """Return the result and last observed state. Discard pages if the user selects again."""
    if selected.count > MAX_SELECTION:
        raise RevitQueryError(f"REFUSED: {selected.count} elements selected. Narrow the selection to {MAX_SELECTION} or fewer.")
    rows = []
    warnings = []
    while len(rows) < selected.count:
        page = await mentions.mention_candidates(executor, "element", source="selection",
                                                 offset=len(rows), limit=SELECTION_LIMIT)
        current = await _state(executor.client)
        if (current.document_id != selected.document_id or
                any(row["document_id"] != selected.document_id for row in page.get("items", []))):
            return {"document_changed": True}, current
        if current != selected:
            return None, current
        if "error" in page:
            return page, current
        if not page["items"] or page["total"] != selected.count:
            raise RevitQueryError("Selection count changed while reading element mentions")
        rows.extend(page["items"])
        warnings.extend(page.get("warnings", []))
    if len(rows) != selected.count or len({row["id"] for row in rows}) != selected.count:
        raise RevitQueryError("Selection membership changed while paging element mentions")
    matches = rows if category is None else [row for row in rows if row["category"] == category]
    result = dict(kind="element", items=matches, offset=0, total=len(matches), truncated=False,
                  category_matched=[], facets=facets(matches))
    if category is not None:
        result["filtered_out"] = len(rows) - len(matches)
    if warnings:
        result["warnings"] = list(dict.fromkeys(warnings))
    return result, selected


async def _observe(executor, mode, category, deadline, settle, poll, clock, sleep):
    baseline = await _state(executor.client, focus=True)
    previous = baseline
    stable_since = clock()
    while (remaining := deadline - clock()) > 0:
        await sleep(min(poll, remaining))
        if clock() >= deadline:
            break
        current = await _state(executor.client)
        now = clock()
        if now >= deadline:
            break
        if current.document_id != baseline.document_id:
            return {"document_changed": True}
        if current != previous:
            stable_since = now
            previous = current
        if not current.count or current.stamp == baseline.stamp:
            continue
        ready = current.count == 1 if mode == "single" else now >= stable_since + settle
        if ready:
            result, previous = await _read_mentions(executor, current, category)
            if result is not None:
                return result
            stable_since = clock()
    return {"timed_out": True}


async def watch_selection(executor, mode, category, timeout_s, settle_ms, poll_ms, *, clock, sleep):
    if mode not in ("single", "multiple", "box"):
        raise ValueError("mode must be single, multiple or box")
    if category is not None and (not isinstance(category, str) or not CATEGORY_RE.fullmatch(category)):
        raise ValueError("category must be a BuiltInCategory identifier such as OST_Walls")
    if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout_s must be positive and finite")
    if type(settle_ms) is not int or settle_ms < 0 or type(poll_ms) is not int or poll_ms <= 0:
        raise ValueError("settle_ms must be a non-negative integer and poll_ms a positive integer")

    deadline = clock() + timeout_s
    # Bound discovery and in-flight reads even on transports that take only (method, params).
    timer = asyncio.timeout(timeout_s)
    try:
        async with timer:
            result = await _observe(executor, mode, category, deadline, settle_ms / 1000, poll_ms / 1000,
                                    clock, sleep)
            return {"timed_out": True} if clock() >= deadline else result
    except TimeoutError:
        if timer.expired():
            return {"timed_out": True}
        raise
    except MethodUnavailable:
        return {"error": "unsupported_addin",
                "hint": f"To use {STATE_METHOD}, upgrade the add-in. Select elements in Revit and use Get current selection."}

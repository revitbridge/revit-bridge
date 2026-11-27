"""Host API for named view activation and in-memory PNG export."""
from __future__ import annotations

import time

from revit_bridge.addin_test import view_reply
from revit_bridge.evidence.ledger import LOCAL_SCOPE, Ledger, summarize_result
from revit_bridge.host_functions import ACTIVATE_VIEW, EXPORT_VIEW_IMAGE
from revit_bridge.revit.capabilities import MethodUnavailable, send_listed_command


async def open_view(client, name: str, *, ledger: Ledger, host: str = "mcp", scope: str = LOCAL_SCOPE) -> dict:
    """View only. Activate an existing view by name, outside the snippet runner's transaction."""
    if not isinstance(name, str) or not name.strip():
        return view_reply(error="invalid_args", hint="Give the name of an existing view.")
    return await _view(client, "open_view", ACTIVATE_VIEW, {"name": name}, ledger, host, scope)


async def capture_view_image(client, width: int = 1600, view_id: int | None = None, *,
                             ledger: Ledger, host: str = "mcp", scope: str = LOCAL_SCOPE) -> dict:
    """Read only. Export a view as PNG. The add-in reduces the width to meet the 1 MiB limit."""
    if type(width) is not int or not 1 <= width <= 1600:
        return view_reply(error="invalid_args", hint="Choose a width of 1 to 1600 pixels.")
    params = {"width": width}
    if view_id is not None:
        if type(view_id) is not int or view_id <= 0:
            return view_reply(error="invalid_args", hint="Give a positive view id or omit it for the active view.")
        params["view_id"] = view_id
    return await _view(client, "capture_view", EXPORT_VIEW_IMAGE, params, ledger, host, scope)


capture_view = capture_view_image  # Original package entry point.


async def _view(client, tool, method, params, ledger, host, scope):
    started = time.monotonic()
    capture = tool == "capture_view"
    summary = {}
    try:
        answer = await send_listed_command(client, method, params)
    except MethodUnavailable as exc:
        return view_reply(error="unsupported_addin", hint=str(exc))
    except (RuntimeError, OSError) as exc:
        out = view_reply(error=str(exc))
    else:
        if capture:
            summary = summarize_result(answer, tool=tool)
            fields = ("png_base64", "width", "height", "view_name")
            valid = bool(summary) and isinstance(answer.get("view_name"), str)
        else:
            fields = ("view_id", "view_name", "view_type")
            valid = isinstance(answer, dict) and all(answer.get(key) is not None for key in fields)
        out = view_reply(**{key: answer[key] for key in fields}) if valid else view_reply(error="bad_reply")
    try:
        out["evidence_id"] = ledger.append({
            "action": tool, "tool": tool, "host": host, "scope": scope, "params": params,
            "confirmed_by": "host:read-only" if capture else "host:ui-only",
            "success": out["error"] is None,
            # An export error may echo image data. Only the caller sees the native diagnostic.
            "error": "capture_failed" if capture and out["error"] else out["error"],
            "result_summary": summary, "duration_ms": int((time.monotonic() - started) * 1000),
        })
    except OSError as exc:
        out["warnings"].append(f"evidence not recorded: {exc}")
    return out

"""check_code: compile C# for ``execute_code`` inside Revit without running it.

``check_code(client, code, parameters)``:

1. ``sandbox.review`` first, locally - a blocked pattern never reaches Revit:
   ``checked: true, compiled: false, blocked: true`` with the review's
   ``warnings``.
2. ``discover(client)``. Only an add-in that offers ``check_code_in_revit``
   compiles: the same wrapper, references and symbol blacklist as
   ``send_code_to_revit``, emitted to memory only - nothing is loaded or
   run, no transaction, no dialog, no document needed. Anything else is
   ``checked: false`` with ``capability`` and ``reason``: the code was not
   pre-compiled, which says nothing about whether it compiles.
3. ``hints`` - one per distinct diagnostic whose id is in ``HINT_IDS``,
   naming the Revit version when it is known and pointing at an API lookup
   tool. ``guidance`` is always ``[]`` here: it is filled in S2b, from the
   experience library.

Every reply has the same keys (``schema_version`` 1)::

    {"schema_version": 1, "checked": bool, "compiled": bool | None, "blocked": bool,
     "capability": "supported" | "unsupported" | "unknown" | None, "reason": str | None,
     "diagnostics": [{"id", "severity", "line", "column", "message", "symbol", "in_wrapper"}],
     "truncated": int, "fingerprint": str | None, "hints": [{"id", "symbol", "text"}],
     "guidance": [], "environment": dict | None, "warnings": [str]}

``compiled`` is None when ``checked`` is false. ``capability`` is None for a
local block (Revit was not asked). ``reason`` is ``"<kind>: <message>"``
(``method_not_found``, ``unreachable``, ``auth``, ``timeout``,
``no_check_code``, ``compiler_error``, ...). ``diagnostics`` are the
add-in's, as it sent them (lines count from the first line of the user's
code). ``environment`` is ``get_environment``'s reply built from the
capabilities (no extra request), None unless the capability is supported.
"""
from __future__ import annotations

import json
import re

from revit_bridge.revit import sandbox
from revit_bridge.revit.capabilities import (
    CHECK_CODE_METHOD,
    METHOD_NOT_FOUND,
    SUPPORTED,
    UNSUPPORTED,
    Capabilities,
    discover,
    error_kind,
    forget,
)
from revit_bridge.revit.environment import get_environment, version_label

SCHEMA_VERSION = 1
HINT_IDS = ("CS1061", "CS0117", "CS0246", "CS0103", "CS1501", "CS1503", "CS7036")
API_LOOKUP_TOOL = "search_revit_api"
SNIPPET_USINGS = "System, System.Linq, System.Collections.Generic, Autodesk.Revit.DB, Autodesk.Revit.UI"

_QUOTED = re.compile(r"'([^']+)'")
_ARGUMENT_COUNT = re.compile(r"takes (\d+) argument")
_ARGUMENT_INDEX = re.compile(r"Argument (\d+)")


def reply(**fields) -> dict:
    """A check_code reply: every key present, ``fields`` override the defaults."""
    base = {
        "schema_version": SCHEMA_VERSION,
        "checked": False,
        "compiled": None,
        "blocked": False,
        "capability": None,
        "reason": None,
        "diagnostics": [],
        "truncated": 0,
        "fingerprint": None,
        "hints": [],
        "guidance": [],
        "environment": None,
        "warnings": [],
    }
    base.update(fields)
    return base


def blocked_reply(code: str) -> dict | None:
    """The reply for code the local sandbox refuses, or None when it passes."""
    safe, warnings = sandbox.review(code)
    if safe:
        return None
    return reply(checked=True, compiled=False, blocked=True, warnings=warnings)


def not_checked(capabilities: Capabilities) -> dict:
    """The reply when the add-in cannot compile for us: unsupported or unknown capability."""
    if capabilities.state == UNSUPPORTED:
        reason = f"method_not_found: {capabilities.reason}"
    else:
        reason = f"{capabilities.error_kind or 'unknown'}: {capabilities.reason}"
    return reply(checked=False, capability=capabilities.state, reason=reason)


# -- hints ------------------------------------------------------------------------------

def _short_type(name: str | None) -> str | None:
    """'Autodesk.Revit.DB.Wall' -> 'Wall', 'List<int>' -> 'List'."""
    if not name:
        return None
    return name.split("(")[0].split("<")[0].split(".")[-1].strip() or None


def _short_member(name: str | None) -> str | None:
    """'Autodesk.Revit.DB.Wall.Create(Document, Curve)' -> 'Wall.Create'."""
    if not name:
        return None
    parts = [p for p in name.split("(")[0].split("<")[0].split(".") if p.strip()]
    return ".".join(parts[-2:]) if parts else None


def _sentence(label: str | None, body: str) -> str:
    return f"{label}: {body}" if label else body[:1].upper() + body[1:]


def hint_for(diagnostic: dict, label: str | None = None) -> dict | None:
    """``{id, symbol, text}`` for one diagnostic in ``HINT_IDS``, or None.

    ``symbol`` is the API name the hint is about (the missing member for
    CS1061 / CS0117, the expected type for CS1503, the method for CS7036),
    read from the message, falling back to the diagnostic's own ``symbol``.
    """
    diag_id = diagnostic.get("id")
    if diag_id not in HINT_IDS:
        return None
    message = str(diagnostic.get("message") or "")
    quoted = _QUOTED.findall(message)
    first = quoted[0] if quoted else None
    second = quoted[1] if len(quoted) > 1 else None
    own = diagnostic.get("symbol") if isinstance(diagnostic.get("symbol"), str) else None
    own = own or None

    if diag_id in ("CS1061", "CS0117"):
        owner, member = _short_type(first), second or own
        if not member:
            return None
        symbol = member
        body = f"'{owner}' has no '{member}'." if owner else f"there is no member '{member}'."
        query = f"{owner} {member}" if owner else member
    elif diag_id == "CS0246":
        symbol = first or own
        if not symbol:
            return None
        body = (f"type or namespace '{symbol}' is not found with the snippet's usings "
                f"({SNIPPET_USINGS}) and references (base library, RevitAPI, RevitAPIUI).")
        query = symbol
    elif diag_id == "CS0103":
        symbol = first or own
        if not symbol:
            return None
        body = f"'{symbol}' does not exist here; the snippet has only `document` and `parameters` in scope."
        query = symbol if symbol[:1].isupper() else None       # `doc`, `uidoc`: a scope slip, not an API name
    elif diag_id == "CS1501":
        symbol = first or own
        if not symbol:
            return None
        count = _ARGUMENT_COUNT.search(message)
        takes = f"takes {count.group(1)} arguments" if count else "takes that many arguments"
        body = f"no overload of '{symbol}' {takes}."
        query = symbol
    elif diag_id == "CS1503":
        symbol = _short_type(second) or own
        if not symbol:
            return None
        index = _ARGUMENT_INDEX.search(message)
        which = f"argument {index.group(1)}" if index else "an argument"
        given = f" is '{first}' but" if first else ""
        body = f"{which}{given} must be '{symbol}'."
        query = symbol
    else:                                                      # CS7036
        symbol = _short_member(second) or own
        if not symbol:
            return None
        param = f" '{first}'" if first else ""
        body = f"'{symbol}' needs an argument for its parameter{param}."
        query = symbol.replace(".", " ")
    text = _sentence(label, body)
    if query:
        text += (f" If an API lookup tool ({API_LOOKUP_TOOL}) is connected, query '{query}' "
                 f"before changing the code.")
    return {"id": diag_id, "symbol": symbol, "text": text}


def hints_for(diagnostics: list[dict], label: str | None = None) -> list[dict]:
    """The hints for these diagnostics, one per distinct (id, symbol)."""
    hints, seen = [], set()
    for diagnostic in diagnostics:
        hint = hint_for(diagnostic, label)
        if hint is None or (hint["id"], hint["symbol"]) in seen:
            continue
        seen.add((hint["id"], hint["symbol"]))
        hints.append(hint)
    return hints


# -- the check --------------------------------------------------------------------------

def _as_dict(result) -> dict | None:
    if isinstance(result, str) and result.strip():
        try:
            result = json.loads(result)
        except ValueError:
            return None
    return result if isinstance(result, dict) else None


async def check_code(client, code: str, parameters: list | None = None) -> dict:
    """Compile ``code`` in Revit without running it; the reply described in the module docstring.

    ``client`` needs ``send_command(method, params)``. Never raises for a
    transport or add-in failure: those are ``checked: false`` with a reason.
    ``guidance`` is always ``[]`` - filled in S2b (experience library).
    """
    refused = blocked_reply(code)
    if refused is not None:
        return refused
    caps = await discover(client)
    if not caps.supported:
        return not_checked(caps)
    environment = await get_environment(client, caps)

    def failed(reason: str, **fields) -> dict:
        return reply(checked=False, capability=SUPPORTED, reason=reason, environment=environment, **fields)

    if not caps.can_check_code:
        return failed(f"no_check_code: the add-in does not offer {CHECK_CODE_METHOD}")
    try:
        resp = await client.send_command(CHECK_CODE_METHOD, {"code": code, "parameters": list(parameters or [])})
    except Exception as exc:  # noqa: BLE001 - refused, reset, closed: not checked
        return failed(f"{Capabilities.from_exception(exc).error_kind}: {str(exc) or type(exc).__name__}")
    if not resp.success:
        message = resp.error or "no reply"
        if resp.error_code == METHOD_NOT_FOUND:
            forget(client)                       # the capability reply promised a method that is not there
            return failed(f"method_not_found: {message}")
        return failed(f"{error_kind(message) or 'rpc_error'}: {message}")
    result = _as_dict(resp.result)
    if result is None:
        return failed(f"bad_reply: unexpected reply to {CHECK_CODE_METHOD}: {str(resp.result)[:200]}")

    diagnostics = [d for d in result.get("diagnostics") or [] if isinstance(d, dict)]
    error_message = str(result.get("errorMessage") or "")
    fingerprint = result.get("fingerprint") if isinstance(result.get("fingerprint"), str) else None
    compiled = result.get("compiled") is True
    if error_message and not diagnostics and not compiled:
        # errorMessage is the compiler itself failing (Roslyn threw, timed out), not a compile error
        return failed(f"compiler_error: {error_message}", fingerprint=fingerprint)
    try:
        truncated = max(0, int(result.get("truncated") or 0))
    except (TypeError, ValueError):
        truncated = 0
    return reply(
        checked=True, compiled=compiled, capability=SUPPORTED, diagnostics=diagnostics, truncated=truncated,
        fingerprint=fingerprint, hints=hints_for(diagnostics, version_label(environment)),
        environment=environment, warnings=[error_message] if error_message else [],
    )

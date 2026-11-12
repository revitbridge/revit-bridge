"""dry_run_code: run C# in the open Revit model and always roll it back.

``dry_run_code(client, code, parameters, ledger=...)``:

1. ``sandbox.review(code)`` first, locally - the same review as
   ``execute_code``; a blocked pattern never reaches Revit:
   ``error: "blocked"`` with the review's ``warnings``.
2. ``discover(client)``. Only an add-in that offers the dry run
   (``Capabilities.can_dry_run``: ``features.dry_run``, else
   ``dry_run_code_in_revit`` in its methods) is asked. One that answered
   without it - an older add-in, or ``-32601`` for the method itself - is
   ``error: "no_dry_run"`` with a hint to use ``check_code`` instead and say
   in the report that the code was not dry-run. A capability that could not
   be read (``unknown``: unreachable, auth, timeout, ...) is
   ``error: "<kind>: <reason>"``, never ``no_dry_run``.
3. ``send_command("dry_run_code_in_revit", {"code", "parameters"})``: no
   ``confirm``, no token. The add-in runs the code inside a transaction
   group and rolls the group back on every path.
4. ``success`` is true only when the add-in answered ``success: true`` and
   ``rolled_back: true``. A reply whose ``rolled_back`` is not ``true``
   (false, missing, or a reply that is not an object) is
   ``error: "dry_run_not_rolled_back"`` - a defect to report at once, never a
   success - with the add-in's keys kept and its own ``success``,
   ``rolled_back`` and ``error`` in ``warnings``. A request that got no
   dry-run reply (connection lost, timeout, any other JSON-RPC error) keeps
   its error text; its ``hint`` says the rollback is not confirmed.
5. Every request sent to the add-in leaves one ledger line,
   ``action: "dry_run"``: the code's sha256 and head, ``params``, the
   document (see below), ``success``, ``error``, and in ``result_summary``
   the ids the code returned plus ``rolled_back`` (null when no dry-run reply
   came back), the ``changes`` counts (``added``, ``modified``, ``deleted``)
   and the numbers of ``failures`` and ``diagnostics`` - each null when the
   add-in gave none, since no value is not zero. No confirmation:
   ``spec_hash``, ``projection_hash``, ``token_prefix``, ``confirmed_by`` and
   ``channel`` are null. The refusals of 1 and 2 reached no code and leave
   no line.
6. A failure whose ``error`` names .NET exception types gets the ``hint``
   ``execute_code`` gives (``execution.failure_hint``): the
   ``get_guidance(stage="fix", exceptions=[...])`` call.

The gate takes no part: no token, no ``gate_refusal``, no
``REVIT_BRIDGE_ALLOW_UNCONFIRMED``. Nor is the execution path's document
probe sent: it runs through ``send_code_to_revit`` in a committed
transaction, which leaves an undo entry, and a dry run leaves the undo stack
as it was. The ledger's ``document`` therefore holds only what the
capability reply says (the Revit version); title and fingerprint are null.

Every reply has the keys of ``reply()`` - the add-in's keys (``success``,
``rolled_back``, ``result``, ``error``, ``diagnostics``, ``changes``,
``failures``, ``fingerprint``) and the bridge's (``evidence_id``, ``hint``,
``warnings``) - plus any other key the add-in sent, as it sent it.
``result`` is the code's return value, decoded from the JSON text the add-in
sends (as ``execute_code``'s is); text that is not JSON stays as it is.
"""
from __future__ import annotations

import json
import time
from typing import Any

from revit_bridge.evidence.ledger import LOCAL_SCOPE, Ledger, code_fields, summarize_result
from revit_bridge.execution import ensure_connected, failure_hint
from revit_bridge.revit import sandbox
from revit_bridge.revit.capabilities import (
    DRY_RUN_METHOD,
    METHOD_NOT_FOUND,
    UNKNOWN,
    Capabilities,
    discover,
    forget,
)

ACTION = "dry_run"
BLOCKED = "blocked"
NO_DRY_RUN = "no_dry_run"
NOT_ROLLED_BACK = "dry_run_not_rolled_back"
CHANGE_KINDS = ("added", "modified", "deleted")

NO_DRY_RUN_HINT = (f"This add-in cannot dry-run (no {DRY_RUN_METHOD}): compile the code with check_code "
                   "instead and say in the report that it was not dry-run.")
NOT_ROLLED_BACK_HINT = ("The add-in did not report that the dry run was rolled back: what the code did may "
                        "still be in the model. This is a defect - tell the designer now, before anything "
                        "else, and do not call dry_run_code again.")
NO_REPLY_HINT = ("No dry-run reply came back, so the rollback is not confirmed: check the model with query "
                 "before you go on.")


def reply(**fields) -> dict:
    """A dry_run_code reply: every key present, ``fields`` override the defaults."""
    base = {
        "success": False,
        "rolled_back": None,
        "result": None,
        "error": None,
        "diagnostics": [],
        "changes": None,
        "failures": [],
        "fingerprint": None,
        "evidence_id": None,
        "hint": None,
        "warnings": [],
    }
    base.update(fields)
    return base


def blocked_reply(code: str) -> dict | None:
    """The reply for code the local sandbox refuses, or None when it passes."""
    safe, warnings = sandbox.review(code)
    if safe:
        return None
    return reply(error=BLOCKED, warnings=warnings)


def not_asked(capabilities: Capabilities) -> dict:
    """The reply when the capability could not be read: nothing reached the code."""
    return reply(error=f"{capabilities.error_kind or 'unknown'}: {capabilities.reason}")


def no_dry_run() -> dict:
    return reply(error=NO_DRY_RUN, hint=NO_DRY_RUN_HINT)


def _as_dict(result) -> dict | None:
    if isinstance(result, str) and result.strip():
        try:
            result = json.loads(result)
        except ValueError:
            return None
    return result if isinstance(result, dict) else None


def _decoded(value: Any) -> Any:
    """The code's return value: JSON text decoded, anything else as it is."""
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _read(result, warnings: list[str]) -> tuple[dict, dict | None]:
    """The reply for what the add-in answered to ``dry_run_code_in_revit``, and that answer as an object."""
    answer = _as_dict(result)
    if answer is None:
        warnings.append(f"unexpected reply to {DRY_RUN_METHOD}: {str(result)[:200]}")
        return reply(error=NOT_ROLLED_BACK, hint=NOT_ROLLED_BACK_HINT, warnings=warnings), None
    own = answer.get("warnings")
    if isinstance(own, list):
        warnings[:0] = own
    out = reply()
    out.update(answer)                                   # every key as the add-in sent it
    out.update(result=_decoded(answer.get("result")), evidence_id=None, hint=None, warnings=warnings)
    if answer.get("rolled_back") is not True:
        warnings.append("add-in reply: " + ", ".join(
            f"{key} {json.dumps(answer.get(key), ensure_ascii=False)}" for key in ("success", "rolled_back", "error")))
        out.update(success=False, error=NOT_ROLLED_BACK, hint=NOT_ROLLED_BACK_HINT)
        return out, answer
    out["success"] = answer.get("success") is True
    if not out["success"]:
        out["hint"] = failure_hint(out["error"])
    return out, answer


def _count(part) -> int | None:
    count = part.get("count") if isinstance(part, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) else None


def _length(value) -> int | None:
    return len(value) if isinstance(value, list) else None


def summary(out: dict, answer: dict | None) -> dict:
    """``result_summary`` of the ledger line: the ids the code returned, and from the add-in's
    ``answer`` the rollback and the counts - null where it gave none (no value is not zero);
    ``rolled_back`` is null when no dry-run reply came back at all."""
    reported = answer or {}
    changes = reported.get("changes") if isinstance(reported.get("changes"), dict) else {}
    return {
        **summarize_result(out.get("result")),
        "rolled_back": reported.get("rolled_back") is True if answer is not None else None,
        "changes": {kind: _count(changes.get(kind)) for kind in CHANGE_KINDS},
        "failures": _length(reported.get("failures")),
        "diagnostics": _length(reported.get("diagnostics")),
    }


def _record(ledger: Ledger, out: dict, answer: dict | None, *, host: str, scope: str, code: str,
            parameters: list, document: dict, duration_ms: int) -> str | None:
    """Append the ledger line; a ledger that cannot be written costs a warning, not the reply."""
    record = {
        "host": host,
        "scope": scope,
        "action": ACTION,
        "params": {"parameters": parameters},
        **code_fields(code),
        "document": document,
        "success": out["success"],
        "error": out["error"] or None,
        "result_summary": summary(out, answer),
        "duration_ms": duration_ms,
        "warnings": list(out["warnings"]),
    }
    try:
        return ledger.append(record)
    except OSError as exc:
        out["warnings"].append(f"evidence not recorded: {exc}")
        return None


async def dry_run_code(client, code: str, parameters: list | None = None, *, ledger: Ledger,
                       host: str = "mcp", scope: str = LOCAL_SCOPE) -> dict:
    """Run ``code`` in Revit and roll it back; the reply described in the module docstring.

    ``client`` needs ``send_command(method, params)`` (and ``ensure_connected()``
    when it has one). Never raises for a transport or add-in failure.
    """
    refused = blocked_reply(code)
    if refused is not None:
        return refused
    params = list(parameters or [])
    try:
        await ensure_connected(client)
    except Exception as exc:  # noqa: BLE001 - refused, timed out, bad settings: nothing reached Revit
        return not_asked(Capabilities.from_exception(exc))
    caps = await discover(client)
    if caps.state == UNKNOWN:
        return not_asked(caps)
    if not caps.can_dry_run:
        return no_dry_run()

    warnings: list[str] = []
    answer: dict | None = None
    started = time.monotonic()
    try:
        resp = await client.send_command(DRY_RUN_METHOD, {"code": code, "parameters": params})
    except Exception as exc:  # noqa: BLE001 - it may have reached Revit: recorded, rollback unconfirmed
        error = str(exc) or type(exc).__name__
        out = reply(error=error, hint=failure_hint(error, NO_REPLY_HINT), warnings=warnings)
    else:
        if not resp.success and resp.error_code == METHOD_NOT_FOUND:
            forget(client)                       # the capability reply promised a method that is not there
            return no_dry_run()
        if not resp.success:
            error = resp.error or "no reply"
            out = reply(error=error, hint=failure_hint(error, NO_REPLY_HINT), warnings=warnings)
        else:
            out, answer = _read(resp.result, warnings)
    document = {"title": None, "revit_version": caps.revit.get("version_number"), "fingerprint": None}
    out["evidence_id"] = _record(ledger, out, answer, host=host, scope=scope, code=code, parameters=params,
                                 document=document, duration_ms=int((time.monotonic() - started) * 1000))
    return out

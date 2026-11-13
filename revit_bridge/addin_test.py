"""test_addin_command, zoom_view, capture_view: run a built add-in DLL in the open Revit and restore the model.

The add-in (``features.addin_test``) loads the DLL, clicks the command's picks
on elements given by id, feeds ``<name>.input.json``, reads back, and restores
the model; ``zoom_view_in_revit`` and ``capture_view_in_revit`` move the view
and save a picture of it. No token and no gate: a test keeps nothing.

``test_addin_command(client, dll, command_class, ..., ledger=...)``:

1. Local checks, nothing sent when one refuses: ``dll`` must be a file on
   this machine (``dll_not_found``; ``dll_unreadable``), ``command_class`` a
   name and ``timeout_seconds`` a positive whole number (``invalid_args``),
   ``restore`` ``"group_rollback"``, ``"undo"`` or none (``invalid_args``),
   ``readback_code`` must pass ``sandbox.review`` (``blocked``, the patterns
   in ``warnings``). The server hashes the DLL (SHA-256).
2. ``discover``: without ``features.addin_test`` the reply is
   ``error: "no_addin_test"`` (also for ``-32601`` on the method); a
   capability that could not be read is ``"<kind>: <reason>"``.
3. ``send_command("test_addin_command_in_revit", {dll, class, timeout_seconds,
   picks?, input?, readback_code?, zoom_to?, restore?})`` - keys left out
   when not given - waiting ``timeout_seconds + REPLY_MARGIN_SECONDS``.
4. The add-in's keys pass through. ``restored_clean`` not ``true`` (or a
   reply that is not an object) is ``error: "addin_test_not_restored"``: the
   model may hold what the command did - tell the designer at once. A loaded
   DLL whose ``dll_sha256`` differs from the server's hash is
   ``error: "dll_hash_mismatch"``. No reply at all keeps its error text, with
   a hint that the restore is not confirmed. Otherwise ``error`` is null: what
   the command itself did is in ``loaded``, ``returned``, ``result_file``,
   ``changes``, ``failures``, ``timed_out``.
5. One ledger line per request sent, ``action: "addin_test"``: ``params``
   holds the DLL path and hash, the class, picks, input, zoom, timeout and
   requested restore; ``code_sha256`` / ``code_head`` the read-back code;
   ``result_summary`` the outcome (``loaded``, ``returned``, ``timed_out``,
   the change counts, the number of failures, ``restore``,
   ``restored_clean``, the add-in's ``dll_sha256``). No confirmation field
   is set.

``zoom_view`` / ``capture_view`` pass their arguments through (no element:
fit), have the same capability check and leave a ledger line each
(``action: "zoom_view"`` / ``"capture_view"``). Every reply of the three has
the keys of its ``*_reply()`` plus any other key the add-in sent.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from revit_bridge.dry_run import CHANGE_KINDS, _as_dict, _count, _length
from revit_bridge.evidence.ledger import LOCAL_SCOPE, Ledger, code_fields
from revit_bridge.execution import ensure_connected
from revit_bridge.revit import sandbox
from revit_bridge.revit.capabilities import METHOD_NOT_FOUND, UNKNOWN, Capabilities, discover, forget

TEST_METHOD = "test_addin_command_in_revit"
ZOOM_METHOD = "zoom_view_in_revit"
CAPTURE_METHOD = "capture_view_in_revit"
RESTORES = ("group_rollback", "undo")
REPLY_MARGIN_SECONDS = 30          # the add-in's own timeout plus loading, restoring and the result file

NO_ADDIN_TEST = "no_addin_test"
NOT_RESTORED = "addin_test_not_restored"
NO_ADDIN_TEST_HINT = ("This add-in cannot run add-in tests (no features.addin_test): test the command through "
                      "Revit's UI as the revit-addin-test skill describes, and say in the report that it was not "
                      "run through the bridge.")
NOT_RESTORED_HINT = ("The add-in did not report that the model was restored after the test: what the command did "
                     "may still be in the model. This is a defect - tell the designer now, before anything else, "
                     "and do not run another test.")
HASH_MISMATCH_HINT = ("The add-in ran another file than the one hashed here (it changed during the test?): "
                      "rebuild if needed and test again before you trust this result.")
NO_REPLY_HINT = ("No test reply came back, so the restore is not confirmed: check the model with query before "
                 "you go on.")


def test_reply(**fields) -> dict:
    """A test_addin_command reply: the add-in's keys (work plan 4.3) and the bridge's, all present."""
    base = {
        "loaded": None, "load_error": None, "returned": None, "command_message": None, "result_file": None,
        "changes": None, "failures": [], "readback": None, "picks_done": [], "restore": None,
        "rolled_back": None, "undone": None, "restored_clean": None, "timed_out": None, "foreground_ok": None,
        "screenshot": None, "dll_sha256": None,
        "error": None, "evidence_id": None, "hint": None, "warnings": [],
    }
    base.update(fields)
    return base


def view_reply(**fields) -> dict:
    """A zoom_view / capture_view reply: the bridge's keys (and ``path``, ``foreground_ok`` for a capture)."""
    base = {"error": None, "evidence_id": None, "hint": None, "warnings": []}
    base.update(fields)
    return base


def _not_asked(caps: Capabilities, reply) -> dict:
    return reply(error=f"{caps.error_kind or 'unknown'}: {caps.reason}")


async def _capability(client, reply) -> tuple[Capabilities | None, dict | None]:
    """The add-in's capabilities when it can test, else the refusal."""
    try:
        await ensure_connected(client)
    except Exception as exc:  # noqa: BLE001 - nothing reached Revit
        return None, _not_asked(Capabilities.from_exception(exc), reply)
    caps = await discover(client)
    if caps.state == UNKNOWN:
        return None, _not_asked(caps, reply)
    if not caps.can_addin_test:
        return None, reply(error=NO_ADDIN_TEST, hint=NO_ADDIN_TEST_HINT)
    return caps, None


def _document(caps: Capabilities) -> dict:
    """Only what the capability reply says: no probe through send_code_to_revit (it leaves an undo entry)."""
    return {"title": None, "revit_version": caps.revit.get("version_number"), "fingerprint": None}


def _append(ledger: Ledger, out: dict, record: dict) -> None:
    try:
        out["evidence_id"] = ledger.append(record)
    except OSError as exc:
        out["warnings"].append(f"evidence not recorded: {exc}")


def check_test_args(dll, command_class, readback_code, timeout_seconds, restore) -> tuple[dict | None, Path | None, str]:
    """The local checks of step 1: ``(refusal, path, sha256)``."""
    path = Path(dll) if isinstance(dll, str) and dll.strip() else None
    if path is None or not path.is_file():
        return test_reply(error="dll_not_found", warnings=[f"{dll}: no such file"],
                          hint="dll takes the path of a built add-in DLL on the machine that runs revit-bridge; "
                               "pass an absolute path."), None, ""
    problems = []
    if not isinstance(command_class, str) or not command_class.strip():
        problems.append("command_class: the full name of the IExternalCommand class is required")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        problems.append(f"timeout_seconds: a positive whole number of seconds, not {timeout_seconds!r}")
    if restore is not None and restore not in RESTORES:
        problems.append(f"restore: one of {', '.join(RESTORES)} or none, not {restore!r}")
    if problems:
        return test_reply(error="invalid_args", warnings=problems), None, ""
    if readback_code:
        safe, patterns = sandbox.review(readback_code)
        if not safe:
            return test_reply(error="blocked", warnings=[f"readback_code: {p}" for p in patterns]), None, ""
    try:
        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return test_reply(error="dll_unreadable", warnings=[f"{path}: {exc}"]), None, ""
    return None, path.resolve(), sha256


def _read_test(result, sha256: str, warnings: list[str]) -> tuple[dict, dict | None]:
    """The reply for what the add-in answered, and that answer as an object (None when it was none)."""
    answer = _as_dict(result)
    if answer is None:
        warnings.append(f"unexpected reply to {TEST_METHOD}: {str(result)[:200]}")
        return test_reply(error=NOT_RESTORED, hint=NOT_RESTORED_HINT, warnings=warnings), None
    own = answer.get("warnings")
    if isinstance(own, list):
        warnings[:0] = own
    out = test_reply()
    out.update(answer)                                   # every key as the add-in sent it
    out.update(error=None, evidence_id=None, hint=None, warnings=warnings)
    if answer.get("restored_clean") is not True:
        out.update(error=NOT_RESTORED, hint=NOT_RESTORED_HINT)
    elif answer.get("loaded") is True and str(answer.get("dll_sha256") or "").lower() != sha256:
        warnings.append(f"dll_sha256: the server hashed {sha256}, the add-in reports {answer.get('dll_sha256')!r}")
        out.update(error="dll_hash_mismatch", hint=HASH_MISMATCH_HINT)
    return out, answer


def _test_summary(answer: dict | None) -> dict:
    """The outcome from the add-in's answer; null where it gave none (no value is not zero)."""
    reported = answer or {}
    changes = reported.get("changes") if isinstance(reported.get("changes"), dict) else {}
    return {
        **{key: reported.get(key) for key in ("loaded", "returned", "timed_out")},
        "changes": {kind: _count(changes.get(kind)) for kind in CHANGE_KINDS},
        "failures": _length(reported.get("failures")),
        **{key: reported.get(key) for key in ("restore", "restored_clean", "dll_sha256")},
    }


async def test_addin_command(client, dll: str, command_class: str, picks: list | None = None,
                             input: dict | None = None, readback_code: str | None = None,  # noqa: A002
                             zoom_to: int | None = None, timeout_seconds: int = 120, restore: str | None = None,
                             *, ledger: Ledger, host: str = "mcp", scope: str = LOCAL_SCOPE) -> dict:
    """Run ``command_class`` of ``dll`` in Revit and restore the model; the reply in the module docstring."""
    refused, path, sha256 = check_test_args(dll, command_class, readback_code, timeout_seconds, restore)
    if refused is not None:
        return refused
    caps, refused = await _capability(client, test_reply)
    if refused is not None:
        return refused

    request = {"dll": str(path), "class": command_class, "timeout_seconds": timeout_seconds}
    for key, value in (("picks", picks), ("input", input), ("readback_code", readback_code or None),
                       ("zoom_to", zoom_to), ("restore", restore)):
        if value is not None:
            request[key] = value
    warnings: list[str] = []
    answer: dict | None = None
    started = time.monotonic()
    try:
        resp = await client.send_command(TEST_METHOD, request, timeout=timeout_seconds + REPLY_MARGIN_SECONDS)
    except Exception as exc:  # noqa: BLE001 - it may have reached Revit: recorded, restore unconfirmed
        out = test_reply(error=str(exc) or type(exc).__name__, hint=NO_REPLY_HINT, warnings=warnings)
    else:
        if not resp.success and resp.error_code == METHOD_NOT_FOUND:
            forget(client)
            return test_reply(error=NO_ADDIN_TEST, hint=NO_ADDIN_TEST_HINT)
        if not resp.success:
            out = test_reply(error=resp.error or "no reply", hint=NO_REPLY_HINT, warnings=warnings)
        else:
            out, answer = _read_test(resp.result, sha256, warnings)
    _append(ledger, out, {
        "host": host, "scope": scope, "action": "addin_test",
        "params": {"dll": str(path), "dll_sha256": sha256, "class": command_class, "picks": picks or [],
                   "input": input, "zoom_to": zoom_to, "timeout_seconds": timeout_seconds, "restore": restore},
        **code_fields(readback_code or None),
        "document": _document(caps),
        "success": out["error"] is None,
        "error": out["error"],
        "result_summary": _test_summary(answer),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "warnings": list(out["warnings"]),
    })
    return out


async def _view(client, method: str, action: str, params: dict, reply, *, ledger: Ledger, host: str,
                scope: str) -> dict:
    caps, refused = await _capability(client, reply)
    if refused is not None:
        return refused
    started = time.monotonic()
    try:
        resp = await client.send_command(method, params)
    except Exception as exc:  # noqa: BLE001
        out = reply(error=str(exc) or type(exc).__name__)
    else:
        if not resp.success and resp.error_code == METHOD_NOT_FOUND:
            forget(client)
            return reply(error=NO_ADDIN_TEST, hint=NO_ADDIN_TEST_HINT)
        answer = _as_dict(resp.result) if resp.success else None
        if not resp.success:
            out = reply(error=resp.error or "no reply")
        elif answer is None:
            out = reply(error="bad_reply", warnings=[f"unexpected reply to {method}: {str(resp.result)[:200]}"])
        else:
            out = reply()
            out.update(answer)
            out.update(error=None, evidence_id=None, hint=None, warnings=list(out.get("warnings") or []))
    _append(ledger, out, {
        "host": host, "scope": scope, "action": action, "params": params, "document": _document(caps),
        "success": out["error"] is None, "error": out["error"],
        "result_summary": {k: out.get(k) for k in ("path", "foreground_ok") if k in out},
        "duration_ms": int((time.monotonic() - started) * 1000), "warnings": list(out["warnings"]),
    })
    return out


def _target(element_id, margin_mm, invalid) -> tuple[dict | None, dict | None]:
    """``{"element_id", "margin_mm"}``, ``{"fit": true}`` without an element, or the refusal."""
    if element_id is None:
        return {"fit": True}, None
    if isinstance(element_id, bool) or not isinstance(element_id, int):
        return None, invalid(error="invalid_args", warnings=[f"element_id: a whole number, not {element_id!r}"])
    if isinstance(margin_mm, bool) or not isinstance(margin_mm, (int, float)) or margin_mm < 0:
        return None, invalid(error="invalid_args", warnings=[f"margin_mm: a number >= 0, not {margin_mm!r}"])
    return {"element_id": element_id, "margin_mm": margin_mm}, None


async def zoom_view(client, element_id: int | None = None, margin_mm: float = 2500, *, ledger: Ledger,
                    host: str = "mcp", scope: str = LOCAL_SCOPE) -> dict:
    params, refused = _target(element_id, margin_mm, view_reply)
    if refused is not None:
        return refused
    return await _view(client, ZOOM_METHOD, "zoom_view", params, view_reply, ledger=ledger, host=host, scope=scope)


def capture_reply(**fields) -> dict:
    return view_reply(**{"path": None, "foreground_ok": None, **fields})


async def capture_view(client, element_id: int | None = None, margin_mm: float = 1500, *, ledger: Ledger,
                       host: str = "mcp", scope: str = LOCAL_SCOPE) -> dict:
    params, refused = _target(element_id, margin_mm, capture_reply)
    if refused is not None:
        return refused
    params.pop("fit", None)                              # a capture without an element takes the view as it is
    return await _view(client, CAPTURE_METHOD, "capture_view", params, capture_reply, ledger=ledger, host=host,
                       scope=scope)

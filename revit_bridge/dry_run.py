"""dry_run_code: run C# in the open Revit model and always roll it back.

``dry_run_code(client, code, parameters, include=..., ledger=...)``:

1. ``prepare(code, include)``, locally - nothing reaches Revit when it
   refuses. ``sandbox.review(code)``, the same review as ``execute_code``: a
   blocked pattern is ``error: "blocked"`` with the review's ``warnings``.
   Then the files of ``include`` (paths of local ``.cs`` files, compiled in
   Revit together with the code so the code can call into them), each read
   as UTF-8 (a BOM is dropped) and sent as ``{"name": <base name>, "text"}``.
   Refused, with every offending file in ``warnings``: more than
   ``MAX_SOURCES`` files (``include_too_many``); a path that is no file
   (``include_not_found``); another extension than ``.cs``
   (``include_not_cs``); two files with the same base name, case ignored
   (``include_duplicate_name``); a file that is not UTF-8 text
   (``include_unreadable``); more than ``MAX_SOURCE_BYTES`` of UTF-8 text in
   all (``include_too_large``); a file the sandbox refuses
   (``include_blocked``, ``"<name>: <pattern>"``).
2. ``discover(client)``. Only an add-in that offers the dry run
   (``Capabilities.can_dry_run``: ``features.dry_run``, else
   ``dry_run_code_in_revit`` in its methods) is asked. One that answered
   without it - an older add-in, or ``-32601`` for the method itself - is
   ``error: "no_dry_run"`` with a hint to use ``check_code`` instead and say
   in the report that the code was not dry-run. With files to include, the
   add-in must also report ``features.dry_run_sources``
   (``Capabilities.can_dry_run_sources``), else ``error:
   "no_dry_run_include"``. A capability that could not be read
   (``unknown``: unreachable, auth, timeout, ...) is
   ``error: "<kind>: <reason>"``, never ``no_dry_run``.
3. ``send_command("dry_run_code_in_revit", {"code", "parameters"})``, plus
   ``sources`` only when there are files to include: no ``confirm``, no
   token. The add-in runs the code inside a transaction group and rolls the
   group back on every path; a diagnostic in an included file names it in
   ``file``.
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
   add-in gave none, since no value is not zero - and ``sources``, the
   included files as ``{"name", "sha256"}`` (the hash of the file's bytes on
   disk, so it matches the file built into the add-in). No confirmation:
   ``spec_hash``, ``projection_hash``, ``token_prefix``, ``confirmed_by`` and
   ``channel`` are null. The refusals of 1 and 2 reached no code and leave
   no line.
6. A failure whose ``error`` names .NET exception types gets a ``hint`` in
   the style of ``execute_code``'s (``execution.failure_hint``): the same
   ``get_guidance(stage="fix", exceptions=[...])`` call, but the changed
   code needs ``check_code`` and another dry run, not a new confirmation
   (``DRY_RUN_EXCEPTION_HINT``, ``failure_hint`` here).

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

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from revit_bridge.evidence.ledger import LOCAL_SCOPE, Ledger, code_fields, summarize_result
from revit_bridge.execution import ensure_connected
from revit_bridge.guidance.scan import exception_types
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
NO_DRY_RUN_INCLUDE = "no_dry_run_include"
NOT_ROLLED_BACK = "dry_run_not_rolled_back"
CHANGE_KINDS = ("added", "modified", "deleted")
# The add-in's limits for dry_run_code_in_revit's sources: 20 files, 400 KB in all. The size is counted
# here as UTF-8 bytes of the text against 400,000, so a set the bridge sends is within the add-in's limit
# whether that counts characters or bytes, 1000 or 1024 to the KB.
MAX_SOURCES = 20
MAX_SOURCE_BYTES = 400_000
SOURCE_SUFFIX = ".cs"

NO_DRY_RUN_HINT = (f"This add-in cannot dry-run (no {DRY_RUN_METHOD}): compile the code with check_code "
                   "instead and say in the report that it was not dry-run.")
NO_DRY_RUN_INCLUDE_HINT = ("This add-in cannot compile included files in a dry run (no features.dry_run_sources): "
                           "paste the code the snippet needs into it, or go on without the dry run and say so in "
                           "the report.")
INCLUDE_HINTS = {
    "include_too_many": f"Include at most {MAX_SOURCES} files: only those the code calls into.",
    "include_not_found": "include takes paths of existing .cs files on the machine that runs revit-bridge; "
                         "pass absolute paths.",
    "include_not_cs": "Only C# source files (.cs) can be included.",
    "include_duplicate_name": "The add-in tells included files apart by their names: two files with the same "
                              "name cannot both be included.",
    "include_unreadable": "Each included file must be readable UTF-8 text.",
    "include_too_large": f"The included files hold more than {MAX_SOURCE_BYTES} bytes in all: include only "
                         "the files the code calls into.",
    "include_blocked": "An included file uses what a dry run refuses (file, process, network, reflection, "
                       "threading): keep that out of the core-logic file.",
}
NOT_ROLLED_BACK_HINT = ("The add-in did not report that the dry run was rolled back: what the code did may "
                        "still be in the model. This is a defect - tell the designer now, before anything "
                        "else, and do not call dry_run_code again.")
NO_REPLY_HINT = ("No dry-run reply came back, so the rollback is not confirmed: check the model with query "
                 "before you go on.")
# E3 for a dry run: execute_code's call (execution.EXCEPTION_HINT), but what the changed code needs
# next is another dry run, not a new confirmation.
DRY_RUN_EXCEPTION_HINT = ('call get_guidance(stage="fix", exceptions={exceptions}) and read those lessons before '
                          'changing the code; the changed code needs check_code and another dry run.')


def failure_hint(error: str | None, hint: str | None = None) -> str | None:
    """``hint`` for a failed dry run: when ``error`` names .NET exception types,
    ``DRY_RUN_EXCEPTION_HINT`` with them (``hint``, if any, after it); otherwise ``hint`` as is."""
    names = exception_types(error)
    if not names:
        return hint
    call = DRY_RUN_EXCEPTION_HINT.format(exceptions=json.dumps(names))
    return f"{call} {hint}" if hint else call


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


def _include_refusal(error: str, warnings: list[str]) -> dict:
    return reply(error=error, hint=INCLUDE_HINTS[error], warnings=warnings)


def read_sources(include) -> tuple[dict | None, list[dict]]:
    """The files of ``include`` as ``[{"name", "text", "sha256"}]``, or the refusal (module docstring, 1).

    ``name`` is the file's base name, ``text`` its UTF-8 content without a
    BOM, ``sha256`` the hash of its bytes on disk. None or an empty list is
    no file at all.
    """
    if not include:
        return None, []
    entries = [include] if isinstance(include, str) else list(include)
    if len(entries) > MAX_SOURCES:
        return _include_refusal("include_too_many", [f"{len(entries)} files given, at most {MAX_SOURCES}"]), []
    paths, missing = [], []
    for entry in entries:
        path = Path(entry) if isinstance(entry, str) and entry.strip() else None
        if path is None or not path.is_file():
            missing.append(f"{entry}: no such file")
        else:
            paths.append(path)
    if missing:
        return _include_refusal("include_not_found", missing), []
    other = [f"{path}: not a {SOURCE_SUFFIX} file" for path in paths if path.suffix.lower() != SOURCE_SUFFIX]
    if other:
        return _include_refusal("include_not_cs", other), []
    by_name: dict[str, list[Path]] = {}
    for path in paths:
        by_name.setdefault(path.name.lower(), []).append(path)
    twice = [f"{same[0].name}: " + ", ".join(str(p) for p in same) for same in by_name.values() if len(same) > 1]
    if twice:
        return _include_refusal("include_duplicate_name", twice), []
    sources, unreadable = [], []
    for path in paths:
        try:
            data = path.read_bytes()
            text = data.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        sources.append({"name": path.name, "text": text, "sha256": hashlib.sha256(data).hexdigest()})
    if unreadable:
        return _include_refusal("include_unreadable", unreadable), []
    total = sum(len(source["text"].encode("utf-8")) for source in sources)
    if total > MAX_SOURCE_BYTES:
        return _include_refusal("include_too_large", [f"{total} bytes in all, at most {MAX_SOURCE_BYTES}"]), []
    blocked = []
    for source in sources:
        safe, patterns = sandbox.review(source["text"])
        if not safe:
            blocked.extend(f"{source['name']}: {pattern}" for pattern in patterns)
    if blocked:
        return _include_refusal("include_blocked", blocked), []
    return None, sources


def prepare(code: str, include=None) -> tuple[dict | None, list[dict]]:
    """The checks that need no Revit: the sandbox on ``code``, then ``read_sources(include)``.
    ``(refusal, [])`` when one refuses, else ``(None, sources)``."""
    refused = blocked_reply(code)
    if refused is not None:
        return refused, []
    return read_sources(include)


def not_asked(capabilities: Capabilities) -> dict:
    """The reply when the capability could not be read: nothing reached the code."""
    return reply(error=f"{capabilities.error_kind or 'unknown'}: {capabilities.reason}")


def no_dry_run() -> dict:
    return reply(error=NO_DRY_RUN, hint=NO_DRY_RUN_HINT)


def no_dry_run_include() -> dict:
    return reply(error=NO_DRY_RUN_INCLUDE, hint=NO_DRY_RUN_INCLUDE_HINT)


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


def summary(out: dict, answer: dict | None, sources: list[dict] | None = None) -> dict:
    """``result_summary`` of the ledger line: the ids the code returned, and from the add-in's
    ``answer`` the rollback and the counts - null where it gave none (no value is not zero);
    ``rolled_back`` is null when no dry-run reply came back at all. ``sources``: the included
    files' names and hashes, ``[]`` when none."""
    reported = answer or {}
    changes = reported.get("changes") if isinstance(reported.get("changes"), dict) else {}
    return {
        **summarize_result(out.get("result")),
        "rolled_back": reported.get("rolled_back") is True if answer is not None else None,
        "changes": {kind: _count(changes.get(kind)) for kind in CHANGE_KINDS},
        "failures": _length(reported.get("failures")),
        "diagnostics": _length(reported.get("diagnostics")),
        "sources": [{"name": source["name"], "sha256": source["sha256"]} for source in sources or []],
    }


def _record(ledger: Ledger, out: dict, answer: dict | None, sources: list[dict], *, host: str, scope: str,
            code: str, parameters: list, document: dict, duration_ms: int) -> str | None:
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
        "result_summary": summary(out, answer, sources),
        "duration_ms": duration_ms,
        "warnings": list(out["warnings"]),
    }
    try:
        return ledger.append(record)
    except OSError as exc:
        out["warnings"].append(f"evidence not recorded: {exc}")
        return None


async def dry_run_code(client, code: str, parameters: list | None = None, *, ledger: Ledger,
                       include: list[str] | None = None, host: str = "mcp", scope: str = LOCAL_SCOPE) -> dict:
    """Run ``code`` in Revit and roll it back; the reply described in the module docstring.

    ``include``: paths of local ``.cs`` files compiled together with ``code``.
    ``client`` needs ``send_command(method, params)`` (and ``ensure_connected()``
    when it has one). Never raises for a transport or add-in failure.
    """
    refused, sources = prepare(code, include)
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
    if sources and not caps.can_dry_run_sources:
        return no_dry_run_include()

    request: dict = {"code": code, "parameters": params}
    if sources:
        request["sources"] = [{"name": source["name"], "text": source["text"]} for source in sources]
    warnings: list[str] = []
    answer: dict | None = None
    started = time.monotonic()
    try:
        resp = await client.send_command(DRY_RUN_METHOD, request)
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
    out["evidence_id"] = _record(ledger, out, answer, sources, host=host, scope=scope, code=code,
                                 parameters=params, document=document,
                                 duration_ms=int((time.monotonic() - started) * 1000))
    return out

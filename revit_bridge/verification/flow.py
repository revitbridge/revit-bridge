"""``plan_verification`` and ``verify_execution`` as a package API (P9, D17).

Both run only fixed read-only probes of the bridge (the document probe of
the execution path, and the probes of the three built-in validators); never
model code, so neither needs a token. ``execute_code``, the TaskSpec and the
confirmation token are untouched: the plan lives beside them, in its own
store, and the model shows it under the card as one line of text.

``plan(store, client, template, params)``::

    {"verification_id", "template", "params", "baseline": {"count"} | null,
     "document": {"title", "revit_version"}, "created_at", "expires_at"}
    | {"error": "invalid_template", "problems": [...]}          nothing written
    | {"error": "revit_unreachable" | "probe_failed" | "store_failed", "message"}

``verify(store, ledger, client, evidence_id, verification_id)``::

    {"verified": bool, "template", "checks": [{name, passed, detail}], "before", "after",
     "evidence_id", "verification_id", "verification_evidence_id", "error": null | str}

``error`` (``verified`` false, the plan not consumed, no ledger line):
``unknown_evidence`` (no such record, or not an execute_code / run_tool
one), ``not_executed`` (the record's ``success`` is false),
``unknown_verification`` / ``verification_used`` / ``verification_expired``,
``document_changed`` (the document of the plan differs from the execution's
or from the one open now, or the open one cannot be identified),
``revit_unreachable`` (nothing could be read).
Otherwise the validator's ``after`` runs on the recorded result (its ids,
``extract_ids``) or, for ``count_delta``, on the baseline; the plan is
consumed and a ledger line with ``action: "verify_execution"`` is written.
``verified`` is true only when the validator passed - and, for
``count_delta``, the execution was recorded after the baseline was taken.
"""
from __future__ import annotations

import time
from typing import Any

from revit_bridge.evidence.ledger import Ledger
from revit_bridge.execution import document_info, ensure_connected
from revit_bridge.validators.base import Check, ValidationReport, failed_report
from revit_bridge.verification.store import VerificationStore, parse_iso
from revit_bridge.verification.templates import check_template, validator_call

ACTION = "verify_execution"
EXECUTIONS = ("execute_code", "run_tool")


def _message(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


async def plan(store: VerificationStore, client, template: Any, params: Any) -> dict:
    """Check the template, take the baseline (count_delta) and the document identity, store the plan."""
    normal, problems = check_template(template, params)
    if problems:
        return {"error": "invalid_template", "problems": problems}
    warnings: list[str] = []
    baseline = None
    try:
        await ensure_connected(client)
    except Exception as exc:  # noqa: BLE001 - refused, timed out, bad settings: nothing was read
        return {"error": "revit_unreachable", "message": _message(exc)}
    try:
        document = await document_info(client, warnings)
        if document.get("title") is None:
            return {"error": "probe_failed", "message": "; ".join(warnings) or "the document probe failed"}
        if template == "count_delta":
            validator, spec, cfg, _ = validator_call(template, normal)
            sample = await validator.before(client, spec, cfg)
            baseline = {"count": int(sample["count"])}
    except OSError as exc:
        return {"error": "revit_unreachable", "message": _message(exc)}
    except Exception as exc:  # noqa: BLE001 - Revit answered, the fixed probe did not work
        return {"error": "probe_failed", "message": f"{type(exc).__name__}: {exc}"}
    try:
        saved = store.create(template, normal, baseline, document)
    except OSError as exc:
        return {"error": "store_failed", "message": _message(exc)}
    return {key: saved[key] for key in ("verification_id", "template", "params", "baseline", "document",
                                        "created_at", "expires_at")}


def _reply(evidence_id: Any, verification_id: Any, template: str | None = None, error: str | None = None,
           report: ValidationReport | None = None, verified: bool = False,
           verification_evidence_id: str | None = None) -> dict:
    return {
        "verified": verified,
        "template": template,
        "checks": [c.model_dump() for c in report.checks] if report else [],
        "before": dict(report.before) if report else {},
        "after": dict(report.after) if report else {},
        "evidence_id": evidence_id,
        "verification_id": verification_id,
        "verification_evidence_id": verification_evidence_id,
        "error": error,
    }


def _order_check(record: dict, saved: dict) -> Check | None:
    """count_delta: a failing check when the execution was recorded before the baseline."""
    ran, planned = parse_iso(record.get("ts")), parse_iso(saved.get("created_at"))
    if ran is None or planned is None or ran >= planned:
        return None
    return Check(name="order", passed=False,
                 detail=f"the execution ({record.get('ts')}) ran before the baseline was taken "
                        f"({saved.get('created_at')}); plan the verification before executing")


async def verify(store: VerificationStore, ledger: Ledger, client, evidence_id: Any, verification_id: Any,
                 host: str = "mcp") -> dict:
    """Read back what an execution changed against its plan (see module doc)."""
    record = ledger.get(evidence_id) if isinstance(evidence_id, str) and evidence_id else None
    if record is None or record.get("action") not in EXECUTIONS:
        return _reply(evidence_id, verification_id, error="unknown_evidence")
    if not record.get("success"):
        return _reply(evidence_id, verification_id, error="not_executed")
    saved = store.get(verification_id)
    template = saved.get("template") if saved else None
    params, problems = check_template(template, (saved or {}).get("params"))
    reason = store.state(saved) if not problems else "unknown_verification"     # a damaged file is no plan
    if reason is not None:
        return _reply(evidence_id, verification_id, template, error=reason)

    started = time.monotonic()
    warnings: list[str] = []
    try:
        await ensure_connected(client)
        current = await document_info(client, warnings)
    except Exception:  # noqa: BLE001 - nothing reached Revit: nothing to record, the plan stays
        return _reply(evidence_id, verification_id, template, error="revit_unreachable")
    planned = saved.get("document")
    if record.get("document") != planned or current != planned or not (planned or {}).get("title"):
        return _reply(evidence_id, verification_id, template, error="document_changed")

    validator, spec, cfg, keywords = validator_call(template, params)
    before: dict = {}
    if template == "count_delta":
        before = {"category": params.get("category"), "count": (saved.get("baseline") or {}).get("count")}
    result = {"ids": (record.get("result_summary") or {}).get("ids") or []}
    try:
        report = await validator.after(client, spec, cfg, before, result, **keywords)
    except OSError:
        return _reply(evidence_id, verification_id, template, error="revit_unreachable")
    except Exception as exc:  # noqa: BLE001 - a validator that cannot run is a failed check
        report = failed_report(template, f"validator could not run: {type(exc).__name__}: {exc}", before)
    if template == "count_delta":
        order = _order_check(record, saved)
        if order is not None:
            report = report.model_copy(update={"passed": False, "checks": [*report.checks, order]})

    _, reason = store.consume(verification_id, evidence_id)
    if reason is not None:                       # another caller redeemed it meanwhile
        return _reply(evidence_id, verification_id, template, error=reason)
    line = {
        "host": host,
        "scope": record.get("scope"),
        "action": ACTION,
        "tool": ACTION,
        "spec_hash": record.get("spec_hash"),
        "projection_hash": record.get("projection_hash"),
        "code_sha256": record.get("code_sha256"),
        "params": {"template": template, "params": params},
        "document": current,
        "success": report.passed,
        "error": None,
        "validation": report.model_dump(),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "warnings": warnings,
        "evidence_id": evidence_id,
        "verification_id": verification_id,
    }
    try:
        line_id = ledger.append(line)
    except OSError:
        line_id = None
    return _reply(evidence_id, verification_id, template, report=report, verified=report.passed,
                  verification_evidence_id=line_id)

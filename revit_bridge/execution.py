"""The execution flow as a package API, shared by every host (spec 8, phase 6 PR 6.0).

``run_pack`` and ``run_code`` run one confirmed execution end to end:

    verify token -> health -> render -> sandbox -> snapshot + preconditions ->
    validator.before -> consume token -> send_code -> validator.after ->
    record_usage -> evidence ledger

``revalidate`` re-runs a recorded execution's assertion against the model as
it is now. All dependencies (store, gate, ledger, client) are passed in, so
the MCP server and the web host construct their own and share the same
behaviour: the host bypass is read only from ``env`` (a host that passes
none has no bypass), the snapshot for the preconditions has a 5 s budget, a
failing ``validator.before`` refuses before the token is consumed, and every
refusal or exception after the token was consumed still leaves a ledger
line. ``scope`` is the device the execution runs on (``device_id`` on a
remote host) or ``"local"``: the token must have been issued for it, the
ledger line records it, and ``revalidate`` refuses a record from another
scope. ``client`` needs ``send_code(code, parameters)`` and
``send_command(method, params)``; when it also has ``ensure_connected()``,
that is awaited once, before any timed probe, and any exception from it
means nothing reached Revit: no token consumed, no ledger line.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from revit_bridge.capabilities.schema import evaluate_preconditions, precondition_categories
from revit_bridge.capabilities.store import SolidifiedTool, ToolStore
from revit_bridge.evidence.ledger import LOCAL_SCOPE, Ledger, code_fields, summarize_result
from revit_bridge.revit import sandbox
from revit_bridge.revit.settings import env_flag
from revit_bridge.snapshot.project import DEFAULT_CATEGORIES, take_snapshot
from revit_bridge.spec.gate import Confirmation, Gate, GateError, confirmation_invalid, confirmation_required
from revit_bridge.spec.models import Action, ParamBinding, Source, TaskSpec, projection_hash
from revit_bridge.validators.base import ValidationReport, ValidatorError, failed_report
from revit_bridge.validators.builtin import get_validator

# Hosts that run their own confirmation flow may lift the gate; only an
# environment the host passes in explicitly is consulted.
ENV_ALLOW_UNCONFIRMED = "REVIT_BRIDGE_ALLOW_UNCONFIRMED"

PRECONDITION_SNAPSHOT_TIMEOUT = 5.0
DOCUMENT_PROBE = 'return new { Title = document.Title, RevitVersion = document.Application.VersionNumber };'
RETRY_HINT = (
    "If this tool fails repeatedly, write fresh code and use execute_code "
    "instead - the tool definition may be outdated."
)


class ExecutionResult(BaseModel):
    """What one execution came to; the same shape the MCP tools return as JSON."""
    success: bool
    tool: str | None = None
    result: Any = None
    error: str | None = None
    validation: ValidationReport | None = None
    evidence_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    preconditions_failed: list[str] = Field(default_factory=list)
    hint: str | None = None
    refusal: dict | None = None          # confirmation_required / confirmation_invalid payload as is


# -- the gate -------------------------------------------------------------------------

def unconfirmed_allowed(env: Mapping[str, str] | None) -> bool:
    """True when the host's environment lifts the gate. ``None`` never does."""
    return env is not None and env_flag(ENV_ALLOW_UNCONFIRMED, env)


def gate_refusal(gate: Gate, token: str, projection: dict, env: Mapping[str, str] | None = None,
                 consume: bool = False, scope: str = LOCAL_SCOPE) -> dict | None:
    """Check ``token`` for ``projection`` under ``scope``; the refusal payload when it must not run, else None.

    A token is a one-time credential issued by ``confirm_spec`` and bound to
    the hash of the execution projection and to the scope it was issued for,
    so a model cannot confirm one thing and run another, nor run it on
    another device. With ``consume=False`` the token is only verified; the
    flow calls again with ``consume=True`` right before dispatch, after every
    check that does not touch Revit, so a refused validation leaves the
    confirmation redeemable.
    """
    if unconfirmed_allowed(env):
        return None
    if not isinstance(token, str) or not token.strip():
        return confirmation_required()
    try:
        if consume:
            gate.consume(token.strip(), projection, scope)
        else:
            gate.verify(token.strip(), projection, scope)
    except GateError as exc:
        return confirmation_invalid(exc)
    return None


def _refused(refusal: dict, tool: str | None = None) -> ExecutionResult:
    """A refusal before anything reached Revit, its payload kept verbatim."""
    return ExecutionResult(
        success=False, tool=tool, error=str(refusal.get("error")), refusal=refusal,
        hint=refusal.get("hint") if isinstance(refusal.get("hint"), str) else None,
        warnings=list(refusal.get("warnings") or []),
    )


def _confirmation_of(gate: Gate, token: str) -> Confirmation | None:
    """The confirmation behind ``token`` (for the ledger); None under the host bypass."""
    if not isinstance(token, str) or not token.strip():
        return None
    return gate.peek(token.strip())


def spec_from_projection(projection: dict, filled: dict | None = None,
                         pack: SolidifiedTool | None = None) -> TaskSpec:
    """A TaskSpec carrying the confirmed values, for validators' {param} references.

    ``filled`` is the parameter set after ``validate_params`` added the pack's
    defaults: a validator may refer to a defaulted parameter the projection
    never mentions. Each binding carries the pack parameter's ``unit`` so a
    validator can convert lengths.
    """
    if projection.get("kind") == "run_tool":
        values = filled if filled is not None else (projection.get("params") or {})
        units = {p["name"]: p.get("unit") for p in (pack.parameters if pack else [])}
        return TaskSpec(
            task=f"run_tool {projection.get('tool')}",
            action=Action(kind="run_tool", tool=projection.get("tool")),
            parameters=[ParamBinding(name=k, value=v, unit=units.get(k), source=Source.answer,
                                     evidence="confirmed projection")
                        for k, v in values.items()],
        )
    return TaskSpec(task="execute_code", parameters=[],
                    action=Action(kind="execute_code", code=projection.get("code"),
                                  code_parameters=projection.get("parameters")))


# -- helpers around Revit ---------------------------------------------------------------

async def ensure_connected(client) -> None:
    """Open the client's connection now, outside every timer.

    A connect that never completes must surface as a transport failure here,
    not as a "probe timed out" warning inside the 5 s budget that would let
    the flow go on to consume the token. Clients without the hook (the web
    relay) connect on first use.
    """
    ensure = getattr(client, "ensure_connected", None)
    if ensure is not None:
        await ensure()


async def document_info(client, warnings: list[str]) -> dict:
    """``{title, revit_version}`` of the open document, for the ledger.

    A transport failure (``OSError``: refused, reset, closed) propagates -
    nothing reached Revit and the caller must not consume the token.
    """
    try:
        resp = await asyncio.wait_for(client.send_code(DOCUMENT_PROBE), timeout=PRECONDITION_SNAPSHOT_TIMEOUT)
        if resp.success and isinstance(resp.result, dict):
            return {"title": resp.result.get("Title"), "revit_version": resp.result.get("RevitVersion")}
        warnings.append(f"document: {resp.error or 'no result'}")
    except asyncio.TimeoutError:
        warnings.append("document: probe timed out")
    except OSError:
        raise
    except Exception as exc:  # noqa: BLE001 - the ledger line is still written
        warnings.append(f"document: {type(exc).__name__}: {exc}")
    return {"title": None, "revit_version": None}


async def preconditions(client, pack: SolidifiedTool, warnings: list[str]) -> tuple[list[str], dict]:
    """Evaluate the pack's preconditions on a fresh snapshot (5 s budget).

    Returns (failures, document). A snapshot that times out or fails skips the
    evaluation with a warning: the execution is not refused for what could
    not be checked.
    """
    if not any(isinstance(item, dict) and "kind" in item for item in pack.preconditions or []):
        return [], await document_info(client, warnings)
    categories = list(DEFAULT_CATEGORIES)
    for cat in precondition_categories(pack):
        if cat not in categories:
            categories.append(cat)
    try:
        snapshot = await asyncio.wait_for(take_snapshot(client, categories), timeout=PRECONDITION_SNAPSHOT_TIMEOUT)
    except asyncio.TimeoutError:
        warnings.append(f"preconditions skipped: snapshot timed out ({PRECONDITION_SNAPSHOT_TIMEOUT:g} s)")
        return [], await document_info(client, warnings)
    except OSError:
        raise                                  # transport: nothing reached Revit
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"preconditions skipped: {type(exc).__name__}: {exc}")
        return [], await document_info(client, warnings)
    warnings.extend(f"snapshot: {w}" for w in snapshot.warnings)
    document = {"title": snapshot.document.get("title"), "revit_version": snapshot.document.get("revit_version")}
    return evaluate_preconditions(pack, snapshot), document


def _record(ledger: Ledger, *, host: str, scope: str, bypass: bool, action: str, tool: SolidifiedTool | None,
            projection: dict, conf: Confirmation | None, code: str | None, document: dict, resp,
            validation: ValidationReport | None, success: bool, error: str | None, duration_ms: int,
            preconditions_failed: list[str], warnings: list[str]) -> str | None:
    """Append the ledger line; a ledger that cannot be written costs a warning, not the result."""
    record = {
        "host": host,
        "scope": scope,
        "action": action,
        "tool": tool.name if tool else None,
        "tool_version": tool.version if tool else None,
        "spec_hash": conf.spec_hash if conf else None,
        "projection_hash": projection_hash(projection),
        "token_prefix": conf.token[:6] if conf else None,
        "confirmed_by": conf.confirmed_by if conf else ("host_bypass" if bypass else None),
        "channel": conf.channel if conf else None,
        "params": projection.get("params") if action == "run_tool" else {"parameters": projection.get("parameters") or []},
        **code_fields(code if action == "execute_code" else None),
        "document": document,
        "success": success,
        "error": error,
        "result_summary": summarize_result(resp.result if resp is not None else None),
        "validation": validation.model_dump() if validation else None,
        "duration_ms": duration_ms,
        "preconditions_failed": preconditions_failed,
        "warnings": list(warnings),
    }
    try:
        return ledger.append(record)
    except OSError as exc:
        warnings.append(f"evidence not recorded: {exc}")
        return None


def _validation_error(kind: str, exc: Exception, before: dict) -> ValidationReport:
    return failed_report(kind, f"validator could not run: {type(exc).__name__}: {exc}", before)


# -- run_pack ---------------------------------------------------------------------------------

async def run_pack(*, store: ToolStore, gate: Gate, ledger: Ledger, client, name: str, params: dict,
                   token: str, host: str = "mcp", env: Mapping[str, str] | None = None,
                   scope: str = LOCAL_SCOPE) -> ExecutionResult:
    """Run capability pack ``name`` with ``params`` under confirmation ``token`` on ``scope``.

    ``success`` means Revit succeeded AND the pack's validator (if any)
    passed; a failed assertion is ``success: false`` with
    ``error: "validation_failed"`` and the result attached.
    """
    if not isinstance(params, dict):
        return _refused({"success": False, "error": "params must be a JSON object"}, name)
    projection = {"kind": "run_tool", "tool": name, "params": params}
    refusal = gate_refusal(gate, token, projection, env, scope=scope)
    if refusal:
        return _refused(refusal, name)

    # Health check - refuse a stale or failing tool
    health = store.health_check(name)
    if health["status"] == "not_found":
        return _refused({"success": False, "error": f"Tool '{name}' not found."}, name)
    if health["recommendation"] == "write_new_code":
        return _refused({
            "success": False,
            "error": f"Tool '{name}' is unhealthy: {'; '.join(health['issues'])}. "
                     f"Write fresh code and use execute_code instead.",
            "health": health,
        }, name)

    code, errors = store.render(name, params)
    if code is None:
        return _refused({"success": False, "error": f"Parameter validation failed: {'; '.join(errors)}"}, name)

    # Security review of the fully rendered code before dispatch (P0-2)
    safe, review_warnings = sandbox.review(code)
    if not safe:
        return _refused({"success": False, "error": "blocked", "warnings": review_warnings}, name)

    # Spec 8 flow: preconditions -> validator.before -> consume token -> execute
    # -> validator.after -> ledger.
    pack = store.load(name)
    _, _, filled = store.validate_params(name, params)      # defaults included, for the validator
    spec = spec_from_projection(projection, filled, pack)
    bypass = unconfirmed_allowed(env)
    warnings: list[str] = []
    started = time.monotonic()
    validator = None
    if pack.validator:
        try:
            validator = get_validator(str(pack.validator.get("kind")))
        except ValidatorError as e:
            warnings.append(f"validator: {e}")
    before: dict = {}

    try:
        await ensure_connected(client)
        failed, document = await preconditions(client, pack, warnings)
    except Exception as e:                     # noqa: BLE001 - nothing reached Revit: nothing to record
        store.record_usage(name, success=False)
        return ExecutionResult(success=False, tool=name, error=str(e) or type(e).__name__, warnings=warnings)

    def refused(error: str) -> ExecutionResult:
        """A refusal before the token is consumed: no execution, but a ledger line
        so evidence() shows why the run did not happen."""
        evidence_id = _record(
            ledger, host=host, scope=scope, bypass=bypass, action="run_tool", tool=pack, projection=projection,
            conf=_confirmation_of(gate, token), code=None, document=document, resp=None, validation=None,
            success=False, error=error, duration_ms=int((time.monotonic() - started) * 1000),
            preconditions_failed=failed, warnings=warnings,
        )
        return ExecutionResult(success=False, tool=name, error=error, preconditions_failed=failed,
                               evidence_id=evidence_id, warnings=warnings)

    if failed:
        return refused("preconditions_failed")
    if validator is not None:
        try:
            before = await validator.before(client, spec, pack.validator)
        except Exception as e:  # noqa: BLE001
            # Without the sample, `after` could only fail; running anyway would
            # invite a retry that duplicates the work. Refuse like a precondition.
            warnings.append(f"validator.before: {type(e).__name__}: {e}")
            return refused("validator_before_failed")
    refusal = gate_refusal(gate, token, projection, env, consume=True, scope=scope)
    if refusal:
        return _refused(refusal, name)
    conf = _confirmation_of(gate, token)
    try:
        resp = await client.send_code(code)
    except Exception as e:
        # Revit may or may not have run it: exactly the case that needs a ledger line
        store.record_usage(name, success=False)
        evidence_id = _record(
            ledger, host=host, scope=scope, bypass=bypass, action="run_tool", tool=pack, projection=projection, conf=conf,
            code=None, document=document, resp=None, validation=None, success=False, error=str(e),
            duration_ms=int((time.monotonic() - started) * 1000), preconditions_failed=[], warnings=warnings,
        )
        return ExecutionResult(success=False, tool=name, error=str(e), evidence_id=evidence_id, warnings=warnings)

    validation: ValidationReport | None = None
    if validator is not None and resp.success:
        try:
            validation = await validator.after(client, spec, pack.validator, before, resp.result)
        except Exception as e:  # noqa: BLE001 - a validator that cannot run is a failed validation
            validation = _validation_error(validator.kind, e, before)
    success = bool(resp.success) and (validation is None or validation.passed)
    error = resp.error if not resp.success else ("validation_failed" if validation and not validation.passed else None)
    store.record_usage(name, success=success)
    duration_ms = int((time.monotonic() - started) * 1000)
    evidence_id = _record(
        ledger, host=host, scope=scope, bypass=bypass, action="run_tool", tool=pack, projection=projection, conf=conf,
        code=None, document=document, resp=resp, validation=validation, success=success, error=error,
        duration_ms=duration_ms, preconditions_failed=[], warnings=warnings,
    )
    return ExecutionResult(
        success=success, tool=name, result=resp.result, error=error, validation=validation,
        evidence_id=evidence_id, warnings=warnings, hint=None if resp.success else RETRY_HINT,
    )


# -- run_code -----------------------------------------------------------------------------------

async def run_code(*, gate: Gate, ledger: Ledger, client, code: str, parameters: list | None, token: str,
                   host: str = "mcp", env: Mapping[str, str] | None = None,
                   scope: str = LOCAL_SCOPE) -> ExecutionResult:
    """Run C# ``code`` under confirmation ``token`` on ``scope``. No pack, so no preconditions and no validator."""
    projection = {"kind": "execute_code", "code": code, "parameters": list(parameters or [])}
    refusal = gate_refusal(gate, token, projection, env, scope=scope)
    if refusal:
        return _refused(refusal)
    # Security review - always enforced before dispatch (P0-2)
    safe, review_warnings = sandbox.review(code)
    if not safe:
        return _refused({"success": False, "error": "blocked", "warnings": review_warnings})
    bypass = unconfirmed_allowed(env)
    warnings: list[str] = []
    started = time.monotonic()
    try:
        await ensure_connected(client)
        document = await document_info(client, warnings)
    except Exception as e:                     # noqa: BLE001 - nothing reached Revit: nothing to record
        return ExecutionResult(success=False, error=str(e) or type(e).__name__, warnings=warnings)
    refusal = gate_refusal(gate, token, projection, env, consume=True, scope=scope)   # the last step before Revit
    if refusal:
        return _refused(refusal)
    conf = _confirmation_of(gate, token)
    try:
        resp = await client.send_code(code, parameters)
    except Exception as e:
        # Revit may or may not have run it: exactly the case that needs a ledger line
        evidence_id = _record(
            ledger, host=host, scope=scope, bypass=bypass, action="execute_code", tool=None, projection=projection,
            conf=conf,
            code=code, document=document, resp=None, validation=None, success=False, error=str(e),
            duration_ms=int((time.monotonic() - started) * 1000), preconditions_failed=[], warnings=warnings,
        )
        return ExecutionResult(success=False, error=str(e), evidence_id=evidence_id, warnings=warnings)
    duration_ms = int((time.monotonic() - started) * 1000)
    evidence_id = _record(
        ledger, host=host, scope=scope, bypass=bypass, action="execute_code", tool=None, projection=projection,
        conf=conf, code=code, document=document, resp=resp, validation=None, success=resp.success, error=resp.error,
        duration_ms=duration_ms, preconditions_failed=[], warnings=warnings,
    )
    return ExecutionResult(success=resp.success, result=resp.result, error=resp.error,
                           evidence_id=evidence_id, warnings=warnings)


# -- revalidate -----------------------------------------------------------------------------------

async def revalidate(*, store: ToolStore, ledger: Ledger, client, evidence_id: str,
                     scope: str | None = None) -> dict:
    """Re-run the validator's assertion for a recorded execution against the model as it is now.

    Returns the ValidationReport (plus ``evidence_id`` and ``tool``) or
    ``{"error": ...}`` when the record, its pack or a validator is missing,
    or (``scope_mismatch``) when ``scope`` is given and the record belongs
    to another one. Transport failures propagate to the caller.
    """
    record = ledger.get(evidence_id)
    if record is None:
        return {"error": "unknown_evidence", "evidence_id": evidence_id}
    if scope is not None and record.get("scope") != scope:
        return {"error": "scope_mismatch", "evidence_id": evidence_id}
    if record.get("action") != "run_tool" or not record.get("tool"):
        return {"error": "no_validator", "message": "only run_tool executions carry a validator"}
    pack = store.load(record["tool"])
    if pack is None or not pack.validator:
        return {"error": "no_validator", "message": f"pack {record['tool']!r} has no validator now"}
    try:
        validator = get_validator(str(pack.validator.get("kind")))
    except ValidatorError as e:
        return {"error": "no_validator", "message": str(e)}
    projection = {"kind": "run_tool", "tool": record["tool"], "params": record.get("params") or {}}
    _, _, filled = store.validate_params(record["tool"], projection["params"])
    spec = spec_from_projection(projection, filled, pack)
    before = (record.get("validation") or {}).get("before") or {}
    result = {"ids": (record.get("result_summary") or {}).get("ids") or []}
    try:
        report = await validator.after(client, spec, pack.validator, before, result)
    except OSError:
        raise
    except Exception as e:  # noqa: BLE001
        report = _validation_error(validator.kind, e, before)
    return {"evidence_id": evidence_id, "tool": record["tool"], **report.model_dump()}

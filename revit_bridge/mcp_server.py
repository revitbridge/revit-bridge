"""
revit-bridge MCP server - code execution and capability packs for Revit.

Usage:
    revit-bridge            run the MCP server on stdio (what hosts call)
    revit-bridge check      ping the Revit add-in and print the connection status
    python -m revit_bridge.mcp_server

Tools:
    - get_project_snapshot : read-only picture of the open model (no gate)
    - query                : read-only model queries by kind (no gate)
    - missing_params       : the questions a pack still needs answered
    - reconcile            : a draft TaskSpec against a snapshot
    - confirm_spec         : validate a TaskSpec, issue a confirmation token
    - execute_code      : send C# code to Revit (confirmation token)
    - solidify_tool     : save successful code as a reusable named tool
    - list_tools        : list all solidified tools
    - get_tool_choices  : query Revit for a tool's dynamic parameter choices
    - run_tool          : execute a solidified tool (confirmation token)
    - evidence          : recent execution records from the ledger
    - validate          : re-run a recorded execution's validator now

The server never calls a model: the host does. Connection settings come from
``REVIT_BRIDGE_*`` environment variables (see README, Configure).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Mapping

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from revit_bridge import __version__
from revit_bridge.capabilities.schema import evaluate_preconditions, precondition_categories
from revit_bridge.capabilities.store import SolidifiedTool, ToolStore
from revit_bridge.evidence.ledger import Ledger, code_fields, summarize_result
from revit_bridge.revit import sandbox
from revit_bridge.revit.client import PING_PROBE, RevitClient
from revit_bridge.revit.pool import RevitClientPool
from revit_bridge.revit.settings import RevitSettings, env_flag
from revit_bridge.snapshot.project import DEFAULT_CATEGORIES, ProjectSnapshot, take_snapshot, validate_categories
from revit_bridge.snapshot.query import QUERY_KINDS, RevitQueryExecutor, run_query
from revit_bridge.spec.gate import Confirmation, Gate, GateError, confirmation_invalid, confirmation_required
from revit_bridge.spec.models import Action, ParamBinding, Source, TaskSpec, projection_hash
from revit_bridge.spec.rules import missing_params as _missing_params
from revit_bridge.spec.rules import reconcile as _reconcile
from revit_bridge.spec.rules import validate_spec
from revit_bridge.validators.base import ValidationReport, ValidatorError
from revit_bridge.validators.builtin import get_validator

# Hosts that run their own confirmation flow (the web demo, until phase 6)
# may lift the gate.
ENV_ALLOW_UNCONFIRMED = "REVIT_BRIDGE_ALLOW_UNCONFIRMED"

_tool_store = ToolStore()
_gate = Gate()
_ledger = Ledger()

HOST_KIND = "mcp"                  # what this process writes into the ledger's "host"
PRECONDITION_SNAPSHOT_TIMEOUT = 5.0
DOCUMENT_PROBE = 'return new { Title = document.Title, RevitVersion = document.Application.VersionNumber };'


# -- Confirmation gate --------------------------------------------------------

def unconfirmed_allowed(env: Mapping[str, str] | None = None) -> bool:
    """True when ``REVIT_BRIDGE_ALLOW_UNCONFIRMED`` lifts the gate."""
    return env_flag(ENV_ALLOW_UNCONFIRMED, env)


def gate_refusal(token: str, projection: dict, env: Mapping[str, str] | None = None,
                 consume: bool = False) -> dict | None:
    """Check ``token`` for ``projection``; the refusal payload when it must not run, else None.

    A token is a one-time credential issued by ``confirm_spec`` and bound to
    the hash of the execution projection, so a model cannot confirm one
    thing and run another. With ``consume=False`` the token is only verified;
    the tools call again with ``consume=True`` right before dispatch, after
    every check that does not touch Revit, so a refused validation leaves
    the confirmation redeemable. Only the host bypass lifts the check.
    """
    if unconfirmed_allowed(env):
        return None
    if not isinstance(token, str) or not token.strip():
        return confirmation_required()
    try:
        if consume:
            _gate.consume(token.strip(), projection)
        else:
            _gate.verify(token.strip(), projection)
    except GateError as exc:
        return confirmation_invalid(exc)
    return None


def _parse_json_arg(value, what: str):
    """MCP hosts send objects; some send the JSON text. Accept both."""
    if isinstance(value, str):
        return json.loads(value)
    return value


def _confirmation_of(token: str) -> Confirmation | None:
    """The confirmation behind ``token`` (for the ledger); None under the host bypass."""
    if not isinstance(token, str) or not token.strip():
        return None
    return _gate.peek(token.strip())


def _spec_from_projection(projection: dict, filled: dict | None = None) -> TaskSpec:
    """A TaskSpec carrying the confirmed values, for validators' {param} references.

    ``filled`` is the parameter set after ``validate_params`` added the pack's
    defaults: a validator may refer to a defaulted parameter the projection
    never mentions.
    """
    if projection.get("kind") == "run_tool":
        values = filled if filled is not None else (projection.get("params") or {})
        return TaskSpec(
            task=f"run_tool {projection.get('tool')}",
            action=Action(kind="run_tool", tool=projection.get("tool")),
            parameters=[ParamBinding(name=k, value=v, source=Source.answer, evidence="confirmed projection")
                        for k, v in values.items()],
        )
    return TaskSpec(task="execute_code", parameters=[],
                    action=Action(kind="execute_code", code=projection.get("code"),
                                  code_parameters=projection.get("parameters")))


async def _document_info(client, warnings: list[str]) -> dict:
    try:
        resp = await asyncio.wait_for(client.send_code(DOCUMENT_PROBE), timeout=PRECONDITION_SNAPSHOT_TIMEOUT)
        if resp.success and isinstance(resp.result, dict):
            return {"title": resp.result.get("Title"), "revit_version": resp.result.get("RevitVersion")}
        warnings.append(f"document: {resp.error or 'no result'}")
    except Exception as exc:  # noqa: BLE001 - the ledger line is still written
        warnings.append(f"document: {type(exc).__name__}: {exc}")
    return {"title": None, "revit_version": None}


async def _preconditions(client, pack: SolidifiedTool, warnings: list[str]) -> tuple[list[str], dict]:
    """Evaluate the pack's preconditions on a fresh snapshot (5 s budget).

    Returns (failures, document). A snapshot that times out or fails skips the
    evaluation with a warning: the execution is not refused for what could
    not be checked.
    """
    if not any(isinstance(item, dict) and "kind" in item for item in pack.preconditions or []):
        return [], await _document_info(client, warnings)
    categories = list(DEFAULT_CATEGORIES)
    for cat in precondition_categories(pack):
        if cat not in categories:
            categories.append(cat)
    try:
        snapshot = await asyncio.wait_for(take_snapshot(client, categories), timeout=PRECONDITION_SNAPSHOT_TIMEOUT)
    except asyncio.TimeoutError:
        warnings.append(f"preconditions skipped: snapshot timed out ({PRECONDITION_SNAPSHOT_TIMEOUT:g} s)")
        return [], await _document_info(client, warnings)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"preconditions skipped: {type(exc).__name__}: {exc}")
        return [], await _document_info(client, warnings)
    warnings.extend(f"snapshot: {w}" for w in snapshot.warnings)
    document = {"title": snapshot.document.get("title"), "revit_version": snapshot.document.get("revit_version")}
    return evaluate_preconditions(pack, snapshot), document


def _record(*, action: str, tool: SolidifiedTool | None, projection: dict, conf: Confirmation | None,
            code: str | None, document: dict, resp, validation: ValidationReport | None,
            success: bool, error: str | None, duration_ms: int, preconditions_failed: list[str],
            warnings: list[str]) -> str | None:
    """Append the ledger line; a ledger that cannot be written costs a warning, not the result."""
    record = {
        "host": HOST_KIND,
        "action": action,
        "tool": tool.name if tool else None,
        "tool_version": tool.version if tool else None,
        "spec_hash": conf.spec_hash if conf else None,
        "projection_hash": projection_hash(projection),
        "token_prefix": conf.token[:6] if conf else None,
        "confirmed_by": conf.confirmed_by if conf else ("host_bypass" if unconfirmed_allowed() else None),
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
        return _ledger.append(record)
    except OSError as exc:
        warnings.append(f"evidence not recorded: {exc}")
        return None


def _validation_error(kind: str, exc: Exception, before: dict) -> ValidationReport:
    from revit_bridge.validators.base import failed_report
    return failed_report(kind, f"validator could not run: {type(exc).__name__}: {exc}", before)


# -- MCP Server ---------------------------------------------------------------

SERVER_INSTRUCTIONS = """\
You are connected to a running Autodesk Revit through the revit-bridge MCP server.
The server executes C# in Revit and stores reusable, parameterised tools. It does
not search documentation and does not generate code: you write the code.

## Tools

0. **get_project_snapshot** / **query** - read-only, no confirmation needed.
   Take a snapshot (document, units, active view, levels, grids, selection,
   links, phases, family types of the requested categories) before you
   interpret a request; use `query(kind, args)` for levels, grids,
   family_types, elements, selection, view_elements, units, counts. Never
   write C# for something these answer.
1. **list_tools** - solidified tools available for execution. Check here first;
   prefer `run_tool` over writing new code when a tool matches the task.
2. **get_tool_choices** - ask Revit for the real values of a tool's dynamic
   parameters (levels, family types, elements). MUST be called before `run_tool`
   for parameters with `choices_from` / `source: query:*`.
3. **run_tool** - execute a solidified tool with parameter values.
4. **execute_code** - send C# code to Revit. The code runs inside an
   ExternalEvent handler with `document` in scope and a Transaction already open
   (do NOT open your own). End with `return <object>;`.
5. **solidify_tool** - save code that worked as a named tool with parameters.

## Confirmation gate (token)

`execute_code` and `run_tool` refuse to run without a `token`. A token comes only
from `confirm_spec(spec)`: build a TaskSpec (task, action, every parameter with
its value, source and evidence, interpretations, snapshot_fingerprint), show the
designer the spec card, get an explicit confirmation, then call `confirm_spec`.
It validates the spec (every parameter sourced, choices from Revit, units and
range words confirmed as interpretations) and returns `{token, spec_hash,
expires_at, card}` or `{errors}`. The token is one-time, expires in 10 minutes
and is bound to the exact tool + parameters (or code) of the spec: running
anything else with it fails with `confirmation_invalid` / `mismatch`.
Use `missing_params(tool, known)` to get the questions still open and
`reconcile(spec, snapshot)` to check a draft against the model before asking
for confirmation.

## Parameter source protocol (prevents silent failures)

Every parameter value must come from one of: the user's own words, a tool result
(`get_tool_choices`, a query you executed), or an answer to a question you asked.
Tool parameters declare a `source`:

- `query:*` / `choices_from` - call `get_tool_choices` first. NEVER guess names.
- `interactive:*` - the user selects in Revit. NEVER fabricate element ids.
- `ask_user` - ask. Do not assume.
- `default` - use the declared default and say so.

Recognise your own rationalisations: "Level 1 is standard", "Generic - 200mm is
common", "I'll use (0,0,0)", "this is probably optional". Query or ask instead.

## Faithful reporting

After `execute_code` or `run_tool`, report exactly what the response says. If it
failed, say so and quote the error text. Do not claim success on an error, and do
not paraphrase errors. If a tool keeps failing, write fresh code with
`execute_code` instead of retrying the tool.

## Resources

- `revit://stats` - tool statistics.
- `revit://tools/{name}` - YAML definition of a solidified tool.
- `revit://connection-status` - whether the Revit add-in is reachable.

## Notes

- Code targets the Revit API of the running Revit (2026 by default).
- Units: Revit internal units are feet; convert millimetres with `/ 304.8`.
- The add-in listens on a local TCP port (default 127.0.0.1:18080, JSON-RPC 2.0).
"""

mcp = MCPServer(
    "revit-bridge",
    version=__version__,
    instructions=SERVER_INSTRUCTIONS,
)

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def _dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


# -- Read-only tools (no gate) ------------------------------------------------

@mcp.tool(annotations=_READ_ONLY)
async def get_project_snapshot(categories: list[str] | None = None) -> str:
    """Read-only picture of the open Revit model: document, units, active view,
    levels, grids, selection, links, phases and the family types of the given
    categories (default: walls, structural columns/framing, floors, doors,
    windows). Partial failures are listed in `warnings`; `fingerprint`
    identifies the model state for `reconcile`. Like every send_code, it runs
    inside a Revit transaction on the add-in side: it fails on read-only
    documents and leaves an undo entry (a no-transaction path is planned)."""
    try:
        cats = validate_categories(categories)
    except ValueError as e:
        return _dumps({"error": "invalid_category", "message": str(e)})
    try:
        client = await RevitClientPool.get_client()
        snapshot = await take_snapshot(client, cats)
    except OSError as e:                     # refused, timed out, reset: no add-in
        return _dumps({"error": "revit_unreachable", "message": str(e) or type(e).__name__})
    except Exception as e:                   # a bug in the snapshot itself, never "invalid_category"
        return _dumps({"error": "snapshot_failed", "message": f"{type(e).__name__}: {e}"})
    return snapshot.model_dump_json(indent=2)


@mcp.tool(annotations=_READ_ONLY)
async def query(kind: str, args: dict | None = None) -> str:
    """Read-only model query, no confirmation needed. kinds: levels, grids,
    family_types (args.categories), elements (args.category, args.limit<=200),
    selection, view_elements (args.limit<=200), units, counts (args.categories).
    Returns {"error": "unknown_kind", "kinds": [...]} for anything else. Runs
    inside a Revit transaction on the add-in side: fails on read-only
    documents and leaves an undo entry (a no-transaction path is planned)."""
    try:
        client = await RevitClientPool.get_client()
        return _dumps(await run_query(RevitQueryExecutor(client), kind, args))
    except OSError as e:
        return _dumps({"error": "revit_unreachable", "kind": kind, "message": str(e) or type(e).__name__})
    except Exception as e:
        return _dumps({"error": "query_failed", "kind": kind, "message": f"{type(e).__name__}: {e}"})


# -- TaskSpec tools (no gate) -------------------------------------------------

@mcp.tool(annotations=_READ_ONLY)
async def missing_params(tool: str, known: dict | str | None = None,
                         snapshot: dict | str | None = None, language: str = "zh") -> str:
    """The questions still open for a capability pack: one per required parameter
    not in `known` ({name: value}), with the real options (levels, family types)
    from `snapshot` - pass the one from get_project_snapshot, or omit it and the
    server takes one; when Revit cannot be reached the questions come without
    options. Returns a JSON list of {id, param, text, why, options, allow_other}."""
    pack = _tool_store.load(tool)
    if pack is None:
        return _dumps({"error": "unknown_tool", "tool": tool})
    try:
        known_values = _parse_json_arg(known, "known") or {}
    except json.JSONDecodeError as e:
        return _dumps({"error": "invalid_args", "message": f"known: {e}"})
    if not isinstance(known_values, dict):
        return _dumps({"error": "invalid_args", "message": "known must be an object {name: value}"})
    snap = None
    if snapshot is not None:
        try:
            snap = ProjectSnapshot.model_validate(_parse_json_arg(snapshot, "snapshot"))
        except (ValidationError, json.JSONDecodeError, TypeError) as e:
            return _dumps({"error": "invalid_snapshot", "message": str(e)})
    else:
        try:
            client = await RevitClientPool.get_client()
            snap = await take_snapshot(client)
        except Exception:  # noqa: BLE001 - best effort: questions still go out, without options
            snap = None
    return _dumps([q.model_dump() for q in _missing_params(pack, known_values, snap, language)])


@mcp.tool(annotations=_READ_ONLY)
async def reconcile(spec: dict | str, snapshot: dict | str | None = None) -> str:
    """Check a draft TaskSpec against the model: values that do not exist
    (levels, family types), missing parameters as questions, readings the
    designer must confirm (units, range words), a stale snapshot. Pass the
    snapshot from get_project_snapshot, or omit it and the server takes one.
    Returns {conflicts, questions, interpretations_required, ready}."""
    try:
        draft = TaskSpec.model_validate(_parse_json_arg(spec, "spec"))
    except (ValidationError, json.JSONDecodeError, TypeError) as e:
        return _dumps({"error": "invalid_spec", "message": str(e)})
    if snapshot is not None:
        try:
            snap = ProjectSnapshot.model_validate(_parse_json_arg(snapshot, "snapshot"))
        except (ValidationError, json.JSONDecodeError, TypeError) as e:
            return _dumps({"error": "invalid_snapshot", "message": str(e)})
    else:
        try:
            client = await RevitClientPool.get_client()
            snap = await take_snapshot(client)
        except OSError as e:                     # same mapping as get_project_snapshot
            return _dumps({"error": "revit_unreachable", "message": str(e) or type(e).__name__})
        except Exception as e:
            return _dumps({"error": "snapshot_failed", "message": f"{type(e).__name__}: {e}"})
    pack = _tool_store.load(draft.action.tool) if draft.action.kind == "run_tool" and draft.action.tool else None
    return _dumps(_reconcile(draft, snap, pack).model_dump())


@mcp.tool()
def confirm_spec(spec: dict | str, confirmed_by: str = "designer", channel: str = "chat") -> str:
    """Turn a designer-confirmed TaskSpec into a one-time execution token.
    Call only after the designer has seen the spec card and said yes.
    Validates the spec (spec 5.1 rules) and returns {token, spec_hash,
    expires_at, card}, or {errors: [{code, param, message}]} without a token."""
    try:
        parsed = TaskSpec.model_validate(_parse_json_arg(spec, "spec"))
    except (ValidationError, json.JSONDecodeError, TypeError) as e:
        return _dumps({"errors": [{"code": "invalid_spec", "param": None, "message": str(e)}]})
    pack = _tool_store.load(parsed.action.tool) if parsed.action.kind == "run_tool" and parsed.action.tool else None
    errors = validate_spec(parsed, pack)
    if errors:
        return _dumps({"errors": [e.model_dump() for e in errors]})
    conf = _gate.issue(parsed, confirmed_by=confirmed_by or "designer", channel=channel or "chat")
    return _dumps({
        "token": conf.token,
        "spec_hash": conf.spec_hash,
        "expires_at": conf.expires_at,
        "card": parsed.card(),
    })


# -- Execution Tools ----------------------------------------------------------

@mcp.tool(annotations=_MUTATING)
async def execute_code(code: str, parameters: list | None = None, token: str = "") -> str:
    """Send C# code to Revit for execution over the local TCP socket.
    Requires a token from confirm_spec for a TaskSpec whose action is
    execute_code with exactly this code and parameters.
    Returns execution result or error message."""
    projection = {"kind": "execute_code", "code": code, "parameters": list(parameters or [])}
    refusal = gate_refusal(token, projection)
    if refusal:
        return _dumps(refusal)
    # Security review - always enforced before dispatch (P0-2)
    safe, review_warnings = sandbox.review(code)
    if not safe:
        return _dumps({"success": False, "error": "blocked", "warnings": review_warnings})
    warnings: list[str] = []
    started = time.monotonic()
    try:
        client = await RevitClientPool.get_client()
        document = await _document_info(client, warnings)
        refusal = gate_refusal(token, projection, consume=True)   # the last step before Revit
        if refusal:
            return _dumps(refusal)
        conf = _confirmation_of(token)
        resp = await client.send_code(code, parameters)
    except Exception as e:
        return _dumps({"success": False, "error": str(e), "warnings": warnings})
    duration_ms = int((time.monotonic() - started) * 1000)
    evidence_id = _record(
        action="execute_code", tool=None, projection=projection, conf=conf, code=code,
        document=document, resp=resp, validation=None, success=resp.success, error=resp.error,
        duration_ms=duration_ms, preconditions_failed=[], warnings=warnings,
    )
    return _dumps({
        "success": resp.success,
        "result": resp.result,
        "error": resp.error,
        "validation": None,
        "evidence_id": evidence_id,
        "warnings": warnings,
    })


# -- Solidification Tools -----------------------------------------------------

@mcp.tool()
def solidify_tool(
    name: str,
    code: str,
    description: str = "",
    parameters: list | str | None = None,
    source_query: str = "",
    validator: dict | str | None = None,
) -> str:
    """Save code that worked as a reusable capability pack (v1) in the user directory.
    parameters: list of {name, type, description, source (designer | tool:<query> |
    answer | default), required, unit?, default?, choices_from?}.
    validator: optional {kind: created_ids | count_delta | param_equals, category, ...}.
    Returns {name, path, version} or {error, problems}."""
    try:
        params = _parse_json_arg(parameters, "parameters") or []
        validator_cfg = _parse_json_arg(validator, "validator")
    except json.JSONDecodeError as e:
        return _dumps({"error": "invalid_args", "message": str(e)})
    if not isinstance(params, list):
        return _dumps({"error": "invalid_args", "message": "parameters must be a list"})
    try:
        tool = _tool_store.solidify(
            name=name, code=code, description=description, parameters=params,
            source_query=source_query, validator=validator_cfg,
        )
    except ValueError as e:
        return _dumps({"error": "invalid_pack", "problems": str(e).split("; ")})
    return _dumps({"name": tool.name, "path": str(_tool_store._tool_path(name)), "version": tool.version})


@mcp.tool(annotations=_READ_ONLY)
def list_tools() -> str:
    """The capability packs available to run_tool, as a JSON list of
    {name, description, version, parameters: [{name, type, source, required, unit?}],
    preconditions, validator, used}. Check here before writing code."""
    tools = _tool_store.list_tools()
    return _dumps([
        {
            "name": t.name,
            "description": t.description,
            "version": t.version,
            "parameters": [
                {k: p[k] for k in ("name", "type", "source", "required", "unit", "choices_from", "default") if k in p}
                for p in t.parameters
            ],
            "preconditions": t.preconditions,
            "validator": (t.validator or {}).get("kind"),
            "used": t.execution_count,
        }
        for t in tools
    ])


@mcp.tool(annotations=_READ_ONLY)
async def get_tool_choices(name: str) -> str:
    """Query Revit for dynamic parameter choices of a solidified tool.
    Call this BEFORE run_tool to discover available levels, family types, etc.
    Returns {param_name: [{label, value}, ...]} for parameters that need selection."""
    dynamic_params = _tool_store.get_dynamic_params(name)
    if not dynamic_params:
        return _dumps({"message": f"Tool '{name}' has no dynamic parameters"})

    try:
        client = await RevitClientPool.get_client()
        choices = await RevitQueryExecutor(client).get_tool_choices(dynamic_params)
        return _dumps(choices)
    except Exception as e:
        return _dumps({"success": False, "error": str(e)})


@mcp.tool(annotations=_MUTATING)
async def run_tool(name: str, params: str = "{}", token: str = "") -> str:
    """Execute a solidified tool by name with given parameters.
    IMPORTANT: Call get_tool_choices first for parameters with choices_from / source: tool:*.
    Requires a token from confirm_spec for a TaskSpec whose action is run_tool
    with exactly this tool and these parameter values.
    params: JSON object of parameter values, e.g. {"level_name": "L1", "height": 3000}"""
    try:
        param_dict = json.loads(params) if params else {}
    except json.JSONDecodeError:
        return _dumps({"success": False, "error": f"Invalid params JSON: {params}"})
    if not isinstance(param_dict, dict):
        return _dumps({"success": False, "error": "params must be a JSON object"})
    projection = {"kind": "run_tool", "tool": name, "params": param_dict}
    refusal = gate_refusal(token, projection)
    if refusal:
        return _dumps(refusal)

    # Health check - warn if tool is stale or failing
    health = _tool_store.health_check(name)
    if health["status"] == "not_found":
        return _dumps({"success": False, "error": f"Tool '{name}' not found."})
    if health["recommendation"] == "write_new_code":
        return _dumps({
            "success": False,
            "error": f"Tool '{name}' is unhealthy: {'; '.join(health['issues'])}. "
                     f"Write fresh code and use execute_code instead.",
            "health": health,
        })

    code, errors = _tool_store.render(name, param_dict)
    if code is None:
        return _dumps({
            "success": False,
            "error": f"Parameter validation failed: {'; '.join(errors)}",
        })

    # Security review of the fully rendered code before dispatch (P0-2)
    safe, review_warnings = sandbox.review(code)
    if not safe:
        return _dumps({"success": False, "error": "blocked", "warnings": review_warnings})

    # Spec 8 flow: preconditions -> validator.before -> consume token -> execute
    # -> validator.after -> ledger. success means Revit succeeded AND the
    # validator (if any) passed.
    pack = _tool_store.load(name)
    _, _, filled = _tool_store.validate_params(name, param_dict)   # defaults included, for the validator
    spec = _spec_from_projection(projection, filled)
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
        client = await RevitClientPool.get_client()
        failed, document = await _preconditions(client, pack, warnings)
        if failed:
            return _dumps({"success": False, "tool": name, "error": "preconditions_failed",
                           "preconditions_failed": failed, "warnings": warnings})
        if validator is not None:
            try:
                before = await validator.before(client, spec, pack.validator)
            except (ValidatorError, Exception) as e:  # noqa: BLE001
                warnings.append(f"validator.before: {type(e).__name__}: {e}")
        refusal = gate_refusal(token, projection, consume=True)
        if refusal:
            return _dumps(refusal)
        conf = _confirmation_of(token)
        resp = await client.send_code(code)
    except Exception as e:
        _tool_store.record_usage(name, success=False)
        return _dumps({"success": False, "tool": name, "error": str(e), "warnings": warnings})

    validation: ValidationReport | None = None
    if validator is not None and resp.success:
        try:
            validation = await validator.after(client, spec, pack.validator, before, resp.result)
        except Exception as e:  # noqa: BLE001 - a validator that cannot run is a failed validation
            validation = _validation_error(validator.kind, e, before)
    success = bool(resp.success) and (validation is None or validation.passed)
    error = resp.error if not resp.success else ("validation_failed" if validation and not validation.passed else None)
    _tool_store.record_usage(name, success=success)
    duration_ms = int((time.monotonic() - started) * 1000)
    evidence_id = _record(
        action="run_tool", tool=pack, projection=projection, conf=conf, code=None,
        document=document, resp=resp, validation=validation, success=success, error=error,
        duration_ms=duration_ms, preconditions_failed=[], warnings=warnings,
    )
    result = {
        "success": success,
        "tool": name,
        "result": resp.result,
        "error": error,
        "validation": validation.model_dump() if validation else None,
        "evidence_id": evidence_id,
        "warnings": warnings,
    }
    if not resp.success:
        result["hint"] = (
            "If this tool fails repeatedly, write fresh code and use execute_code "
            "instead - the tool definition may be outdated."
        )
    return _dumps(result)


# -- Evidence tools -----------------------------------------------------------

@mcp.tool(annotations=_READ_ONLY)
def evidence(limit: int = 20, tool: str | None = None) -> str:
    """The most recent execution records from the evidence ledger, newest first
    (optionally for one tool): who confirmed what, what ran, what the validator said."""
    limit = max(1, min(int(limit or 20), 200))
    return _dumps(_ledger.recent(limit, tool))


@mcp.tool(annotations=_READ_ONLY)
async def validate(evidence_id: str) -> str:
    """Re-run the validator's assertion for a recorded execution against the model
    as it is now (for the designer's later check). Returns the ValidationReport,
    or {error} when the record, its pack or a validator is missing."""
    record = _ledger.get(evidence_id)
    if record is None:
        return _dumps({"error": "unknown_evidence", "evidence_id": evidence_id})
    if record.get("action") != "run_tool" or not record.get("tool"):
        return _dumps({"error": "no_validator", "message": "only run_tool executions carry a validator"})
    pack = _tool_store.load(record["tool"])
    if pack is None or not pack.validator:
        return _dumps({"error": "no_validator", "message": f"pack {record['tool']!r} has no validator now"})
    try:
        validator = get_validator(str(pack.validator.get("kind")))
    except ValidatorError as e:
        return _dumps({"error": "no_validator", "message": str(e)})
    projection = {"kind": "run_tool", "tool": record["tool"], "params": record.get("params") or {}}
    _, _, filled = _tool_store.validate_params(record["tool"], projection["params"])
    spec = _spec_from_projection(projection, filled)
    before = (record.get("validation") or {}).get("before") or {}
    result = {"ids": (record.get("result_summary") or {}).get("ids") or []}
    try:
        client = await RevitClientPool.get_client()
        report = await validator.after(client, spec, pack.validator, before, result)
    except OSError as e:
        return _dumps({"error": "revit_unreachable", "message": str(e) or type(e).__name__})
    except Exception as e:  # noqa: BLE001
        report = _validation_error(validator.kind, e, before)
    return _dumps({"evidence_id": evidence_id, "tool": record["tool"], **report.model_dump()})


# -- Resources ----------------------------------------------------------------

@mcp.resource("revit://stats")
def api_stats() -> str:
    """Solidified tool statistics."""
    tools = _tool_store.list_tools()
    return (
        f"revit-bridge {__version__}\n"
        f"Solidified tools: {len(tools)} "
        f"(built-in: {_tool_store.builtin_dir}; user: {_tool_store.user_dir})"
    )


@mcp.resource("revit://tools/{name}")
def tool_resource(name: str) -> str:
    """Returns the YAML definition of a solidified tool by name."""
    tool_path = _tool_store.path_of(name)
    if tool_path is None:
        return json.dumps({"error": f"Tool '{name}' not found."})
    return tool_path.read_text(encoding="utf-8")


@mcp.resource("revit://evidence/recent")
def evidence_recent() -> str:
    """The 20 most recent evidence records."""
    return _dumps(_ledger.recent(20))


@mcp.resource("revit://connection-status")
async def connection_status() -> str:
    """Check whether the Revit add-in is reachable."""
    return json.dumps(await check_connection(), indent=2, ensure_ascii=False)


# -- check subcommand ---------------------------------------------------------

CHECK_PROBE = PING_PROBE


async def check_connection(settings: RevitSettings | None = None) -> dict:
    """Open a fresh connection, read the open document's title, describe the outcome.

    The probe is a read-only snippet rather than ``say_hello`` (which pops a
    dialog in Revit). ``reachable`` is true as soon as the add-in answers at
    all; what it answered is reported separately: ``document`` is the title
    of the open document, ``document_error`` the add-in's message when the
    probe could not run (no document open, wrong token, ...). ``error``
    holds transport failures only.
    """
    settings = settings or RevitSettings.from_env()
    status = settings.describe()
    status["document"] = None
    status["document_error"] = None
    client = RevitClient(settings=settings)
    try:
        await client.connect()
        resp = await client.send_code(CHECK_PROBE)
        status["reachable"] = bool(resp.success or resp.raw)   # raw: a reply came back
        status["error"] = None if status["reachable"] else resp.error
        if resp.success and isinstance(resp.result, str):
            status["document"] = resp.result
        elif status["reachable"]:
            status["document_error"] = resp.error or "probe returned no title"
    except Exception as exc:
        status["reachable"] = False
        status["error"] = str(exc) or type(exc).__name__
    finally:
        await client.disconnect()
    status["status"] = "connected" if status["reachable"] else "disconnected"
    return status


# -- Entry Point --------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="revit-bridge",
        description="MCP server bridging designers, AI hosts and a running Revit.",
    )
    parser.add_argument("--version", action="version", version=f"revit-bridge {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server on stdio (default)")
    sub.add_parser("check", help="ping the Revit add-in and print the connection status")
    args = parser.parse_args(argv)

    if args.command == "check":
        status = asyncio.run(check_connection())
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return 0 if status["reachable"] else 1

    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())

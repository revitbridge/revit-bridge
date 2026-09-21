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
import os
import sys
from collections.abc import Mapping

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from revit_bridge import __version__
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.evidence.ledger import Ledger
from revit_bridge.execution import ENV_ALLOW_UNCONFIRMED, ExecutionResult, run_code, run_pack
from revit_bridge.execution import gate_refusal as _gate_refusal
from revit_bridge.execution import revalidate as _revalidate
from revit_bridge.revit.client import PING_PROBE, RevitClient
from revit_bridge.revit.pool import RevitClientPool
from revit_bridge.revit.settings import RevitSettings, env_flag
from revit_bridge.snapshot.project import ProjectSnapshot, take_snapshot, validate_categories
from revit_bridge.snapshot.query import QUERY_KINDS, RevitQueryExecutor, run_query
from revit_bridge.spec.gate import Gate
from revit_bridge.spec.models import TaskSpec
from revit_bridge.spec.rules import missing_params as _missing_params
from revit_bridge.spec.rules import reconcile as _reconcile
from revit_bridge.spec.rules import validate_spec

_tool_store = ToolStore()
_gate = Gate()
_ledger = Ledger()


# -- Confirmation gate --------------------------------------------------------

def unconfirmed_allowed(env: Mapping[str, str] | None = None) -> bool:
    """True when ``REVIT_BRIDGE_ALLOW_UNCONFIRMED`` lifts the gate (this process's environment by default)."""
    return env_flag(ENV_ALLOW_UNCONFIRMED, env)


def gate_refusal(token: str, projection: dict, env: Mapping[str, str] | None = None,
                 consume: bool = False) -> dict | None:
    """The package gate check against this server's Gate; ``env`` defaults to the process environment."""
    return _gate_refusal(_gate, token, projection, os.environ if env is None else env, consume=consume)


def _parse_json_arg(value, what: str):
    """MCP hosts send objects; some send the JSON text. Accept both."""
    if isinstance(value, str):
        return json.loads(value)
    return value


class _PooledClient:
    """The pooled connection, opened on first use so refusals need no Revit."""

    async def send_code(self, code: str, parameters: list | None = None):
        client = await RevitClientPool.get_client()
        return await client.send_code(code, parameters)

    async def send_command(self, method: str, params: dict | None = None):
        client = await RevitClientPool.get_client()
        return await client.send_command(method, params)


def _execution_json(result: ExecutionResult) -> str:
    """The tool reply: a refusal payload as is, otherwise the ExecutionResult fields."""
    if result.refusal is not None:
        return _dumps(result.refusal)
    payload = result.model_dump(exclude={"refusal"})
    if payload["hint"] is None:
        del payload["hint"]
    return _dumps(payload)


# -- MCP Server ---------------------------------------------------------------

SERVER_INSTRUCTIONS = """\
You are connected to a running Autodesk Revit through revit-bridge 0.2. The server
runs capability packs and C# in Revit; it never calls a model. You turn the
designer's words into a TaskSpec in which every parameter has a source, get it
confirmed, run it with the token, and report what the validator found.

## Flow (always, in this order)

1. get_project_snapshot - what exists: document, units, levels, grids, family
   types, selection. Note `fingerprint`.
2. query(kind, args) for anything else read-only: levels, grids, family_types,
   elements, selection, view_elements, units, counts. No token needed. Never
   write C# for a read.
3. list_tools, then missing_params(tool, known) - the questions still open, with
   the real options (levels, types). Ask the designer all of them in one round.
4. reconcile(spec, snapshot) - a draft TaskSpec against the model: values that do
   not exist, ambiguous names, units and range words to confirm, stale snapshot.
   Repeat until `ready` is true.
5. Show the spec card; wait for an explicit yes.
6. confirm_spec(spec) -> {token, expires_at, card} or {errors}. Errors name the
   rule: missing_param, no_evidence, unsourced_choice, guessed_value,
   default_not_declared, bad_preference_ref, unconfirmed_interpretation,
   blocked_code. Fix the spec; never invent a token.
7. run_tool(name, params, token) or execute_code(code, parameters, token) with
   exactly the confirmed values. The token is one-time, expires in 10 minutes and
   is bound to that tool + params (or code): anything else is confirmation_invalid.
8. Read the reply. `success` is true only when Revit succeeded AND the pack's
   validator passed; validation_failed means the model did not change as claimed.
   Quote `validation.checks`, `error` and `evidence_id`; validate(evidence_id)
   re-runs the assertion later, evidence(limit, tool) lists past runs.

## TaskSpec

{task, action: {kind: run_tool|execute_code, tool|code}, parameters: [{name,
value, unit?, source, evidence}], interpretations: [{param?, text, confirmed}],
snapshot_fingerprint, language}. Sources: designer (evidence = their words),
tool (evidence = "tool:<name>"), answer (evidence = question id), preference
(evidence = "preference:<name>"), default (evidence = "default:<tool>", only
when the pack declares one). A number without a unit for a parameter that has
one, or a range word such as "on F2", is an interpretation the designer must
confirm.

## Never

- Guess a level, type, element id or coordinate: query, then ask.
- Set a value "for now", "as usual" or "probably": it is a question.
- Claim success on an error or on a failed validation; do not soften errors.
- Call run_tool / execute_code without a token from confirm_spec.

## Notes

- Revit internal units are feet; packs take millimetres. Code for execute_code
  runs inside an open transaction with `document` in scope; end with `return`.
- Read-only tools also run inside a transaction on the add-in: they fail on a
  read-only document.
- solidify_tool saves code that worked as a v1 pack (parameters with source,
  required, unit; optional validator). Resources: revit://stats,
  revit://tools/{name}, revit://evidence/recent, revit://connection-status.
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
    result = await run_code(gate=_gate, ledger=_ledger, client=_PooledClient(), code=code,
                            parameters=parameters, token=token, host="mcp", env=os.environ)
    return _execution_json(result)


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
    result = await run_pack(store=_tool_store, gate=_gate, ledger=_ledger, client=_PooledClient(), name=name,
                            params=param_dict, token=token, host="mcp", env=os.environ)
    return _execution_json(result)


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
    try:
        return _dumps(await _revalidate(store=_tool_store, ledger=_ledger, client=_PooledClient(),
                                        evidence_id=evidence_id))
    except OSError as e:
        return _dumps({"error": "revit_unreachable", "message": str(e) or type(e).__name__})


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

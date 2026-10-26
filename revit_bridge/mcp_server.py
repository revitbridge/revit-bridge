"""
revit-bridge MCP server - code execution and capability packs for Revit.

Usage:
    revit-bridge            run the MCP server on stdio (what hosts call)
    revit-bridge check      ping the Revit add-in and print the connection status,
                            its capability and environment
    revit-bridge guidance check|index|explain
                            validate the guidance layers, rebuild the generated
                            index, or show what get_guidance would recommend
                            (see revit_bridge.guidance_cli)
    python -m revit_bridge.mcp_server

Tools:
    - get_project_snapshot : read-only picture of the open model (no gate)
    - query                : read-only model queries by kind (no gate)
    - missing_params       : the questions a pack still needs answered
    - reconcile            : a draft TaskSpec against a snapshot
    - confirm_spec         : validate a TaskSpec, issue a confirmation token
    - check_code        : compile C# in Revit without running it (no gate)
    - execute_code      : send C# code to Revit (confirmation token)
    - solidify_tool     : save successful code as a reusable named tool
    - list_tools        : list all solidified tools
    - get_tool_choices  : query Revit for a tool's dynamic parameter choices
    - run_tool          : execute a solidified tool (confirmation token)
    - evidence          : recent execution records from the ledger
    - validate          : re-run a recorded execution's validator now
    - plan_verification : before an execute_code: pick a read-back template (no gate)
    - verify_execution  : after it: read the model back against the plan (no gate)
    - get_guidance      : lessons for writing Revit code, by stage (read-only)

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
from revit_bridge import checking, guidance_cli
from revit_bridge import guidance as _guidance
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.evidence.ledger import Ledger
from revit_bridge.execution import ENV_ALLOW_UNCONFIRMED, ExecutionResult, run_code, run_pack
from revit_bridge.execution import gate_refusal as _gate_refusal
from revit_bridge.execution import revalidate as _revalidate
from revit_bridge.instructions import SNIPPET_CONTRACT_SUMMARY, server_instructions
from revit_bridge.paths import guidance_dir, guidance_layer_dirs
from revit_bridge.revit.capabilities import Capabilities
from revit_bridge.revit.pool import RevitClientPool
from revit_bridge.revit.probe import CHECK_PROBE, check_connection  # noqa: F401 - re-exported
from revit_bridge.revit.settings import RevitSettings, env_flag
from revit_bridge.snapshot.project import ProjectSnapshot, take_snapshot, validate_categories
from revit_bridge.snapshot.query import QUERY_KINDS, RevitQueryExecutor, run_query
from revit_bridge.spec.gate import Gate
from revit_bridge.spec.models import TaskSpec
from revit_bridge.spec.rules import missing_params as _missing_params
from revit_bridge.spec.rules import reconcile as _reconcile
from revit_bridge.spec.rules import validate_spec
from revit_bridge.verification import VerificationStore
from revit_bridge.verification import plan as _plan_verification
from revit_bridge.verification import verify as _verify_execution

_tool_store = ToolStore()
_gate = Gate()
_ledger = Ledger()
_verifications = VerificationStore()               # <data root>/verifications, resolved at each call
_guidance_library: _guidance.Library | None = None   # loaded on first use, then kept


def guidance_library() -> _guidance.Library:
    """The guidance layers, validated on first use and cached; raises GuidanceError when the
    framework layer is invalid. The company, project and personal directories are read from
    the environment here, once per load (``paths.guidance_layer_dirs``); a bad one is left out
    with a ``layer_invalid`` warning."""
    global _guidance_library
    if _guidance_library is None:
        _guidance_library = _guidance.load_layers(guidance_dir(), **guidance_layer_dirs())
    return _guidance_library


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
    """The pooled connection, opened on first use so refusals need no Revit.

    ``ensure_connected`` lets the execution flow open it before any timed
    probe, so a connect that fails or never completes (or a bad setting such
    as REVIT_BRIDGE_PORT=abc) is "nothing reached Revit" rather than a probe
    timeout.
    """

    async def ensure_connected(self) -> None:
        await RevitClientPool.get_client()

    async def send_code(self, code: str, parameters: list | None = None, confirm: dict | None = None):
        client = await RevitClientPool.get_client()
        return await client.send_code(code, parameters, confirm=confirm)

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

# The instruction text lives in revit_bridge.instructions, shared with the
# host rendering (host_instructions) so the two cannot drift.
SERVER_INSTRUCTIONS = server_instructions()

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
    categories - BuiltInCategory OST_* names such as "OST_Walls", nothing else
    (default: walls, structural columns/framing, floors, doors,
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
    Categories are BuiltInCategory OST_* names, e.g. "OST_Walls" (not "Walls").
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
    not in `known` ({name: value}; a value of null or "" is not an answer, 0 and
    false are), with the real options (levels, family types)
    from `snapshot` - pass the one from get_project_snapshot, or omit it and the
    server takes one; when Revit cannot be reached the questions come without
    options and `why` says so - never invent options. Returns a JSON list of
    {id, param, text, why, options, allow_other, kind, blocks}: kind
    "user_intent" (the designer's to answer), blocks = the steps it holds up
    (["spec", "execute"]: go on with work that does not depend on it)."""
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
    Returns {conflicts, questions, interpretations_required, ready}. A value
    that matches several names is also a question of kind "user_intent"; one
    that does not exist is kind "model_fact": check the model (query) before
    asking the designer."""
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

def _with_snippet_contract(fn):
    """Put the snippet contract sentence - the summary of the revit-core/snippet-contract
    lesson, its one source - where the tool description says <snippet contract>."""
    fn.__doc__ = fn.__doc__.replace("<snippet contract>", f"Snippet contract: {SNIPPET_CONTRACT_SUMMARY}.")
    return fn


@mcp.tool(annotations=_READ_ONLY)
@_with_snippet_contract
async def check_code(code: str, parameters: list | None = None) -> str:
    """Compile C# for execute_code inside Revit WITHOUT running it: only
    compiles, never executes, needs no token or confirmation, changes nothing
    (same wrapper, references and blocked symbols as execute_code).
    <snippet contract> Call it
    before showing the card for execute_code and fix the code until
    `compiled` is true. Returns {schema_version, checked, compiled, blocked,
    capability, reason, diagnostics: [{id, severity, line, column, message,
    symbol, in_wrapper}], truncated, fingerprint, hints, guidance, environment,
    warnings}; line/column count from the first line of your code.
    `guidance` (get_guidance items): on errors, up to 2 lessons with `body` -
    read them before changing the code; a `truncated` one ends with the
    get_guidance(mode="read", ids=[...]) call for its full text. When it
    compiles: pointers (`summary`, no body) to read as needed.
    `checked: false` means NO local pre-compilation happened (older add-in,
    or the capability is unknown - see `reason`): say "not pre-compiled
    locally" on the card."""
    refused = checking.blocked_reply(code)            # the sandbox needs no Revit
    if refused is not None:
        return _dumps(refused)
    try:
        client = await RevitClientPool.get_client()
    except Exception as e:                            # refused, timed out, bad settings: not checked
        return _dumps(checking.not_checked(Capabilities.from_exception(e)))
    return _dumps(await checking.check_code(client, code, parameters, library=guidance_library))


@mcp.tool(annotations=_MUTATING)
@_with_snippet_contract
async def execute_code(code: str, parameters: list | None = None, token: str = "") -> str:
    """Send C# code to Revit for execution over the local TCP socket.
    <snippet contract>
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


# -- Read-back verification (execute_code) ------------------------------------

@mcp.tool(annotations=_READ_ONLY)
async def plan_verification(template: str, params: dict | str) -> str:
    """Plan how the result of an execute_code will be read back, BEFORE confirm_spec.
    Runs only the bridge's own read-only probes, never your code; no token.
    template / params (category: a BuiltInCategory OST_* name such as "OST_Walls"):
    - created_ids: {"category"} - every id the result returns (ElementId / Ids /
      ElementIds fields) exists and is of that category.
    - count_delta: {"category", "expected": int} - the number of instances of the
      category moves by exactly expected (negative for deletions); the count is
      sampled now, as the baseline.
    - param_equals: {"category", "checks": [{"param", "expected", "unit", "tolerance"}]}
      - on every id the result returns, parameter `param` (a BuiltInParameter name
      such as "WALL_USER_HEIGHT_PARAM", or a display name) equals `expected`;
      `unit` (mm | m | feet | null = mm) is the unit of `expected` for lengths,
      `tolerance` (in that unit; null = 0.5 mm for lengths, 1e-6 otherwise).
    The code must return the ids for created_ids and param_equals. No template
    fits: plan nothing and say the result will not be verified. Show the plan to
    the designer in one line under the spec card. Returns {verification_id,
    template, params, baseline: {count} | null, document, created_at, expires_at}
    (valid 30 minutes, for one verify_execution) or {error: "invalid_template",
    problems} | {error: "revit_unreachable" | "probe_failed" | "store_failed", message}."""
    try:
        params_value = _parse_json_arg(params, "params")
    except json.JSONDecodeError as e:
        return _dumps({"error": "invalid_template", "problems": [f"params: {e}"]})
    return _dumps(await _plan_verification(_verifications, _PooledClient(), template, params_value))


@mcp.tool(annotations=_READ_ONLY)
async def verify_execution(evidence_id: str, verification_id: str) -> str:
    """After execute_code: read the model back against the plan from plan_verification.
    Runs only the bridge's own read-only probes; no token. Returns {verified,
    template, checks: [{name, passed, detail}], before, after, evidence_id,
    verification_id, verification_evidence_id, error}. `verified` is true only when
    the read-back passed. `error` (plan not used, nothing recorded): unknown_evidence,
    not_executed (the execution failed), unknown_verification, verification_expired,
    verification_used, document_changed, revit_unreachable. Report exactly one of:
    executed and verified (verified true) / executed but verification failed (quote
    checks[].detail) / executed, not verified (no plan, or an error). The check is a
    ledger line of its own: evidence(tool="verify_execution")."""
    return _dumps(await _verify_execution(_verifications, _ledger, _PooledClient(), evidence_id,
                                          verification_id, host="mcp"))


# -- Guidance -----------------------------------------------------------------

@mcp.tool(annotations=_READ_ONLY)
def get_guidance(
    mode: str = "recommend",
    stage: str | None = None,
    skill_categories: list[str] | None = None,
    revit_categories: list[str] | None = None,
    symbols: list[str] | None = None,
    errors: list[str] | None = None,
    exceptions: list[str] | None = None,
    revit_version: str | None = None,
    ids: list[str] | None = None,
    seen_hashes: list[str] | None = None,
) -> str:
    """Lessons for writing Revit code, chosen by rules (no model), read-only, no token.
    mode "recommend" needs stage: orient (task start: the execute_code snippet
    contract, units, versions), write (before writing C#: pass symbols = the API
    type/member names in your code and revit_categories = OST_* names), fix
    (check_code failed: errors = diagnostic ids such as CS1061 plus symbols =
    the diagnostics' symbols; or exceptions = runtime exception type names),
    port, verify. skill_categories (e.g. "revit-geometry") picks a whole category.
    revit_version ("2026.5") drops lessons for other versions. seen_hashes skips
    bodies you already have. mode "index" lists every lesson without bodies;
    mode "read" returns the full text of ids. Returns JSON {schema_version, mode,
    stage, items: [{id, revision, hash, layer, category, title, kind, stage,
    revit, applicability, why, body, bytes, truncated}], omitted: [{id, title,
    reason}], missing, budget, warnings, error}."""
    try:
        library = guidance_library()
    except _guidance.GuidanceError as e:
        return _dumps({"error": "guidance_invalid", "problems": e.problems})
    return _dumps(_guidance.respond(
        library, mode, stage=stage, skill_categories=skill_categories, revit_categories=revit_categories,
        symbols=symbols, errors=errors, exceptions=exceptions, revit_version=revit_version, ids=ids,
        seen_hashes=seen_hashes,
    ))


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


@mcp.resource("revit://guidance/index")
def guidance_index() -> str:
    """Every guidance lesson, one line each: id | layer | revit range | title."""
    try:
        return _guidance.render_index(guidance_library())
    except _guidance.GuidanceError as e:
        return "".join(f"{line}\n" for line in ["guidance_invalid"] + [f"- {p}" for p in e.problems])


@mcp.resource("revit://connection-status")
async def connection_status() -> str:
    """Check whether the Revit add-in is reachable; with its capability and environment."""
    return json.dumps(await check_connection(environment=True), indent=2, ensure_ascii=False)


# -- check subcommand ---------------------------------------------------------
# check_connection lives in revit_bridge.revit.probe; it stays importable from
# here because the 0.1 web host imports it from this module.


# -- Entry Point --------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="revit-bridge",
        description="MCP server bridging designers, AI hosts and a running Revit.",
    )
    parser.add_argument("--version", action="version", version=f"revit-bridge {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server on stdio (default)")
    sub.add_parser("check", help="ping the Revit add-in and print the connection status, "
                                 "its capability and environment")
    guidance_cli.add_parser(sub)
    args = parser.parse_args(argv)

    if args.command == "guidance":
        return guidance_cli.run(args)

    if args.command == "check":
        status = asyncio.run(check_connection(environment=True))
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return 0 if status["reachable"] else 1

    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())

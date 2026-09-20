"""
revit-bridge MCP server - code execution and capability packs for Revit.

Usage:
    revit-bridge            run the MCP server on stdio (what hosts call)
    revit-bridge check      ping the Revit add-in and print the connection status
    python -m revit_bridge.mcp_server

Tools:
    - get_project_snapshot : read-only picture of the open model (no gate)
    - query                : read-only model queries by kind (no gate)
    - execute_code      : send C# code to Revit (spec_confirmed gate)
    - solidify_tool     : save successful code as a reusable named tool
    - list_tools        : list all solidified tools
    - get_tool_choices  : query Revit for a tool's dynamic parameter choices
    - run_tool          : execute a solidified tool (spec_confirmed gate)

The server never calls a model: the host does. Connection settings come from
``REVIT_BRIDGE_*`` environment variables (see README, Configure).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from revit_bridge import __version__
from revit_bridge.capabilities.store import ToolStore
from revit_bridge.revit import sandbox
from revit_bridge.revit.client import RevitClient
from revit_bridge.revit.pool import RevitClientPool
from revit_bridge.revit.settings import RevitSettings, env_flag
from revit_bridge.snapshot.project import take_snapshot
from revit_bridge.snapshot.query import QUERY_KINDS, RevitQueryExecutor, run_query

# Hosts that run their own confirmation flow (the web demo) may lift the gate.
ENV_ALLOW_UNCONFIRMED = "REVIT_BRIDGE_ALLOW_UNCONFIRMED"

_tool_store = ToolStore()


# -- Spec confirmation gate ---------------------------------------------------

def unconfirmed_allowed(env: Mapping[str, str] | None = None) -> bool:
    """True when ``REVIT_BRIDGE_ALLOW_UNCONFIRMED`` lifts the gate."""
    return env_flag(ENV_ALLOW_UNCONFIRMED, env)


def gate_refusal(spec_confirmed: bool, env: Mapping[str, str] | None = None) -> dict | None:
    """Return the refusal payload when execution must not proceed, else None."""
    if spec_confirmed or unconfirmed_allowed(env):
        return None
    return {
        "success": False,
        "error": "refused_unconfirmed_spec",
        "message": (
            "Execution refused: spec_confirmed is false. Show the designer the task "
            "spec (every parameter with its source), get their confirmation, then "
            "call again with spec_confirmed=true."
        ),
    }


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

## Spec confirmation gate (spec_confirmed)

`execute_code` and `run_tool` take `spec_confirmed` (default false) and refuse to
run while it is false. Before setting it to true you must have shown the designer
the task spec - every parameter with its value and where the value came from -
and received their confirmation. Never set `spec_confirmed=true` on your own.

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
    identifies the model state for `reconcile`."""
    try:
        client = await RevitClientPool.get_client()
        snapshot = await take_snapshot(client, categories)
        return snapshot.model_dump_json(indent=2)
    except ValueError as e:
        return _dumps({"error": "invalid_category", "message": str(e)})
    except Exception as e:
        return _dumps({"error": "revit_unreachable", "message": str(e) or type(e).__name__})


@mcp.tool(annotations=_READ_ONLY)
async def query(kind: str, args: dict | None = None) -> str:
    """Read-only model query, no confirmation needed. kinds: levels, grids,
    family_types (args.categories), elements (args.category, args.limit<=200),
    selection, view_elements (args.limit<=200), units, counts (args.categories).
    Returns {"error": "unknown_kind", "kinds": [...]} for anything else."""
    try:
        client = await RevitClientPool.get_client()
        return _dumps(await run_query(RevitQueryExecutor(client), kind, args))
    except Exception as e:
        return _dumps({"error": "revit_unreachable", "kind": kind, "message": str(e) or type(e).__name__})


# -- Execution Tools ----------------------------------------------------------

@mcp.tool(annotations=_MUTATING)
async def execute_code(code: str, parameters: list | None = None, spec_confirmed: bool = False) -> str:
    """Send C# code to Revit for execution over the local TCP socket.
    Refused unless spec_confirmed=true (the designer confirmed the task spec).
    Returns execution result or error message."""
    refusal = gate_refusal(spec_confirmed)
    if refusal:
        return _dumps(refusal)
    # Security review - always enforced before dispatch (P0-2)
    safe, warnings = sandbox.review(code)
    if not safe:
        return _dumps({"success": False, "error": "blocked", "warnings": warnings})
    try:
        client = await RevitClientPool.get_client()
        resp = await client.send_code(code, parameters)
        return _dumps({
            "success": resp.success,
            "result": resp.result,
            "error": resp.error,
        })
    except Exception as e:
        return _dumps({"success": False, "error": str(e)})


# -- Solidification Tools -----------------------------------------------------

@mcp.tool()
def solidify_tool(
    name: str,
    code: str,
    description: str = "",
    parameters: str = "[]",
    tags: str = "",
    source_query: str = "",
) -> str:
    """Save a successful code execution as a reusable named tool.
    parameters: JSON array of {name, type, description, source?, default?, choices_from?}
    tags: comma-separated tags"""
    try:
        params = json.loads(parameters) if parameters else []
    except json.JSONDecodeError:
        params = []

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    tool = _tool_store.solidify(
        name=name,
        code=code,
        description=description,
        parameters=params,
        tags=tag_list,
        source_query=source_query,
    )
    return f"Tool '{tool.name}' solidified successfully. Saved to {_tool_store._tool_path(name)}"


@mcp.tool(annotations=_READ_ONLY)
def list_tools() -> str:
    """List all solidified tools available for execution."""
    tools = _tool_store.list_tools()
    if not tools:
        return "No solidified tools yet. Use solidify_tool to save successful code."
    lines = []
    for t in tools:
        params_str = ", ".join(p.get("name", "?") for p in t.parameters) if t.parameters else "none"
        lines.append(f"- {t.name}: {t.description} (params: {params_str}, used: {t.execution_count}x)")
    return "\n".join(lines)


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
async def run_tool(name: str, params: str = "{}", spec_confirmed: bool = False) -> str:
    """Execute a solidified tool by name with given parameters.
    IMPORTANT: Call get_tool_choices first for parameters with choices_from / source: query:*.
    Refused unless spec_confirmed=true (the designer confirmed the task spec).
    params: JSON object of parameter values, e.g. {"level_name": "L1", "height": 3000}"""
    refusal = gate_refusal(spec_confirmed)
    if refusal:
        return _dumps(refusal)
    try:
        param_dict = json.loads(params) if params else {}
    except json.JSONDecodeError:
        return _dumps({"success": False, "error": f"Invalid params JSON: {params}"})

    # Health check - warn if tool is stale or failing
    health = _tool_store.health_check(name)
    if health["status"] == "not_found":
        return _dumps({"success": False, "error": f"Tool '{name}' not found."})
    if health["recommendation"] == "fallback_to_rag":
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
    safe, warnings = sandbox.review(code)
    if not safe:
        return _dumps({"success": False, "error": "blocked", "warnings": warnings})

    # Execute via client pool
    try:
        client = await RevitClientPool.get_client()
        resp = await client.send_code(code)
        _tool_store.record_usage(name, success=resp.success)
        result = {
            "success": resp.success,
            "tool": name,
            "result": resp.result,
            "error": resp.error,
        }
        if not resp.success:
            result["hint"] = (
                "If this tool fails repeatedly, write fresh code and use execute_code "
                "instead - the tool definition may be outdated."
            )
        return _dumps(result)
    except Exception as e:
        _tool_store.record_usage(name, success=False)
        return _dumps({"success": False, "error": str(e)})


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


@mcp.resource("revit://connection-status")
async def connection_status() -> str:
    """Check whether the Revit add-in is reachable."""
    return json.dumps(await check_connection(), indent=2, ensure_ascii=False)


# -- check subcommand ---------------------------------------------------------

CHECK_PROBE = "return document.Title;"


async def check_connection(settings: RevitSettings | None = None) -> dict:
    """Open a fresh connection, read the open document's title, describe the outcome.

    The probe is a read-only snippet rather than ``say_hello`` (which pops a
    dialog in Revit). ``reachable`` means the add-in ran it; ``document`` is
    the title it returned.
    """
    settings = settings or RevitSettings.from_env()
    status = settings.describe()
    status["document"] = None
    client = RevitClient(settings=settings)
    try:
        await client.connect()
        resp = await client.send_code(CHECK_PROBE)
        status["reachable"] = bool(resp.success)
        status["error"] = None if resp.success else resp.error
        if resp.success and isinstance(resp.result, str):
            status["document"] = resp.result
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

# revit-bridge

Intent bridge between designers and AI for Autodesk Revit: an MCP server that
executes code in a running Revit, keeps a library of reusable capability
packs, and refuses to run anything the designer has not confirmed.

The package contains no model SDK, no vector store and no web framework. Your
MCP host (Claude Desktop, Claude Code, any MCP client) brings the model; the
[revit-bridge-addin](https://github.com/revitbridge/revit-bridge-addin) inside
Revit executes the code.

## Install

Prerequisites: Python 3.11+ with [`uv`](https://docs.astral.sh/uv/) (for `uvx`),
Revit 2026 with the add-in installed and its local TCP listener on (default
`127.0.0.1:18080`).

**Claude Desktop** — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "revit-bridge": {
      "command": "uvx",
      "args": ["revit-bridge"]
    }
  }
}
```

**Claude Code** - the plugin (skill + MCP server + confirmation hook, recommended):

```
/plugin marketplace add revitbridge/revit-bridge
/plugin install revit-bridge@revit-bridge
```

The plugin's `.mcp.json` starts the server with `uvx`; its `hooks/hooks.json`
denies `execute_code` / `run_tool` calls that lack `spec_confirmed=true`
(`uv run` executes `plugin/hooks/spec_gate.py`, so `uv` must be on PATH). The
skill is also installable on its own with
`npx skills add revitbridge/revit-bridge`.

MCP server only, without the skill and hook:

```bash
claude mcp add revit-bridge -- uvx revit-bridge
```

**Any MCP client** — run `uvx revit-bridge` as a stdio server. Pass connection
settings through environment variables (see Configure), for example in the
`env` block of the host's server entry.

From a checkout: `uv sync` then `uv run revit-bridge`.

## Use

1. Start Revit, open a project and make sure the add-in is listening.
2. Check the connection:

   ```bash
   uvx revit-bridge check
   ```

   Prints host, port, whether a token is set, the open document's title
   (`document`, or `document_error` when the add-in answered but could not
   read it) and `"status": "connected"`; the exit code is 0 when the add-in
   answered, 1 otherwise.

3. In your host, work in three steps:

   - `get_project_snapshot` — what the model contains (units, levels, grids,
     family types, selection, …); `query(kind, args)` answers single read-only
     questions (`levels`, `grids`, `family_types`, `elements`, `selection`,
     `view_elements`, `units`, `counts`). Neither needs confirmation.
   - `list_tools` — see the capability packs (`create_wall`, `query_levels`, …).
   - `get_tool_choices` with a tool name — Revit returns the real levels,
     family types or elements for that tool's dynamic parameters.
   - `run_tool` with the chosen values and `spec_confirmed=true`.

   `execute_code` takes arbitrary C# for tasks no pack covers. The code runs
   inside Revit with `document` in scope and a transaction already open; end it
   with `return <object>;`. `solidify_tool` saves code that worked as a new pack.

**Confirmation gate.** `execute_code` and `run_tool` default to
`spec_confirmed=false` and return `refused_unconfirmed_spec` until the host has
shown the designer every parameter with its source and received confirmation.
Hosts that run their own confirmation flow can set
`REVIT_BRIDGE_ALLOW_UNCONFIRMED=1`.

Resources: `revit://stats`, `revit://tools/{name}`, `revit://connection-status`.

## Configure

| Variable | Default | Meaning |
|---|---|---|
| `REVIT_BRIDGE_HOST` | `127.0.0.1` | Host where the add-in listens |
| `REVIT_BRIDGE_PORT` | `18080` | TCP port of the add-in |
| `REVIT_BRIDGE_TOKEN` | *(unset)* | Pre-shared token, sent with every request when the add-in has one configured |
| `REVIT_BRIDGE_TIMEOUT` | `60` | Seconds to wait for a command to finish |
| `REVIT_BRIDGE_ALLOW_UNCONFIRMED` | *(unset)* | `1` lifts the `spec_confirmed` gate (host-internal flows only) |
| `REVIT_BRIDGE_DATA_DIR` | `%LOCALAPPDATA%
evit-bridge` (Windows), `~/.local/share/revit-bridge` (else) | Per-user data: solidified packs, `usage.json`, later the evidence ledger |
| `REVIT_BRIDGE_CAPABILITIES_DIR` | `<data dir>/capabilities` | User pack directory; the packs shipped in the wheel stay visible, a user pack of the same name replaces one |

Development:

```bash
uv sync
uv run pytest
uv build
uvx --from . revit-bridge check
```

License: MIT. Issues and discussion: <https://github.com/revitbridge/revit-bridge/issues>.

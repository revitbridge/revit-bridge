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
denies `execute_code` / `run_tool` calls that carry no confirmation `token`
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

**Your own host** — the same flow is a package API: `revit_bridge.execution`
(`run_pack`, `run_code`, `revalidate`) with your own `ToolStore`, `Gate`,
`Ledger` and Revit client, plus `revit_bridge.host_instructions()` for a host
whose interface does the confirmation.

From a checkout: `uv sync` then `uv run revit-bridge`.

**Develop a standalone C# add-in** — the `revit-addin-dev` skill in
`plugin/skills/` continues an existing project or creates a minimal command
project for Revit 2024, 2025, 2026, or 2027, including the 2025.5/2026.5 .NET 10
updates (2027 is .NET 10 only). Select the exact installed release, not just its year. It uses bridge tools for relevant model
context and prototype checks, then delivers a DLL that runs independently
of the bridge. Coding and building can proceed without a live connection.

From a checkout, load both skills with `claude --plugin-dir ./plugin`, or
copy the complete `plugin/skills/revit-addin-dev` folder into your agent's
skills directory. Example request: "Use revit-addin-dev to continue my C#
plugin, inspect the selected walls through revit-bridge where needed, and
build an independent command." The bundled `scripts/new_addin.py --help`
describes the starter generator; it does not install or launch the add-in.

## Use

1. Start Revit, open a project and make sure the add-in is listening.
2. Check the connection:

   ```bash
   uvx revit-bridge check
   ```

   Prints host, port, whether a token is set, the open document's title
   (`document`, or `document_error` when the add-in answered but could not
   read it) and `"status": "connected"`, plus `capability` (`supported` when
   the add-in answers `get_bridge_capabilities`, `unsupported` for an older
   add-in, `unknown` with the `reason` otherwise) and `environment` (Revit
   version and runtime); the exit code is 0 when the add-in answered, 1
   otherwise.

3. In your host, the flow is snapshot → questions → confirmation → execution →
   validation:

   - `get_project_snapshot` — what the model contains (units, levels, grids,
     family types, selection, …); `query(kind, args)` answers single read-only
     questions (`levels`, `grids`, `family_types`, `elements`, `selection`,
     `view_elements`, `units`, `counts`). Neither needs confirmation.
   - `list_tools` — the capability packs (eight built-ins such as
     `create_wall`, `create_structural_column`, `query_levels`, plus your own);
     `missing_params(tool, known)` — the questions still open, with the real
     options, who can answer each (`kind`) and the steps it holds up
     (`blocks`); `get_tool_choices(name)` — levels, types or elements for a pack's
     dynamic parameters.
   - `reconcile(spec, snapshot)` — a draft TaskSpec against the model: values
     that do not exist, missing parameters, readings to confirm (units, "on
     F2"), a stale snapshot.
   - `check_code(code, parameters)` — compiles C# for `execute_code` in Revit
     without running it (no token): `diagnostics` with lines in your code,
     `hints`, and `guidance` (the lessons for those errors, or pointers once it
     compiles); `checked: false` when the add-in cannot (an older add-in).
   - `get_guidance(stage=...)` — lessons for writing Revit code (the snippet
     contract, units, API pitfalls), chosen by stage, API symbols, OST_*
     categories, compiler errors and run-time exceptions; read-only, no token.
   - `confirm_spec(spec)` with the TaskSpec the designer confirmed, then
     `run_tool(name, params, token)` with exactly the confirmed values.
   - `plan_verification(template, params)` / `verify_execution(evidence_id,
     verification_id)` — for `execute_code`: a read-back check (`created_ids`,
     `count_delta`, `param_equals`) planned before confirming, run after; no token.

   `execute_code` takes arbitrary C# for tasks no pack covers. The code runs
   inside Revit with `document` in scope and a transaction already open; end it
   with `return <object>;`. When it throws in Revit, the reply's `hint` names
   the `get_guidance(stage="fix", exceptions=[...])` call for that exception.
   `solidify_tool` saves code that worked as a new pack.

   A pack's validator (`created_ids`, `count_delta` or `param_equals`) asserts
   the outcome after the run: `success` is true only when Revit succeeded *and*
   the assertion held, and every execution leaves a line in the evidence ledger
   (`evidence`, `validate(evidence_id)`).

**Devices and the dialog in Revit.** A host that reaches Revit over the
network (the web demo) pairs each add-in installation once:
`DeviceStore.create_pairing()` hands out a `XXXX-XXXX` code that is valid for
ten minutes, the add-in redeems it with `DeviceStore.redeem(code)` and keeps
the device token it gets back, and the browser that asked for the pairing
keeps a browser key. Only sha256 digests of the code, the token and the key
are stored (`<data dir>/auth/devices.json`); `verify_device`,
`verify_browser` and `revoke` are how a host authorises a request afterwards.
Each confirmation and each ledger line then carries the `scope` it belongs to
- the `device_id`, or `local` for the add-in on this machine - and a token
confirmed for one device cannot be redeemed on another. Independently of any
client setting, `execute_code` asks the designer once more *in Revit*: the
request carries the confirmed spec card, the add-in shows it with Yes/No and
waits up to three minutes, and a No comes back as
`error: "declined_on_device"` (the token is spent and the run is in the
ledger). `run_tool` runs capability packs without that dialog.

**Confirmation gate.** `execute_code` and `run_tool` return
`confirmation_required` without a `token`. A token comes only from
`confirm_spec(spec)`: the TaskSpec lists every parameter with its value, source
and evidence; the server validates it and issues a one-time token bound to
exactly that tool and those parameters (or that code). Running anything else
with it, reusing it, or using it after 10 minutes fails with
`confirmation_invalid`. `missing_params` and `reconcile` help build the spec.
Hosts that run their own confirmation flow can set
`REVIT_BRIDGE_ALLOW_UNCONFIRMED=1`.

Resources: `revit://stats`, `revit://tools/{name}`, `revit://evidence/recent`,
`revit://connection-status`, `revit://guidance/index`.

**Guidance layers and CLI.** `get_guidance` and `check_code` read the packaged
lessons (framework) and up to three directories laid out the same way: company
(`REVIT_BRIDGE_GUIDANCE_COMPANY`), project (`REVIT_BRIDGE_GUIDANCE_PROJECT`) and
personal (`<data dir>/guidance`). A higher layer replaces a lesson of the same id,
except a company lesson with `enforced: true`; a layer that fails validation is
left out with a `layer_invalid` warning. `uvx revit-bridge guidance check` validates
them (exit 1 on a problem), `guidance index [DIR] [--check]` rebuilds the lesson
indexes and the `revit-core` skill export, and `guidance explain --stage fix --errors
CS1061 --symbols Wall LocationCurve` prints what would be recommended, and why.

The plugin ships an eval suite (`plugin/evals/`, the three baseline tasks and
two second-turn cases, with mocks of the MCP server). Run it by hand from
`plugin/` with `claude plugin eval .` (on Windows in a UTF-8 console:
`chcp 65001` first, or the Chinese prompts reach the model garbled); it is not
part of CI.

## Pair with revit-api-docs

[revit-api-docs](https://github.com/revitbridge/revit-api-docs) is an optional
second server: `search_revit_api` looks API names up in the Revit 2026 API
reference, and `check_code` hints point at it. The plugin does not start it: add
it with `claude mcp add revit-api-docs -- uvx revit-api-docs`, or in one config:

```json
{"mcpServers": {"revit-bridge": {"command": "uvx", "args": ["revit-bridge"]},
                "revit-api-docs": {"command": "uvx", "args": ["revit-api-docs"]}}}
```

Its first run downloads a 335 MB data set (`uvx revit-api-docs download` fetches
it ahead of time). Optional Jev reranking (`REVIT_API_DOCS_RERANK_PROVIDER=jev`,
`TYPESAFE_API_KEY`): see [its README](https://github.com/revitbridge/revit-api-docs#configure).

## Configure

| Variable | Default | Meaning |
|---|---|---|
| `REVIT_BRIDGE_HOST` | `127.0.0.1` | Host where the add-in listens |
| `REVIT_BRIDGE_PORT` | `18080` | TCP port of the add-in |
| `REVIT_BRIDGE_TOKEN` | *(unset)* | Pre-shared token, sent with every request when the add-in has one configured |
| `REVIT_BRIDGE_TIMEOUT` | `60` | Seconds to wait for a command to finish |
| `REVIT_BRIDGE_ALLOW_UNCONFIRMED` | *(unset)* | `1` lifts the confirmation-token gate (host-internal flows only) |
| `REVIT_BRIDGE_CONFIRM_TTL` | `600` | Seconds a confirmation token stays valid |
| `REVIT_BRIDGE_DATA_DIR` | `%LOCALAPPDATA%\revit-bridge` (Windows), `~/.local/share/revit-bridge` (else) | Per-user data: solidified packs, `usage.json`, the evidence ledger, `auth/devices.json`, `verifications/` (read-back plans) |
| `REVIT_BRIDGE_CAPABILITIES_DIR` | `<data dir>/capabilities` | User pack directory; the packs shipped in the wheel stay visible, a user pack of the same name replaces one |
| `REVIT_BRIDGE_EVIDENCE_DIR` | `<data dir>/evidence` | Evidence ledger (`<YYYY-MM>.jsonl`) and pending confirmations |
| `REVIT_BRIDGE_GUIDANCE_COMPANY` | *(unset)* | Company guidance layer: a directory laid out like `plugin/guidance`; its lessons replace packaged ones of the same id |
| `REVIT_BRIDGE_GUIDANCE_PROJECT` | *(unset)* | Project guidance layer, above the company one; the personal layer is `<data dir>/guidance` when it exists |

Development:

```bash
uv sync
uv run pytest
uv build
uvx --from . revit-bridge check
uv run revit-bridge guidance check plugin/guidance
uv run revit-bridge guidance index plugin/guidance --check
```

License: MIT. Issues and discussion: <https://github.com/revitbridge/revit-bridge/issues>.

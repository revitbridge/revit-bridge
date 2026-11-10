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

**Develop a standalone C# add-in, phase by phase** — the skills in
`plugin/skills/` follow an add-in from the designer's sentence to the release.
Each phase skill works on its own; the bridge components (MCP server,
revit-bridge-addin, `revit-api-docs`) only add to a phase, and each phase
skill's `SKILL.md` has a table saying what they add.

| Phase | Skill | What it does | Needs a component |
| --- | --- | --- | --- |
| Brief | `revit-task-brief` | Rewrites a one-sentence request ("I pick two crossing pipes and the first one gets jogged over the other") into a testable brief: numbered behaviour, inputs with units and defaults, cancel and failure rules, project facts, deliverables, verification, decisions. | No; with the bridge it reads the project facts itself |
| Coding | `revit-addin-dev` | Continues a project or creates a minimal command project for Revit 2024 to 2027, including the 2025.5/2026.5 .NET 10 updates (select the exact installed release, not just its year), and delivers a DLL that runs without the bridge. | No; the bridge adds model context and prototype checks |
| Bridging | `revit-bridge` + `revit-core` | Builds, changes and queries the open model through the server: snapshot, reconcile, confirm, execute, verify. | Yes: the MCP server and the revit-bridge-addin |
| Test | `revit-addin-test` | Runs the compiled add-in in a real Revit without disturbing others on the machine, reads journals, finds why Revit hangs, judges a build matrix, compares base and branch. | No; with the revit-bridge-addin calls go through its protocol |
| Release | `revit-addin-release` | Multi-year release build, obfuscation, installer, acceptance on a real machine, the review record, logs from a user's machine. | No |

### Install the skills in another project

The six skills (`revit-task-brief`, `revit-addin-dev`, `revit-addin-test`,
`revit-addin-release`, `revit-bridge`, `revit-core`) are self-contained
folders: a `SKILL.md` plus `references/`, `scripts/` and `assets/`. Any agent
that reads skill folders can use them, with or without the MCP server. The
four add-in phase skills need no bridge connection at all (testing and
acceptance need a real Revit, not the server); `revit-bridge` and `revit-core` describe the bridge
tools and are most useful with the server configured; in a repository without
the server their descriptions only cost context, so install `--profile addin`
there.

| Where you work | Install |
| --- | --- |
| Claude Code, everything (skills + MCP server + confirmation hook) | `/plugin marketplace add revitbridge/revit-bridge` then `/plugin install revit-bridge@revit-bridge` |
| Claude Code, one project (skills only) | `uvx revit-bridge skills install --to .claude/skills` in the project |
| Claude Code, every project of this user (skills only) | `uvx revit-bridge skills install --to ~/.claude/skills` |
| Codex, dsh and other agents reading `.agents/skills` | `uvx revit-bridge skills install --to .agents/skills` |
| One skill only | add its name: `uvx revit-bridge skills install revit-addin-dev --to .claude/skills` |
| An add-in repository that does not use the bridge | `uvx revit-bridge skills install --profile addin --to .claude/skills` (`revit-task-brief` + `revit-addin-dev` + `revit-addin-test` + `revit-addin-release`; `--profile bridge` is the other pair, `revit-bridge` + `revit-core`) |
| Without `uv` | `pip install revit-bridge` then `revit-bridge skills install --to <dir>`; or copy the folder from `plugin/skills/` of a checkout, or `npx skills add revitbridge/revit-bridge` |
| From a checkout, without copying | `claude --plugin-dir ./plugin` |

`revit-bridge skills list` prints the names and descriptions. `install`
copies whole folders, leaves other skills in the directory alone, and refuses
to overwrite an existing folder of the same name unless `--force` is given,
so local edits survive an accidental re-install. To upgrade, run it again
with `--force` after `uvx` has fetched the new release (`uvx
revit-bridge@latest skills install ...`).

The bundled scripts need Python 3.11+ or Windows PowerShell 5.1+:
`new_addin.py` in `revit-addin-dev`; `journal_tail.py`, `addin_snapshot.py`,
`revit_ps.ps1` and `window_list.ps1` in `revit-addin-test`; `collect_logs.ps1`
with its `collect_logs.bat` launcher in `revit-addin-release`. Nothing else is
installed and none of them starts, installs or closes a Revit.

Example request once installed: "Use revit-addin-dev to continue my C#
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
Output of `revit-bridge guidance` and `revit-bridge skills` sent to a pipe or a
file is UTF-8; in Windows PowerShell 5.1 run `[Console]::OutputEncoding =
[Text.Encoding]::UTF8` before piping or capturing it, otherwise Chinese text
shows as mojibake (`revit-bridge check` prints ASCII-escaped JSON there and
needs nothing).

The plugin ships an eval suite (`plugin/evals/`, the three baseline tasks and
two second-turn cases, with mocks of the MCP server). Run it by hand from
`plugin/` with `claude plugin eval .` (on Windows in a UTF-8 console:
`chcp 65001` first, or the Chinese prompts reach the model garbled); it is not
part of CI.

## Pair with revit-api-docs

[revit-api-docs](https://github.com/revitbridge/revit-api-docs) is an optional second server: `search_revit_api` looks API names up in the Revit 2026 API reference, and `check_code` hints point at it.
The plugin does not start it: add it with `claude mcp add revit-api-docs -- uvx revit-api-docs`, or in one config:

```json
{"mcpServers": {"revit-bridge": {"command": "uvx", "args": ["revit-bridge"]},
                "revit-api-docs": {"command": "uvx", "args": ["revit-api-docs"]}}}
```

Its first run downloads the data set `v1.1-data` (267 MB; `uvx revit-api-docs download` fetches it ahead of time): the Revit 2026 API reference, including constructors such as `new XYZ(x, y, z)` and interface members.
From revit-api-docs 0.3.0 on, `TYPESAFE_API_KEY` alone turns on Jev reranking; Cohere is optional.
For multi-step task requests (several steps in one sentence) also set an embedding key, `REVIT_API_DOCS_EMBEDDING_API_KEY` (OpenRouter, model `openai/text-embedding-3-large`), to fuse vector search with keyword search.
On 223 English test queries the fused nDCG@5 was 0.647 against 0.557 for keyword search alone, both without reranking, and 0.842 with Jev.
Each query makes one more embedding call (about 0.9 s, negligible cost); every setting is in [its README](https://github.com/revitbridge/revit-api-docs#configure).

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

## Use from dsh (DeepSeek Harness)

dsh runs an agent that can talk to revit-bridge and revit-api-docs like any other MCP client, and it iterates on its own: writes, compiles, reads the model, fixes, until the task is done. The flow for a feature developed against the live Revit:

1. Install the add-in in Revit (Releases of `revit-bridge-addin`), open the project you want to work on and click **Add-Ins > Revit MCP Switch**. `uvx revit-bridge check` must print `"status": "connected"` with that document's title.
2. Register both servers in the dsh profile you use (`~/.dsh/profiles/<profile>/cordis.patch.yml`, a top-level list):

   ```yaml
   - insert:
       - id: mcp-revit-bridge
         name: '@deepseek-ai/dsh-mcp-client'
         config: { serverName: revit-bridge, transport: stdio, command: uvx, args: [revit-bridge] }
       - id: mcp-revit-api-docs
         name: '@deepseek-ai/dsh-mcp-client'
         config: { serverName: revit-api-docs, transport: stdio, command: uvx, args: [revit-api-docs] }
   ```

   Keys (`TYPESAFE_API_KEY`, `REVIT_API_DOCS_EMBEDDING_API_KEY`) and a proxy go into each server's `env`; dsh strips `KEY`/`TOKEN` variables from its own environment, so set them there, not globally.
3. Put the skills where dsh finds them: copy `plugin/skills/revit-task-brief`, `revit-addin-dev`, `revit-addin-test`, `revit-addin-release`, `revit-core` and `revit-bridge` into `<project>/.agents/skills/`.
4. Start dsh in the project directory and give it the task in one or two designer sentences, plus the facts it cannot discover: "Revit 2026 with the test model is open on this machine", whether anyone will answer questions, and what the report must contain. With `--profile headless --json` it runs unattended and streams events; interactive mode lets you answer the bridge's confirmation requests, which is what lets the agent prototype in the open model (`check_code`, `confirm_spec`, `execute_code`) before it ports the logic into the add-in.
5. Read the agent's report against the brief (what was built, what it verified in Revit, what it could not), then install the add-in and test the picks, dialogs and cancel paths yourself.

An unattended run cannot confirm `execute_code`; the agent then verifies by compile checks and model reads only. Keep the project open on a copy of the model: a prototype that hangs inside Revit blocks the bridge until the request ends.

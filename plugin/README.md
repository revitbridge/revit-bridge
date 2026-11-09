# Revit Bridge plugin

Build, change and query a running Autodesk Revit model from Claude: six skills, one local MCP server and one confirmation hook. The model takes a snapshot first, traces every parameter to a source, shows a spec card, and executes only with a one-time token the designer confirmed.

## What is inside

| Component | Purpose |
|---|---|
| `skills/revit-bridge` | The bridge protocol: snapshot, query, reconcile, confirm, execute, verify. Use it when the designer wants something built, changed, queried or reported in the open model. |
| `skills/revit-task-brief` | Rewrites a short designer request into a testable brief before any code is written. |
| `skills/revit-addin-dev` | The coding phase of a standalone Revit C# add-in: creates, continues, debugs or migrates its code; the bridge, when present, adds model context and prototype checks. |
| `skills/revit-addin-test` | The test phase: runs the compiled add-in in a real Revit without disturbing others on the machine, reads journals, judges the build matrix, compares base and branch. Needs no bridge component. |
| `skills/revit-addin-release` | The release phase: multi-year release build, obfuscation, installer, acceptance on a real machine, the review record, logs from a user's machine. Needs no bridge component. |
| `skills/revit-core` | The `execute_code` snippet contract, internal units, ElementId and other version differences, the Revit 2024 to 2027 runtime matrix. |
| `.mcp.json` | Starts the `revit-bridge` MCP server with `uvx` (the `revit-bridge` package from PyPI, pinned to one version). |
| `hooks/hooks.json` | A `PreToolUse` hook that denies `execute_code` and `run_tool` unless the call carries a token from `confirm_spec`. It runs `hooks/spec_gate.py` with `uv`. |
| `guidance/` | Lessons the MCP server serves through `get_guidance` and `check_code`. |

## What it runs and where data goes

- The MCP server runs on your machine and talks only to the Revit add-in on `127.0.0.1:18080` (the [revit-bridge-addin](https://github.com/revitbridge/revit-bridge-addin), installed separately, with **Add-Ins > Revit MCP Switch** turned on). It sends nothing to any other host.
- The hook reads the tool call from standard input and prints a permission decision. It makes no network calls.
- No credentials are read or stored. A pre-shared token is optional and only needed when you configured one in the add-in's settings; set `REVIT_BRIDGE_TOKEN` in your environment in that case.
- Nothing from the Revit model leaves your machine except through the conversation with Claude.

## Requirements

- Autodesk Revit with the revit-bridge add-in installed (see that repository for the supported Revit versions).
- `uv` on PATH (`uvx` starts the server, `uv run` starts the hook).
- Claude Code or Cowork on the same machine as Revit. In claude.ai chat the local server cannot start, so only the skills load there.

## Install

```text
/plugin marketplace add revitbridge/revit-bridge
/plugin install revit-bridge@revit-bridge
```

Then ask, for example: "Add a 3 m high generic wall on Level 1 from grid A1 to A3." Claude snapshots the project, shows the spec card, and waits for your confirmation before anything is written to the model.

Documentation, the Python package and the changelog: https://github.com/revitbridge/revit-bridge. License: MIT.

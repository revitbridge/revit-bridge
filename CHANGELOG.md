# Changelog

All notable changes to `revit-bridge` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `revit_bridge.paths`: per-user data root (`%LOCALAPPDATA%/revit-bridge` on
  Windows, `$XDG_DATA_HOME/revit-bridge` or `~/.local/share/revit-bridge`
  elsewhere; `REVIT_BRIDGE_DATA_DIR` overrides) with `user_capabilities_dir()`,
  `evidence_dir()`, `builtin_capabilities_dir()` and `skills_dir()`.
- `ToolStore(user_dir, builtin_dir)` reads two directories: the read-only
  built-in packs and the user directory. A user pack with the same name
  replaces a built-in one; `solidify` / `update` / `delete` write only the
  user directory (`update` copies a built-in pack there first, `delete` of a
  built-in pack leaves an empty `<name>.disabled` marker; `enable(name)`
  removes it). `path_of(name)` tells which file is in effect.
- Execution counters (`execution_count`, `last_used`, `failure_count`) live in
  `<user dir>/usage.json`; pack files are never rewritten to record a run.
- The plugin skills (`plugin/skills/`) ship inside the wheel as
  `revit_bridge/skills/`; `revit_bridge.skills_dir()` locates them.
- Capability pack files are read in both layouts: files without
  `schema_version` are the 0.1 layout (`version` reads as `0.0.0`, parameter
  `source` / `unit` / `required` are inferred, string preconditions become
  `{text: ...}`); files written by the store carry `schema_version: 1`,
  `version`, `revit_versions`, `validator`, `fixtures`, `approved_by` /
  `approved_at`, and `update()` bumps the patch version when `code_template`
  or `parameters` change.

### Changed

- `REVIT_BRIDGE_CAPABILITIES_DIR` now overrides only the user directory; the
  built-in packs stay visible. `default_capabilities_dir()` is kept for 0.1
  callers.
- `tags` on capability packs are read but no longer written (`applies_when`
  replaces them); `solidify(tags=...)` is accepted and ignored.
- Plugin 0.1.1: `plugin/.mcp.json` runs the PyPI release (`uvx revit-bridge`)
  instead of a git checkout, so installs no longer need `uvx --refresh` to pick
  up new versions.

## [0.1.0] - 2026-09-18

First release of the standalone package, extracted from the former
`revit-api-rag` monorepo.

### Added

- MCP server `revit-bridge` (stdio) with tools `execute_code`, `solidify_tool`,
  `list_tools`, `get_tool_choices`, `run_tool` and resources `revit://stats`,
  `revit://tools/{name}`, `revit://connection-status`.
- `spec_confirmed` execution gate on `execute_code` and `run_tool`;
  `REVIT_BRIDGE_ALLOW_UNCONFIRMED=1` lifts it for host-internal flows.
- `revit-bridge check` subcommand that pings the Revit add-in and exits 0/1.
- Connection settings from environment variables: `REVIT_BRIDGE_HOST`,
  `REVIT_BRIDGE_PORT`, `REVIT_BRIDGE_TOKEN`, `REVIT_BRIDGE_TIMEOUT`;
  the token is sent with every JSON-RPC request.
- Eleven built-in capability packs shipped inside the wheel (a reference pack
  lives in `capabilities/examples/`, which the store never reads);
  `REVIT_BRIDGE_CAPABILITIES_DIR` overrides the directory.
- Slot token helpers (`revit_bridge.auth.tokens`) for the web relay.
- `RevitQueryExecutor.get_tool_choices(dynamic_params)` resolves a pack's
  `choices_from` sources (`levels`, `family_types:<OST_*>`, `floor_types`,
  `elements:<OST_*>`) and `RevitQueryExecutor.get_project_units()` reads the
  project's length unit. The MCP `get_tool_choices` tool and the web host both
  call these instead of carrying their own Revit snippets.
- `escape_param_value` in `revit_bridge.capabilities`: `ToolStore.render_code`
  escapes quotes, backslashes and newlines in string parameters so a value
  cannot terminate the C# string literal it is rendered into.
- Claude Code plugin in `plugin/` (skill `revit-bridge` with pattern, workflow and
  standards references, `.mcp.json`, PreToolUse spec gate hook) and the
  marketplace manifest `.claude-plugin/marketplace.json`.
- Test suite with a fake Revit TCP server; CI and PyPI trusted publishing workflows.

### Removed (compared with the monorepo server)

- RAG tools `search_revit_api`, `get_code_examples`, `generate_code` and every
  model / vector-store dependency. Hosts bring their own model.

[Unreleased]: https://github.com/revitbridge/revit-bridge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/revitbridge/revit-bridge/releases/tag/v0.1.0

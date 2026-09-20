# revit-bridge

Python package + MCP server. Public repository: only `README.md`, `CHANGELOG.md`
and this file are documentation; design notes and logs live in the private
notes repository.

## Build and test

```bash
uv sync                      # creates .venv, installs dev group (pytest)
uv run pytest                # unit tests; a fake Revit TCP server, no Revit needed
uv build                     # sdist + wheel; wheel bundles capabilities/ as revit_bridge/capabilities/builtin
                             # and plugin/skills as revit_bridge/skills
uvx --from . revit-bridge check   # ping the add-in on REVIT_BRIDGE_HOST:REVIT_BRIDGE_PORT
```

## Layout

- `revit_bridge/mcp_server.py` — MCP tools, `main()` (`serve` | `check`).
- `revit_bridge/revit/` — TCP JSON-RPC client, connection pool, settings from `REVIT_BRIDGE_*`, sandbox.
- `revit_bridge/snapshot/` — query atoms and model queries.
- `revit_bridge/paths.py` — per-user data root (`REVIT_BRIDGE_DATA_DIR`), user/built-in pack directories, `skills_dir()`.
- `revit_bridge/capabilities/` — capability pack store (built-in packs in `capabilities/` at repo root, user packs
  and `usage.json` in the data root; a user pack overrides a built-in one of the same name).
- `revit_bridge/spec/`, `validators/`, `evidence/`, `auth/` — TaskSpec models, later phases, slot token helpers.
- `tests/` — pytest; `tests/fake_revit.py` fakes the add-in.
- `plugin/` — Claude Code plugin: `skills/revit-bridge/SKILL.md` (+ `references/`), `.mcp.json`,
  `hooks/hooks.json` + `hooks/spec_gate.py`. `.claude-plugin/marketplace.json` at the repo root
  publishes it. Validate with `claude plugin validate .` and `claude plugin validate ./plugin --strict`;
  try locally with `claude --plugin-dir ./plugin`.

## Hard constraints

- No LLM SDK (openai, anthropic, google-genai, cohere, ...), no chromadb, no FastAPI.
  Runtime dependencies are exactly `mcp`, `pydantic>=2`, `pyyaml`, `websockets`.
  `tests/test_package.py` enforces this.
- Configuration comes only from `REVIT_BRIDGE_*` environment variables. No config files.
- No `sys.path` manipulation, no imports from other repositories.
- `execute_code` and `run_tool` keep the `spec_confirmed` gate; do not weaken it.
- Capability pack format, TaskSpec structure and the plugin `SKILL.md` structure are
  defined by planning. If a change needs them, stop and record `BLOCKED` in the notes log.
- Scripts and workflow files are ASCII only.

## Commits

Prefix commit subjects with `revit-bridge: `. Before committing run
`uv sync; uv run pytest; uv build` and make sure `uv tree` shows none of the
forbidden packages.

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
uvx --from . revit-bridge check   # read the open document's title through the add-in on
                                  # REVIT_BRIDGE_HOST:REVIT_BRIDGE_PORT; exit 0 when it answered
```

## Layout

- `revit_bridge/mcp_server.py` — MCP tools (thin wrappers), `main()` (`serve` | `check` | `guidance` | `skills`).
- `revit_bridge/guidance_cli.py` — `revit-bridge guidance check|index|explain` (validate the guidance layers,
  rebuild the generated parts, show what `get_guidance` would recommend); `skills_cli.py` — `revit-bridge
  skills list|install` (copy bundled skill folders into a directory, `--profile addin|bridge|all`).
- `revit_bridge/execution.py` — the execution flow as a package API (`run_pack`, `run_code`, `revalidate`,
  `ExecutionResult`), shared by the MCP server and the web host; `instructions.py` — the model instructions
  (`server_instructions()` / `host_instructions()` from one text).
- `revit_bridge/checking.py` — `check_code`: compile C# in Revit without running it (local `sandbox.review`
  first, then the add-in's `check_code_in_revit`); the reply adds `hints` and `guidance` lessons.
- `revit_bridge/revit/` — TCP JSON-RPC client, connection pool, settings from `REVIT_BRIDGE_*`, sandbox,
  `probe.py` (`check_connection`, `PING_PROBE`).
- `revit_bridge/snapshot/` — query atoms, model queries (`query.py`, incl. the `query` tool kinds),
  `project.py` (`ProjectSnapshot`, `take_snapshot`).
- `revit_bridge/paths.py` — per-user data root (`REVIT_BRIDGE_DATA_DIR`), user/built-in pack directories,
  `auth_dir()`, `skills_dir()`; `revit_bridge/jsonfile.py` — the locked read-modify-write both JSON stores use.
- `revit_bridge/capabilities/` — capability pack store (built-in v1 packs in `capabilities/` at repo root, user
  packs and `usage.json` in the data root; a user pack overrides a built-in one of the same name); `schema.py`
  validates pack files and evaluates preconditions.
- `revit_bridge/spec/` — `models.py` (TaskSpec), `rules.py` (validation, missing params, reconcile), `gate.py`
  (confirmation tokens). `validators/` — post-execution assertions; `evidence/` — the JSONL ledger;
  `auth/` — `devices.py` (pairing codes, device tokens and browser keys, hashed; `DeviceStore` in
  `<data dir>/auth/devices.json`), `tokens.py` (the 0.1 slot helpers, deprecated, still shipped).
- `revit_bridge/guidance/` — lessons for writing Revit code, chosen without a model: `model.py` (format v1
  vocabulary, data classes, version ranges), `loader.py` (validate one root, merge the four layers),
  `select.py` (`recommend`, `pointers`, `index`, `read`: the `get_guidance` replies), `scan.py` (API names
  in C#, exception types in an error text), `contract.py` (the snippet contract from
  `revit-core/snippet-contract`), `indexer.py` (what `revit-bridge guidance index` writes).
- `revit_bridge/verification/` — read-back checks for ad-hoc code: `flow.py` (`plan` / `verify`, behind
  `plan_verification` / `verify_execution`), `templates.py` (`created_ids`, `count_delta`, `param_equals`
  mapped onto the built-in validators), `store.py` (plans in `<data dir>/verifications/`).
- `tests/` — pytest; `tests/fake_revit.py` fakes the add-in.
- `plugin/` — Claude Code plugin: `skills/revit-bridge/SKILL.md` (+ `references/`), `.mcp.json`,
  `hooks/hooks.json` + `hooks/spec_gate.py`, `guidance/` (the guidance library, format v1: `guidance.yaml`
  and one directory per category, a `SKILL.md` plus any `lessons/<slug>.md`; in the wheel as
  `revit_bridge/guidance_content`; `skills/revit-core/` is its export, written by `revit-bridge guidance
  index`). `.claude-plugin/marketplace.json` at the repo root
  publishes it. Validate with `claude plugin validate .` and `claude plugin validate ./plugin --strict`;
  try locally with `claude --plugin-dir ./plugin`.

## Hard constraints

- No LLM SDK (openai, anthropic, google-genai, cohere, ...), no chromadb, no FastAPI.
  Runtime dependencies are exactly `mcp`, `pydantic>=2`, `pyyaml`.
  `tests/test_package.py` enforces this.
- Configuration comes only from `REVIT_BRIDGE_*` environment variables. No config files.
- No `sys.path` manipulation, no imports from other repositories.
- `execute_code` and `run_tool` run only with a one-time token from `confirm_spec`
  (bound to the exact tool + parameters, or code); do not weaken the gate.
- Capability pack format, TaskSpec structure and the plugin `SKILL.md` structure are
  defined by planning. If a change needs them, stop and record `BLOCKED` in the notes log.
- Scripts and workflow files are ASCII only.

## Commits

Prefix commit subjects with `revit-bridge: `. Before committing run
`uv sync; uv run pytest; uv build` and make sure `uv tree` shows none of the
forbidden packages.

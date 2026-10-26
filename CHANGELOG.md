# Changelog

All notable changes to `revit-bridge` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `revit-addin-dev` skill for developing independent C# Revit add-ins with
  optional bridge context and prototype checks. Includes existing-project
  guidance, snippet-to-command conversion, and a standalone project generator
  for Revit 2024/2025/2026, including the 2025.5/2026.5 .NET 10 updates,
  with no automatic installation. Generated builds reject incompatible API
  years and .NET 8/.NET 10 reference combinations before dependency resolution.
- `revit-addin-dev`: Revit 2027 starter (`--revit-version 2027`,
  `net10.0-windows`, .NET 10 only; `2027.5` and other unlisted releases are
  rejected). A 2027 build accepts only 27.x API assemblies and rejects any
  target other than `net10.0-windows`; it omits the 2025/2026 .NET 8/.5
  checks, which would reject the Revit 2027 API (27.0.x) as earlier .NET 8
  references. The references add the .NET 10 SDK, `global.json`, the 2027
  isolation types and manifest settings (not used by the starter manifest),
  .NET 8's end of support on 2026-11-10, and the bridge's
  `environment`/`capability` reply as the source of the connected Revit
  version. The 2027 starter has not been loaded in Revit 2027 yet.
- `revit-addin-dev` starter writes a `global.json` next to the project in
  the shape of Autodesk's Revit 2027 sample: `10.0.100` with
  `rollForward: minor` for 2025.5, 2026.5 and 2027 (a .NET 10 SDK), and
  `8.0.100` with `rollForward: latestMajor` for 2025 and 2026 (any SDK 8 or
  newer; a newer SDK downloads the `net8.0` targeting pack on first
  restore); none for 2024. The generated `.csproj` and `.addin` are
  unchanged.
- `check_code(code, parameters)` MCP tool (read-only, no token): compiles C#
  for `execute_code` inside Revit without running it, through the add-in's
  `check_code_in_revit`, after the local sandbox review (a blocked pattern
  never reaches Revit). The reply (`schema_version` 1) has `checked`,
  `compiled`, `blocked`, `capability`, `reason`, `diagnostics` (line and
  column in the user's code), `truncated`, `fingerprint`, `hints` (for
  CS1061, CS0117, CS0246, CS0103, CS1501, CS1503 and CS7036, naming the
  Revit version), `guidance` (lessons, see Changed), `environment` and
  `warnings`. `checked: false` means the code was not pre-compiled: an older
  add-in, or the capability is unknown. Package API:
  `revit_bridge.checking.check_code(client, code, parameters)`.
- `revit_bridge.revit.capabilities.discover(client)`: the add-in's
  `get_bridge_capabilities` as `supported`, `unsupported` (JSON-RPC -32601:
  an older add-in) or `unknown` (no connection, wrong token, timeout, any
  other error), the original error kept in `reason`. Cached per connection;
  `RevitClient.connection_generation` counts connects.
- `revit_bridge.revit.environment.get_environment(client)`: Revit version,
  runtime and Kit from the capabilities, or on an older add-in from a probe
  snippet over `send_code_to_revit` (needs an open document, runs in a
  transaction). `error` names why facts are missing (`no_document`,
  `read_only`, `auth`, `timeout`, `unreachable`, ...), never "no Kit".
- `get_guidance` MCP tool (read-only, no token): lessons for writing Revit
  code, chosen and ordered by rules, without a model. `mode="recommend"`
  takes a `stage` (`orient`, `write`, `fix`, `port`, `verify`) and the
  signals of the moment (`symbols`, `revit_categories`, `errors`,
  `exceptions`, `skill_categories`, `revit_version`, `seen_hashes`) and
  answers within a budget (5 lessons, 12000 UTF-8 bytes); every item carries
  `id`, `revision`, `hash`, `layer`, `applicability` and `why`. An error code
  counts only together with a symbol. `mode="index"` lists every lesson
  without bodies, `mode="read"` returns full texts by id. Resource
  `revit://guidance/index`: one line per lesson.
- Guidance content format v1 in `plugin/guidance/` (in the wheel as
  `revit_bridge/guidance_content`, found by `paths.guidance_dir()`):
  `guidance.yaml` (category order, the `always` category, budget), one
  directory per category with a `SKILL.md` (`name` and `description` only)
  and `lessons/<slug>.md` with a validated front matter (id, revision,
  stage, kind, Revit version range, triggers). An invalid root is rejected
  as a whole, every problem listed; the tool then answers
  `{"error": "guidance_invalid", "problems": [...]}`. Package API:
  `revit_bridge.guidance` (`load`, `recommend`, `index`, `read`).
- Seed guidance: eleven categories (`revit-core`, `revit-collect`,
  `revit-params`, `revit-geometry`, `revit-families`, `revit-elements`,
  `revit-structure`, `revit-mep`, `revit-views`, `revit-addin`,
  `revit-verify`) and thirteen lessons in the first four: the
  `execute_code` snippet contract, internal units, `ElementId.Value`, the
  bridge-owned transaction, the Revit 2024-2027 runtime matrix, collector
  filters, instances versus types, the selection, parameter lookup and
  storage types, built-in parameters, wall location curves, curve length and
  `Line.CreateBound`. API facts are checked against the Revit 2026 API
  documentation data set and the Revit API assemblies 2024-2027.

### Changed

- `revit-bridge check` and `revit://connection-status` also report
  `capability` and `environment`; the exit code still follows `reachable`.
  `check_connection(settings, environment=False)` keeps its reply unless
  asked, so a host's health check sends no extra request.
- Instructions, both renderings: a snippet contract in Notes (`document` and
  `parameters` in scope, the default usings, the reference whitelist without
  a JSON library, the refused symbols, the open transaction,
  `return <object>;`, OST_* categories). The server rendering has the model
  call `check_code` for `execute_code` before the card, and write "not
  pre-compiled locally" on the card when `checked` is false; the
  `revit-bridge` skill follows the same flow. Both say 0.3.
- The server rendering and the `revit-bridge` skill call `get_guidance` at
  three points: at the start of a task (`stage="orient"`), before writing
  `execute_code` C# (`stage="write"` with the API symbols and OST_*
  categories) and after `check_code` errors (`stage="fix"` with the error ids
  and symbols). To stay within 60 lines, step 6 of the server rendering no
  longer lists the rule names (each error names its rule) and step 8 is
  rewrapped.
- `check_code` fills `guidance` (it was always empty), from the guidance
  library and without a model; items have the `get_guidance` item shape. On
  compile errors: up to two `fix` lessons with their bodies, chosen by the
  diagnostic ids, their symbols and the API names in the code (comments and
  string literals skipped); an error code pairs only with the diagnostics'
  own symbols, never with a name from the code, so the lesson about the
  reported error comes first; each body is at most 2400 UTF-8 bytes, and a cut
  one has `truncated: true` and ends with the line
  `[truncated: get_guidance(mode="read", ids=["<id>"])]`. When the code
  compiles: up to five `write` lessons as pointers (`summary`, no body). The
  Revit version in `environment` drops lessons for other versions and marks
  the rest `verified`. Not checked or blocked: empty. A library that does
  not load: empty, with the warning `guidance_unavailable: <first problem>`.
  The reply keys and `hints` are unchanged. Package API:
  `check_code(client, code, parameters, library=...)`,
  `revit_bridge.guidance.pointers` and `revit_bridge.guidance.code_symbols`.
- Guidance selection order: lessons with an error hit (`why` has an
  `error:` entry) come before the others, each group still in category
  order, `order`, id; `get_guidance` results change only when `errors` are
  passed. `recommend` and `pointers` take an optional `error_symbols` (package
  API only, not a `get_guidance` parameter): when given, an error code pairs
  only with them; otherwise with `symbols`, as before.
- Guidance writing rule: a lesson body should stay at or under 2300 UTF-8
  bytes (noted in `revit-core/SKILL.md`); the loader lists larger bodies in
  `Library.warnings` without rejecting them. Four seed lessons (2314-2376
  bytes) are over it today.
- The server rendering and the `revit-bridge` skill read the lesson bodies
  in `check_code`'s `guidance` before changing code (`get_guidance(mode="read")`
  for a `truncated` one, `stage="fix"` for more) and treat the pointers of a
  clean compile as optional reading. To stay within 60 lines, step 6 of the
  server rendering no longer says that each error names its rule and step 7
  no longer names `confirmation_invalid`.
- `get_project_snapshot` and `query` say categories are OST_* names
  (`OST_Walls`).

## [0.3.0] - 2026-09-22

Authorization per device: a pairing code turns one add-in installation into a
device with its own token, a confirmation and a ledger line say which device
they belong to, and ad-hoc code asks the designer on the device before it
runs.

### Added

- `revit_bridge.auth.devices`: `DeviceStore` (`<data dir>/auth/devices.json`)
  with `create_pairing`, `redeem`, `verify_device`, `verify_browser`,
  `revoke`, `touch`, `list`, `purge`, and the models `PairingCode`, `Device`,
  `Redeemed` plus `DeviceError("invalid_code")`. A pairing code is
  `XXXX-XXXX` from an alphabet without `0/O/1/I` and lasts ten minutes; the
  code, the device token and the browser key are returned once in plain text
  and stored only as sha256, compared in constant time. Exported from
  `revit_bridge.auth`.
- `revit_bridge.paths.auth_dir()` - `<data dir>/auth`.
- `revit_bridge.jsonfile.LockedJsonFile`: the locked read-modify-write and
  atomic replace `usage.json` had, now shared with `devices.json`.
- `RevitClient.send_code(code, parameters, confirm=...)` and
  `send_command(method, params, timeout=...)`; `RevitResponse.error_code`
  carries the JSON-RPC error code the add-in answered with.

### Changed

- **Breaking (hosts that call the package API).** A confirmation and an
  execution belong to a *scope*: the `device_id` it runs on, or `"local"`
  for the add-in on this machine.
  - `Confirmation` gains `scope` (default `"local"`) and `card`;
    `Gate.issue(spec, confirmed_by, channel, scope="local")` and
    `verify` / `consume` / `redeem(token, projection, scope="local")` refuse
    another scope with `GateError("mismatch")` - the same reason as tampered
    parameters, so a refusal never says whose token it was.
  - The ledger record gains `scope` (after `host`);
    `Ledger.recent(limit, tool, scope=None)` filters on it. Lines written by
    0.2 have no `scope` and read as `"local"`.
  - `run_pack` / `run_code(..., scope="local")` verify, consume and record
    under that scope; `revalidate(..., scope=None)` answers
    `{"error": "scope_mismatch"}` for a record from another one.
  - The MCP server passes none of them and keeps the local defaults, so its
    tool replies are unchanged.
- **Breaking (the add-in protocol).** `run_code` adds `confirm`
  (`{kind: "execute_code", title: "revit-bridge", message: <the confirmed
  spec card, else the first 200 characters of the code>}`) to the
  `send_code_to_revit` params, and that one request waits
  `max(REVIT_BRIDGE_TIMEOUT, 180)` seconds for the designer's answer.
  `run_pack`, the probes and the reads send no `confirm`, so the policy is
  the package's and does not depend on a client setting. An add-in that
  answers `-32001` ("declined on device") gives
  `ExecutionResult(success=False, error="declined_on_device")` with the
  token consumed and a ledger line. A client of `run_code` must accept the
  `confirm` keyword in its `send_code`.
- `host_instructions()`: the execution comes back "as a message from the
  host" instead of "as a tool message".

### Deprecated

- `revit_bridge.auth.tokens` (the slot helpers `slot_token_required`,
  `load_slot_tokens`, `verify_slot_token`, `parse_handshake_token`) and with
  them the `MCP_BRIDGE_SLOT_TOKEN*` environment variables: devices replace
  slots. They still work in 0.3 and are removed in 0.4.

## [0.2.1] - 2026-09-21

The execution flow as a package API, so the web host (phase 6) runs the same
gate, preconditions, validators and ledger as the MCP server instead of a
copy.

### Added

- `revit_bridge.execution`: `ExecutionResult` (the MCP tools' JSON shape as a
  model) and `run_pack(store, gate, ledger, client, name, params, token, host,
  env)`, `run_code(gate, ledger, client, code, parameters, token, host, env)`,
  `revalidate(store, ledger, client, evidence_id)`. Dependencies are passed
  in; the host bypass is read only from the `env` a host passes (none means
  no bypass); the ledger's `host` column is the `host` argument. `run_tool`,
  `execute_code` and `validate` are thin wrappers over them.
- `revit_bridge.host_instructions()`: the model instructions for a host whose
  interface does the confirmation (`propose_spec` instead of `confirm_spec` /
  `run_tool` / `execute_code`), rendered from the same source text as
  `SERVER_INSTRUCTIONS` (`revit_bridge.instructions`).
- `revit_bridge.revit.probe`: `check_connection`, `PING_PROBE`, `CHECK_PROBE`
  (moved from `mcp_server` / `revit.client`; re-exported from
  `revit_bridge.revit`, and `revit_bridge.mcp_server.check_connection` still
  works).

### Changed

- `run_tool` / `execute_code` replies always carry the `ExecutionResult`
  keys (`tool`, `result`, `validation`, `preconditions_failed` may be null or
  empty); refusal payloads (`confirmation_required`, `confirmation_invalid`,
  not found, unhealthy, invalid parameters, blocked) are unchanged.
- `RevitClient.connect()` raises `ConnectionError` when the connect times
  out, instead of `TimeoutError`.

### Removed

- The 0.1 slot models `SlotState`, `SlotStatus`, `SlotSource`, `QuestionItem`,
  `ActionStep`, `ConstraintViolation` from `revit_bridge.spec`.

## [0.2.0] - 2026-09-21

The bridge itself: a project snapshot before interpretation, a TaskSpec in
which every parameter has a source, a one-time confirmation token instead of a
model-set boolean, a validator that declares completion, and an evidence
ledger. Breaking for hosts: `spec_confirmed` is gone (see Changed).

### Added

- Capability pack v1 (`revit_bridge.capabilities.schema`): `validate_pack(data)`
  checks a pack file as written (`schema_version: 1`, semver `version`,
  explicit `source` and `required` on every parameter, `unit`, `choices_from`,
  placeholders declared, `preconditions` of kind `levels_min` /
  `category_present` or `text`, `validator` configuration, `fixtures`).
  `solidify` / `update` refuse a pack that fails it. The built-in packs are
  v1 files: every parameter carries `source` / `required` / `unit`, coordinates
  and dimensions are the designer's (no more `(0, 0)` defaults), each pack
  declares evaluable preconditions and a validator, and descriptions no longer
  say "uses first available ...". Eight packs ship as built-ins; the three
  zero-parameter demo packs (`create_column`, `create_column_2`,
  `create_room`, with type, level and coordinates hard-coded) moved to
  `capabilities/examples/`, which the store never loads.
- `evaluate_preconditions(pack, snapshot)`; `run_tool` takes a snapshot (5 s
  budget) and refuses with `preconditions_failed` before consuming the token.
- Validators (`revit_bridge.validators`): `created_ids`, `count_delta`,
  `param_equals`, each one read-only C# probe; `category` / `expected` may be
  `"{param}"`. `run_tool` runs `before` -> execute -> `after`; **`success` now
  means Revit succeeded and the validator passed**; a failed assertion returns
  `success: false, error: "validation_failed"` with the result attached.
- Evidence ledger (`revit_bridge.evidence.ledger`): one JSONL line per
  execution in `<evidence dir>/<YYYY-MM>.jsonl`; `run_tool` / `execute_code`
  return `evidence_id`, `validation` and `warnings`. MCP `evidence(limit, tool)`,
  `validate(evidence_id)` (re-runs the assertion now) and the resource
  `revit://evidence/recent`.
- Plugin 0.2.0: `SKILL.md` rewritten for the 0.2 flow (snapshot, `query` for
  reads, `missing_params` / `reconcile`, `confirm_spec` + `token`, an
  *Interpretations* section for units and range words, `default:<tool>` in the
  source table; English trigger phrases first in `description`; requires the
  0.2 server). `plugin/evals/`: five `claude plugin eval` cases (the three
  baseline tasks and two second-turn cases) with mocks of the MCP server; run
  them by hand, they are not part of CI. `SERVER_INSTRUCTIONS` rewritten as the
  0.2 flow summary.
- `list_tools` returns JSON with `version`, `parameters` (source, required,
  unit), `preconditions`, `validator`; `solidify_tool` takes `parameters` as a
  list and an optional `validator`, and returns the problems of an invalid pack.
- TaskSpec (`revit_bridge.spec.models`): `Source`, `ParamBinding`,
  `Interpretation`, `Action`, `WorkflowState`, `TaskSpec` with
  `execution_projection()`, `canonical_json()`, `spec_hash()` and `card()`.
- `revit_bridge.spec.rules`: `validate_spec(spec, pack)` (the 5.1 rules:
  `missing_param`, `no_evidence`, `unsourced_choice`, `guessed_value`,
  `default_not_declared`, `bad_preference_ref`, `unconfirmed_interpretation`,
  `blocked_code`), `missing_params(pack, known, snapshot, language)` ->
  questions with real options from a snapshot, `reconcile(draft, snapshot,
  pack)` -> conflicts (`not_found`, `ambiguous`, `unit_missing`,
  `stale_snapshot`), questions, interpretations required (units, range words
  such as "在 F2 上"), `ready`.
- Confirmation gate (`revit_bridge.spec.gate`): `confirm_spec(spec)` validates a
  TaskSpec and issues a one-time token bound to the hash of its execution
  projection; `run_tool` / `execute_code` redeem it (`confirmation_required`
  without one, `confirmation_invalid` with reason `expired | used | mismatch |
  unknown`). Tokens expire after `REVIT_BRIDGE_CONFIRM_TTL` seconds (default
  600) and survive a server restart once via `<evidence dir>/pending/`.
- MCP tools `missing_params(tool, known, language?)`, `reconcile(spec, snapshot?)`
  and `confirm_spec(spec, confirmed_by?, channel?)`.
- `get_project_snapshot(categories?)` MCP tool and `revit_bridge.snapshot.take_snapshot`:
  one read-only C# block plus one family-types command return a `ProjectSnapshot`
  (document, units, active view, levels, grids, family types of the requested
  categories, selection, links, phases, `warnings` for partial failures,
  `fingerprint` over title + Revit version + levels). No confirmation gate.
- `query(kind, args)` MCP tool and `revit_bridge.snapshot.run_query`: read-only
  kinds `levels`, `grids`, `family_types`, `elements`, `selection`,
  `view_elements`, `units`, `counts` with whitelisted `args` (`categories`,
  `category`, `limit` clamped to 200); unknown kinds return
  `{"error": "unknown_kind", "kinds": [...]}`. New read-only templates
  `RevitQueryExecutor.get_grids / get_elements / get_view_elements / get_counts`.
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
  Updates from several hosts sharing one data directory are serialised
  through `usage.json.lock` (a writer that cannot take the lock within 2 s
  proceeds anyway; a lock older than 10 s is treated as abandoned). The
  counters are advisory, not an audit trail.
- `ToolStore.render(name, params)` returns `(code, errors)` and refuses code in
  which a `{placeholder}` survives substitution; `render_code` keeps the 0.1
  shape. A parameter without a default must be given a value even when
  `required: false`.
- `ToolStore()` raises `ValueError` when its user directory is the built-in
  pack directory.
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

- `ToolStore.health_check` recommends `write_new_code` where 0.1 said
  `fallback_to_rag`; `solidify_tool` no longer takes `tags`.
- **Breaking:** `execute_code` and `run_tool` take `token` instead of
  `spec_confirmed`; a model-set boolean no longer authorises anything. The
  plugin hook denies calls without a `token`. `REVIT_BRIDGE_ALLOW_UNCONFIRMED=1`
  still lifts the gate for host-internal flows (removed in phase 6).
- `RevitClient.ping()` runs the read-only `return document.Title;` probe
  instead of `say_hello` (a dialog in Revit).
- `revit-bridge check` (and `revit://connection-status`) probes with the
  read-only snippet `return document.Title;` instead of `say_hello`, which
  opened a dialog in Revit; the output gains a `document` field.
- `REVIT_BRIDGE_CAPABILITIES_DIR` now overrides only the user directory; the
  built-in packs stay visible. `default_capabilities_dir()` is kept for 0.1
  callers and always names the user directory.
- Parameter sources written as `query:<kind>` / `interactive:<kind>` are read
  as `tool:<kind>`, and a `tool:<query>` source without `choices_from` gets
  `choices_from: <query>` so `get_tool_choices` can resolve it.
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

[Unreleased]: https://github.com/revitbridge/revit-bridge/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/revitbridge/revit-bridge/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/revitbridge/revit-bridge/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/revitbridge/revit-bridge/releases/tag/v0.1.0

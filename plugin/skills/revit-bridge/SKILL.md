---
name: revit-bridge
description: Use whenever the designer wants something built, changed, queried or reported in a running Autodesk Revit model - walls, columns, beams, floors, rooms, levels, grids, doors, windows, MEP, clearances, data exports - through the revit-bridge MCP tools (create wall, place column, change height, list levels, count elements, export). 在 Revit 中创建、修改、查询构件时使用（墙、柱、梁、楼板、房间、标高、轴网、门窗、管线、净高、数据导出）。Enforces the bridge protocol - snapshot first, every parameter traced to a source, ask what you cannot know, confirm the spec, execute only with a token from confirm_spec, read the validator.
---

# revit-bridge

You are connected to a live Revit model through the `revit-bridge` MCP server (**0.2 or newer**: it has `get_project_snapshot`, `query`, `confirm_spec`; if those tools are missing, tell the designer to update with `uvx --refresh revit-bridge` and stop). You are not the designer. Your job is to turn their words into a TaskSpec in which **every parameter has a source**, get that spec confirmed, run it with the token, and report what the validator found.

Answer in the designer's language. Keep questions short; keep the spec card complete.

## The bridge flow

Always in this order. Do not skip a step because the task looks simple.

1. **Snapshot** - `get_project_snapshot` (pass `categories` for types you need, e.g. `["OST_StructuralColumns"]`). Keep its `fingerprint`; the spec names it.
2. **Read** - anything else you need to know is `query(kind, args)`: `levels`, `grids`, `family_types` (`categories`), `elements` (`category`, `limit`), `selection`, `view_elements`, `units`, `counts`. Reads need no token. Never write C# to read.
3. **Choose the pack** - `list_tools`. If one matches, `missing_params(tool, known)` returns the questions still open with the real options (levels, types). Only when no pack fits do you write `execute_code` C#.
4. **Ask** - one round covering every open question (see *Questions*). Do not execute while a question is open.
5. **Reconcile** - build the draft TaskSpec, call `reconcile(spec, snapshot)`. Fix every `conflict` and answer every `question`; add every `interpretations_required` item to the spec and get it confirmed. Repeat until `ready` is true.
6. **Confirm** - show the spec card (below) and wait for an explicit "yes / confirm / go ahead". A silent designer has not confirmed. Then `confirm_spec(spec)` returns `{token, expires_at, card}` - or `{errors}`, which you fix in the spec, never by arguing.
7. **Execute** - `run_tool(name, params, token)` or `execute_code(code, parameters, token)` with **exactly** the confirmed values (same numbers, same code, byte for byte). The token is one-time, expires in 10 minutes and is bound to that tool + params; anything else is refused as `confirmation_invalid`. A hook denies both calls without a token. Never invent a token.
8. **Verify and report** - read `success`, `error`, `validation` and `evidence_id`. `success` is true only when Revit succeeded **and** the pack's validator passed; `validation_failed` means the model did not change as claimed - say so. Quote ids, counts and `validation.checks`. `validate(evidence_id)` re-runs the assertion later; `evidence()` lists past runs.

## Parameter source protocol

Every value in a spec comes from exactly one source, written in `source` with its `evidence`:

| Source | Meaning | `evidence` |
|---|---|---|
| `designer` | the designer's own words | the words, quoted; do not turn "3 m" into 3000 without an interpretation |
| `tool` | a snapshot, `query`, `get_tool_choices` or `missing_params` option | `tool:<name>`; the designer still picks when there is more than one candidate |
| `answer` | the designer's answer to a question you asked | the question id (`q_level_name`) |
| `preference` | a value from `references/standards/personal/` | `preference:<name>`; shown on the card, the designer can override it |
| `default` | a default the pack declares (`default: 3000` on the parameter) | `default:<tool>`; allowed only for that parameter, shown on the card |

Anything else is you deciding for the designer. That is the failure this skill exists to prevent. `confirm_spec` enforces it: `unsourced_choice` (a level/type not from a query or an answer), `guessed_value` (a designer parameter from elsewhere), `default_not_declared`, `no_evidence`, `bad_preference_ref`, `missing_param`.

Recognise your own rationalisations and do the opposite:

- "Level 1 is the obvious level" - query levels, then ask if more than one.
- "Generic - 200mm is a common wall type" - query types, then ask.
- "I'll put it at (0,0) for now" - ask where. Coordinates and dimensions are always the designer's.
- "This parameter is probably optional" - assume it is required; ask.
- "The designer would surely want the default" - show the default on the card; let them say so.
- "I can infer it from the Revit API docs" - the docs tell you the type, never the designer's value.

Never invent element ids, level names, type names or coordinates.

## Interpretations

A reading you make is not a value: it goes into `interpretations` and must be confirmed (`confirmed: true`) before `confirm_spec` accepts the spec. Always write one for:

- **units** - a number without a unit for a parameter that has one: `"3000 按 mm 理解"`. Put the unit on the binding (`unit: "mm"`) once confirmed.
- **range words** - "on F2 / 在 F2 上 / 整层 / 所有 ..." scoping a level: `"'F2 上' 理解为底部约束为 F2"`. `reconcile` lists these as `interpretations_required`.
- **quantities and defaults you spelled out** - "two walls" = 2 elements, "as usual" = a named preference.

Show interpretations on the card as their own lines; the designer confirms them with the card.

## Questions

A question is complete when it names the parameter, says why it is needed, offers the real options when a tool result exists (levels, types, elements - as returned, not invented), and leaves room for "other". `missing_params` gives you this shape (`id`, `param`, `text`, `why`, `options`). Group all open parameters into one round; order: ambiguity -> family type -> level -> position -> dimensions -> the rest.

Quantities: "two / three / several / 多个 / 各" means N elements. Shared parameters (type, height, level) are asked once; per-element parameters (positions, hosts) are asked N times, never reused.

Ambiguity you must not resolve yourself:

| The designer says | Could mean | Ask |
|---|---|---|
| back / 背面 | north side or the rear of the building | which |
| front / 前面 | south side or the entrance side | which |
| left / right / 左边 / 右边 | depends on the viewpoint | which viewpoint |
| big / standard / 大的 / 标准 | an unknown size | the number |
| "the wall" / 那面墙 | which of several | the element (select in Revit or `query elements`) |
| 3m / 3000 | metres vs millimetres are usually clear, but 3 alone is not | the unit |

## The TaskSpec

What `reconcile` and `confirm_spec` take (JSON):

```json
{"task": "在 F2 建三根结构柱",
 "action": {"kind": "run_tool", "tool": "create_structural_column"},
 "parameters": [
   {"name": "level_name", "value": "F2", "source": "answer", "evidence": "q_level_name"},
   {"name": "type_name", "value": "UC305x305x97", "source": "tool", "evidence": "tool:get_tool_choices"},
   {"name": "x", "value": 0, "unit": "mm", "source": "designer", "evidence": "(0,0)"},
   {"name": "y", "value": 0, "unit": "mm", "source": "designer", "evidence": "(0,0)"}],
 "interpretations": [{"param": null, "text": "'F2 上' 理解为底部约束为 F2", "confirmed": true}],
 "steps": ["snapshot (done)", "run_tool x3", "count columns on F2 after"],
 "snapshot_fingerprint": "815f955df85f56f1", "language": "zh"}
```

For `execute_code`: `"action": {"kind": "execute_code", "code": "...", "code_parameters": []}` and the same parameter lines for every value the code embeds. One spec per execution; N elements = N specs (or one pack call per element), each with its own token.

## The spec card

Show it before asking for confirmation, in the designer's language, as plain text (no JSON). `confirm_spec` returns the same card as `card`:

```
Task: create 3 structural columns
Tool: create_structural_column (run_tool)
Parameters:
  level_name = "F2"                    source: answer (q_level_name)
  type_name  = "UC305x305x97"          source: tool (tool:get_tool_choices)
  x, y       = (0,0) (6000,0) (12000,0) mm   source: designer ("at 0, 6000 and 12000")
Interpretations:
  [ ] 'F2 上' read as base level F2
Steps: 1 snapshot (done)  2 run_tool x3  3 count columns on F2 after
Confirm? (yes / change something)
```

Every line has a source. If a line has none, you are not ready to ask for confirmation.

## Faithful reporting

Report exactly what the tool response says. If `success` is false, say it failed and quote `error`; if `error` is `validation_failed`, say what the validator expected and found (`validation.checks[].detail`). Never claim success on an error, never paraphrase an error into something milder, never say "created" when only the token was issued. `preconditions_failed` and `validator_before_failed` mean the model is not in the state the pack needs - report the reason, do not retry blindly. If a tool fails twice, stop retrying it and write fresh code with `execute_code` (still through steps 5-7).

## Self-check before you ask for confirmation

1. Does every required parameter of the tool/code have a line on the card? `missing_params` returns nothing?
2. Does every line carry a source from the table above? No source = you invented it.
3. Did every `choices_from` / `tool:*` value come from a snapshot, a query or `get_tool_choices`? Not = you guessed a name.
4. Is every unit and range word an interpretation line, confirmed?
5. For N > 1, are there N sets of per-element values?
6. Is `reconcile` `ready`? Is the execution step last, after the token?

## References

Read the one that matches the task before drafting the spec.

- `references/patterns/` - what to ask per operation type: `point_based`, `line_based`, `surface_based`, `hosted`, `modify`, `delete`, `query`, `composite`.
- `references/workflows/` - multi-stage tasks: `clearance_calculation`, `element_data_export`.
- `references/standards/company/` - company/GB-T aligned rules (naming, levels and grids, walls, rooms, views and sheets, MEP colours, MEP routing, office layout). They constrain values and give you things to check; they are not a source for a value the designer has to choose.
- `references/standards/personal/` - the designer's own preferences (`preference:<name>` sources). Start from `TEMPLATE.md`.

## Notes on the tools

- Revit internal units are feet; the built-in packs take millimetres and convert.
- `execute_code` runs inside an open Transaction with `document` in scope; do not open another; end with `return <object>;`. Read-only tools also run inside a transaction on the add-in and fail on a read-only document.
- `solidify_tool(name, code, description, parameters, source_query, validator)` saves code that worked as a v1 pack: every parameter with `source`, `required`, `unit`; a `validator` (`created_ids`, `count_delta`, `param_equals` - use BuiltInParameter names such as `WALL_USER_HEIGHT_PARAM`, not display names).
- The add-in must be running (ribbon *Revit MCP Switch*); if a call fails with a connection error, say so and stop.

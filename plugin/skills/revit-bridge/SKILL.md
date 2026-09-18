---
name: revit-bridge
description: 在 Revit 中创建、修改、查询构件时使用（墙、柱、梁、楼板、房间、标高、轴网、门窗、管线、净高、数据导出）。Use whenever the designer wants something built, changed, queried or reported in a running Autodesk Revit model through the revit-bridge MCP tools. Enforces the bridge protocol - snapshot first, every parameter traced to a source, ask what you cannot know, confirm the spec, execute with spec_confirmed=true, verify.
---

# revit-bridge

You are connected to a live Revit model through the `revit-bridge` MCP server. You are not the designer. Your job is to turn their words into a task spec in which **every parameter has a source**, get that spec confirmed, run it, and report what actually happened.

Answer in the designer's language. Keep questions short; keep the spec card complete.

## The bridge flow

Always in this order. Do not skip a step because the task looks simple.

1. **Snapshot** - learn what exists before you propose anything. Call `get_project_snapshot` when the server offers it (revit-bridge >= 0.2). On servers without it (0.1.x): `list_tools`, then `get_tool_choices(<tool>)` for the tool you intend to use; anything else you need to know (existing grids, selected elements, counts) is a read-only query - list it in the spec as its own step, because queries are executions too.
2. **Reconcile** - compare the request with the snapshot. Map each requested value onto something that exists (a level name, a family type, an element). Anything that does not exist or is ambiguous becomes a question.
3. **Ask** - one round of questions covering every missing or ambiguous parameter (see *Questions*). Use the host's question tool (`AskUserQuestion` in Claude Code) or plain chat. Do not execute while a question is open.
4. **Confirm the spec** - show the spec card (see below) and wait for an explicit "yes / confirm / go ahead". A silent designer has not confirmed.
5. **Execute** - `run_tool(..., spec_confirmed=true)` or `execute_code(..., spec_confirmed=true)`, exactly as confirmed. A hook denies both without `spec_confirmed=true`; the server refuses too. Never set it true before step 4.
6. **Verify and report** - read the tool response, restate what was created/changed (ids, counts, names), quote errors verbatim. Where possible confirm with a query (count after vs before).

## Capability library first

Before writing C#: `list_tools`. If a tool matches the task, use `run_tool`; only write `execute_code` when no tool fits or a tool keeps failing. When new code worked and is reusable, offer `solidify_tool` with declared parameter sources.

## Parameter source protocol

Every value you put into a spec must come from exactly one of these sources, and the source is written on the spec card:

| Source | Meaning | Rule |
|---|---|---|
| `designer` | the designer's own words | quote them; do not "interpret" 3 m as 3000 without saying so |
| `tool:<name>` | a result of `get_tool_choices`, the snapshot, or a query you ran | the designer still picks when there is more than one candidate |
| `answer` | the designer's answer to your question | |
| `preference:<name>` | a value from `references/standards/personal/` | allowed only if it is written there; show it on the card as a preference the designer can override |
| `default:<tool>` | a default declared by the tool definition | allowed only when the definition declares it; show it on the card |

Anything else is you deciding for the designer. That is the failure this skill exists to prevent.

Recognise your own rationalisations and do the opposite:

- "Level 1 is the obvious level" - query levels, then ask if more than one.
- "Generic - 200mm is a common wall type" - query types, then ask.
- "I'll put it at (0,0,0) for now" - ask where.
- "This parameter is probably optional" - assume it is required; ask.
- "The designer would surely want the default" - show the default on the card; let them say so.
- "I can infer it from the Revit API docs" - the docs tell you the type, never the designer's value.

Never invent element ids, level names, type names or coordinates. Never call `run_tool` with a `choices_from` / `query:*` parameter you did not obtain from `get_tool_choices` or a query.

## Questions

A question is complete when it names the parameter, says why it is needed, offers the real options when a tool result exists (levels, types, elements - as returned, not invented), and leaves room for "other". Group all open parameters into one round; order: ambiguity -> family type -> level -> position -> dimensions -> the rest.

Quantities: "two / three / several / 多个 / 各" means N elements. Shared parameters (type, height, level) are asked once; per-element parameters (positions, hosts) are asked N times, never reused.

Ambiguity you must not resolve yourself:

| The designer says | Could mean | Ask |
|---|---|---|
| back / 背面 | north side or the rear of the building | which |
| front / 前面 | south side or the entrance side | which |
| left / right / 左边 / 右边 | depends on the viewpoint | which viewpoint |
| big / standard / 大的 / 标准 | an unknown size | the number |
| "the wall" / 那面墙 | which of several | the element (select in Revit or query) |
| 3m / 3000 | metres vs millimetres are usually clear, but 3 alone is not | the unit |

## The spec card

Show it before asking for confirmation, in the designer's language, as plain text (no JSON):

```
Task: create 3 structural columns
Tool: create_structural_column (run_tool)
Parameters:
  level_name = "F2"                    source: answer (options from tool:get_tool_choices)
  type_name  = "UC305x305x97"          source: tool:get_tool_choices, chosen by designer
  positions  = (0,0) (6000,0) (12000,0) mm   source: designer
  height     = 3600 mm                 source: preference:default_column_height (override?)
Steps: 1 query levels (done)  2 run_tool  3 count columns on F2 after
Confirm? (yes / change something)
```

Every line has a source. If a line has none, you are not ready to ask for confirmation.

## Faithful reporting

Report exactly what the tool response says. If `success` is false, say it failed and quote `error`. Never claim success on an error, never paraphrase an error into something milder, never say "created" when the response only says "queued". If a tool fails twice, stop retrying it and write fresh code with `execute_code` (still through steps 4-5).

## Self-check before you ask for confirmation

1. Does every required parameter of the tool/code have a line on the card? Missing = it will fail.
2. Does every line carry a source from the table above? No source = you invented it.
3. Did every `choices_from` / `query:*` value come from a tool result? Not = you guessed a name.
4. For N > 1, are there N sets of per-element values?
5. Is there any value you "feel" the designer "probably" wants? Then it is a question, not a value.
6. Is the execution step the last step, after confirmation? If not, reorder.

## References

Read the one that matches the task before drafting the spec.

- `references/patterns/` - what to ask per operation type: `point_based`, `line_based`, `surface_based`, `hosted`, `modify`, `delete`, `query`, `composite`.
- `references/workflows/` - multi-stage tasks: `clearance_calculation`, `element_data_export`.
- `references/standards/company/` - company/GB-T aligned rules (naming, levels and grids, walls, rooms, views and sheets, MEP colours, MEP routing, office layout). They constrain values and give you things to check; they are not a source for a value the designer has to choose.
- `references/standards/personal/` - the designer's own preferences (`preference:<name>` sources). Start from `TEMPLATE.md`.

## Notes on the tools

- Revit internal units are feet; the built-in tools take millimetres and convert.
- `execute_code` runs inside an open Transaction with `document` in scope; do not open another; end with `return <object>;`.
- The add-in must be running (ribbon *Revit MCP Switch*); if a call fails with a connection error, say so and stop.

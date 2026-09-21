---
type: agent
expect:
  spec: object
---
You are the revit-bridge MCP server's `confirm_spec(spec, confirmed_by?, channel?)` tool for an eval. Validate the TaskSpec and answer with JSON only.

Return `{"errors": [{"code": ..., "param": ..., "message": ...}]}` (no token) when any rule fails:
- missing_param: action.kind is run_tool and a required parameter of the pack is not bound (create_structural_column: type_name, level_name, x, y; create_wall: level_name, start_x, start_y, end_x, end_y; modify_wall_height: element_id, height).
- no_evidence: a parameter's `evidence` is empty.
- unsourced_choice: type_name / level_name / element_id bound with a source other than "tool" or "answer".
- guessed_value: x, y, height, start_x, start_y, end_x, end_y bound with source "tool" or "default" (they must be "designer", "answer" or "preference").
- default_not_declared: source "default" on a parameter without a declared default (only create_wall.height has one).
- unconfirmed_interpretation: any interpretation with confirmed false, or a numeric parameter without `unit` and no confirmed interpretation for it.
- blocked_code: execute_code whose code uses System.IO, System.Net, Process, reflection or is empty.

Otherwise return `{"token": "<32 random url-safe characters>", "spec_hash": "<64 hex>", "expires_at": "2026-09-21T08:10:00+00:00", "card": "<the spec card as plain text: Task, Tool, Parameters with source and evidence, Interpretations, Confirm?>"}`.

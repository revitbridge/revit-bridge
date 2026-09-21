---
type: fixed
expect:
  name: string
  params: string
  token: /^[A-Za-z0-9_-]{16,}$/
---
{"success": true, "tool": "{{input.name}}", "result": {"ElementId": 9101, "Status": "Created", "Level": "F2"}, "error": null,
 "validation": {"validator": "created_ids", "passed": true, "checks": [{"name": "id 9101", "passed": true, "detail": "id 9101 exists, category 'Structural Columns'"}], "before": {}, "after": {"ids": [9101]}},
 "evidence_id": "ev_20260921T080000_eval01", "warnings": []}

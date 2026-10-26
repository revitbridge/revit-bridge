---
name: revit-collect
description: "Collecting and filtering Revit elements in C#: FilteredElementCollector, categories, instances versus types, view and selection scopes. Use when code finds, counts or iterates elements."
---

# revit-collect

Finding elements: collector filters, the difference between placed elements and their types, the active view and the current selection.

Lessons in this category are chosen and ordered by the revit-bridge MCP server's `get_guidance` tool (by stage, API symbols, OST_* categories, compiler error codes and exceptions); `get_guidance(mode="read", ids=[...])` returns a lesson's full text.

## Lessons

- [collector-needs-filter](lessons/collector-needs-filter.md) - apply `OfCategory` / `OfClass` / `WhereElementIs...` before reading a collector
- [instances-vs-types](lessons/instances-vs-types.md) - a category holds its types: say `WhereElementIsNotElementType` or `WhereElementIsElementType`
- [selection-get-element-ids](lessons/selection-get-element-ids.md) - `Selection.GetElementIds()`; there is no `Selection.Elements`

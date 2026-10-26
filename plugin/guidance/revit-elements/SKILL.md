---
name: revit-elements
description: "Creating and changing walls, floors, doors, windows and rooms in Revit C#. Use when code creates or modifies these elements."
---

# revit-elements

Model elements of the architectural disciplines: creation calls, hosts, constraints and the parameters that drive them.

Lessons in this category are chosen and ordered by the revit-bridge MCP server's `get_guidance` tool (by stage, API symbols, OST_* categories, compiler error codes and exceptions); `get_guidance(mode="read", ids=[...])` returns a lesson's full text.

## Lessons

- [floor-ceiling-type-and-level-ids](lessons/floor-ceiling-type-and-level-ids.md) - `Floor.Create(document, loops, floorType.Id, level.Id)`: another kind of id throws `ArgumentException`

---
name: revit-mep
description: "MEP in Revit C#: pipes, ducts, systems and connectors. Use when code creates or inspects MEP elements."
---

# revit-mep

Mechanical, electrical and plumbing elements: systems, connectors and routing.

Lessons in this category are chosen and ordered by the revit-bridge MCP server's `get_guidance` tool (by stage, API symbols, OST_* categories, compiler error codes and exceptions); `get_guidance(mode="read", ids=[...])` returns a lesson's full text.

## Lessons

- [pipe-rebuild-keeps-diameter](lessons/pipe-rebuild-keeps-diameter.md) - `Pipe.Create` takes no size: a new segment gets the type's default diameter; split with `PlumbingUtils.BreakCurve` and copy `RBS_PIPE_DIAMETER_PARAM`

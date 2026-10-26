---
name: revit-geometry
description: "Locations and geometry in Revit C#: LocationCurve and LocationPoint, curves, transforms and linked models. Use when code measures, places or moves elements."
---

# revit-geometry

Where elements are and how to measure them: location curves and points, curve lengths and end points, creating lines.

Lessons in this category are chosen and ordered by the revit-bridge MCP server's `get_guidance` tool (by stage, API symbols, OST_* categories, compiler error codes and exceptions); `get_guidance(mode="read", ids=[...])` returns a lesson's full text.

## Lessons

- [wall-length-from-location](lessons/wall-length-from-location.md) - `(wall.Location as LocationCurve).Curve`, not `wall.LocationCurve`
- [curve-length-property](lessons/curve-length-property.md) - `Curve.Length` is a property; there is no `GetLength()`
- [line-create-bound](lessons/line-create-bound.md) - `Line.CreateBound(XYZ, XYZ)`, not `Application.Create.NewLineBound`

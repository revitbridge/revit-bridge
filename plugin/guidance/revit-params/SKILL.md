---
name: revit-params
description: "Reading and writing Revit parameters in C#: BuiltInParameter, shared parameters, StorageType and unit conversion. Use when code reads or sets parameter values."
---

# revit-params

Parameter access: finding the right parameter in a language-independent way, reading it by its storage type and writing it with the matching `Set` overload.

Lessons in this category are chosen and ordered by the revit-bridge MCP server's `get_guidance` tool (by stage, API symbols, OST_* categories, compiler error codes and exceptions); `get_guidance(mode="read", ids=[...])` returns a lesson's full text.

## Lessons

- [lookup-parameter-storage-type](lessons/lookup-parameter-storage-type.md) - `LookupParameter` can return null; read by `StorageType`, not `AsValueString`
- [builtin-parameter-first](lessons/builtin-parameter-first.md) - `get_Parameter(BuiltInParameter.X)` instead of a translated display name

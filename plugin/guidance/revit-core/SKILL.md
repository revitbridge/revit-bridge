---
name: revit-core
description: "Required reading before writing Revit C# through revit-bridge: the execute_code snippet contract (a method body with document and parameters, the transaction already open, the allowed references), internal units, ElementId and other version differences, and the Revit 2024-2027 runtime matrix. Use at the start of any task that may write code for Revit."
---

# revit-core

What every snippet and every port has to respect: the shape of an `execute_code` snippet, who owns the transaction, Revit's internal units, API changes between releases, and which framework and SDK each release needs.

Lessons in this category are chosen and ordered by the revit-bridge MCP server's `get_guidance` tool (by stage, API symbols, OST_* categories, compiler error codes and exceptions); `get_guidance(mode="read", ids=[...])` returns a lesson's full text.

## Lessons

- [snippet-contract](lessons/snippet-contract.md) - the wrapper: `document` and `parameters` in scope, usings, references, refused identifiers, `return <object>;`
- [internal-units-feet](lessons/internal-units-feet.md) - lengths are decimal feet; convert with `UnitUtils` and `UnitTypeId`
- [elementid-value](lessons/elementid-value.md) - `ElementId.Value` (long); `IntegerValue` is obsolete in 2024-2025 and gone from 2026
- [transaction-owned-by-bridge](lessons/transaction-owned-by-bridge.md) - never start a `Transaction` in a snippet; nest a `SubTransaction` if needed
- [runtime-matrix-2024-2027](lessons/runtime-matrix-2024-2027.md) - target framework, `global.json` and manifest folder per Revit release

## Writing lessons

Keep a lesson body at or under 2300 UTF-8 bytes (the loader warns above that): a failed `check_code` inlines at most 2400 bytes of a lesson, and the margin covers a translation and the truncation line.

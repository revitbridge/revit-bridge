# Query elements / 查询操作

Read-only: count, list, read parameters, filter.

Trigger words: 查询、获取、列出、显示、统计、搜索、查找、有多少、有哪些、信息 / query, get, list, show, count, search, find, how many, info.

## Parameters and where each comes from

Most queries need few questions. Ask only when the target is unclear:

- "look at the info" -> which elements?
- "how many" -> which category? which scope (model / view / level)?
- "this wall's height" -> which wall (select in Revit or query candidates)?

Built-in query tools (`query_levels`, `query_model_stats`) go through `run_tool`; anything else is `execute_code` with a collector. Both are executions: put them on the card as read-only steps and confirm (the gate applies), unless the host has already lifted the gate for queries.

## Common mistakes

- `FilteredElementCollector` needs `OfClass()` or `OfCategory()`.
- Instances vs types: `WhereElementIsNotElementType()`.
- Parameter names may be localised; prefer `BuiltInParameter` where one exists.
- Report the numbers the query returned, with units converted (feet -> mm/m/m2).

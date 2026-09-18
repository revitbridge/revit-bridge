# Element data export and reporting / 元素数据导出

Trigger words: 导出、报表、统计、汇总、清单、明细、表格、数据、属性值、批量提取 / export, report, summary, schedule, list, table, data, extract, batch.

## Stages (a blueprint, trim it to the request)

1. **Data source** - category (walls / columns / rooms / ...), scope (model / current view / selection), links or not. Ask what is not stated.
2. **Collect** - `FilteredElementCollector` with category/type filters, optional parameter filter ("walls higher than 3 m"), optional view filter.
3. **Properties** - which parameters. If the designer names them, use those; otherwise ask, offering the common set for the category (name, type, level, key dimension). Read with `LookupParameter` / `get_Parameter(BuiltInParameter...)`; use `AsValueString()` for display values and convert internal units for numbers.
4. **Output** - ask the format (table in chat / CSV file path / write to a shared parameter), sorting (level, type) and whether a totals row is wanted.

Stages 1-3 are read-only; stage 4 may write a file or a parameter. One spec card for the whole plan; the report states the row count and where the output went.

## Short-cuts that are not export

- "how many walls" -> a count, pattern `query`.
- "the height of this wall" -> one value, pattern `query`.

## API quick reference

| Purpose | API |
|---|---|
| parameter by name | `element.LookupParameter("name")` (localised) |
| built-in parameter | `element.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM)` |
| value | `.AsString()` / `.AsDouble()` / `.AsInteger()` / `.AsValueString()` |
| type name | `document.GetElement(element.GetTypeId()).Name` |
| rooms | `Room.Area`, `Room.Volume`, `Room.Number` |

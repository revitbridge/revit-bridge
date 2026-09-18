# Delete elements / 删除操作

Trigger words: 删除、移除、清除、去掉、拆除 / delete, remove, clear, demolish.

## Parameters and where each comes from

| Parameter | Source | Notes |
|---|---|---|
| target element(s) | `tool:` query (category / filter / view) then the designer confirms the exact list or count, or the designer selects in Revit | never delete by a guessed id |
| scope for "all X" | designer / answer | whole model vs current view vs a level |

Before the card: query and show what will be deleted (count and a sample of names/ids). Deleting a host (wall) also deletes its doors and windows - say so on the card.

## Common mistakes

- `Document.Delete` inside the open transaction is committed by the add-in; there is no undo from here.
- Batch deletes use `FilteredElementCollector` + `OfCategory`, not one id at a time.

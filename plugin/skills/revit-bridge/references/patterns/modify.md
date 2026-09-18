# Modify elements / 修改操作

Move, rotate, change a parameter, change type.

Trigger words: 修改、移动、旋转、改变、调整、设置、更改、偏移 / modify, move, rotate, change, adjust, set, update, offset.

## Parameters and where each comes from

| Parameter | Source | Notes |
|---|---|---|
| target element(s) | `tool:` query (by category / filter) then the designer confirms the list, or the designer selects in Revit | never assume "the last created element" |
| kind of change | designer / answer | move / rotate / parameter / type |
| move: vector (dx, dy, dz) mm | designer / answer | relative displacement, not a target position |
| rotate: axis point and angle | designer / answer | both required |
| parameter: name and new value | designer / answer; verify the parameter exists and is writable via a query | localised names differ (中文参数名) |
| type: new type | `tool:get_tool_choices` then the designer picks | `ChangeTypeId()`, not assignment |

Batch changes ("all walls on F2 to 3600 high"): query the set first, show the count and a sample on the card, then execute. The count after execution goes into the report.

## Common mistakes

- A move vector is relative; "move to (x, y)" needs the current position first (query it).
- Rotation needs an axis (a point + direction) and an angle in radians in the API; the card shows degrees.

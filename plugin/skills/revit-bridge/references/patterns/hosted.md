# Hosted elements / 宿主构件

Doors, windows and other families that must sit on a host (usually a wall).

Trigger words: 门、窗、窗户、单开门、双开门、推拉门、飘窗、落地窗、平开窗 / door, window, casement, sliding door, sliding window.

## Parameters and where each comes from

| Parameter | Source | Notes |
|---|---|---|
| host element | `tool:` query (elements:OST_Walls) then the designer picks, or the designer selects in Revit | never invent an ElementId; N elements may have N hosts |
| family type | `tool:get_tool_choices` (doors / windows) then the designer picks | |
| level | `tool:get_tool_choices` then the designer picks | |
| position on the host (x, y, z) mm | designer / answer | on the wall centre line |
| sill height (windows) | designer / answer / `preference:sill_height` if defined | measured from the level, not from the floor finish |

## Common mistakes

- `NewFamilyInstance` for hosted families needs the host `Element`, not just a point.
- Activate the `FamilySymbol` first.
- Sill height is from the level; do not add the floor thickness.

## Quantity template

Element {i}: host wall id + position (x, y, z) mm.

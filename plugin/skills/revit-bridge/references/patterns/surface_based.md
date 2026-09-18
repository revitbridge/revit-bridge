# Surface-based elements / 面状构件

Floors, roofs, ceilings, topography - created from a closed boundary.

Trigger words: 楼板、地板、板、底板、屋顶、天花、天花板、吊顶、场地、地形 / floor, slab, roof, ceiling, topography, site.

## Parameters and where each comes from

| Parameter | Source | Notes |
|---|---|---|
| type (FloorType / RoofType / CeilingType) | `tool:get_tool_choices` then the designer picks | thickness is part of the type, not a separate parameter |
| level | `tool:get_tool_choices` then the designer picks | |
| boundary points (x, y) mm | designer / answer | at least 3; the last closes to the first |
| structural? (floor, roof) | answer or declared default shown on the card | |

"5 m x 3 m" is a size, not a position: ask for the origin (or a corner) before computing the four vertices, and show the computed vertices on the card as `source: designer (5x3 m at origin from answer)`.

## Common mistakes

- `Floor.Create` needs a `CurveLoop`, not an area.
- The boundary must be closed and non-self-intersecting.

## Quantity template

Element {i}: boundary points list.

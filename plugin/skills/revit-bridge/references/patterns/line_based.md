# Line-based elements / 线性构件

Walls, beams, pipes, ducts, cable trays, conduits - anything created along a curve.

Trigger words: 墙、墙体、隔墙、承重墙、梁、横梁、主梁、次梁、管道、管线、风管、桥架、线管 / wall, beam, girder, pipe, duct, cable tray, conduit.

## Parameters and where each comes from

| Parameter | Source | Notes |
|---|---|---|
| family / wall type | `tool:get_tool_choices` then the designer picks | never a default type |
| level | `tool:get_tool_choices` then the designer picks | |
| start and end points (x, y, z) mm | designer / answer | N elements = N pairs, never reused |
| wall: height, structural? | designer / answer / declared default (shown on the card) | height is not the level |
| beam: structural type | `Autodesk.Revit.DB.Structure.StructuralType.Beam` | fully qualified |
| pipe / duct: size, system type | `tool:` query for system types, size from the designer | |

## Common mistakes

- The API wants a `Line`/`Curve`; the designer gives two points - the code converts, the spec shows points.
- Do not confuse element height with level elevation.
- With N > 1 never reuse coordinates between elements.

## Quantity template

Element {i}: start (x, y, z) end (x, y, z) mm.

# Point-based elements / 点式构件

Columns, furniture, equipment, lighting fixtures, plumbing fixtures, planting - anything placed at one point.

Trigger words: 柱、柱子、结构柱、家具、沙发、桌子、椅子、设备、灯、灯具、洁具、马桶 / column, structural column, furniture, equipment, lighting, fixture.

## Parameters and where each comes from

| Parameter | Source | Notes |
|---|---|---|
| family type | `tool:get_tool_choices` (family_types:<category>) then the designer picks | never "the first available" |
| level | `tool:get_tool_choices` (levels) then the designer picks if more than one | |
| placement point (x, y, z) mm | designer / answer | one point per element; N elements = N points, never reused |
| structural type (structural columns) | answer or the tool's declared default | `StructuralType.Column`, fully qualified in code |
| top level / height (if mentioned) | designer / answer | do not add it unasked |

## Common mistakes

- `FamilySymbol` must be activated before placement (`if (!symbol.IsActive) symbol.Activate();`).
- Structural columns need `Autodesk.Revit.DB.Structure.StructuralType.Column`; omitting it fails.
- Points are model coordinates in feet inside the API; the built-in tools take mm.

## Quantity template

Element {i}: position (x, y, z) mm - ask for all N before drafting the spec.

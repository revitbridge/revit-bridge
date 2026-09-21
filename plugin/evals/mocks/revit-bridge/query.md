---
type: fixed
expect:
  kind: [levels, grids, family_types, elements, selection, view_elements, units, counts]
---
{"kind": "{{input.kind}}", "note": "eval mock: this fake model answers every kind at once; read the part that matches kind",
 "levels": [{"id": 30, "name": "F1", "elevation_mm": 0.0}, {"id": 31, "name": "F2", "elevation_mm": 4000.0},
            {"id": 32, "name": "F3", "elevation_mm": 8000.0}, {"id": 33, "name": "RF", "elevation_mm": 12000.0}],
 "grids": {"count": 6, "items": [{"id": 101, "name": "A", "x_mm": 0}, {"id": 102, "name": "B", "x_mm": 6000}, {"id": 103, "name": "C", "x_mm": 12000},
                                 {"id": 104, "name": "1", "y_mm": 0}, {"id": 105, "name": "2", "y_mm": 6000}, {"id": 106, "name": "3", "y_mm": 12000}]},
 "family_types": {"OST_StructuralColumns": ["UC305x305x97", "UC254x254x73", "RC400x400"], "OST_Walls": ["Generic - 200mm", "Generic - 300mm"]},
 "elements": {"OST_Walls": {"total": 6, "items": [
     {"id": 5001, "name": "Generic - 200mm", "category": "Walls", "type": "Generic - 200mm", "level": "F1"},
     {"id": 5002, "name": "Generic - 200mm", "category": "Walls", "type": "Generic - 200mm", "level": "F1"},
     {"id": 5003, "name": "Generic - 200mm", "category": "Walls", "type": "Generic - 200mm", "level": "F1"},
     {"id": 5004, "name": "Generic - 200mm", "category": "Walls", "type": "Generic - 200mm", "level": "F2"},
     {"id": 5005, "name": "Generic - 200mm", "category": "Walls", "type": "Generic - 200mm", "level": "F2"},
     {"id": 5006, "name": "Generic - 300mm", "category": "Walls", "type": "Generic - 300mm", "level": "F2"}]}},
 "selection": {"count": 0, "items": []},
 "view_elements": {"view": "F1 - Structural Plan", "total": 3, "items": [{"id": 5001, "name": "Generic - 200mm", "category": "Walls"}, {"id": 5002, "name": "Generic - 200mm", "category": "Walls"}, {"id": 5003, "name": "Generic - 200mm", "category": "Walls"}]},
 "units": {"length": "mm", "raw": "autodesk.unit.unit:millimeters-1.0.1", "display_name": "Millimeters"},
 "counts": {"OST_Walls": 6, "OST_StructuralColumns": 0, "OST_Levels": 4, "OST_Grids": 6}}

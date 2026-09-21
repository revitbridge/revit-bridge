---
type: fixed
---
[{"name": "create_structural_column", "description": "Create a structural column of a named family type at X/Y on a level.", "version": "1.0.0",
  "parameters": [{"name": "type_name", "type": "string", "source": "tool:family_types", "required": true, "choices_from": "family_types:OST_StructuralColumns"},
                 {"name": "level_name", "type": "string", "source": "tool:levels", "required": true, "choices_from": "levels"},
                 {"name": "x", "type": "double", "source": "designer", "required": true, "unit": "mm"},
                 {"name": "y", "type": "double", "source": "designer", "required": true, "unit": "mm"}],
  "preconditions": [{"kind": "levels_min", "value": 1}, {"kind": "category_present", "category": "OST_StructuralColumns"}], "validator": "created_ids", "used": 0},
 {"name": "create_wall", "description": "Create a straight wall between two points on a level, with the given height.", "version": "1.0.0",
  "parameters": [{"name": "level_name", "type": "string", "source": "tool:levels", "required": true, "choices_from": "levels"},
                 {"name": "start_x", "type": "double", "source": "designer", "required": true, "unit": "mm"},
                 {"name": "start_y", "type": "double", "source": "designer", "required": true, "unit": "mm"},
                 {"name": "end_x", "type": "double", "source": "designer", "required": true, "unit": "mm"},
                 {"name": "end_y", "type": "double", "source": "designer", "required": true, "unit": "mm"},
                 {"name": "height", "type": "double", "source": "default", "required": false, "unit": "mm", "default": 3000}],
  "preconditions": [{"kind": "levels_min", "value": 1}], "validator": "count_delta", "used": 0},
 {"name": "modify_wall_height", "description": "Set the unconnected height of one wall, chosen by ElementId.", "version": "1.0.0",
  "parameters": [{"name": "element_id", "type": "integer", "source": "tool:elements", "required": true, "choices_from": "elements:OST_Walls"},
                 {"name": "height", "type": "double", "source": "designer", "required": true, "unit": "mm"}],
  "preconditions": [{"text": "the wall must not be attached at the top (attached walls ignore the unconnected height)"}], "validator": "param_equals", "used": 0},
 {"name": "query_levels", "description": "List all levels in the model with their elevations (read-only).", "version": "1.0.0", "parameters": [], "preconditions": [], "validator": "count_delta", "used": 0}]

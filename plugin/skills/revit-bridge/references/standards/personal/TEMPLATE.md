# Personal preferences / 个人偏好（模板）

Copy this file to `<your-name>.md` in this directory (or keep it in your own project under `.claude/`) and fill it in. Each entry becomes a `preference:<name>` source the assistant may use for a parameter it would otherwise have to ask about. Preferences are always shown on the spec card as `source: preference:<name>` so you can override them.

Leave an entry blank if you want to be asked every time.

## Types / 常用类型

| name | value | applies to |
|---|---|---|
| `default_wall_type` | | interior partitions when the request names no type |
| `default_column_type` | | structural columns when the request names no type |
| `default_floor_type` | | |
| `default_door_type` | | |
| `default_window_type` | | |

## Levels / 标高规则

| name | value | applies to |
|---|---|---|
| `working_level` | | new elements when the request names no level and the model has more than one |
| `column_top_rule` | | e.g. "top = next level up" |

## Dimensions / 尺寸习惯

| name | value | applies to |
|---|---|---|
| `default_wall_height` | | e.g. 3000 mm |
| `default_column_height` | | |
| `sill_height` | | windows |
| `door_size` | | e.g. 900 x 2100 mm |

## Units and coordinates / 单位与坐标

| name | value |
|---|---|
| `input_units` | mm (numbers without a unit are millimetres) |
| `origin_convention` | e.g. "project base point" |
| `grid_naming` | e.g. "letters along X, numbers along Y" |

## Output / 输出

| name | value |
|---|---|
| `report_language` | zh / en |
| `export_format` | csv / table |

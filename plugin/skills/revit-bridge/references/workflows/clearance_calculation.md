# Clearance and distance calculation / 净高与间距计算

Multi-stage: geometry has to be collected and compared before anything is reported or written.

Trigger words: 净高、净空、间距、距离、碰撞、干涉、检测、梁底、板底、顶面、底面、标高差 / clearance, headroom, distance, clash, interference, gap, beam bottom, slab top.

## Stages (a blueprint, trim it to the request)

1. **Collect sources** - which beams / which floors: current view (`OwnedByView`), a level, or elements the designer selects. Ask the scope; do not assume "all".
2. **Extract geometry** - `get_BoundingBox(null)`: beam bottom = `Min.Z`, slab top = `Max.Z`; convert feet to mm before showing anything.
3. **Linked models** (only if the designer says the floors/beams are in a link) - `RevitLinkInstance`, `GetLinkDocument()`, `GetTotalTransform()` on every coordinate. Ask which link when there are several.
4. **Compute** - clearance = slab top - beam bottom per pair; sanity check (negative = already clashing); many-to-many pairs need the nearest slab per beam. `ReferenceIntersector` if the designer wants exact rays (3D view required).
5. **Output** - ask the format: table in chat, write to a parameter, CSV. Show the non-compliant rows first if a threshold was given.

Stages 1-4 are read-only queries; stage 5 may write. The spec card lists the stages you will run, with the scope and threshold as sourced values, and confirmation covers the whole plan. Keep the stage state in your report so the designer can stop and resume ("stage 3 done, 12 beams, 2 links").

## Decision points to ask, not assume

| Decision | Ask when |
|---|---|
| specific beams vs all beams | the designer says "选择 / 指定" or the scope is unclear |
| which link | more than one `RevitLinkInstance` |
| which floors | more than one level or the request names none |
| output format | always, unless stated |
| compliance threshold | the designer wants a pass/fail |

## API quick reference

| Purpose | API |
|---|---|
| beams | `BuiltInCategory.OST_StructuralFraming` |
| floors | `BuiltInCategory.OST_Floors` |
| view scope | `.OwnedByView(activeViewId)` |
| bounding box | `element.get_BoundingBox(view)` (`null` = model) |
| links | `FilteredElementCollector.OfClass(typeof(RevitLinkInstance))`, `GetLinkDocument()`, `GetTotalTransform()` |
| exact rays | `ReferenceIntersector` |
| units | `UnitUtils.ConvertFromInternalUnits(v, UnitTypeId.Millimeters)` |

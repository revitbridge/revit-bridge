---
schema_version: 1
id: revit-mep/pipe-rebuild-keeps-diameter
revision: 1
title: "A rebuilt pipe takes the type's default size: split with BreakCurve, copy the diameter"
summary: "`Pipe.Create` takes no size: a new segment gets the type's default diameter; split with `PlumbingUtils.BreakCurve` and copy `RBS_PIPE_DIAMETER_PARAM`"
stage: [write, fix]
kind: pitfall
revit: ">=2024 <2028"
order: 10
triggers:
  categories: [OST_PipeCurves, OST_PipeFitting, OST_DuctCurves]
  symbols: [Pipe.Create, Pipe, PlumbingUtils.BreakCurve, NewElbowFitting, RBS_PIPE_DIAMETER_PARAM, MEPCurve, Duct.Create]
---

## Scenario

`Pipe.Create(document, systemTypeId, pipeTypeId, levelId, start, end)` has no size argument: the new pipe gets the pipe type's default diameter, not the size of the pipe it replaces. Of 16 generated pipe-jog add-ins all deleted the original and rebuilt it this way; 12 never copied the diameter, so a 50 mm pipe came back at the default size. The diameter is the instance parameter `RBS_PIPE_DIAMETER_PARAM`. `Pipe` and `PlumbingUtils` are in `Autodesk.Revit.DB.Plumbing`, not imported by the snippet wrapper.

## Wrong

```csharp
Pipe seg = Pipe.Create(document, sysId, typeId, levelId, a, b);   // default size, not original.Diameter
document.Delete(original.Id);
```

## Right

```csharp
var original = document.GetElement(new ElementId(Convert.ToInt64(parameters[0]))) as Autodesk.Revit.DB.Plumbing.Pipe;
if (original == null) return new { Ok = false };
double diameter = original.Diameter;
// Split in place: both halves keep type, system, level, diameter and the far connections.
XYZ at = ((LocationCurve)original.Location).Curve.Evaluate(0.5, true);
ElementId secondId = Autodesk.Revit.DB.Plumbing.PlumbingUtils.BreakCurve(document, original.Id, at);
// A segment made from scratch must get the size copied explicitly.
ElementId systemTypeId = original.MEPSystem != null ? original.MEPSystem.GetTypeId() : ElementId.InvalidElementId;
var added = Autodesk.Revit.DB.Plumbing.Pipe.Create(document, systemTypeId, original.GetTypeId(), original.ReferenceLevel.Id, at, at + XYZ.BasisZ);
Parameter size = added.get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM);
bool copied = size != null && !size.IsReadOnly && size.Set(diameter);
return new { Ok = copied, Second = secondId.Value, Added = added.Id.Value };
```

## Versions

`Pipe.Create`, `PlumbingUtils.BreakCurve`, `MEPCurve.Diameter` and `RBS_PIPE_DIAMETER_PARAM` are unchanged in 2024-2027.

## Source

- revit-api-docs data set (Revit 2026): `Pipe.Create(Document, ElementId systemTypeId, ElementId pipeTypeId, ElementId levelId, XYZ, XYZ)`, no size parameter; `PlumbingUtils.BreakCurve(Document, ElementId pipeId, XYZ ptBreak)`; `MEPCurve.Diameter` (get only).
- dsh evaluation 2026-10-27, PipeJog: 12 of 16 builds lost the diameter; the Right code passes `check_code` on Revit 2026.5.

---
schema_version: 1
id: revit-geometry/line-create-bound
revision: 1
title: "Create lines with Line.CreateBound(XYZ, XYZ), not Application.Create.NewLineBound"
summary: "`Line.CreateBound(XYZ, XYZ)`, not `Application.Create.NewLineBound`"
stage: [write, fix]
kind: pitfall
revit: ">=2024 <2028"
order: 30
triggers:
  symbols: [NewLineBound, NewLine, CreateBound, CreateUnbound]
  errors: [CS1061, CS0117]
---

## Scenario

Older samples create lines through the creation application (`document.Application.Create.NewLineBound(...)`). That object has no `NewLineBound` or `NewLine`; lines come from the static `Line.CreateBound(XYZ, XYZ)` (a segment) or `Line.CreateUnbound(XYZ origin, XYZ direction)`. Points are in feet. A segment shorter than Revit's short-curve tolerance is refused with `ArgumentsInconsistentException`.

## Wrong

```csharp
Line line = document.Application.Create.NewLineBound(start, end);   // CS1061: no 'NewLineBound'
```

## Right

```csharp
double sixMetres = UnitUtils.ConvertToInternalUnits(6000, UnitTypeId.Millimeters);
XYZ start = new XYZ(0, 0, 0);
XYZ end = new XYZ(sixMetres, 0, 0);
Line line = Line.CreateBound(start, end);
return new { LengthMm = UnitUtils.ConvertFromInternalUnits(line.Length, UnitTypeId.Millimeters),
             ShortCurveToleranceFeet = document.Application.ShortCurveTolerance };
```

## Versions

`Line.CreateBound(XYZ, XYZ)` and `Line.CreateUnbound` are present in 2024.0-2027.2; `Autodesk.Revit.Creation.Application` has neither `NewLineBound` nor `NewLine` in any of them.

## Source

- revit-api-docs data set (Revit 2026): `Line.CreateBound` "Creates a new instance of a bound linear curve" (`XYZ endpoint1, XYZ endpoint2`; `ArgumentsInconsistentException` "Curve length is too small for Revit's tolerance (as identified by Application.ShortCurveTolerance)"); no `NewLineBound` row. Revit 2026 SDK samples use `Line.CreateBound`.
- Metadata of the Revit API assemblies 2024.0, 2024.3, 2025.0, 2025.4, 2026.0, 2026.4, 2027.2 and the installed 2026.5 and 2027.0 (2025.5 not checked). The built-in packs `create_wall`, `create_beam` and `create_floor` use `Line.CreateBound`.

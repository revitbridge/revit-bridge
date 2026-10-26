---
schema_version: 1
id: revit-geometry/curve-length-property
revision: 1
title: "Curve length is the Length property; there is no GetLength()"
summary: "`Curve.Length` is a property; there is no `GetLength()`"
stage: [write, fix]
kind: pitfall
revit: ">=2024 <2028"
order: 20
triggers:
  symbols: [GetLength, ApproximateLength, Curve]
  errors: [CS1061]
---

## Scenario

`Curve` exposes its length as the property `Length` (exact, in feet) and `ApproximateLength` (a quick estimate, exact for lines and arcs). There is no `GetLength()` method. Both throw `InvalidOperationException` for an unbound curve that is not periodic.

## Wrong

```csharp
double len = curve.GetLength();   // CS1061: 'Curve' has no 'GetLength'
```

## Right

```csharp
var wall = new FilteredElementCollector(document).OfClass(typeof(Wall)).FirstElement() as Wall;
var location = wall != null ? wall.Location as LocationCurve : null;
if (location == null) return new { Found = false };
Curve curve = location.Curve;
double exactFeet = curve.IsBound ? curve.Length : 0;
return new {
    Found = true,
    LengthMm = UnitUtils.ConvertFromInternalUnits(exactFeet, UnitTypeId.Millimeters),
    Bound = curve.IsBound };
```

## Versions

`Curve.Length`, `Curve.ApproximateLength` and `Curve.IsBound` are present and `Curve.GetLength` absent in 2024.0-2027.2.

## Source

- revit-api-docs data set (Revit 2026): `Curve.Length` "The exact length of the curve" (throws `InvalidOperationException` "when the curve is unbound and not periodic"); `Curve.ApproximateLength` "may deviate by a factor of 2 in some cases. The computation is exact for lines and arcs"; no `Curve.GetLength` row.
- Metadata of the Revit API assemblies 2024.0, 2024.3, 2025.0, 2025.4, 2026.0, 2026.4, 2027.2 and the installed 2026.5 and 2027.0 (2025.5 not checked).
- One of the old-API cases in the project's API retrieval evaluation (queries modelled on `check_code` hints).

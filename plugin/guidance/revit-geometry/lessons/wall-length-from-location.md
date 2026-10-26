---
schema_version: 1
id: revit-geometry/wall-length-from-location
revision: 1
title: "Wall length and end points: (wall.Location as LocationCurve).Curve, not wall.LocationCurve"
summary: "`(wall.Location as LocationCurve).Curve`, not `wall.LocationCurve`"
stage: [write, fix]
kind: pitfall
revit: ">=2024 <2028"
order: 10
triggers:
  categories: [OST_Walls]
  symbols: [Wall, LocationCurve, Location]
  errors: [CS1061]
---

## Scenario

`Wall` has no `LocationCurve` property. Every element has `Location`; for a wall it is a line location, so cast it to `LocationCurve` and read `.Curve`. The cast gives null for a wall without a location curve: check it. Lengths and points are in feet.

## Wrong

```csharp
double length = wall.LocationCurve.Curve.Length;   // CS1061: 'Wall' has no 'LocationCurve'
```

## Right

```csharp
var result = new List<object>();
foreach (Wall wall in new FilteredElementCollector(document).OfClass(typeof(Wall)).Cast<Wall>())
{
    var location = wall.Location as LocationCurve;
    if (location == null) continue;
    Curve curve = location.Curve;
    XYZ start = curve.GetEndPoint(0);
    XYZ end = curve.GetEndPoint(1);
    result.Add(new {
        Id = wall.Id.Value,
        LengthMm = UnitUtils.ConvertFromInternalUnits(curve.Length, UnitTypeId.Millimeters),
        StartFeet = new[] { start.X, start.Y, start.Z },
        EndFeet = new[] { end.X, end.Y, end.Z } });
}
return result;
```

Point-based elements (a column, a furniture instance) have a `LocationPoint` with `.Point` instead.

## Versions

`Wall` (with its base classes) has no `LocationCurve` member in 2024.0-2027.2; `Element.Location`, `LocationCurve.Curve`, `Curve.Length`, `Curve.GetEndPoint` and `LocationPoint.Point` are present in all of them.

## Source

- revit-api-docs data set (Revit 2026): no `Wall.LocationCurve` row; `Element.Location` "A wall is an example of an element that has a line location"; `LocationCurve` "Provides location functionality for all elements that are based upon a curve"; `Curve.Length` "The exact length of the curve". Revit 2026 SDK samples (ProximityDetection_WallJoinControl, DirectionCalculation, CreateDimensions) use `wall.Location as LocationCurve` with a null check.
- Metadata of the Revit API assemblies 2024.0, 2024.3, 2025.0, 2025.4, 2026.0, 2026.4, 2027.2 and the installed 2026.5 and 2027.0 (2025.5 not checked).

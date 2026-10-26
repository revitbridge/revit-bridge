---
schema_version: 1
id: revit-elements/floor-ceiling-type-and-level-ids
revision: 1
title: "Floor.Create and Ceiling.Create need a FloorType or CeilingType id and a Level id, or throw ArgumentException"
summary: "`Floor.Create(document, loops, floorType.Id, level.Id)`: another kind of id throws `ArgumentException`"
stage: [write, fix]
kind: pitfall
revit: ">=2024 <2028"
order: 10
triggers:
  categories: [OST_Floors, OST_Ceilings]
  symbols: [FloorType, CeilingType]
  exceptions: [Autodesk.Revit.Exceptions.ArgumentException]
---

## Scenario

`Floor.Create(document, profile, floorTypeId, levelId)` and `Ceiling.Create(document, curveLoops, ceilingTypeId, levelId)` check their `ElementId`s at run time: `Autodesk.Revit.Exceptions.ArgumentException` "The ElementId levelId is not a Level" or "The floorTypeId does not correspond to a FloorType" (`CeilingType` for a ceiling). A wall type or family symbol as the type, a view as the level, or swapped ids all fail. The same exception reports loops that are no valid boundary (empty, intersecting, open, not planar, not horizontal).

## Wrong

```csharp
Floor.Create(document, loops, wallType.Id, level.Id);   // a WallType is not a FloorType
```

## Right

```csharp
Level level = new FilteredElementCollector(document).OfClass(typeof(Level))
    .Cast<Level>().FirstOrDefault(l => l.Name == (parameters[0] as string));
FloorType floorType = new FilteredElementCollector(document).OfClass(typeof(FloorType))
    .Cast<FloorType>().FirstOrDefault(t => t.Name == (parameters[1] as string));
if (level == null || floorType == null) return new { Created = false };
double side = UnitUtils.ConvertToInternalUnits(Convert.ToDouble(parameters[2]), UnitTypeId.Millimeters);
XYZ a = new XYZ(0, 0, 0), b = new XYZ(side, 0, 0), c = new XYZ(side, side, 0), d = new XYZ(0, side, 0);
CurveLoop loop = CurveLoop.Create(new List<Curve> {
    Line.CreateBound(a, b), Line.CreateBound(b, c), Line.CreateBound(c, d), Line.CreateBound(d, a) });
Floor floor = Floor.Create(document, new List<CurveLoop> { loop }, floorType.Id, level.Id);
return new { Created = true, Id = floor.Id.Value };
```

## Versions

The same overloads and exception notes in 2024.0-2027.2.

## Source

- revit-api-docs data set (Revit 2026): `Floor.Create(Document, IList<CurveLoop>, ElementId, ElementId)` ArgumentException "The ElementId levelId is not a Level. -or- The floorTypeId does not correspond to a FloorType. -or- The input curve loops cannot compose a valid boundary"; `Ceiling.Create` the same with "ceilingTypeId does not correspond to a CeilingType" (namespace `Autodesk.Revit.Exceptions`).
- RevitAPI.xml of 7 builds 2024.0-2027.2: same overloads and notes; the Right code compiles in the `execute_code` wrapper against 2024.0, 2025.0, 2026.4.

---
schema_version: 1
id: revit-core/internal-units-feet
revision: 1
title: "Lengths in the API are decimal feet: convert with UnitUtils and UnitTypeId"
stage: [orient, write, fix]
kind: fact
revit: ">=2024 <2028"
order: 20
triggers:
  symbols: [UnitUtils, ConvertToInternalUnits, ConvertFromInternalUnits, UnitTypeId, DisplayUnitType, XYZ]
  errors: [CS0103, CS0246]
---

## Scenario

The API reads and writes lengths in Revit's internal unit, decimal feet: `XYZ` coordinates, `Curve.Length`, `Level.Elevation`, `Parameter.AsDouble()` and `Parameter.Set(double)` on length parameters. The project's display units (`query` kind `units`) do not change that. Designers and the built-in packs speak millimetres; 1 ft = 304.8 mm.

## Wrong

```csharp
var end = new XYZ(3000, 0, 0);    // 3000 feet, not 3000 mm
double ft = UnitUtils.ConvertToInternalUnits(3000, DisplayUnitType.DUT_MILLIMETERS);  // CS0103: no DisplayUnitType
```

## Right

```csharp
double lengthFt = UnitUtils.ConvertToInternalUnits(3000, UnitTypeId.Millimeters);
var end = new XYZ(lengthFt, 0, 0);
var level = new FilteredElementCollector(document)
    .OfClass(typeof(Level)).Cast<Level>().FirstOrDefault();
double? elevationMm = level == null ? (double?)null
    : UnitUtils.ConvertFromInternalUnits(level.Elevation, UnitTypeId.Millimeters);
return new { EndXFeet = end.X, ElevationMm = elevationMm };
```

Other quantities have their own unit ids (`UnitTypeId.Meters`, `UnitTypeId.SquareMeters`, `UnitTypeId.Degrees`, ...): pass the one that matches the value.

## Versions

`UnitUtils.ConvertToInternalUnits(double, ForgeTypeId)`, `ConvertFromInternalUnits(double, ForgeTypeId)` and the `UnitTypeId` properties above are present in 2024.0-2027.2; the `DisplayUnitType` enum is absent from all of them.

## Source

- revit-api-docs data set (Revit 2026): `UnitUtils.ConvertToInternalUnits` "Converts a value from a given unit to Revit's internal units" (`double value, ForgeTypeId unitTypeId`); `Level.Elevation` "The value is given in decimal feet"; no `DisplayUnitType` row.
- Metadata of the Revit API assemblies 2024.0, 2024.3, 2025.0, 2025.4, 2026.0, 2026.4, 2027.2 and the installed 2026.5 and 2027.0: as in Versions (2025.5 not checked).
- `get_project_snapshot` converts `Level.Elevation` to millimetres with the same factor.

---
schema_version: 1
id: revit-families/activate-symbol-before-placement
revision: 1
title: "Activate the FamilySymbol before NewFamilyInstance: an inactive type throws ArgumentException"
summary: "`if (!symbol.IsActive) symbol.Activate();` before `NewFamilyInstance`, or `ArgumentException` \"The symbol is not active\""
stage: [write, fix]
kind: pitfall
revit: ">=2024 <2028"
order: 10
triggers:
  symbols: [NewFamilyInstance, FamilySymbol]
  exceptions: [Autodesk.Revit.Exceptions.ArgumentException, Autodesk.Revit.Exceptions.InvalidOperationException]
---

## Scenario

Revit may deactivate a family type that no instance uses (`FamilySymbol.IsActive` false). `document.Create.NewFamilyInstance` with an inactive type throws `Autodesk.Revit.Exceptions.ArgumentException` ("The symbol is not active"); with a `Level`, also when the type or the level "does not exist in the given document". The overload without a level throws `InvalidOperationException` for a level-based family. Both are Revit types: `catch (ArgumentException)` in a snippet catches `System.ArgumentException` only.

## Wrong

```csharp
// inactive type
document.Create.NewFamilyInstance(point, symbol, level, Autodesk.Revit.DB.Structure.StructuralType.NonStructural);
```

## Right

```csharp
FamilySymbol symbol = new FilteredElementCollector(document).OfClass(typeof(FamilySymbol))
    .Cast<FamilySymbol>().FirstOrDefault(s => s.Name == (parameters[0] as string));
Level level = new FilteredElementCollector(document).OfClass(typeof(Level))
    .Cast<Level>().FirstOrDefault(l => l.Name == (parameters[1] as string));
if (symbol == null || level == null) return new { Placed = false };
if (!symbol.IsActive)
{
    symbol.Activate();
    document.Regenerate();   // geometry is empty until regenerated
}
double mm = UnitUtils.ConvertToInternalUnits(1, UnitTypeId.Millimeters);
XYZ point = new XYZ(Convert.ToDouble(parameters[2]) * mm, Convert.ToDouble(parameters[3]) * mm, 0);
FamilyInstance placed = document.Create.NewFamilyInstance(
    point, symbol, level, Autodesk.Revit.DB.Structure.StructuralType.NonStructural);
return new { Placed = true, Id = placed.Id.Value };
```

## Versions

The same members and exception notes in 2024.0-2027.2.

## Source

- revit-api-docs data set (Revit 2026): `ItemFactoryBase.NewFamilyInstance(XYZ, FamilySymbol, Level, StructuralType)` ArgumentException "Thrown if The symbol is not active", "...the level does not exist in the given document"; `(XYZ, FamilySymbol, StructuralType)` InvalidOperationException "Thrown if the family is level-based"; `FamilySymbol.Activate`: until regeneration the geometry "will still be empty"; both derive from `Autodesk.Revit.Exceptions.ApplicationException`.
- RevitAPI.xml of 7 builds 2024.0-2027.2: same members and notes; the Right code compiles in the `execute_code` wrapper against 2024.0, 2025.0, 2026.4.

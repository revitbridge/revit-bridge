---
schema_version: 1
id: revit-collect/instances-vs-types
revision: 1
title: "Say whether you want instances or types: WhereElementIsNotElementType / WhereElementIsElementType"
stage: [write, fix]
kind: pitfall
revit: ">=2024 <2028"
order: 20
triggers:
  symbols: [OfCategory, WhereElementIsElementType, WhereElementIsNotElementType, ElementType, FamilySymbol, GetTypeId]
  exceptions: [InvalidCastException]
---

## Scenario

`OfCategory` keeps every element of the category, and a category also holds its types: door types (`FamilySymbol`) sit in `OST_Doors` next to the placed doors. Add `WhereElementIsNotElementType()` for placed elements or `WhereElementIsElementType()` for types. An instance names its type with `GetTypeId()`; a `FamilyInstance` also has `Symbol`.

## Wrong

```csharp
var doors = new FilteredElementCollector(document)
    .OfCategory(BuiltInCategory.OST_Doors)
    .Cast<FamilyInstance>()      // InvalidCastException on the door types
    .ToList();
```

## Right

```csharp
List<string> doorTypes = new FilteredElementCollector(document)
    .OfCategory(BuiltInCategory.OST_Doors)
    .WhereElementIsElementType()
    .Select(t => t.Name)
    .ToList();
List<FamilyInstance> doors = new FilteredElementCollector(document)
    .OfCategory(BuiltInCategory.OST_Doors)
    .WhereElementIsNotElementType()
    .OfType<FamilyInstance>()
    .ToList();
Element firstType = doors.Count > 0 ? document.GetElement(doors[0].GetTypeId()) : null;
return new { Types = doorTypes, Placed = doors.Count, FirstType = firstType != null ? firstType.Name : null };
```

## Versions

`WhereElementIsElementType`, `WhereElementIsNotElementType`, `Element.GetTypeId`, `FamilyInstance.Symbol` and `BuiltInCategory.OST_Doors` are present in 2024.0-2027.2.

## Source

- revit-api-docs data set (Revit 2026): `WhereElementIsNotElementType` "Only elements that are not element types will pass"; `WhereElementIsElementType` "Only elements that are ElementTypes will pass"; `Element.GetTypeId` "Returns the identifier of this element's type" (invalid id when the element cannot have one). The Revit 2026 SDK sample CreateDuctworkStiffener collects family symbols with `OfCategory(BuiltInCategory.OST_FabricationDuctworkStiffeners).OfClass(typeof(FamilySymbol))`: a category holds its types.
- Metadata of the Revit API assemblies 2024.0, 2024.3, 2025.0, 2025.4, 2026.0, 2026.4, 2027.2 and the installed 2026.5 and 2027.0 (2025.5 not checked).

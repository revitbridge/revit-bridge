---
schema_version: 1
id: revit-params/lookup-parameter-storage-type
revision: 2
title: "Parameters: LookupParameter can return null; read by StorageType, not AsValueString"
summary: "`LookupParameter` can return null; read by `StorageType`, not `AsValueString`"
stage: [write, fix]
kind: pitfall
revit: ">=2024 <2028"
order: 10
triggers:
  symbols: [LookupParameter, StorageType, AsDouble, AsInteger, AsString, AsElementId, AsValueString]
  exceptions: [NullReferenceException]
---

## Scenario

`LookupParameter(name)` returns null when no parameter has that name, and an arbitrary one when several do. A value is stored as one `StorageType` - `Double`, `Integer`, `String`, `ElementId` or `None` - and is read with the matching `AsDouble()`, `AsInteger()`, `AsString()` or `AsElementId()`. `AsDouble()` is in internal units (feet for lengths). `AsValueString()` is the display text with units: for showing, not for arithmetic.

## Wrong

```csharp
double h = wall.LookupParameter("Room Code").AsDouble(); // null -> NullReferenceException; wrong type -> meaningless
double w = double.Parse(wall.LookupParameter("Width").AsValueString()); // display text, not a number
```

## Right

```csharp
Element wall = new FilteredElementCollector(document).OfClass(typeof(Wall)).FirstElement();
Parameter p = wall != null ? wall.LookupParameter("Room Code") : null; // a project or shared parameter
if (p == null) return new { Found = false };
object value;
switch (p.StorageType)
{
    case StorageType.Double: value = p.AsDouble(); break; // internal units
    case StorageType.Integer: value = p.AsInteger(); break;
    case StorageType.String: value = p.AsString(); break;
    case StorageType.ElementId: value = p.AsElementId().Value; break;
    default: value = null; break;
}
return new { Found = true, HasValue = p.HasValue, Storage = p.StorageType.ToString(),
    Value = value, Display = p.AsValueString() };
```

Before `Set(...)`, check `IsReadOnly` and pass the type that matches `StorageType` (`Set(double)`, `Set(int)`, `Set(string)`, `Set(ElementId)`).

## Versions

Every member above is present in 2024.0-2027.2. `ElementId.Value` needs 2024 or later.

## Source

- revit-api-docs data set (Revit 2026): `Element.LookupParameter` remarks (no match returns null; with several matches "the first one encountered will be returned. This match is determined at random"); `Parameter.StorageType` "Based on the value of this property the correct access and set methods should be used"; `Parameter.AsValueString` "Gets the parameter value as a string with units".
- Metadata of the Revit API assemblies 2024.0, 2024.3, 2025.0, 2025.4, 2026.0, 2026.4, 2027.2 and the installed 2026.5 and 2027.0, including the four `Parameter.Set` overloads (2025.5 not checked).

---
schema_version: 1
id: revit-core/elementid-value
revision: 1
title: "ElementId: use Value (long); IntegerValue is obsolete in 2024-2025 and gone from 2026"
summary: "`ElementId.Value` (long); `IntegerValue` is obsolete in 2024-2025 and gone from 2026"
stage: [orient, write, fix]
kind: pitfall
revit: ">=2024 <2028"
order: 30
triggers:
  symbols: [IntegerValue, ElementId]
  errors: [CS1061, CS0618]
---

## Scenario

Element ids are 64-bit: `ElementId.Value` returns `long`. Code written for older Revit uses `IntegerValue` (`int`) and `new ElementId(int)`.

## Wrong

```csharp
int id = wall.Id.IntegerValue;   // 2024-2025: warning CS0618 (obsolete); 2026 and later: CS1061
```

## Right

```csharp
List<long> ids = new FilteredElementCollector(document)
    .OfClass(typeof(Wall))
    .ToElementIds()
    .Select(id => id.Value)
    .ToList();
long first = ids.Count > 0 ? ids[0] : ElementId.InvalidElementId.Value;
Element again = document.GetElement(new ElementId(first));
return new { Count = ids.Count, First = first, Found = again != null };
```

## Versions

- 2024.0 to 2025.4: `IntegerValue` and the `ElementId(Int32)` constructor are marked obsolete; the property's message: "This property is deprecated in Revit 2024 and may be removed in a future version of Revit. Please use the Value property instead."
- 2026.0, 2026.4, 2026.5, 2027.0, 2027.2: both are gone. `Value` (`Int64`) and `ElementId(Int64)` exist in every version checked.

## Source

- revit-api-docs data set (Revit 2026): `ElementId.Value` "Provides the value of the element id as a 64-bit integer" (`public long Value { get; }`); the `ElementId` member list has `ElementId(Int64)` and no `IntegerValue`.
- Metadata of the Revit API assemblies 2024.0, 2024.3, 2025.0, 2025.4, 2026.0, 2026.4, 2027.2 and the installed 2026.5 and 2027.0, including the obsolete messages quoted above (2025.5 not checked).

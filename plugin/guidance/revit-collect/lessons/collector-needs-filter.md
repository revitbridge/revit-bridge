---
schema_version: 1
id: revit-collect/collector-needs-filter
revision: 2
title: "FilteredElementCollector needs a filter before you read it"
summary: "apply `OfCategory` / `OfClass` / `WhereElementIs...` before reading a collector"
stage: [write, fix]
kind: pitfall
revit: ">=2024 <2028"
order: 10
triggers:
  symbols: [FilteredElementCollector, ToElements, ToElementIds, FirstElement]
  exceptions: [InvalidOperationException]
---

## Scenario

A new `FilteredElementCollector(document)` has no condition yet. Reading it - `ToElements()`, `ToElementIds()`, `FirstElement()`, `foreach` or LINQ - throws `InvalidOperationException`: "Extraction or iteration of elements is not permitted without a filter". A LINQ `Where` is not a collector filter. Apply a native filter first (`OfCategory`, `OfClass`, `WhereElementIsNotElementType`, `WhereElementIsElementType`, ...); native filters also run before elements are expanded, faster than filtering in LINQ.

## Wrong

```csharp
var all = new FilteredElementCollector(document).ToElements(); // no filter: throws
var walls = new FilteredElementCollector(document)
    .Where(e => e is Wall).ToList(); // still no collector filter
```

## Right

```csharp
IList<Element> walls = new FilteredElementCollector(document)
    .OfCategory(BuiltInCategory.OST_Walls)
    .WhereElementIsNotElementType()
    .ToElements();
ICollection<ElementId> levelIds = new FilteredElementCollector(document)
    .OfClass(typeof(Level))
    .ToElementIds();
View view = document.ActiveView;
int inView = view != null && FilteredElementCollector.IsViewValidForElementIteration(document, view.Id)
    ? new FilteredElementCollector(document, view.Id).WhereElementIsNotElementType().GetElementCount()
    : 0;
return new { Walls = walls.Count, Levels = levelIds.Count, InActiveView = inView };
```

Use one extraction method per collector: calling another resets it and stops an open iterator.

## Versions

The constructors `(Document)`, `(Document, ElementId)`, `(Document, ICollection<ElementId>)` and every method above are present in 2024.0-2027.2.

## Source

- revit-api-docs data set (Revit 2026): `FilteredElementCollector` "requires that at least one condition be set before making the attempt to access the elements"; `ToElements` throws `InvalidOperationException` "The collector does not have a filter applied. Extraction or iteration of elements is not permitted without a filter"; the class remarks on native filters before LINQ and on the collector resetting.
- Metadata of the Revit API assemblies 2024.0, 2024.3, 2025.0, 2025.4, 2026.0, 2026.4, 2027.2 and the installed 2026.5 and 2027.0 (2025.5 not checked).
- `query` (kinds `elements`, `counts`, `view_elements`) uses the same filters.

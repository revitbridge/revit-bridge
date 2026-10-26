---
schema_version: 1
id: revit-collect/selection-get-element-ids
revision: 1
title: "The current selection is Selection.GetElementIds(); Selection.Elements does not exist"
summary: "`Selection.GetElementIds()`; there is no `Selection.Elements`"
stage: [write, fix]
kind: pitfall
revit: ">=2024 <2028"
order: 30
triggers:
  symbols: [Selection, GetElementIds, SetElementIds, Elements]
  errors: [CS1061]
---

## Scenario

`uidoc.Selection.Elements` does not compile: the `Selection` class has no `Elements` member, it returns ids. In a snippet there is no `uidoc`: build one from `document`. To read the selection without code, `query` with kind `selection` does it.

## Wrong

```csharp
var uidoc = new UIDocument(document);
foreach (Element e in uidoc.Selection.Elements) { }   // CS1061: no 'Elements'
```

## Right

```csharp
var uidoc = new UIDocument(document);
ICollection<ElementId> ids = uidoc.Selection.GetElementIds();
var items = ids
    .Select(id => document.GetElement(id))
    .Where(e => e != null)
    .Select(e => new { Id = e.Id.Value, Name = e.Name,
                       Category = e.Category != null ? e.Category.Name : "" })
    .ToList();
return new { Count = ids.Count, Items = items };
```

`Selection.SetElementIds(ICollection<ElementId>)` changes the selection. `GetElementIds` "may not be complete. See GetReferences for more options."

## Versions

`UIDocument(Document)`, `UIDocument.Selection`, `Selection.GetElementIds` and `SetElementIds` are present, and `Selection.Elements` absent, in 2024.0, 2024.3, 2025.0, 2026.0, 2026.4 and the installed 2026.5 and 2027.0. `ElementId.Value` needs 2024 or later.

## Source

- revit-api-docs data set (Revit 2026): `Selection.GetElementIds` "Returns the ids of the elements that are currently selected within the project. The selection may not be complete. See GetReferences for more options."; the `Selection` rows list no `Elements`; `UIDocument` "Use a database level Document to construct a UI-level Document".
- Metadata of RevitAPIUI for the versions under Versions.
- One of the old-API cases in the project's API retrieval evaluation (queries modelled on `check_code` hints). `get_project_snapshot` reads the selection with the same `new UIDocument(document)` and `GetElementIds()` calls.

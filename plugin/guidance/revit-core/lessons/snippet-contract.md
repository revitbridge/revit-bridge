---
schema_version: 1
id: revit-core/snippet-contract
revision: 1
title: "execute_code snippets are a method body: only document and parameters are in scope"
stage: [orient, write, fix]
kind: fact
revit: any
order: 10
triggers:
  symbols: [doc, uidoc, uiapp, commandData, ActiveUIDocument, JsonConvert, Newtonsoft]
  errors: [CS0103, CS0246]
---

## Scenario

`execute_code` and `check_code` compile your code as the body of one method:

```csharp
// written by the add-in around your code
public static object Execute(Document document, object[] parameters)
{
    // your code
}
```

- In scope: `document` (the active `Document`) and `parameters` (`object[]`). There is no `doc`, `uidoc`, `uiapp` or `commandData`.
- Usings: `System`, `System.Linq`, `System.Collections.Generic`, `Autodesk.Revit.DB`, `Autodesk.Revit.UI`.
- References: the base libraries (mscorlib or System.Private.CoreLib, System, System.Core, System.Runtime, System.Collections, System.Linq, netstandard), RevitAPI and RevitAPIUI. No JSON library.
- Refused before compiling: the identifiers `IO`, `Process`, `Assembly`, `DllImport`, `File`, `Registry` anywhere, even as a variable name. The bridge also refuses network, reflection, threading, `dynamic` and `unsafe` code.
- It already runs inside a transaction (`revit-core/transaction-owned-by-bridge`).
- End with `return <object>;`. The add-in serializes the returned object to JSON: return plain values (ids, names, numbers, lists, anonymous objects), not Revit objects.

## Wrong

```csharp
var walls = new FilteredElementCollector(doc).OfClass(typeof(Wall));  // CS0103 'doc'
var ids = uidoc.Selection.GetElementIds();                             // CS0103 'uidoc'
return JsonConvert.SerializeObject(walls);                             // no JSON library
```

## Right

```csharp
var uidoc = new UIDocument(document);   // UI access from the document in scope
int selected = uidoc.Selection.GetElementIds().Count;
int walls = new FilteredElementCollector(document)
    .OfClass(typeof(Wall))
    .GetElementCount();
return new { Title = document.Title, Selected = selected, Walls = walls };
```

## Versions

The contract is the add-in's, the same for every Revit version.

## Source

- Wrapper, usings, reference whitelist, refused identifiers: `CodeExecutor` in revit-bridge-addin (wrapper version 1); transaction and JSON result: its execute-code handler. Extra refused patterns: `revit_bridge/revit/sandbox.py`.
- Every member in Right: in the revit-api-docs data set (Revit 2026) and the Revit API assemblies 2020-2027 (metadata check). `get_project_snapshot` reads the selection with the same `new UIDocument(document)` code.

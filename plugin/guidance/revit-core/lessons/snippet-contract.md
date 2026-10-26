---
schema_version: 1
id: revit-core/snippet-contract
revision: 2
title: "execute_code snippets are a method body: only document and parameters are in scope"
summary: "a method body with `document` and `parameters` in scope, already in a transaction (never open one); fixed usings and references; end with `return <object>;`"
stage: [orient, write, fix]
kind: fact
revit: any
order: 10
triggers:
  symbols: [doc, uidoc, uiapp, commandData, ActiveUIDocument, JsonConvert, Newtonsoft]
  errors: [CS0103, CS0246]
---

## Contract

Snippet contract (execute_code): a method body with `document` and `parameters`
(object[]) in scope, already inside a transaction (never open one); usings
System, System.Linq, System.Collections.Generic, Autodesk.Revit.DB,
Autodesk.Revit.UI; references only the base library, RevitAPI and RevitAPIUI
(no JSON library); IO, Process, Assembly, DllImport, File, Registry, reflection
and threading are refused; end with `return <object>;`; categories are OST_*.

## Scenario

`execute_code` and `check_code` compile your code as this method's body:

```csharp
// the add-in's wrapper
public static object Execute(Document document, object[] parameters)
{
    // your code
}
```

- `document` is the active `Document`; no `doc`, `uidoc`, `uiapp` or `commandData`.
- The base library: mscorlib or System.Private.CoreLib, System, System.Core, System.Runtime, System.Collections, System.Linq, netstandard.
- The add-in refuses those six identifiers anywhere, even as a variable name; the bridge also refuses reflection, threading, network, `dynamic` and `unsafe` code.
- The add-in serializes the returned object to JSON: return plain values (ids, names, numbers, lists, anonymous objects), not Revit objects.

## Wrong

```csharp
var walls = new FilteredElementCollector(doc).OfClass(typeof(Wall)); // CS0103 'doc'
var ids = uidoc.Selection.GetElementIds(); // CS0103 'uidoc'
return JsonConvert.SerializeObject(walls); // no JSON library
```

## Right

```csharp
var uidoc = new UIDocument(document); // UI access
int selected = uidoc.Selection.GetElementIds().Count;
int walls = new FilteredElementCollector(document).OfClass(typeof(Wall)).GetElementCount();
return new { Title = document.Title, Selected = selected, Walls = walls };
```

## Versions

The contract is the add-in's, the same for every Revit version.

## Source

- Wrapper, usings, reference whitelist, refused identifiers: `CodeExecutor` in revit-bridge-addin (wrapper version 1); transaction and JSON result: its execute-code handler. Extra refused patterns: `revit_bridge/revit/sandbox.py`.
- Every member in Right: in the revit-api-docs data set (Revit 2026) and the Revit API assemblies 2020-2027 (metadata check). `get_project_snapshot` reads the selection with the same `new UIDocument(document)` code.

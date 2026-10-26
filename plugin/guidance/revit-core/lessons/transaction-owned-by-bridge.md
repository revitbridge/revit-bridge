---
schema_version: 1
id: revit-core/transaction-owned-by-bridge
revision: 1
title: "The bridge owns the transaction: never start a Transaction in a snippet"
stage: [orient, write, fix]
kind: pitfall
revit: any
order: 40
triggers:
  symbols: [Transaction, TransactionGroup, TransactionMode]
  exceptions: [InvalidOperationException]
---

## Scenario

For `execute_code` the add-in starts a `Transaction` on the active document, runs your snippet and commits when it returns. If the snippet throws, nothing is committed and the error is reported. A document can have only one transaction open at a time, so a `Transaction` you start inside the snippet fails with `InvalidOperationException` ("Starting a new transaction is not permitted..."); a `TransactionGroup` cannot start during an active transaction either.

## Wrong

```csharp
using (var t = new Transaction(document, "Set mark"))   // a second transaction: refused
{
    t.Start();
    // ...
    t.Commit();
}
```

## Right

```csharp
// Already inside the bridge's transaction: change the model directly.
var wall = new FilteredElementCollector(document)
    .OfClass(typeof(Wall)).FirstElement() as Wall;
if (wall == null) return new { Changed = false };
Parameter mark = wall.get_Parameter(BuiltInParameter.ALL_MODEL_MARK);
bool ok;
// To undo part of the work, nest a SubTransaction inside the open transaction.
using (var sub = new SubTransaction(document))
{
    sub.Start();
    ok = mark != null && !mark.IsReadOnly && mark.Set("A-01");
    if (ok) sub.Commit(); else sub.RollBack();
}
return new { Changed = ok, Wall = wall.Id.ToString() };
```

## Versions

Same for every version: it is the add-in's wrapper. `Transaction`, `SubTransaction` and `TransactionGroup` exist in the API assemblies 2020-2027.

## Source

- revit-bridge-addin execute-code event handler: compiles first (no transaction when that fails), then `new Transaction(doc, ...)`, `Start()`, the snippet, `Commit()`.
- revit-api-docs data set (Revit 2026): `Transaction` "A document can have only one transaction open at any given time"; `Transaction.Start` throws `InvalidOperationException` when "Starting a new transaction is not permitted. It could be because another transaction already started..."; `TransactionGroup.Start`: "Transaction group cannot be started during an active transaction"; `SubTransaction.Start`: "A sub-transaction can only be started in an open transaction".
- Members used above checked in the API assemblies 2020-2027 (metadata). The built-in `create_wall` pack says the same: "Transaction already active - do NOT create one".

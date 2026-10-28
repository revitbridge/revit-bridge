# Brief template

Copy, fill every section, delete nothing. Replace `<...>`.

```
# <Name> - <one sentence of purpose>

Kind: <standalone IExternalCommand add-in | ribbon command | bridge snippet>. Transaction mode: <Manual | ReadOnly>.

## Behaviour
1. The user picks <what, with which filter and how many>. <What is rejected and the message.>
2. The command computes <what>.
3. A dialog asks for: <field> (<unit>, default <value>, valid <range>); <field> (...).
4. In one transaction the command <changes what, in what order, with which API>. <What it must not touch.>
5. At the end it shows <summary TaskDialog with ...> and returns Result.Succeeded.

## Rejections and edge cases
- Escape at any pick or Cancel in the dialog: Result.Cancelled, no change.
- Failure inside the transaction: roll back, Result.Failed, the reason in the message.
- <invalid selection / geometry / overlap / missing family / no room ...>: <behaviour>.
- Inserts: every planned opening is checked against every insert on the host (FindInserts) and against each other before the first placement; an overlap is refused or moved with a message.
- Validation: every refused input and every failure reaches the user with its reason; no silent catch.

## Facts taken from the open project
- <fact>: <rule for choosing it>. <Read now through the bridge: found "..." | to read at run time.>

## Transaction scope
<one transaction / transaction group>, closed on every path (commit after all edits succeeded, otherwise roll back, exceptions included); no model change outside it; FailureHandlingOptions with a preprocessor. After rollback <what is true>.

## Deliverables and environment
- Files in the current directory: <sources>, <Name>.csproj, <Name>.addin; assembly name, root namespace and add-in name <Name>; new AddInId.
- Target: Revit <release> -> <framework>; RevitAPI.dll and RevitAPIUI.dll from <folder>, Private=false.
- Build: <command>; required result: zero errors. Do not install; <who installs and tests>.
- Known environment: <an open Revit with ..., reachable tools, ...>.

## Verification
- Implementer: build; <bridge prototype of ... on the open model>; <check_code of ...>. If the prototype needs a confirmation nobody can give, the report says so.
- Only in Revit by a person: <picks, dialog, visual result, ...>.
- Report: build command and result; each step above with verified / not verified and how.

## Decisions taken
- <open choice>: <value chosen> - <reason>.
```

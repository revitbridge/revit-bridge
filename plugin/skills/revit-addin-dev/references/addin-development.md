# Independent add-in implementation

## Version and project

Use the existing repository's target and reference strategy. For a new project, the bundled starter uses the following explicit targets:

| Revit | Target framework | Starter `global.json` SDK |
| --- | --- | --- |
| 2024 | `net48` | none |
| 2025 before 2025.5 | `net8.0-windows` | `8.0.100` |
| 2025.5 | `net10.0-windows` | `10.0.100` |
| 2026 before 2026.5 | `net8.0-windows` | `8.0.100` |
| 2026.5 | `net10.0-windows` | `10.0.100` |
| 2027 | `net10.0-windows` | `10.0.100` |

Match both `RevitAPI.dll` and `RevitAPIUI.dll` to that Revit release and runtime. Retain pinned reference packages if the project already uses them; the starter instead references a supplied installation directory and uses `Private=false` for Autodesk assemblies. Do not redistribute those assemblies with the plugin. Check unsupported releases separately rather than extrapolating the table.

The starter uses x64, a Windows desktop framework reference for .NET 8/10, and no third-party runtime package. Override `RevitApiDir` for a nonstandard installation; `RevitAPIPath` and `RevitAPIUIPath` can independently point to matching reference assemblies. The build rejects missing files, a mismatched API year, and .NET 8 versus .5/.NET 10 reference mismatches before resolving dependencies; a 2027 project also rejects any target other than `net10.0-windows`. Output directories include the update version so the two runtime builds remain separate.

For .NET 8 and .NET 10 targets the starter writes a `global.json` next to the project in the shape of Autodesk's Revit 2027 sample (below): the SDK version from the table and `"rollForward": "minor"`. `minor` takes the latest patch of the requested feature band, otherwise a higher feature band or minor version of the same major; it never selects another major. A 2025.5/2026.5/2027 project therefore builds with any installed .NET 10 SDK, and a 2025/2026 project needs a .NET 8 SDK even when only newer SDKs are installed (`dotnet` then stops with "A compatible .NET SDK was not found. Requested SDK version: 8.0.100"). Install a .NET 8 SDK, or relax `rollForward` deliberately (for example `latestMajor`) if a newer SDK may build the .NET 8 target. A 2024 (`net48`) project gets no `global.json`. `dotnet` looks for `global.json` from the current directory upward, so run builds from the project directory. In an existing repository, keep its own SDK policy.

For a local installation, inspect the API DLL file version and `RevitAPI.runtimeconfig.json`, then check `dotnet --list-sdks`. The runtime installed with Revit is not a development SDK. [Autodesk's .NET 10 transition notice](https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/Revit--Requirements-for-products-affected-by-the-Microsoft--NET-10-transition.html) covers the 2025.5/2026.5 change. Do not substitute 2026.0 reference packages merely to get a successful build against a 2026.5 requirement. If an SDK is missing, prepare the appropriate SDK within the user's installation permissions.

Microsoft ends .NET 8 support on 2026-11-10 ([.NET support policy](https://dotnet.microsoft.com/en-us/platform/support/policy/dotnet-core)). Projects for 2025/2026 before .5 still target .NET 8; plan their move to the .5 update and `net10.0-windows`, which also requires their users to run that Revit update.

[Autodesk's .NET migration guide](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API/files/Revit_API_Developers_Guide/Introduction/Getting_Started/Using_the_Autodesk_Revit_API/Revit_API_Revit_API_Developers_Guide_Introduction_Getting_Started_Using_the_Autodesk_Revit_API_NET8_Update_html.html) describes the Revit 2025 transition to .NET 8 and dependency migration. Compile each supported Revit target; one successful build does not establish cross-version compatibility.

## Revit 2027

Revit 2027 runs on .NET 10 and has no .NET 8 build: a bare `2027` targets `net10.0-windows`, and the starter offers no 2027.5 variant. Build with a .NET 10 SDK. Reference packages such as `Nice3point.Revit.Api.RevitAPI` 2027.x also target `net10.0-windows`. Autodesk's sample pins the SDK in `global.json`:

```json
{
  "sdk": {
    "version": "10.0.100",
    "rollForward": "minor"
  }
}
```

The starter writes exactly this `global.json` for 2027 (and for 2025.5/2026.5). Its reference check accepts any 27.x API assembly; the 2025/2026 .5 checks do not apply.

Revit 2027 also adds assembly isolation and dependency handling: new types such as `AddInDependencyBase`, `ClientIdDependency`, and `ContextNameDependency`, and the manifest settings `PublicAssemblies`, `Dependencies`, and `UseAllContextsForDependencyResolution`. The starter's `.addin` uses none of them. When an add-in needs them, follow Autodesk's post and the complete sample in the Revit 2027 SDK rather than reconstructing the XML from these names.

See [Autodesk's Revit 2027 SDK post](https://blog.autodesk.io/revit-2027-sdk-net-10-api-changes-and-additions/) for these changes. This skill's 2027 starter has been compiled against Revit 2027 API assemblies but not yet loaded or run in Revit 2027; report a 2027 build as build passed, not as a loaded add-in, until that check is done.

## Entry points and changes

A public `IExternalCommand` implements `Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)`. Obtain `UIDocument` through `commandData.Application.ActiveUIDocument`, check it for null, and obtain the database document from it. Match the namespace/class to `.addin` `FullClassName`. Add an `IExternalApplication` only when startup/shutdown work such as ribbon registration is needed.

For writes, use `[Transaction(TransactionMode.Manual)]` and own an explicit transaction around the actual changes. Check document writability and relevant preconditions. Commit only after required edits succeed; inspect `TransactionStatus` and handle a non-committed result. Do not return success after a rollback or swallow an exception after partial work. A pending transaction is not a committed one. Keep service methods within a clearly defined transaction owner rather than nesting ordinary transactions.

For observational commands, use `[Transaction(TransactionMode.ReadOnly)]` and no transaction. The starter is observational; changing it to a writer also requires changing its transaction mode. Treat `Autodesk.Revit.Exceptions.OperationCanceledException` from interactive picking as `Result.Cancelled`. Return `Result.Failed` with an actionable `message` for actual failures.

See the official [command interface](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/ad99887e-db50-bf8f-e4e6-2fb86082b5fb.htm) and [transaction attributes](https://help.autodesk.com/cloudhelp/2024/ENU/Revit-API/files/Revit_API_Developers_Guide/Introduction/Add_In_Integration/Revit_API_Revit_API_Developers_Guide_Introduction_Add_In_Integration_Attributes_html.html).

## Model and UI semantics

- Use `UnitUtils` with the target version's unit API; preserve the user's units. Length, area, volume, and angle require their own conversions.
- Identify built-in parameters by `BuiltInParameter`, shared parameters by GUID where available. Check `StorageType`, `IsReadOnly`, and type/instance ownership before setting a value; formatted text is not the stored numeric value.
- Type changes can affect unselected instances. Resolve the intended impact before implementing a workaround such as duplicating a type.
- Prefer bounded collectors by category/class, selection, or view. Re-resolve elements in the intended document; linked-model geometry also needs link context and transforms.
- For modeless WPF, create an `ExternalEvent` in a valid API context and raise requests to an `IExternalEventHandler`. Keep pending request state explicit and revalidate document/targets in the handler. Dispose/unsubscribe when the UI lifetime ends. Use background work only for data detached from Revit API objects.

[Autodesk's External Events guide](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API/files/Revit_API_Developers_Guide/Advanced_Topics/Revit_API_Revit_API_Developers_Guide_Advanced_Topics_External_Events_html.html) documents the modeless UI mechanism.

## Build, install, debug

1. Inspect build targets before running them in an existing repository: Debug builds sometimes deploy automatically. Use a configuration that fits the user's authorized scope.
2. Build against the actual target API. Test pure logic outside Revit where useful; an ordinary unit test runner cannot establish Revit API integration behavior.
3. Check the generated `.addin`: command/application type, unique and stable AddInId, full assembly path, `FullClassName`, and the developer's VendorId. The starter creates a build-local manifest with an absolute DLL path. Moving the DLL requires updating that path.
4. For an authorized local install, copy only this manifest and its required artifacts to the user's chosen location. A typical per-user manifest directory is `%APPDATA%/Autodesk/Revit/Addins/<year>/`; confirm the installed version's directory. Preserve other plugins and do not overwrite an unrelated manifest.
5. Load/invoke the compiled command in the matching Revit. For a loaded DLL replacement, use the existing development loader if compatible, or coordinate a Revit restart; never kill the user's session to unlock a file. Attach a debugger to the intended `Revit.exe` when needed.
6. Exercise the feature's acceptance check, including no document/empty selection, cancellation, and failure rollback where applicable. Report which paths actually ran.

See [Autodesk add-in registration](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API/files/Revit_API_Developers_Guide/Introduction/Add_In_Integration/Revit_API_Revit_API_Developers_Guide_Introduction_Add_In_Integration_Add_in_Registration_html.html) for manifest fields. For load failures, check manifest identity/path, API year/framework, and missing dependencies before changing business logic.

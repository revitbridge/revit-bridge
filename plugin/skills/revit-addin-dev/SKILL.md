---
name: revit-addin-dev
description: Create, continue, debug, or migrate standalone Autodesk Revit C# add-ins, using revit-bridge for relevant model context and prototype verification. Use for IExternalCommand, ribbon commands, ExternalEvent, .csproj, .addin manifests, and turning a bridge snippet into a compiled plugin. 开发、接续、调试独立 Revit C# 插件，借助 revit-bridge 获取上下文和验证功能。For direct model edits without a plugin deliverable, use the revit-bridge operating skill instead.
---

# Revit Add-in Development

Deliver an ordinary C# add-in that runs inside Revit. Use revit-bridge during development when real model facts or a focused experiment would help. The resulting DLL should work without Python, MCP, or an agent session unless the user explicitly requests those integrations.

## Continue the user's project

Read the project's instructions, Git changes, solution/project files, shared MSBuild properties, manifests, command entry points, and relevant tests first. Establish the exact Revit release (including its update), .NET target, API reference source, UI approach, build command, and any build targets that install files or start processes. Revit 2025.5/2026.5 use .NET 10; earlier 2025/2026 releases use .NET 8; Revit 2027 runs only on .NET 10. Check the installed API file version and `RevitAPI.runtimeconfig.json` when available rather than relying only on the year. Preserve existing work, add-in identity, dependencies, and architecture.

Infer these details from the repository before asking. If the target year cannot be determined and changes implementation, ask once; continue version-independent work. For an existing project, implement the requested feature in its current structure. Use the starter below only for a new project or an explicitly requested isolated prototype.

Turn the request into a concrete command behavior: inputs, selection/scope, changes or output, cancellation, and a useful acceptance check. Keep unresolved model-specific choices as runtime inputs or lookups; sample element IDs and family names are not portable configuration.

## Use the bridge where it helps

| Development need | Next step |
| --- | --- |
| Implement a generic command, explain an API, repair a build | Work from source and target-version API references; a running Revit is optional. |
| Understand the selected objects, actual parameter identity, loaded types, or an observed failure | Read the smallest useful model context through available bridge tools. |
| Try an algorithm in the current model | Use a focused bridge prototype within the existing confirmation protocol, then verify the result. |
| Deliver the feature as an independent plugin | Port the logic into the project's command/service structure, build against the target Revit API, and test the compiled entry point separately. |

Read [Bridge-assisted development](references/bridge-development.md) before using live tools or porting a snippet. If no bridge is available, continue coding and compiling; identify only the checks that require Revit. Code generation or a successful build alone does not authorize edits to a live model.

## Implement in the correct runtime

Read [Add-in implementation](references/addin-development.md) for version selection, transactions, parameter semantics, UI, and installation/debugging.

The two execution contexts differ:

- A bridge snippet is a method body with `document` and `parameters`; the bridge host owns its transaction and serializes its return value.
- An independent `IExternalCommand` gets `UIApplication` from `commandData.Application`, returns `Result`, and owns transactions for writes under `TransactionMode.Manual`. A read-only command can use `TransactionMode.ReadOnly`.
- Modeless UI schedules Revit work through `ExternalEvent`; a WPF event handler or background thread is not automatically a Revit API context.

Reuse the useful algorithm, not the bridge wrapper. Keep pure calculations separate where that enables meaningful tests. Avoid introducing a service container, framework, or new package merely to wrap one command.

## Start a new add-in

The bundled generator creates a minimal read-only external command, a project, and a manifest. It requires Python 3.11+ and refuses an existing destination. Resolve its path relative to this skill directory, independent of the user's working directory:

```powershell
python "<skill-dir>/scripts/new_addin.py" --name WallInspector --revit-version 2026.5 --vendor-id MYCO --output "<workspace>/WallInspector"
```

Choose the actual project name, target release, and developer's vendor identity. The starter supports 2024, 2025, 2025.5, 2026, 2026.5, and 2027. A bare 2025/2026 selects the pre-.5 .NET 8 target; .5 and 2027 require a .NET 10 SDK, and 2027 has no .NET 8 build. Other versions require checking their SDK before adapting it. It creates a fresh AddInId for a new project; preserve that identity on subsequent edits. It also writes a `global.json`: for .NET 10 targets Autodesk's sample values (`10.0.100`, `rollForward: minor`, a .NET 10 SDK), for .NET 8 targets `8.0.100` with `rollForward: latestMajor` (any SDK 8 or newer; a newer SDK downloads the `net8.0` targeting pack on first restore); 2024 gets none.

The starter assets live in [assets/addin](assets/addin). Build the generated project from its directory, so that `global.json` applies, with `dotnet build WallInspector.csproj -c Release -p:RevitApiDir="<matching Revit installation>"`. It generates a local `.addin` beside the DLL, pointing to that DLL, and does not install or launch anything. Replace the sample selection summary with the requested feature, including its appropriate transaction mode.

## Verify and hand over

Build the target configuration and run meaningful tests already present or needed for new nontrivial logic. If an SDK or reference assembly is unavailable, report the missing prerequisite precisely; do not substitute another Revit year's API and claim compatibility.

Distinguish source generated, build passed, bridge prototype checked, compiled add-in loaded, and feature verified. A bridge experiment cannot verify the compiled plugin's manifest, ribbon, dependency loading, or ExternalEvent lifecycle. When Revit testing is available and authorized, verify the compiled command on the intended scope, including cancellation and rollback for a modifying feature.

Deliver the source changes, build command and artifact paths, and any remaining Revit checks. Installation should target this add-in only; preserve unrelated manifests. Follow existing authorization for deployment, and do not terminate Revit to release a locked DLL without permission.

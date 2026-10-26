---
schema_version: 1
id: revit-core/runtime-matrix-2024-2027
revision: 1
title: "Revit 2024-2027: target framework, global.json and manifest folder per release"
stage: [orient, port]
kind: fact
revit: ">=2024 <2028"
order: 50
triggers:
  symbols: [IExternalCommand, IExternalApplication, ExternalCommandData, UIControlledApplication]
---

## Scenario

`execute_code` snippets need none of this: the add-in compiles them for the running Revit. It matters when you build or port an independent add-in (the `revit-addin-dev` skill) or move code between releases.

| Revit | Target framework | Starter `global.json` (SDK, rollForward) |
| --- | --- | --- |
| 2024 | `net48` | none |
| 2025 before 2025.5 | `net8.0-windows` | `8.0.100`, `latestMajor` |
| 2025.5 | `net10.0-windows` | `10.0.100`, `minor` |
| 2026 before 2026.5 | `net8.0-windows` | `8.0.100`, `latestMajor` |
| 2026.5 | `net10.0-windows` | `10.0.100`, `minor` |
| 2027 | `net10.0-windows` | `10.0.100`, `minor` |

- Build against `RevitAPI.dll` and `RevitAPIUI.dll` of that release and runtime; a .5 update needs .NET 10 references, not the .0 ones. Check releases outside the table rather than extrapolating it.
- Revit 2027 has no .NET 8 build and refuses all-users manifests under `C:\ProgramData\Autodesk\Revit\Addins\2027` (its journal says they "won't be loaded"); install them to `C:\Program Files\Autodesk\Revit\Addins\2027`. Per-user manifests in `%APPDATA%\Autodesk\Revit\Addins\2027` still load. Earlier releases accept `ProgramData`, so an installer for several releases picks the folder per release.

## Versions

Assembly metadata agrees with the table: RevitAPIUI 2024.0 and 2024.3 target .NET Framework 4.8; RevitAPI and RevitAPIUI 2025.0, 2025.4, 2026.0 and 2026.4 target .NET 8.0; the installed 2026.5 (26.5.0.0), 2027.0 (27.0.10.0) and the 2027.2 reference target .NET 10.0. The 2025.5 row was not checked locally.

## Source

- Table, 2027 manifest folders and the .5 rules: `plugin/skills/revit-addin-dev/references/addin-development.md` in this package (its sources: Autodesk's .NET 10 transition notice and Revit 2027 SDK post; the 2027 starter was loaded and run in Revit 2027.0.1).
- `TargetFrameworkAttribute` and assembly versions read from the API assemblies listed under Versions.

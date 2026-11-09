# Release build, installer and acceptance on a real machine

Every rule here comes from a concrete failure or decision during the first release of a multi-year Revit add-in (ten Revit releases, obfuscated, installer, shared test machine, 2026-11). Jump to the stage you are in: change code, build matrix, obfuscate, install, accept in Revit, diagnose, write the record. Command and log patterns are in [diagnostics.md](diagnostics.md) (dependency set, installer, pre-push scan, a user's machine) and in the `revit-addin-test` skill, `references/diagnostics.md` (processes, journals, registration, helper).

## Three things to settle before the first build

1. **Revit 2025 and later run on .NET (8, then 10).** A .NET Framework C++/CLI mixed-mode assembly (many legacy CAD and geometry SDKs) cannot load into that process, and no version bump fixes it. Keep such a library in a small **out-of-process** helper (a `net48` x64 console executable started by the add-in, results passed as a file or pipe) and use that path for every Revit year, so the behaviour is identical across years.
2. **One DLL per Revit release.** 2016 and 2018 are `net462`, 2019 and 2020 `net472`, 2021 to 2024 `net48`, 2025 and 2026 `net8.0-windows`, 2025.5/2026.5/2027 `net10.0-windows` (see the `revit-addin-dev` skill, `references/addin-development.md`). The installer carries one directory per year, and each year's `.addin` `Assembly` points at **that year's** DLL. Do not share one DLL across runtimes.
3. **Some names are resolved from strings at run time.** `FullClassName` in the `.addin`, `{Binding PropertyName}` and `{x:Type local:Class}` in XAML, JSON POCO property names, spreadsheet column names. The compiler does not check them, so a rename (including one done by an obfuscator) fails silently in Revit. Any change that touches such a name **must** be loaded once in a real Revit.

## Build matrix: what "passes" means

What counts as a passing build matrix is defined in the revit-addin-test skill (SKILL.md, 'What passes').

## Obfuscation before release

If the project obfuscates (Obfuscar or similar), configure it for this add-in, not by copying another project's file:

- Obfuscate only the add-in's own assembly. Third-party libraries ship untouched.
- An add-in with XAML bindings, JSON POCOs, `INotifyPropertyChanged` or reflection-based libraries needs `KeepPublicApi=true`, `HidePrivateApi=true` and **`RenameProperties`, `RenameFields`, `RenameEvents` off**. A project with no XAML and dictionary-based JSON can rename everything; yours probably cannot.
- Command classes, including `internal` ones, may be renamed when the ribbon reads `typeof(Command).FullName` at run time: the name changes consistently. The `IExternalApplication` named in the `.addin` is public and kept by `KeepPublicApi`.
- Let the packaging script scan each `.xaml` for types referenced through its own `xmlns:local` namespace and emit a `SkipType` for each, so a later XAML reference to an internal type does not break the release.
- **Verifying obfuscation is not "it compiled".** Read the mapping file: the application class marked skipped, XAML-referenced classes skipped, command classes renamed. Then load the obfuscated build in a real Revit and run every command once.
- Searching the DLL for a class name as a string proves nothing: .NET metadata stores namespace and type name separately, so `My.Ribbon.App` is never a contiguous byte sequence. Trust the mapping file.

## Installer and registration

Two registration mechanisms exist, and an add-in uses **one**:

- Classic manifest: `%ProgramData%\Autodesk\Revit\Addins\<year>\<Name>.addin` (per machine, administrator install) or `%APPDATA%\Autodesk\Revit\Addins\<year>\` (per user), `Assembly` as the absolute path of that year's DLL. Revit 2027 refuses the `ProgramData` folder; see the `revit-addin-dev` skill, `references/addin-development.md`.
- Autoloader bundle: `ApplicationPlugins\<Name>.bundle\PackageContents.xml`.

The same `AddInId` registered both ways is a **duplicate**: Revit logs `Duplicate addins` and loads the classic one, which is usually the stale development copy. The installer removes other manifests of this add-in in the same folder (back them up first) and stale bundles of every user profile. Under an elevated installer `{userappdata}` is the administrator's profile; to clean every user, enumerate `%SystemDrive%\Users\*`.

Installer pitfalls met in practice:

- Inno Setup reads a script as UTF-8 only when it has a BOM; a script with **two** BOMs fails with "line 1 is not inside a section". Strip before adding. Set `[*.iss] charset = utf-8-bom` in `.editorconfig`.
- `VersionInfoVersion` must be numeric (`1.0.0`, not `0.0.1-test`).
- An XML comment in a `.csproj` cannot contain `--` (MSB4025).
- Inno Setup writes no install log by default; `setup.exe /LOG="<path>"` does. Ask for it when an installation on a user's machine went wrong.
- A launcher that starts a helper hidden (VBS, a `/hidden` switch) discards its console, so a crash at start leaves no log at all. Write the first log line before anything can fail, and run the visible launcher once when the log is empty.

## "Installed but not in Revit" or "still the old version"

Check in this order; do not guess:

1. **Was Revit restarted after the install?** The newest journal of that year must be newer than the installed files. A journal from before the install means the session predates it.
2. **Is it registered twice?** Search the journal for `Duplicate addins`. If yes, remove the stale registration (classic manifest pointing at a Debug directory is the usual culprit).
3. **Which DLL did Revit load?** The journal line `Added pushbutton ... assembly: <path>` names it.
4. Only then read `AddInLoadFailureMessage` and the add-in's own log.

When the journal mentions the add-in nowhere, it was never registered for the Windows account that runs Revit (an elevated installer may have written the per-user files into the administrator's profile), or the unsigned-add-in prompt was once answered "Do not load"; both leave no trace beyond the absence. [user-machine-support.md](user-machine-support.md) covers the same question on a user's machine.

On first start Revit asks whether to load an unsigned add-in; choose "Always Load". Automation answers that dialog by window class, never by closing Revit.

## Acceptance on a real machine (release build, every release)

Minimum: one `net48` year (2024) and one .NET year (2025 or 2026) each get the full list; every other shipped year gets load plus one run of each command.

1. Close your own Revit instances, install the **release package** (not the DLL from the build directory).
2. **Open a saved private copy of a project.** Do not create a new project through the API as a probe: on 2025 and 2026 Revit stopped responding to the mouse for minutes afterwards, with the add-in uninvolved. With a saved copy the same probes passed 21 of 21 times.
3. The ribbon tab appears and the button set equals the release configuration.
4. Sign-in or licensing, when the add-in has it, shows the right identity and greys out business buttons when signed out. Check which server the local settings file points at before judging.
5. Run every shipped command once on real input. Count, position and size of created elements match expectations. One native Undo restores the model.
6. Failure paths once each: Escape, an unsupported pick, a timeout, Cancel.
7. Write the result into the four-cell table (below). Anything not run is "not tested" and goes to open items. Never write "passed" for a step that did not run.

**A `.5` release on the test machine.** Revit 2025.5 and 2026.5 run on .NET 10, and a `net8.0` DLL built for 2025/2026 still loads and runs there; the journal then logs `API_ERROR Assembly version conflict in some references in <Name>.dll` (RevitAPI 26.0 against 26.5, `System.Runtime` 8 against 10) at every load and every command, as it does for Autodesk's own bundled add-ins. Default position: the `net8.0` build is accepted for that machine when every shipped command passes the list above on it; the review record names the journal's `Release:` line, the conflict count and the decision as a known item. Build a dedicated `.5` configuration (`net10.0-windows`) when any command fails there or the add-in uses an API that changed in the `.5` release, and never report the `.5` release as tested from a pre-`.5` run.

## A shared test machine

- Before starting Revit, list the running `Revit.exe` processes with PID, start time and command line. **Never touch another person's Revit**: no clicks, no closing, no debugger attach. Close your own with `CloseMainWindow()`; answer "No" to its save prompt.
- Copy every DWG, RVT, RFA and template to a private directory before use. Hash the originals before and after and record both hashes.
- Delete temporary `.addin` files when done. At the end compare every file under the user and machine `Addins\*` folders with the state you found; they must match.
- Crop screenshots to the Revit window. A full-screen capture on a shared machine has captured another user's open credentials once; the image was destroyed.
- Do not change proxies, network, firewall or other tools' processes. Build output and evidence stay on the data drive.
- A scripted session (start Revit, drive the add-in, compare base and branch, restore the install) follows the `revit-addin-test` skill, `references/in-revit-autotest.md`, and uses the helper scripts in that skill's `scripts/` directory.

## Out-of-process helper failures

When the add-in reports "helper exited with code 1 ... Exception: eXxx":

1. Run the **installed** helper executable (not the one in the build directory) with **exactly the arguments the add-in passes** (including flags such as block expansion) on a sample file with a known result. Success means the deployment is fine and the input is at fault; failure means a missing dependency.
2. Find the file the user actually chose (journal: `AFileNavDialog::OnFileNameOK`), copy it to the private directory and reproduce.
3. Classify the failure:
   - A single-entity exception (`eNullObjectId` and friends): the read loop needs a per-entity `try/catch` that skips the entity, counts it under an `unsupported` bucket, writes one warning and continues. One dangling reference must not fail a whole drawing with thousands of healthy entities.
   - A missing dependency (`e312`, a managed wrapper failing to load): deploy the complete file set recorded in the deployment manifest with SHA-256 per file; the packaging script verifies it on every build. It is the set that was proven, not a theoretical minimum.
   - Locked layers: open with `GetObject(id, ForRead, openErased: false, forceOpenOnLockedLayer: true)`.
   - Units: a drawing declaring inches (`INSUNITS=1`) is often drawn in millimetres; converting by the declared unit scales everything by 25.4. Default to millimetres and offer "use the drawing's declared unit" as an option.
4. After the fix, the failing file passes **and** the known samples still produce the same counts.

## The review record

Four rounds of rework on one batch were almost entirely record problems. Rules:

- In the behaviour-change table the "before" column is the **base branch's actual behaviour**, verified with `git show <base>:<file>`, never "the previous round" or "the old core".
- **Four cells per button: normal, linked model, skipped, cancelled.** Each cell holds the actual result with the evidence file and line, or "not applicable: reason", or "not tested" listed again under open items. State the dependency commit and sample version used.
- The commit message's "Verification" section says where (which line, which evidence) it was verified, not "builds". No notes about the editing process.
- Never reword or rebase commits already under review; append a commit, and put corrections to earlier messages in an "errata" section.
- Evidence is text, JSON, CSV or PNG, cropped to the Revit window. No DWG, RVT, RFA, PCP, DLL or EXE, also not inside a zip. Obfuscation mappings stay out of the repository.
- Before pushing, list the changed file types, open every new zip, and grep the diff for passwords, `ftp://user:pass@` and `-----BEGIN`. Local backup refs that hold drawings are never pushed. See [diagnostics.md](diagnostics.md) §3.

## Comparing a new command with the old one

When the new command reads a file that the old one read from an imported instance, compare like for like: import the same file origin-to-origin on the base branch and run the old command; select the same file on the new branch. Compare element counts and skip counts, then sample at least ten walls (position, thickness) and ten columns (centre, size, rotation, including rotated ones; use a synthetic fixture if the real drawing has none). Explain every difference (for example the old code never read entities inside blocks, or read every layer).

## Pitfalls, by frequency

- Teigha-style SDKs and `System` both define `Exception`; write `System.Exception` in `catch`.
- `Parameter.Set(string)` on a non-text parameter returns `false` and does not throw; check the return value instead of treating the whole run as rolled back.
- `Parameter.AsDouble()` returns 0, not 1, for an unset numeric parameter; check `HasValue` first.
- `CannotKeepJoined` and `CannotJoinElementsError` share the same message text; Revit 2025 raises the latter. Identify failures by `FailureDefinitionId`, not by text.
- Probing with `NewProjectDocument` on 2025/2026 can hang Revit; use a saved copy.
- Running a long build as `nohup ... &` inside a tool shell ends the shell early and orphans the build, which keeps files locked; use the shell's own background mode.
- MSYS `ln -s` on Windows copies the tree instead of linking; use `mklink /J` for junctions and remove them with `rmdir` so the target survives.
- Admin installers resolving `{userappdata}` to the administrator profile (see Installer).
- Two BOMs in an Inno script, a non-numeric `VersionInfoVersion`, `--` in a `.csproj` comment (see Installer).

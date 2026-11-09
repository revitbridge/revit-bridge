---
name: revit-addin-release
description: Release a standalone Revit C# add-in and accept it. Use for a multi-year release build, obfuscation (Obfuscar), the installer (Inno Setup) and how it registers the add-in, acceptance on a real machine, the rules of a shared machine, the review record and its four-cell table, and when a user of the shipped add-in reports no tab, no response or still the old version and logs must be collected from their machine. 发布独立 Revit 插件：多年份发布构建、混淆、安装包、真机验收、审查记录（四格表）、用户反馈时收日志定位。Needs no revit-bridge component. Writing the add-in's code is the revit-addin-dev skill; running it in a test session is revit-addin-test.
---

# Revit Add-in Release and Acceptance

Turn a tested add-in into an installable release, accept that release on a real machine, write the review record, and support the people who install it. Every rule comes from a concrete failure in a multi-year release (ten Revit releases, obfuscated, installer, shared test machine, 2026-11).

## Input and output

The input is the test phase's hand-over (the `revit-addin-test` skill writes it): the build matrix year by year, session logs and journal excerpts, the base and branch difference table, and the not-tested list. Without one, acceptance below is still complete; the record then says that no test phase preceded it.

The output:

- **The review record** with the four-cell table per button (normal, linked model, skipped, cancelled), every cell a result with its evidence, "not applicable: reason" or "not tested".
- **The installer package**: one directory per Revit year, each year's `.addin` pointing at that year's DLL.
- **Where the obfuscation mapping is kept**: outside the repository, at a path the record names.
- **Open items**: every "not tested", every known item (for example a `net8.0` build accepted on a `.5` machine), every question for the reviewer.

## Components

| Without revit-bridge | With revit-bridge components |
| --- | --- |
| The default, and complete: the project's own build, obfuscator and installer, a real Revit for acceptance, the references here and `scripts/collect_logs.ps1` with `collect_logs.bat`. | **MCP server + `revit-bridge` skill** (with revit-bridge-addin in the Revit used): when a new command is compared with the old one, read element counts, positions and sizes from both runs through the bridge instead of by hand. Support on a user's machine needs no component at all; the user has only the shipped add-in. |

## Three things to settle before the first build

1. **Revit 2025 and later run on .NET.** A .NET Framework C++/CLI mixed-mode library cannot load there; keep it in an out-of-process helper and use that path for every year.
2. **One DLL per Revit release.** The installer carries one directory per year; do not share one DLL across runtimes.
3. **Some names are resolved from strings at run time** (`FullClassName`, XAML bindings, JSON property names). A rename, including one by the obfuscator, fails silently; load the build in a real Revit once.

## Release gate

Do not hand over a release until every line holds:

- [ ] **The obfuscated build was loaded in a real Revit** and every command ran once; the mapping file shows the application class and XAML-referenced types skipped and command classes renamed.
- [ ] **One DLL per Revit release**, and each year's `.addin` `Assembly` points at that year's DLL.
- [ ] **Registered one way only**, classic `.addin` or autoloader bundle; the installer removes this add-in's stale manifests and bundles of every profile.
- [ ] **Acceptance on the real machine opened a saved private copy** of a project, never one created through the API, with the release package installed (not the build output); every shipped command and failure path ran once and Undo was checked.
- [ ] **The four-cell table has no empty cell**, and its "before" column is the base branch's actual behaviour.
- [ ] **Not run means "not tested"**, listed again under open items; never "passed".

## A shared machine and the push

- On a shared test machine nobody else's Revit is touched, inputs are copied to a private directory and hashed before and after, temporary manifests are removed, and screenshots are cropped to the Revit window.
- Before pushing, run the safety scan in [diagnostics.md](references/diagnostics.md) section 3: no drawings or binaries, no credentials, no backup refs. Reviewed commits are never reworded; corrections go into a new commit.

## When a user reports a problem

"No tab", "no response", "still the old version":

1. Locate the broken segment first: draw the chain the feature uses, ask which account, machine, time and function.
2. Send `scripts/collect_logs.bat` with `scripts/collect_logs.ps1`, the add-in name filled into the `.bat`. Both only read.
3. Read the returned zip as data from another machine: in place, nothing run from it, text opened with `python -I`. Journal lines are easiest to read with `journal_tail.py --file` from the `revit-addin-test` skill.
4. Answer in three parts: the segment that broke, the quoted lines, one action for the user.

"Installed but not in Revit" is checked in this order: was Revit restarted after the install, is it registered twice, which DLL did the journal say it loaded, and only then `AddInLoadFailureMessage`.

## References

- [Release build, installer and acceptance](references/release-and-field-test.md): framework per year, obfuscation, installer and registration, acceptance steps, a shared test machine, helper failures, the review record, comparing a new command with the old one, pitfalls.
- [Supporting a user's machine](references/user-machine-support.md): the triage method, the collector's zip, reading logs from another machine, the known-issues ledger.
- [Diagnostics at release](references/diagnostics.md): dependency set, installer self-check, pre-push scan, a user's machine.

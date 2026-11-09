---
name: revit-addin-test
description: Test a compiled Revit C# add-in in a real Revit. Use to load and run the add-in in a real Revit, script an autotest session that starts Revit, drives the add-in and restores the install, read Revit journals, find out why Revit hangs or is not responding during a test, explain Duplicate addins in a journal, decide whether a build matrix passes, and compare a base and a branch build call by call. 真机跑插件、读 journal、Revit 卡住或无响应时找原因、编译矩阵是否通过、新旧分支对比。Works without any revit-bridge component. Writing the add-in's code is the revit-addin-dev skill; release, installer and acceptance are revit-addin-release.
---

# Revit Add-in Testing

Run the compiled add-in in a real Revit and say exactly what was tested. Everything here needs only a real Revit, Python 3.11+ and Windows PowerShell 5.1; no revit-bridge component is required, and one only makes some steps faster.

## Input and output

The input is the coding phase's hand-over (the `revit-addin-dev` skill writes it; ask for the missing items when it came from elsewhere): the build command, each target Revit year with its .NET target, artifact paths with SHA-256 hashes, the commands to test with input, expected result, cancel and failure path, and what was already checked through the bridge and what was not.

The output goes to the release phase (the `revit-addin-release` skill):

- the build matrix, year by year, judged by "What passes" below;
- per session: year, commit, installed DLL hash, model copy, the session log and the journal lines that matter (`scripts/journal_tail.py`);
- the base and branch difference table, every difference attributed to one intended change;
- what was not tested, each as a manual check with sample, steps and expected result.

## Components

| Without revit-bridge | With revit-bridge components |
| --- | --- |
| The default. A plain add-in is driven through its journal and window automation: readiness is `API_SUCCESS { Starting External Application` and its pushbutton in the journal, dialogs are found by window class, results come from the add-in's own log. The four `scripts/` are all the tooling: `revit_ps.ps1`, `window_list.ps1`, `journal_tail.py`, `addin_snapshot.py`. | **revit-bridge-addin** in the Revit under test: readiness is its port answering a ping, calls go through its protocol (JSON lines on `127.0.0.1:<port>`) and every reply is appended to a JSONL file, so the same calls on base and branch diff line by line. **MCP server + `revit-bridge` skill**: read model facts (counts, parameters, positions) before and after a command instead of reading them off a screenshot. |

## Shared-machine rules

1. **List running Revits before installing or launching** (`scripts/revit_ps.ps1 -RequireNone`). A Revit you did not start means: do not install, do not launch; wait, and if it stays, tell the user.
2. **End only the Revit you started**, by the pid your launcher printed: `CloseMainWindow()` and "No" to the save prompt first, `taskkill /PID <pid> /T /F` only a minute later and only on your scratch copy.
3. **When the user says pause**, stop your background tasks and the helpers they spawned, end your Revit, restore the installed add-in, note where you were and wait for an explicit go.
4. **Never modify the user's files.** Copy sample models to a scratch directory and open the copy; hash originals and links before and after; the user's own add-in logs are not deleted on restore.
5. **Never touch production** during a test: no release upload, no manifest change, no server.

## One session

1. **Snapshot the installed add-in once**, before the first install: `scripts/addin_snapshot.py snapshot "<install dir>" <backup dir>`.
2. **Install only the year under test** and check that the installed DLL's hash equals the build you mean to test.
3. **Refuse if any Revit runs**, then start Revit and wait until the add-in is ready: observed in the journal or the port, never timed.
4. **Run the calls**, recording each result as it arrives; calls that can open a modal dialog go last, with their own timeout.
5. **Collect at once** the add-in log, the newest journal and the worker log of that year; then end only your pid.
6. **Run the same calls on base and branch**, same year, same model copy.
7. **Finish**: reinstall the base build, run `scripts/addin_snapshot.py verify` and a final `scripts/revit_ps.ps1`.

The full procedure, the masking rules for comparing base and branch, timing and pitfalls are in [Automated test sessions in a real Revit](references/in-revit-autotest.md).

## When it is stuck

Signs: a call times out, `revit_ps.ps1` shows `responding=False`, the add-in log stops at one command. `scripts/window_list.ps1 -ProcessId <pid>` names the dialog Revit waits on and `scripts/journal_tail.py <year>` shows what it was doing; [in-revit-autotest.md](references/in-revit-autotest.md) section 6 lists the dialogs met so far and their causes. Answer the unsigned add-in prompt by window automation, never by closing Revit. A round with a stuck background worker is void: fix the cause and rerun the whole round. For "not loaded", "registered twice" (`Duplicate addins`) or "old DLL", read [Diagnostics while testing](references/diagnostics.md) sections 2 and 3: a manifest on disk says registered, only the journal says loaded.

## What passes

Use the build tool the project already uses. Some mixed resx/WPF projects build only under Visual Studio's `MSBuild.exe` (`dotnet build` reports MSB3822/MSB3823 on image resources); do not switch tools to make an error go away.

```
MSBuild.exe <Project>.csproj -restore -t:Rebuild -p:Configuration=Release<year> -p:BaseOutputPath=<outside the workspace>\ -nologo -v:m -clp:Summary
```

A matrix passes only when all of these hold:

- **Every supported year builds with 0 errors.** Years the product does not support are listed as such, not skipped silently.
- **Warnings are compared per `file + code` against the base branch and none is new.** An equal or lower total is not evidence: deleting old code lowers the total and hides new warnings.
- **Every commit on the branch builds** for one representative configuration. A commit that does not build means the history was rewritten badly.
- Compare the two sides at the **same verbosity**; `-v:normal` prints resolution warnings (MSB3245) that `-v:m` does not.
- Record the commit and tree hash of every dependency repository or reference package before and after the build.
- Output goes to a drive with space; a system drive at 99 % breaks restore in confusing ways.

A session passes when base and branch are identical except for the intended changes, each explained, and the restore `verify` is clean. A step that did not run is "not tested", never "passed"; it goes on the not-tested list for the release phase.

# Automated test sessions in a real Revit

How to run an add-in's checks inside a real Revit from an agent session without disturbing anyone else on the machine, and how to compare a branch with its base. Written after a bridge add-in's review batches (2026-10 to 2026-11); one of them was stopped because repeated Revit restarts got in the way of a colleague opening a project. The etiquette matters more than the test itself.

Helper scripts live in this skill's `scripts/` directory (`SK` below):

| Script | What it does |
| --- | --- |
| `revit_ps.ps1 [-RequireNone]` | Lists every `Revit.exe`: pid, year, start time, parent, responding, window title, arguments. `-RequireNone` exits 3 when any Revit runs. |
| `window_list.ps1 -ProcessId <pid>` | Visible top-level windows (class and title) of one process: finds the modal dialog a stuck Revit waits on. |
| `journal_tail.py <year> [--lines N] [--all] [--file F]` | Decodes the newest journal of that year (UTF-16, UTF-8 or GBK) and prints the dialog, transaction, ExternalEvent, background-work and error lines, plus whether each RevitWorker log says it is accepting requests. |
| `addin_snapshot.py snapshot <install dir> <backup dir>` / `verify <install dir> <backup dir> [--ignore <glob> ...]` | Copies the installed add-in folder with SHA-256 hashes before the first install; after the tests, reports changed, added and missing files and exits non-zero unless the set is restored. |
| `collect_logs.ps1 -Name <Name>` (with `collect_logs.bat`; in the `revit-addin-release` skill's `scripts/`) | On a user's machine: zips the add-in's manifests, bundles and DLL versions, the recent journals and the add-in's own logs to the Desktop, read-only. See the `revit-addin-release` skill, `references/user-machine-support.md`. |

## 1. Shared-machine rules

1. **Before installing or launching, list the running Revits** (`revit_ps.ps1 -RequireNone`). Any Revit you did not start means: do not install, do not launch. Wait; if it stays, stop and tell the user. Another person's Revit locks that year's DLL, so an install half-succeeds (manifest replaced, DLL not), and every launch you add disturbs them.
2. **End only the Revit you started**, by the pid your launcher printed, never by process name. Close it the way a user would first: `CloseMainWindow()`, then "No" to the save prompt ([diagnostics.md](diagnostics.md) section 1). Fall back to `taskkill /PID <pid> /T /F` (`/T` also ends its RevitWorker) only when the process is still there a minute later and the open document is your scratch copy, so nothing of value is unsaved; a scripted session that drove the add-in through its protocol is normally in that state.
3. **When the user says pause**: stop your background tasks and every helper process they spawned (shells, Python, tails: they would start the next round); end your own Revit; restore the installed add-in (section 4); write down where you were; wait for an explicit go.
4. **Never modify the user's files.** Copy sample models to a scratch directory before opening them, and open the copy; load linked files read-only from their original path; hash originals and links before and after; add-in logs written by the user's own Revit sessions are their data and are not deleted or overwritten on restore.
5. **Never touch production** during a test: no release upload, no manifest change, no server.

## 2. Prepare

- **Build** the configuration under test; for a quick per-commit check build one .NET Framework year and one .NET year.
- **Snapshot the installed add-in once**, before the first install: `python SK/addin_snapshot.py snapshot "<install dir>" <scratch>/pre-install-backup`. For a per-user classic install the directory is `%APPDATA%\Autodesk\Revit\Addins\<year>\<Name>\` (plus the `.addin` beside it); for a bundle, `%APPDATA%\Autodesk\ApplicationPlugins\<Name>.bundle\`. A machine-level classic install has two directories, the DLL folder (for example `%ProgramData%\<Name>\`) and the manifest folder (`%ProgramData%\Autodesk\Revit\Addins\<year>\`): snapshot each into its own backup directory and verify both.
- **Install only the year under test** by copying that year's DLL over the installed one. Do not run a whole-bundle installer from a worktree that built one year: it replaces every year the worktree built and deletes the years it did not.
- **Base-branch baseline**: install the base build the same way, from the main checkout.
- Before each run, check that the installed DLL's hash equals the build you mean to test, and record it.

## 3. One session

A session is: refuse if any Revit runs; start Revit; wait until the add-in is ready; run the calls; collect logs; end only your pid.

- **Readiness** is observed, not timed: for a bridge add-in, the TCP port answers a ping and the document is open (a .NET-year Revit may take 45 to 70 seconds to open its port); for a plain add-in, the journal shows `API_SUCCESS { Starting External Application` and the pushbutton. On the first start of a new build Revit asks about the unsigned add-in; answer "Load Once" or "Always Load" by window automation, never by closing Revit.
- **Calls** go to the add-in's own protocol (for example JSON lines on `127.0.0.1:<port>`); append each reply to a JSONL file as it arrives; treat a socket timeout as "Revit is stuck", write one `timeout` line and stop the session. Put calls that can open a modal dialog (exports, dialogs) **last**, with their own timeout, because the session ends there.
- **Collect** immediately after the calls: the add-in log, the newest journal and worker log of that year. Some launchers delete the add-in log of that year when they start, so copy the previous one first.
- **Run the same calls on base and branch**, same year, same model copy. For anything timing-related, run the call several dozen times and compare distributions, not one result.

## 4. Finish: restore and verify

Reinstall the base build for the years you replaced, then `python SK/addin_snapshot.py verify "<install dir>" <scratch>/pre-install-backup --ignore "Logs/*"` and `revit_ps.ps1` to confirm no Revit of yours is left. Put back only the logs of the years your own Revit used; a year someone else's Revit also used during the test is recorded, not restored, because copying the backup over would delete what their Revit wrote. The hand-over names the three facts: every year's DLL equals the base build, configuration and manifest equal the snapshot, nothing is missing.

## 5. Compare base and branch

- Mask what legitimately differs: output directories, request ids, GUIDs, unique ids, timestamps in names. Compare only the part both sides have when the runner truncates output.
- **New element ids shift as a block** when the branch creates more elements or a .NET-year Revit commits background work asynchronously, so a literal diff reports hundreds of false differences. Map ids above the model's existing range one-to-one across the whole file (the mapping must be consistent everywhere), then diff.
- **Every remaining difference is attributed to one intended change.** A difference that matches no intended change is a finding. "Identical except for the intended changes, each explained" is the only pass.
- **Pure geometry can be checked without Revit**: a console project that references the installed `RevitAPI.dll`, with `AssemblyResolve` and `PATH` pointing at the Revit directory, can use `XYZ`, `Transform` and `BoundingBoxXYZ`. `Element` objects cannot be constructed, so keep the calculation in an `internal` function that takes bounding boxes and transforms.

## 6. When Revit hangs or answers strangely

Signs: the call's socket times out; `revit_ps.ps1` shows `responding=False`; the add-in log stops at one command. Then `window_list.ps1 -ProcessId <pid>` shows the dialog it waits on and `journal_tail.py <year>` shows what it was doing. Journals of a Chinese Revit are GBK, not UTF-16; the script decodes by content.

Dialogs met in practice and their causes:

- **"Export with Temporary Hide/Isolate"**: a view on the sheet has temporary hide/isolate on, and `Document.Export` asks. Fix in the add-in: inside a `TransactionGroup`, disable the temporary mode, export, roll the group back.
- **"Cannot export while color fill is being calculated"** (`TaskDialog_Calculation_In_Progress`): the RevitWorker is stuck on the Steel Connections LocalDB (its log stops at "Testing connection to Steel Connections databases...") and the background calculation never completes. If that LocalDB was started by your own Revit, `SqlLocalDB stop SteelConnections<year> -k` and rerun the whole round; the round is void. Prevention: check the worker log for "accepting" at the start of each round.
- **A view switch that sometimes stays "requested"**: the command ran inside `Idling`, where the active view cannot change. Run commands only from an `ExternalEvent` handler.

**Probes**: for an experiment inside the real Revit, add a temporary command class to the add-in in a throw-away worktree. Build it once, never commit it, and expect the registered-commands count check to fail by one while it exists. Delete it and `git checkout` the touched files afterwards.

## 7. Pitfalls

1. Evidence file names must not start with `test`: pytest's doctest collection picks up `test*.txt` and fails a repository-root run. Use `revit-test-*.txt`.
2. A launcher that deletes the add-in log on start: copy the log before every session.
3. Timing races: the same branch can give two results on two runs. Before calling a difference a regression, run the base several dozen times to see whether it varies too.
4. File write times are one-second resolution: an export re-run within the same second is missed by a modification-time check. Call several times.
5. Bash heredocs eat backslashes: write complex Python to a file with an editor tool, then run it. Repository files may be CRLF; normalise before text replacement and restore afterwards.
6. Git Bash `sha256sum <path with backslashes>` prefixes the output with `\`; use `sha256sum < file`.
7. A coverage checker that only sees literal string concatenation misses text passed through variables or lambdas.

## 8. Timing reference (one machine, 2026-10)

| Step | Time |
| --- | --- |
| One round (full contract run plus supplementary calls, then smoke plus mutating calls), 2025 / 2026 | about 4 / 4 to 7 minutes (one 120 s timeout adds 2) |
| Full contract run, Revit kept open | 2025 about 1.5 min, 2026 about 2.5 min |
| Session on a sample model (8 MB model plus 20 MB link) | about 1.3 min (port after about 55 s) |
| All-years build / one-year incremental | 4 to 5 min / about 8 s |

Count one Revit start and one stop per round when estimating, and tell the user beforehand how many times Revit will open and close.

## 9. What the hand-over says

- Per round: year, commit under test, installed DLL hash (equal to the build's), model copy used.
- Base and branch numbers (for example `128/128`), and every difference with the change it is attributed to.
- Cases that could not be constructed on this machine, listed as manual checks with sample, steps and expectation.
- Restore result: the `verify` output and the final `revit_ps.ps1` output.

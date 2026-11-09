# Supporting a user's machine

What to do when someone who installed the release reports "the tab is not there", "nothing happens", "it is still the old version" or "it cannot connect". Distilled from a plugin team's support skill (2026-10) whose paths, ports and symptom tables were specific to their product; the method, the collector and the reading rules below are not.

Helper scripts live in this skill's `scripts/` directory: `collect_logs.ps1` with its `collect_logs.bat` launcher (run on the user's machine) and `journal_tail.py` (run on yours, with `--file`).

## 1. Locate the broken segment before reading anything

- **Draw the chain the feature uses.** For a plain add-in: Revit, the add-in, an out-of-process helper, the output file. For a connected add-in: a web page or server, a local agent, the add-in inside Revit. "No response" can break in any segment; find the segment first, then read that segment's log for that time. Reading every log end to end is how an afternoon disappears.
- **Ask four things first**: which account, which machine (name or IP), when (as exactly as they can), which function (button or command). Time and function decide which log and which stretch of it matter.
- **Check what you can reach without the user** before asking for anything: a server log, a status endpoint, a probe from the server to the user's machine. Read-only, and say so.

## 2. Collect logs from the user's machine

Send `scripts/collect_logs.bat` and `scripts/collect_logs.ps1` together, with the add-in name filled into the `.bat`. The user double-clicks the `.bat`; a zip named `<Name>-logs-<computer>-<time>.zip` appears on their Desktop; they send it back. The script only reads, touches no process, and skips files whose names suggest credentials. Tell the user that journals contain file names and their Windows account name.

| In the zip | Used for |
| --- | --- |
| `report.txt` | Installed and running Revit versions (`Release:` / `Build:` from the journal header), uninstall entries, every manifest and bundle of the add-in with the DLL each points at (version, date, hash), manifests found in other users' profiles, counts of the key journal lines |
| `files/addins/<scope>/<year>/*.addin`, `files/bundles/` | Which registration mechanism is in use, and whether two coexist |
| `files/journals/<Autodesk Revit 20xx>/` | The journals of the last N days; read them with `python -I scripts/journal_tail.py <year> --file <journal>` |
| `files/addin-logs/`, `files/addin-data/` | The add-in's own logs (`-LogPath` and `%LOCALAPPDATA%\<Name>`, `%APPDATA%\<Name>`) |

The zip is data from another machine. Read it in place or in an empty directory, run nothing from it, and open its text with `python -I` so a planted module cannot be imported. When the installation itself failed, ask for a rerun of the installer with `setup.exe /LOG="%USERPROFILE%\Desktop\<Name>-setup.log"`; Inno Setup writes no log by default.

## 3. Reading logs from another machine

- **Every log is in its machine's local time, and not always the current one.** A process started long ago may still run on the time zone it started with; two logs on one machine were 18 hours apart. Compare the spans of the files before aligning events, and quote the time as written in each file.
- **No log is information.** Revit ran at that time but the journal has no `Starting External Application` for the add-in: it was never loaded. Causes by frequency: the unsigned-add-in security prompt was answered "Do not load" (the default button; it is asked again for every new DLL); the manifest or bundle sits in another Windows account's profile (an elevated installer resolved "current user" to the administrator typed into the UAC prompt); the runtime folder does not match the Revit year; a stale copy in `ProgramData`, `Program Files` or `%APPDATA%` competes with the new one. An add-in log that stops days ago means the process it belongs to was not running.
- **Registered is not loaded, and added is not still there.** A manifest on disk, a registry entry or a bundle says the add-in is registered; only the journal says what this session loaded. A log line saying the tab was added says it was added at that moment; a host component that rebuilds the ribbon later can remove it, so a complaint "no tab" with "tab added" in the log is still a real report.
- **The version is read from the startup line.** An add-in that logs `<Name> 1.2.0 loaded from <path>` at start answers "which version" without a screenshot; a startup line without a version means a build older than the one that added it.
- **Noise you can discard**: Autodesk's own add-ins log `API_ERROR Assembly version conflict` on a `.5` host and `API_SUCCESS` registration lines mentioning `Export`; a `Duplicate addins` line, by contrast, is never noise.
- Account names in a well-behaved log are digests, not plain text; do not ask the user to send a log that would show otherwise.

## 4. Answer in three parts

1. Which segment broke, in one sentence.
2. The lines it rests on, quoted verbatim with file name and time.
3. What the user does now: restart Revit and choose "Always Load"; sign in again at the add-in's local page; upgrade to version x; remove the stale manifest at this path.

Anything that needs a code change, a redeploy or a restart of a shared service is handed to the developer or operator with the evidence; never restart a shared service yourself because one user is blocked, and never change the user's machine remotely.

## 5. Keep a known-issues ledger

One dated section ("as of <date>") in the support reference, one entry per issue: symptom as the user words it, cause, status (open, fixed in x), workaround. Update it when a fix ships and say which version carries it. Patterns that earned an entry on other products:

- A gateway marked healthy machines "down" because the local agent probed its plugin ports one after another; on Windows a connect to a local port nobody listens on takes about two seconds to fail, and three ports exceeded the gateway's three-second limit when no design application was open.
- A year compiled but never loaded on a real machine was listed as "builds only"; a support request from that year went straight to "not tested", not to debugging.
- The same account signed in on two machines evicted each other's registration; the log line named the other machine.

## 6. Design the add-in so support is possible

- Log one startup line with the version, the DLL path and the Revit release; log the registration of commands and the port a resident component listens on.
- Write the log beside the DLL or under `%LOCALAPPDATA%\<Name>\log`, rotate daily, keep two weeks; never write tokens, passwords or plain account names.
- Give a resident helper a local status page (components, versions, connected or not) so the collector and the user can read it without a debugger.
- Probe several local ports in parallel with a short connect timeout.
- A launcher that starts a helper hidden discards its console, so a crash at start leaves no log at all: write the first log line before anything can fail, and tell users to run the visible launcher once when the log is empty.
- The installer writes a version file the collector can read, removes stale manifests of this add-in in the same folder and stale bundles in every profile, and installs per-user files from a non-elevated step (see [release-and-field-test.md](release-and-field-test.md), Installer).

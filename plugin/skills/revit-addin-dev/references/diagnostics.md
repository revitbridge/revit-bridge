# Diagnostics: processes, journals, installed manifests, pre-push scan, a user's machine

Commands used on a real test machine. PowerShell blocks run in a PowerShell shell, Bash blocks in Git Bash. Replace `<Name>` with the add-in's name.

## 1. Who is running Revit (before starting one)

```powershell
Get-CimInstance Win32_Process -Filter "Name='Revit.exe'" |
  ForEach-Object { '{0} start={1} cmd={2}' -f $_.ProcessId, $_.CreationDate.ToString('MM-dd HH:mm'), $_.CommandLine }
Get-Process Revit | Select-Object Id, MainWindowTitle, StartTime
```

A PID you did not start is off limits. To close your own:

```powershell
$p = Get-Process -Id <PID>; [void]$p.CloseMainWindow(); Start-Sleep 8
# A "save changes?" prompt is a #32770 child window; find its "No" button and post BM_CLICK (0x00F5).
```

## 2. Revit journals

`%LOCALAPPDATA%\Autodesk\Revit\Autodesk Revit <year>\Journals\journal.NNNN.txt`. The highest number is not always the newest; sort by modification time. Journals are mixed-encoding text, so grep with `-a`.

```bash
J="$LOCALAPPDATA/Autodesk/Revit/Autodesk Revit 2026/Journals"
latest=$(ls -t "$J"/journal.*.txt | head -1)
grep -a -i "<Name>" "$latest"
```

| Question | Pattern |
| --- | --- |
| Did the add-in start, from which DLL | `API_SUCCESS { Starting External Application` and `Added pushbutton .* assembly:` |
| Registered twice | `Duplicate addins` / `in bundle folder is duplicated` |
| Why it failed to load | `AddInLoadFailureMessage` |
| Which file the user picked in a file dialog | `AFileNavDialog::OnFileNameOK` |
| What a pick returned | `SEL RESULT:` |
| Crash or non-UI-thread API call | `native0x80000003` / `non-main-thread` |
| Was this session started after the install | compare the journal's modification time with the installed files' |
| 2027 manifest in a refused folder | `won't be loaded` |

## 3. Where the add-in is registered

```bash
# classic manifests, machine and user
find /c/ProgramData/Autodesk/Revit/Addins "$APPDATA/Autodesk/Revit/Addins" -iname "<Name>*.addin"
grep -o "<Assembly>[^<]*" <file.addin>          # which DLL it points at
# autoloader bundles (a leftover one duplicates the classic manifest)
ls "$APPDATA/Autodesk/ApplicationPlugins" /c/ProgramData/Autodesk/ApplicationPlugins | grep -i "<Name>"
```

A manifest or bundle on disk says the add-in is registered, not that this session loaded it; the journal (section 2) says what was loaded. A per-user manifest can also sit in another Windows account's profile when an elevated installer wrote it (`/c/Users/*/AppData/Roaming/Autodesk/Revit/Addins/<year>/`).

## 4. Reproduce an out-of-process helper failure

```powershell
$exe = "C:\ProgramData\<Name>\2025\Helper\Helper.exe"       # the installed copy, not the build output
& $exe "F:\private\copy.dwg" --out "F:\private\out.json" <the flags the add-in passes> 2> "F:\private\err.txt"
$LASTEXITCODE                                               # 0 means usable
Get-Content "F:\private\err.txt" | Select-Object -Last 3    # metrics line or the exception
```

Run a known-good sample first and compare its counts. Read large JSON with Python; `ConvertFrom-Json` fails on it and floods the console.

## 5. Verify a deployed dependency set

```powershell
Get-ChildItem "C:\ProgramData\<Name>\2025\Helper" | Get-FileHash -Algorithm SHA256
```

Compare with the deployment manifest shipped in the repository. A missing native module typically shows as a numeric error code or a managed wrapper that fails to load.

## 6. Installer script self-check

```bash
head -c6 build/<Name>-setup.iss | xxd | head -1      # exactly one UTF-8 BOM (ef bb bf) then text
```

## 7. Pre-push safety scan

```bash
# origin/main stands for the base branch your project reviews against (master, develop, ...)
git diff --name-only --diff-filter=AM origin/main..HEAD | grep -iE "\.(dwg|rvt|rfa|rte|pcp|dll|exe|pdb)$"
git diff origin/main..HEAD | grep -E "^\+" | grep -iE "password[\"'=: ]|ftp://[^ ]*:[^ ]*@|-----BEGIN (RSA|PRIVATE)"
# open every new zip and check its name list for the types above
git for-each-ref refs/backup | wc -l                 # local refs with drawing evidence: never push them
```

A `password` hit is a leak only when it is a real credential or a `ftp://user:pass@` URL; field names and test strings (`example.invalid`, `test-only-password`) are not.

## 8. On a user's machine

When a user reports "no tab", "no response" or "still the old version", send them `scripts/collect_logs.bat` and `scripts/collect_logs.ps1` with the add-in name filled in. The zip they return holds the manifests, bundles and DLL versions (section 3), the recent journals (section 2, read with `journal_tail.py <year> --file <copy>`) and the add-in's own logs. Treat it as data from another machine: read it in place, run nothing from it, open its text with `python -I`. The triage order and the reading rules are in [user-machine-support.md](user-machine-support.md).

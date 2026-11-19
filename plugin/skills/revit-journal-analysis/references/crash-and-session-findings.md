# Crash and session findings

What to look for once the right journal is open, what each marker means, and how to write it up. Every marker here gives a candidate: quote it with its line number and read the record around it ([journal-anatomy.md](journal-anatomy.md) section 4) before you say what it means.

## 1. How the session ended

| End state | Evidence | Read next |
| --- | --- | --- |
| Clean | `finished recording journal file` as the last line, after `ExitManagedInstance`, `Journal Exit` and `License shutdown starting` | Whatever the user reports, before the exit |
| No clean exit | No `finished recording journal file` | Revit crashed, was ended by force (Task Manager, `taskkill`), lost power, or the copy was cut short: read the crash path (section 2), the last 20 lines, the last `Jrn.Command`, the last dialog and the last timestamp |
| Live | No end marker yet, and Revit still writes the file (its process runs, the file grows) | Not a finding; read a fresh copy later |

- The last timestamp (`'C DD-Mon-YYYY hh:mm:ss.mmm;`) is the last moment the session is known to have been alive. Quote it as written: it is in that machine's local time.
- The user's last action is the last `Jrn.Command`, `Jrn.RibbonEvent` or dialog input above the end, not the last line: Revit goes on writing resource samples and API records after it.

## 2. The crash path

Markers Revit writes on its way down:

| Marker | What it says |
| --- | --- |
| `Exception occurred` | Revit's crash protection caught an exception; the next record holds `ExceptionCode=0x... ExceptionFlags=0x... ExceptionAddress=...` |
| `ExceptionCode=0x<code>` | The kind of exception (section 3). A code of zero is no exception |
| `System exception in` | A native exception report |
| `Exception occurred during Revit crash protection` | A second failure while Revit tried to recover |
| `unhandledExceptionFilter is executed` | Nothing handled the exception; the process normally ends after it |
| `TaskDialog_Fatal_Error_Occured` (spelled so) | The fatal-error dialog, found by its id, the same in every UI language |
| `SLOG $<hex> ... >Crash` | A crash record |
| `CEF failed to initialize` | Revit's embedded Chromium did not start; the next comment line names the failure. Seen before a .NET exception at start-up; often missing runtime libraries or another add-in that ships its own CEF |

- **Exception markers followed by** the fatal-error dialog, the unhandled-exception filter, a failure in crash protection, an `SLOG ... >Crash` record or no clean exit: Revit most likely crashed there. Exception markers in a session that ended cleanly were caught and survived; list them all the same.
- **The last command before the first exception** is the last `Jrn.Command` above it: the user's last action, a candidate trigger, not the cause.
- One crash leaves several of these lines in two or three records: it is one incident.
- Then look for `.dmp` files and a CER log of the same session (section 8), and note the phase the exception fell in ([journal-anatomy.md](journal-anatomy.md) section 5).

## 3. Exception codes

A code names the kind of failure, never its cause.

| Code | Kind |
| --- | --- |
| `0xe0434352` | .NET (managed) exception |
| `0xe06d7363` | C++ exception |
| `0xc0000005` | Access violation |
| `0x80000003` | Breakpoint or internal assertion: the program stopped itself |
| `0xc00000fd` | Stack overflow |
| `0xc0000409` | Fast fail (stack buffer overrun) |
| `0xc0000374` | Heap corruption |
| `0xc0000017` | Out of memory |

## 4. Licence

- `License initialization` starts licensing in every session; `LicenseUpd(N)` and `Adlsdk` lines follow it.
- Errors: `Adlsdk Error`, and `LicenseUpd(N)` lines that contain `Error`.
- Licence trouble usually shows before any other symptom. One error line does not mean the licence is invalid: read what start-up, opening and exit did after it.
- `License shutdown starting` belongs to a normal exit; it is no error.

## 5. Opening a model

- Every model opened: `openFromModelPath [<path>]`. Count distinct paths, not lines.
- Failures: `Load exception "<text>"`, `openFromModelPath did not complete`, `exceptionsInOnOpenDocument`. One failed open usually writes several of them: one incident. The reason is in the exception text (a damaged file, disk space, a cancel, a file from a newer release).
- `Missing ESSchema`: an Extensible Storage schema the model refers to is missing. Autodesk has documented a related error for one release; check the release before you suggest any fix.
- The dialogs around the opening tell the rest, for example `TaskDialog_Unresolved_References` (linked files not found).

## 6. Add-in assembly version conflicts

`API_ERROR { : Assembly version conflict in some references in <assembly> assembly ...` means an add-in references another version of an assembly than the one Revit has loaded. In most cases the add-in still loads and works; Autodesk's own add-ins log these on a `.5` release, at every load and every command. Count them per assembly (the first line and the number of lines), never per line, and treat them as a cause only when that add-in then fails.

## 7. Lines that are not program faults

- **Modelling messages.** A comment whose text, right after the nesting prefix, starts with `ERROR:` (or the same word in the UI language) is a warning Revit showed the user while modelling, such as a constraint that cannot be kept. Report it only when the question is about the model.
- `DBG_INFO` and `DBG_WARN:` appear in healthy sessions; a `DBG_WARN:` alone means neither damage nor a crash. `DBG_ERROR:` and `Assertion failed:` are worth reading with their context.
- **Generic words** (`fatal error`, `unhandled exception`, `access violation`, `out of memory`, `access denied`, `file not found`, `failed to synchronize`, `File not saved`, `cannot reconcile the differences`) also occur in quoted messages, handled exceptions and negations. Read the line before you count it.
- An exception type inside an `API_ERROR` record (an `ArgumentException`, a `FileNotFoundException`) is an API call that failed for an add-in, often one the add-in handled. It points at that add-in, not at a crash.
- `Steel Connections DB extension` appears in every session that loads Steel Connections. Only an exception that names `registered by application Steel Connections DB extension`, or `SQLLocalDB instance is malfunctioning`, is a finding (section 8).

## 8. Logs next to the journal

| Log | Where | Linked to a session by |
| --- | --- | --- |
| Crash dumps | `journal.NNNN.NNNN.dmp` in the `Journals` folder | The session number in the name. Binary: count and size them, never read them as text. A dump usually means an exception |
| Customer Error Reporting | `%LOCALAPPDATA%\Autodesk\CER\<id>\cer.log`, one folder per reported process, other Autodesk products included | Its head names the process, as in `"pid":<n>,"product":"RVT","release":"<year>"`: the same pid as the journal's `Session<pid>_P0MainQueue`. Without a pid, an overlapping time span. A running process keeps its log open: copy it first |
| Steel Connections | `%ProgramData%\Autodesk\Revit Steel Connections <year>\<lang>\DatabaseConnectionErrors.log`, UTF-16 with a byte-order mark; entries `[n][HH:MM:SS] text` | The year, then the time. Entries carry only a time of day, so the date comes from the file's modification time; a match on the time of day alone is weak |

- When the SQL Server Express LocalDB behind Steel Connections fails, the journal often shows nothing; the evidence is in that log. Repairing the LocalDB instance happens outside Revit.
- Every link is a candidate: state its basis (the pid; the year and time span; the time of day only).
- The `revit-addin-release` skill's log collector copies the journals with their worker logs and dumps, but no CER or Steel Connections logs; ask for those when the end state calls for them.

## 9. Write it up

Per session, then per finding:

```text
journal.NNNN.txt  <size> bytes, sha256 <first 12 hex>, read as <encoding> [<n> lines degraded: <line numbers>]
  Build: ...  Release: ...  pid <n>  <first timestamp> .. <last timestamp>
  End state: clean | no clean exit | live
  Findings (candidates, not verified):
    line <n>: "<quoted text>"  suggests ...; confirmed by ...
  Counts: <n> records, <n> hit lines, <n> incidents (each named)
  Not read: <files, line ranges, limits>
```

- Quote short: the marker and enough of the line to find it again; never paste whole journals into a report.
- Replace the user, domain and machine names (`<user>`, `<domain>`, `<machine>`) and personal paths in anything you hand on.
- End with one sentence on what is most likely and the one step that would confirm it: a reproduction, the dump, the CER log, or the user's own account of the last action.

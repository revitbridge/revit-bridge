# Crash and session findings

What to look for once the right journal is open, what each marker means, and how to write it up. Every marker here gives a candidate: quote it with its line number and read the record around it ([journal-anatomy.md](journal-anatomy.md) section 4) before you say what it means.

## 1. How the session ended

| End state | Evidence | Read next |
| --- | --- | --- |
| Clean | `finished recording journal file` as the last line, after `ExitManagedInstance`, `Journal Exit` and `License shutdown starting` | Whatever the user reports, before the exit |
| Ended by Revit after a fatal error | `program terminated` as the last line, no `finished recording journal file` | The crash path (section 2) just above it: the exception, the fatal-error box and its answer |
| No clean exit | No `finished recording journal file` | Revit crashed, was ended by force (Task Manager, `taskkill`), lost power, or the copy was cut short: read the crash path (section 2), the last 20 lines, the last `Jrn.Command`, the last dialog and the last timestamp |
| Live | No end marker yet, and Revit still writes the file (its process runs, the file grows) | Not a finding; read a fresh copy later |

- The last timestamp (`'C DD-Mon-YYYY hh:mm:ss.mmm;`) is the last moment the session is known to have been alive. Quote it as written: it is in that machine's local time.
- The user's last action is the last `Jrn.Command`, `Jrn.Close`, `Jrn.RibbonEvent` or dialog input above the end, not the last line: Revit goes on writing resource samples and API records after it. Skip the records Revit writes itself ([journal-anatomy.md](journal-anatomy.md) section 3): after the last document closes, the last `Jrn.Command` is Revit's own `ID_REVIT_MODEL_BROWSER_OPEN`, and the user's last action is the `Jrn.Close` and the save-prompt answer above it.
- **Was the user quitting?** `ID_APP_EXIT` says so. A clean exit without it was started some other way (another program or a script closed Revit); say that the journal does not name who. Without it, a session that ends with no clean exit after `[Jrn.CloseDocumentFile]` ended while a document was closing, not while Revit was exiting. Worker and queue tear-down lines (`Revit worker N closed cleanly`, `Terminating RevitWorker Processes`) follow a normal exit and a crash alike and say nothing about which it was.
- **No exception marker and no dump.** The journal cannot tell apart a process ended by force, a hang the user ended, a power loss and a process that died without a record. Name what Revit was doing in the last records, list those causes without ranking them, and say what would tell them apart (the user's own account, the Windows event log, a CER log for that process id).

## 2. The crash path

Markers Revit writes on its way down:

| Marker | What it says |
| --- | --- |
| `Exception occurred` | Revit's crash protection caught an exception; the next record holds `ExceptionCode=0x... ExceptionFlags=0x... ExceptionAddress=...` |
| `ExceptionCode=0x<code>` | The kind of exception (section 3). A code of zero is no exception |
| `Managed exception occurred:` | A .NET exception reached Revit's top level. The next line is its message, then `WPF RenderMode at time of exception was: ...`, then `ExceptionCode=0xe0434352`; there is no `Exception occurred` line in this form. The message is the best lead in the journal: it often names the component (a web view, a WPF control, a library) and so the kind of add-in to look at |
| `Unconverted MessageBox "<text>"`, then `Jrn.Data "MessageBox"` with the answer, then `[Jrn.AutoConvertedMessageBox] Rvt.Attr.MessageId: IDS_UNRECOVERABLE_ERROR Rvt.Attr.MessageAnswer: <IDYES, IDNO or IDOK>` | The fatal-error message box where it is not a task dialog (seen in a 2019 journal). The text is in the UI language; `IDS_UNRECOVERABLE_ERROR` is the same in all of them, though not every such box writes that line. When the box offers to save recovery files, the answer says whether any were written |
| `program terminated` as the last line | Revit ended the process itself after a fatal error |
| `captureTryCrash 0x<code>` | Revit caught an exception inside a guarded operation and wrote a crash dump for it; the code is the kind of exception (section 3). The session usually goes on and can end cleanly, so this is the marker behind a `.dmp` file in a session nobody saw crash. Read what follows it: often a failed model open (section 5) and the dialog that reported it |
| `System exception in` | A native exception report |
| `Exception occurred during Revit crash protection` | A second failure while Revit tried to recover |
| `unhandledExceptionFilter is executed` | Nothing handled the exception; the process normally ends after it |
| `TaskDialog_Fatal_Error_Occured` (spelled so) | The fatal-error dialog, found by its id, the same in every UI language |
| `SLOG $<hex> ... >Crash` | A crash record |
| `CEF failed to initialize` | Revit's embedded Chromium did not start; the next comment line names the failure. Seen before a .NET exception at start-up; often missing runtime libraries or another add-in that ships its own CEF |

- **Exception markers followed by** the fatal-error dialog, the unhandled-exception filter, a failure in crash protection, an `SLOG ... >Crash` record or no clean exit: Revit most likely crashed there. Exception markers in a session that ended cleanly were caught and survived; list them all the same.
- **The last command before the first exception** is the last `Jrn.Command` above it: the user's last action, a candidate trigger, not the cause.
- One crash leaves several of these lines in two or three records: it is one incident.
- **Compare the sessions of one folder.** When some sessions crashed and others ended cleanly, list the load records of each ([addin-records.md](addin-records.md)) and what the user did last. An add-in that started in every crashed session and in no clean one, or the same message, the same exception address and the same last action in every crash, is a strong candidate; one that is in all of them is not. State it as a correlation with the line numbers of both lists; the dump or a run without that add-in confirms it.
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

- Every model loaded: `openFromModelPath [<path>]`, for the model the user opened and for every linked model loaded with it. Count distinct paths, not lines.
- **Which one the user opened**: the `"File Name"` value in the `Jrn.Data` record right after `ID_REVIT_FILE_OPEN` (it can be a relative path). The other paths are its links: say "loaded the link", not "opened the model". The `File: Open` line that follows the command marks the start of the open, not its end.
- The `openFromModelPath` line starts with the seconds the load took and is written when the attempt to load that file ends, whether it worked or not, so linked models appear before the model that hosts them. A failed attempt is followed by `openFromModelPath did not complete`; the line alone does not mean the model opened. In a journal that stops during an open, a host with no `openFromModelPath` line of its own had not finished opening: report the open as unfinished, not as completed, and name the link or import the last records were working on.
- Failures: `Load exception "<text>"`, `openFromModelPath did not complete`, `exceptionsInOnOpenDocument`. One failed open usually writes several of them: one incident. The reason is in the exception text (a damaged file, disk space, a cancel, a file from a newer release).
- A model opened from the cloud (Autodesk Docs, BIM 360) fails in other words and writes none of the strings above: section 10.
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
| Crash dumps | `journal.NNNN.NNNN.dmp` in the `Journals` folder | The session number in the name. Binary: count and size them, never read them as text. A dump means an exception was captured, not that the session crashed: look for `captureTryCrash` or `ExceptionCode=` near the dump's time, then at the end state |
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
- Replace the user, domain and machine names (`<user>`, `<domain>`, `<machine>`), IP addresses (`<ip>`) and personal paths in anything you hand on. Look for them where the journal writes them: the profile folder name under the `Users` directory of any drive, UNC paths `\\<host>\`, `Additional IP address/name found for host <host>: <ip>`, printer lines such as `PortName : <ip>`, and server names in central-model paths. A four-part number after `version` is an assembly version, not an address.
- End with one sentence on what is most likely and the one step that would confirm it: a reproduction, the dump, the CER log, or the user's own account of the last action.

## 10. Cloud models

A model opened from Autodesk Docs or BIM 360 is downloaded piece by piece, and the journal records each request. Described from Revit 2026 journals; treat other releases as likely similar and check the strings.

| Record | What it says |
| --- | --- |
| `Jrn.RibbonEvent "ModelBrowserOpenDocumentEvent:<method>:{...}` | The user's click on a cloud model in Home; `<method>` is `open`, `openAndAuditPreviousCloudVersion` and so on |
| `Open Cloud Model Method: <method>, Region: <r>, Model Guid: <g>, Project Guid: <g>` | One open attempt starts. Count these lines for the attempts; the same model guid is the same model |
| `ModelPath Created: ... Path = "CLD:\\<region>\{<project guid>}<project>\{<model guid>}<model>.rvt"` | Which project and model |
| A `GetFileAsync (HTTP)` line holding `{"start_time":"<UTC>","http_method":"GET","http_url":"...","http_response_code":<code>}` | One download. `200` is normal. Any other code is followed by `Status code on HEADER retrieval = <reason>` and `HTTP Request Exception Error encountered.` |
| `Exception Model missing bundle from S3]`, `Model missing bundle from S3`, `Thread "RevitSrvToolDownloader_<n>" terminating due to ...` | A download failed for good and its thread stopped |
| `status: ERROR` or `status: ERROR : Cannot connect to central model`, one line of an analytics block (`logAnalyticsRequest_`, then `context_call:`, `duration:`, `status:` lines) | The outcome Revit reported for that step; the block is one record |
| `CentralModelException "<text>"` | The open gave up with that reason |
| `<seconds>== ... [Cloud]standardOpenDocumentFile` | How long the whole open took |
| `Reading BasicFileInfo from "<model>" failed. The local model has to be recreated.` | The cached local copy is not usable and is fetched again: the start of a repeated open, not the failure |

- **Incidents.** One failed open is one incident: the click, the `Open Cloud Model Method` line, a failed request and its retry (two lines with the same code), the `missing bundle` lines, the `status: ERROR` block and perhaps a `CentralModelException`. Three attempts on one model are three incidents with one cause to look for.
- **Read the HTTP code, not only the message.** `Model missing bundle from S3` is what Revit concludes; the response code above it is the evidence. For `403`, compare `Expires=<Unix seconds>` in that request's URL with the `"start_time"` of the same line, both in UTC: a request made after `Expires` was refused because its download link had run out, which points at an open that took too long, not at missing data or missing permission. An earlier request with the same `Expires` and code `200` confirms it. Do the arithmetic and write both times down: `Expires` is seconds since 1970-01-01 in UTC (`date -u -d @<n>`, or `python -c "import datetime; print(datetime.datetime.fromtimestamp(<n>, datetime.timezone.utc))"`), and it is compared with the `"start_time"` inside the JSON, which ends in `Z`, never with the journal's own time stamp at the start of the line, which is the machine's local time and can be many hours away. A real gap is seconds to minutes. What the server decided stays unverified from the journal.
- **Not program faults.** `File not found exception: cld://...` followed by a file-name syntax message, repeated in passes over every linked cloud model (hundreds of lines), is also written at start-up before anything is opened: count passes and distinct models, not lines. `[Jrn.OnLineHelpData] ... HID_MODEL_CORRUPTED` names the help topic of the window Revit showed; it does not show that the model is corrupt. The window itself is a `WpfWindow(<name>,<title>)` action in a `Jrn.AddInEvent` line; its message text is not recorded.
- **A session that ends after a failed cloud open** with no exception marker and no dump is still the case of section 1: the failed open is what Revit was doing, not a recorded cause of the end.
- **Redact more.** A cloud journal also holds project and model guids, project and model names (`CLD:` paths, `Project Name:`, `""displayName""`), the values of `x-ads-*` request headers, `CFID` values, signed download links (`Signature=`, `Key-Pair-Id=`), and `SLOG host=` / `server=` lines with the machine's full name and address, next to `User's Name` and `User's Machine Name`. Keep a guid's identity when you replace it (the same placeholder for the same guid), or the attempts can no longer be told apart.

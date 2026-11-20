# Journal anatomy: files, encodings, lines, records, phases

How a Revit journal is laid out, so that a plain text search is read correctly. Markers are quoted as Revit writes them. What each finding means is in [crash-and-session-findings.md](crash-and-session-findings.md); add-in records are in [addin-records.md](addin-records.md).

## 1. Files and sessions

Revit writes to `%LOCALAPPDATA%\Autodesk\Revit\Autodesk Revit <year>\Journals\` (Revit LT: `Autodesk Revit LT <year>`). Group the files into sessions by their names only; never guess a session from the content.

| File | Role | How to treat it |
| --- | --- | --- |
| `journal.NNNN.txt` | The main journal of one session | Read it |
| `journal.NNNN.workerN.log` | A worker process of that session | Read it when the main journal points at background work. It mostly covers the worker's start-up; its `ExternalDBApplication <Class> (...) was not loaded in RevitWorker` lines are normal in every session |
| `journal.NNNN.NNNN.dmp` | A crash dump of that session | Binary: count and size it, never read it as text |
| `journal.NNNN.txt.copy.txt` | A copy of the main journal | Compare it with the main file before reading both |
| `journal.NNNN.txt.abbrev`, `journal.NNNN.txt.copy.txt.abbrev` | An abbreviated journal | Read it only when the main file is missing; it is not the whole session |
| `dump.NNNN.txt` | Usually empty | List it |
| Any other name | Unrecognised | Read it as a session of its own |

- The highest number is not always the newest session: sort by modification time and match the session to the time the user reports.
- Two files with the same size and SHA-256 are one input: read one, list the other as a duplicate. A zero-byte file is listed as empty, not read.
- **A live journal.** While Revit runs it keeps its journal open and appends to it. Copy the file, read the copy, and expect the last line to be cut off. A journal without its end marker is live, not a crash, while its Revit still runs: the process id in `Session<pid>_P0MainQueue` belongs to a running `Revit.exe`, or the file keeps growing.
- **A copied journal** (from a user's zip, a ticket, any folder other than `Journals`) no longer says its Revit year through its folder; take the year from `Build:` and `Branch:` in its header.
- Note which files you read with their sizes; a SHA-256 of each makes the report checkable later.
- If a file changed while you read it (size or modification time differ before and after), the read is void: copy it and read the copy.

## 2. Encoding

A journal has one encoding per file, chosen by the machine that wrote it. Decide it file by file, in this order:

1. A byte-order mark decides (UTF-8 or UTF-16).
2. Otherwise, the whole file decodes strictly as UTF-8.
3. Otherwise, the Windows ANSI code page of the machine that wrote it. The header line `Actual language code=<XXX>` (in 2024 and later journals) names the Revit UI language, which suggests the code page for a localized UI: `CHS` GB18030 (a superset of GBK), `CHT` cp950, `JPN` cp932, `KOR` cp949, `RUS` cp1251, `PLK`, `CSY`, `HUN` cp1250, `DEU`, `FRA`, `ITA`, `ESP`, `PTB`, `NLD` cp1252. `ENU` and `ENG` say nothing about the system: an English Revit on a Chinese Windows writes GBK.
4. Otherwise, cp1252.

Pitfalls:

- GB18030 decodes almost any byte sequence, so Western cp1252 text "decodes" into runs of rare Chinese characters. Accept GB18030 only when the decoded non-ASCII text reads as common Chinese; otherwise go on to the next candidate.
- cp1252 also decodes almost anything. A Chinese journal with a few damaged bytes must not be read as cp1252: keep the Chinese decoding and mark the damaged bytes.
- When no candidate decodes the whole file, read it anyway with the most plausible one, show each undecodable byte as `\xNN`, keep the line numbers, and report how many lines were affected and which. Never drop bytes or replace them silently (`errors="ignore"`).
- A file with NUL bytes is UTF-16 without a byte-order mark, or not a journal. A byte-level `grep` finds no ASCII marker in UTF-16 text; decode it first (`Select-String` reads UTF-16 with a byte-order mark).
- Split lines at CRLF, LF and a lone CR, and at nothing else. Python's `str.splitlines()` also splits at form feeds and other separators, and its line numbers then drift from `grep -n`; split with `re.split(r"\r\n|\r|\n", text)` instead. Say which tool's line numbers you quote.
- Read a journal from another machine with `python -I`, so that nothing stored next to it can be imported.

## 3. Line kinds

Most lines are indented by one or two spaces, in a long session nearly all of them. "Starts with" below means after that leading space. Never anchor a search at column 0 (`^Jrn\.`, `^'C`): it drops most of the records and then the "last command" it finds is wrong. Search without an anchor, or with `^\s*Jrn\.`.

| A line that | Is |
| --- | --- |
| starts with `'` | A comment: Revit's own log text. Most carry a nesting prefix such as `' 0:< ` |
| starts with a quote, one capital letter, a date and a time, such as `'C 01-Jan-2026 10:00:00.000;` | A timestamped record. The letter varies; do not read a meaning into it |
| starts with `Jrn.` | A recorded action or value: the part Revit can replay |
| ends with ` _` | Continues on the next line, in `Jrn.` lines and comments alike |

What the lines hold:

- **Header**, in the first thirty or so lines: `started recording journal file`, `' Build:`, `' Branch:`, `' Release:` (not every release writes `Release:`) and `Actual language code=`. The very first line is a memory sample (`Initial VM:`).
- **Process id**: `Session<pid>_P0MainQueue`, a little further down; used to match a CER log.
- **Older releases** write less: a Revit 2019 header has `Build:` and `Branch: RELEASE_2019` but no `Release:` and no `Actual language code=`, the journal has no `Session<pid>` line (the process id is then unknown from the journal) and no `[Jrn.AddInManifest]` records, and worker shutdown reads `Terminating Slave Processes`. Report a missing field as missing; do not infer it.
- **User actions**: `Jrn.Command` (a ribbon command, a shortcut or an internal command id such as `ID_REVIT_FILE_OPEN`; `ID_APP_EXIT` is the user quitting Revit), `Jrn.Close "[<document>]" , "<view>"` (the user closing a view; closing a document's last view closes the document, followed by `[Jrn.CloseDocumentFile]`), `Jrn.RibbonEvent`, view and keyboard input (`Jrn.MouseMove`, `Jrn.LButtonDown`, `Jrn.Wheel`, `Jrn.Key`), dialog input (`Jrn.PushButton`, `Jrn.ComboBox`, `Jrn.Edit`, `Jrn.TreeCtrl` and similar control names). `Jrn.RibbonEvent "Execute external command:` is a click on an add-in's button; the rest of the line names the control and often the command class.
- **Not user actions**: `Jrn.Directive` (settings Revit writes itself), `Jrn.Data` (values recorded for replay, dialog answers included), `Jrn.AddInEvent` (an add-in's own window) the automatic `Jrn.RibbonEvent "TabActivated:` at start-up, and `Jrn.Command "Internal" , " , ID_REVIT_MODEL_BROWSER_OPEN"`, which Revit writes itself when the last document closes and the Home screen comes back. The word `"Internal"` alone decides nothing: `ID_REVIT_FILE_OPEN` and `ID_APP_EXIT` are written as `"Internal"` too and are the user's.
- **API records**: `API_SUCCESS { ... }`, `API_ERROR { ... }` and `API_INFO`: what the Revit API did for an add-in. The braces can span lines; a record ends at its closing `}`.
- **Resource samples**: `Delta VM:` lines with their RAM figures and `GUI Resource Usage`, written after operations. A sample, not an event.
- **Debug markers**: `DBG_INFO` and `DBG_WARN:` (both common in healthy sessions), `DBG_ERROR:`, `Assertion failed:`.
- **Dialogs**: `TaskDialog "<text>"` is the message the user saw, `Id : TaskDialog_<Name>` its id when the dialog has one (the same in every UI language; some dialogs record only their buttons, as `CommonButtons :` or numbered `Command Links:`), and a `Jrn.Data` record with `"TaskDialogResult"`, often on its continuation line, is the answer.
- **Repeats**: a line that ends with ` - N times` stands for N identical lines. It is one hit line.

## 4. How lines group into records

Read a finding in its record, never as a lone line. Every line belongs to exactly one record; a record is a run of consecutive lines, and a blank line stays in the record it sits in.

| What you see | One record |
| --- | --- |
| A `Jrn.` line and its ` _` continuation lines | One operation |
| `API_SUCCESS {`, `API_ERROR {` or `API_INFO {` up to its closing `}` | One API event |
| An error message with its exception type, its stack lines (`at ...`, `--->`, `--- End of`) and an indented inner exception | One error |
| The same error line several times in a row | One error, repeated |
| `Exception occurred` and the `ExceptionCode=... ExceptionFlags=... ExceptionAddress=...` line after it | One native exception |
| VM and RAM lines with `GUI Resource Usage` | One resource sample |
| What an add-in registers at start-up, up to its `Starting External Application` record; the records of its unloading | One add-in block each ([addin-records.md](addin-records.md)) |

- A record also ends at a blank line, a new timestamped record, a new `Jrn.` line, a new API brace or a resource sample.
- A brace or a continuation still open at the end of the file is unclosed: typical of a journal cut off by a crash or still being written. Quote it as unclosed.
- Records that are identical once the timestamp, the nesting prefix and ` - N times` are removed are one fingerprint: report "N identical records", not N incidents.

## 5. Session phases

A session moves through these phases in order. The phase a finding falls in narrows what to suspect: an exception while add-ins load points at an add-in, one during shutdown at clean-up after the work was done.

| Phase | Starts at |
| --- | --- |
| Start-up | The first line |
| Licensing | `License initialization`, `LicenseUpd(`, `Adlsdk` |
| Add-in loading | The first `Starting External Application:` or `Starting External DB Application:` |
| Document open | `openFromModelPath`, or an open command such as `ID_REVIT_FILE_OPEN`, `ID_APPMENU_PROJECT_OPEN`, `ID_FAMILY_OPEN`, `ID_FILE_NEW_CHOOSE_TEMPLATE` or `ID_FAMILY_NEW` |
| Interactive | The first user action (section 3); answering the open dialog with `Jrn.Data` does not end the opening |
| Shutdown | `ExitManagedInstance`, `Journal Exit`, `License shutdown starting`, `finished recording journal file` |

Lines with no marker of their own belong to the phase they sit in. During shutdown Revit also writes `Total session time spent reclaiming memory` and, in some releases, `CefSharp has been shutdown`.

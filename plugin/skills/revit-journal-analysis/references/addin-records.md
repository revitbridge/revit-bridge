# Add-in records: what the journal says about each add-in

A manifest or a bundle on disk says an add-in is registered; only the journal says what this session did with it. These records answer "did it load", "which file", "whose is it" and "what did it do". Line kinds and record grouping are in [journal-anatomy.md](journal-anatomy.md).

## 1. The load record

Revit writes one record for each external application it starts:

```text
' 0:< API_SUCCESS { Starting External Application: <Name>, Class: <Namespace.Class>, Vendor : <VendorId>(<description>), Assembly: <path to the DLL>, Assembly Version: <version> }
```

- `Starting External DB Application:` is the same record for an `IExternalDBApplication`. A search for `Starting External Application` alone misses it.
- In general the record, once the timestamp, the nesting prefix and the `API_SUCCESS {`, `API_ERROR {` or `API_INFO {` wrapper are set aside, starts with `Starting`, `Creating`, `Created`, `Loading` or `Loaded`, then `External Application`, `External DB Application` or `Add-in`, then a colon. Search for that shape, not for one spelling.
- The record usually fits on one line but can continue on the next lines up to its `}`, with `Vendor` or `Assembly` on a later line. Read to the closing brace.
- **A failed load** is the same record under `API_ERROR {`; the reason is in the record and in the lines right after it.
- **Start-up registrations come first.** What an add-in registers in its `OnStartup` (events, commands, services, updaters, ribbon buttons) is usually written before Revit writes that add-in's own load record. The registrations just above a load record usually belong to it; they are not another add-in.
- Count add-ins by load records, never by registrations: one add-in leaves dozens of `Registering ... event` lines.

```bash
grep -a -n -E "(Starting|Creating|Created|Loading|Loaded) (an? )?(External (DB )?Application|Add-?in) *:" "$f"
grep -a -n -E "API_ERROR \{ *Starting External" "$f"
grep -a -o -E "Vendor *: *[^(]*\([^)]*\)" "$f" | sort | uniq -c
```

The last line lists every vendor field with its count. The text in the parentheses contains commas (`Vendor : <id>(<company>, <website>)`): match up to the closing parenthesis and never split a load record at commas, or every vendor with a description is lost.

## 2. The other add-in records

Each kind is the stage of an add-in's life the record belongs to.

| Kind | Records, as the journal writes them |
| --- | --- |
| load | `[Jrn.AddInManifest]` with `Rvt.Attr.AddInName:`, `Rvt.Attr.AddInId:`, `Rvt.Attr.CommandClassName:`, `Rvt.Attr.CommandVendorId:`, `Rvt.Attr.CommandVendorDescription:`, `Rvt.Attr.AddInCodeSigningStatus:` and `Rvt.Attr.AddInLoadFailureMessage:` (newer releases; seen in 2026 and 2027 journals). `Registered an external service: Name(...); VendorId(...)`, `Registered an external server: Name(...)`, `[Jrn.RegisterExternalServer]`. `API registering command ... by application <X>`, `Replacing command id '...' ... from application '<X>'`, `Registering <Event> event by application <X> (<add-in id>)`. `Third Party Updater '<X>: <updater>' registered`. `The add-in folder '...'`, `The requested assembly '...' was loaded to the context` |
| button | `Added pushbutton Id: N, name: <button>, text: ..., class: ..., assembly: <path>, parentId: ...`; also `Added Pushbutton <id>, Name <button>, Class <class>, VendorId <id> (<description>), Assembly <path>` (search without regard to case) |
| unload | `Unregistering <Event> event by application <X>`, `API unregistering command`, `Third Party Updater '...' has been unregistered`, `Forcibly unregistering Updater`, `Forcibly removing all update triggers`, `Unregistering all external services` |
| run | `[Jrn.ExternalEventExecution]` and `[Jrn.AddInDynamicUpdate]` with `Rvt.Attr.AddInName:` and `Rvt.Attr.VendorId:`, `[Jrn.ExternalServerExecution]`, `[Jrn.ExternalResourceLoad]`; a click on the add-in's button is `Jrn.RibbonEvent "Execute external command:` |
| not loaded | `ExternalDBApplication <Class> was not loaded`; `Add-in manifest file <path> won't be loaded` (Revit 2027 refusing an all-users manifest under `ProgramData`) |
| UI | `Jrn.AddInEvent` (an add-in's own window, `WpfWindow(...)`), `AddInJournalClient` |

- `Rvt.Attr.AddInLoadFailureMessage:` is in every manifest record. `NoError` means Revit accepted the manifest and its assembly, not that the application started: an application whose load record is under `API_ERROR {` can still have `NoError` here. Any other value (for example `WrongAssembly` or `LoadCancelled`) is why the add-in was not loaded at all. A search for the field name alone hits every add-in; read the value, and judge "did it start" from the load record of the same class.
- `Rvt.Attr.AddInCodeSigningStatus:` is `Signed` or `Unsigned`. An unsigned add-in raises the security dialog `TaskDialog_Security_Unsigned_File_Loading` (`Always Load`, `Load Once`, `Do Not Load`) the first time a new DLL loads. The journal often records neither which add-in the prompt was for nor the button chosen; the outcome is in the manifest record: `Unsigned` with `LoadCancelled` and no load record is an add-in that was not loaded at that prompt.
- Two applications can share a display name and differ in `Class:`. Match a load record, its manifest record and its buttons by class, not by name.

## 3. Where the name comes from

Only from the record's own text:

- a load record: the text after `Starting External Application:` up to `, Class:`, and the class after `Class:`;
- `Rvt.Attr.AddInName:` in a manifest, event or update record;
- `by application <X>`, where X ends before the add-in id in parentheses, and `from application '<X>'`: the owner the record names;
- an updater `'<A>: <B>'` is updater B of add-in A; `'Revit: ...'` updaters are Revit's own;
- the `Name(...)` of a registered service or server is the service's name, not its add-in's;
- a pushbutton's `name:` names a button, not an add-in.

A record without a name of its own takes the owner it names; otherwise it stays unnamed. Never derive a name from a DLL path, a folder or a file name: obfuscated, renamed and loader-based builds break that inference, and one folder can hold several add-ins.

## 4. Autodesk or third party

Decide the origin from the `Vendor` and `VendorId` fields of the same record, and from nothing else. Go down the table and take the first row that fits:

| # | Evidence in the record | Origin |
| --- | --- | --- |
| 1 | No vendor; an empty or placeholder one (`none`, `unknown`, `n/a`, `null`, `?`); a vendor text cut off (a parenthesis left open) | Unknown |
| 2 | The vendor fields of the record name Autodesk and another company or another company's website | Unknown |
| 3 | Vendor id `ADSK`, as in `Vendor : ADSK(<description>)` | Autodesk |
| 4 | A declared `VendorId(Autodesk)` or `VendorId(Autodesk <product>)` | Autodesk |
| 5 | A website in the vendor text whose host is `autodesk.com` or ends with `.autodesk.com`, whatever the vendor id | Autodesk |
| 6 | Any other vendor id, or any other website | Third party |

- A vendor id that is not `ADSK` is not a disagreement: it is often the product's own code. `Vendor : RPCA(Autodesk, www.autodesk.com)` is Autodesk by row 5; `Vendor : ACME(Acme Software, www.example.com)` is third party by row 6.

- The vendor field is what the add-in's own manifest declares, so the origin is a declared one: write "declares Autodesk", not "is Autodesk's". A third-party tool can declare `ADSK`. Keep the classification the table gives; when the class or the assembly's location makes you doubt the declaration, add that as a separate candidate with its line.
- Never carry a vendor over from the previous record; each record stands on its own.
- Never decide from the word "Autodesk" elsewhere in the line, from the add-in's name or from the DLL path: `Program Files\Autodesk\...` and `ProgramData\Autodesk\ApplicationPlugins` hold third-party add-ins too.
- Quote the vendor field with its line number as the evidence, and for "unknown" say which case applies.
- A failed load (`API_ERROR { Starting External Application`) still has a vendor field: read it, so that a failing third-party add-in is found by its origin.

## 5. Questions and where the answer is

| Question | Where to read |
| --- | --- |
| Did add-in X load in this session? | Its load record under `API_SUCCESS {`. Under `API_ERROR {` it failed: read the record. A manifest record whose `AddInLoadFailureMessage:` is not `NoError`, or `won't be loaded`: refused. Nothing at all: this session never read its registration; the usual causes are in the `revit-addin-release` skill |
| Which DLL did Revit use? | `Assembly:` in the load record and `assembly:` in its pushbuttons. That names the file, not who made it |
| Which third-party add-ins loaded? | Load records whose origin is third party by section 4: name, class, the vendor field and the line number of each |
| Did its code run? | `Jrn.RibbonEvent "Execute external command:` for a button click, run records with its `Rvt.Attr.AddInName:`, and `API_ERROR` records that follow them |
| Did it unload cleanly? | Its unload records near the shutdown markers; their absence in a session without a clean exit says nothing about the add-in |

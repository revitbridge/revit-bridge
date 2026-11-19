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
```

## 2. The other add-in records

Each kind is the stage of an add-in's life the record belongs to.

| Kind | Records, as the journal writes them |
| --- | --- |
| load | `[Jrn.AddInManifest]` with `Rvt.Attr.AddInName:`, `Rvt.Attr.AddInId:`, `Rvt.Attr.CommandClassName:`, `Rvt.Attr.CommandVendorId:`, `Rvt.Attr.CommandVendorDescription:`, `Rvt.Attr.AddInCodeSigningStatus:` and `Rvt.Attr.AddInLoadFailureMessage:` (newer releases; seen in 2026 and 2027 journals). `Registered an external service: Name(...); VendorId(...)`, `Registered an external server: Name(...)`, `[Jrn.RegisterExternalServer]`. `API registering command ... by application <X>`, `Replacing command id '...' ... from application '<X>'`, `Registering <Event> event by application <X> (<add-in id>)`. `Third Party Updater '<X>: <updater>' registered`. `The add-in folder '...'`, `The requested assembly '...' was loaded to the context` |
| button | `Added pushbutton Id: N, name: <button>, text: ..., class: ..., assembly: <path>, parentId: ...` |
| unload | `Unregistering <Event> event by application <X>`, `API unregistering command`, `Third Party Updater '...' has been unregistered`, `Forcibly unregistering Updater`, `Forcibly removing all update triggers`, `Unregistering all external services` |
| run | `[Jrn.ExternalEventExecution]` and `[Jrn.AddInDynamicUpdate]` with `Rvt.Attr.AddInName:` and `Rvt.Attr.VendorId:`, `[Jrn.ExternalServerExecution]`, `[Jrn.ExternalResourceLoad]`; a click on the add-in's button is `Jrn.RibbonEvent "Execute external command:` |
| not loaded | `ExternalDBApplication <Class> was not loaded`; `Add-in manifest file <path> won't be loaded` (Revit 2027 refusing an all-users manifest under `ProgramData`) |
| UI | `Jrn.AddInEvent` (an add-in's own window, `WpfWindow(...)`), `AddInJournalClient` |

- `Rvt.Attr.AddInLoadFailureMessage:` is in every manifest record. `NoError` means the add-in loaded; any other value (for example `WrongAssembly` or `LoadCancelled`) is why it did not. A search for the field name alone hits every add-in; read the value.
- `Rvt.Attr.AddInCodeSigningStatus:` is `Signed` or `Unsigned`. An unsigned add-in raises the security dialog `TaskDialog_Security_Unsigned_File_Loading` the first time a new DLL loads; read the answer that follows it.

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

Decide the origin from the `Vendor` and `VendorId` fields of the same record, and from nothing else:

| Evidence in the record | Origin |
| --- | --- |
| Vendor id `ADSK`, as in `Vendor : ADSK(<description>)` | Autodesk |
| A declared `VendorId(Autodesk)` or `VendorId(Autodesk <product>)` | Autodesk |
| A website in the vendor text whose host is `autodesk.com` or ends with `.autodesk.com` | Autodesk |
| Any other vendor id, or any other website | Third party |
| No vendor; an empty or placeholder one (`none`, `unknown`, `n/a`, `null`, `?`); a vendor text cut off (a parenthesis left open); vendor fields in one record that disagree | Unknown |

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

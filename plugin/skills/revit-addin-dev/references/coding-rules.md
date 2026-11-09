# Coding rules from reviewed add-in branches

Each rule below was written after a review of a real add-in branch found the defect it describes (six feature packages, four review rounds, 2026-10 to 2026-11, Revit 2016 to 2026). Follow them while writing and run the checklist at the end before handing a branch over. Transaction shapes and the five non-negotiables are in [addin-development.md](addin-development.md); release and acceptance in the `revit-addin-release` skill, `references/release-and-field-test.md`.

## 1. Transactions: every failure path inside a transaction rolls back clean

- **No product, no residue.** When a command ends with nothing created (every link unloaded, nothing matched, nothing selected), roll back the whole transaction. Do not leave the view-filter types, fill styles or families it created on the way.
- **Create, validate, then delete.** Replacing an element means create the replacement, check it, then delete the original. Delete-first leaves nothing when the create fails.
- **Resolve failures explicitly, never by Revit's default.** The default resolution of `CannotJoinElementsError` deletes elements. Choose the resolution you mean (`DetachElements` to keep walls), apply it only to failures on elements this run created and only when `HasResolutionOfType` and `IsFailureResolutionPermitted` hold; every other error rolls the transaction back.
- **No unhandled exception leaves a transaction.** It reaches Revit as "external command failure" with the transaction in an undefined state. Catch, `RollBack`, then report as in section 3.
- **One undo-name prefix** set once at startup (a `Transaction` name prefix in the application), not assembled in each command.

## 2. Decide by identity, not by text

- Failure definitions by `FailureDefinitionId`: two definitions share the text "cannot keep elements joined"; Revit 2025 raises the one the author did not handle.
- Categories by `BuiltInCategory`, never by a localized name such as "结构框架" or "Structural Framing": the other UI language selects nothing.
- Layers, families and types looked up by name state their scope. Two tree nodes or two import instances can share a name; filter by the instance the user clicked, not the first match.
- Enums, `StorageType` and `FailureSeverity` compared as enums, not through `ToString()`.

## 3. Errors and cancellation: one message, honest result, nothing swallowed

- Pick one dialog owner. Either show your own dialog once and return `Result.Cancelled` with `message` left empty, or return `Result.Failed` with `message` set and let Revit show it. Setting `message` after your own dialog produces two dialogs.
- Escape during a pick (`OperationCanceledException`) returns `Result.Cancelled` and shows nothing. Half of 39 reviewed buttons once raised "command failed" on Escape.
- No silent `catch {}`. A swallowed exception becomes a line "failed: reason" in the results window. A sleeve once came out unrotated because the rotation failure was swallowed.
- Main text in the user's language; native .NET and Revit text in an expandable details area, including the full `ToString()` of non-domain exceptions.
- Every skipped element gets a line with its id and the reason. Where the reasons come from a shared library in another language, keep a translation table whose keys equal the source strings character for character, placeholders included, and check it in CI.

## 4. "Nothing" is not zero and not success

- `HasValue` before `AsDouble()` or `AsInteger()`. An unset area factor read as 0 zeroed room areas and produced negative net areas without an error.
- `Parameter.Set(string)` on a non-text parameter returns `false` and does not throw; check it.
- Dictionaries with `TryGetValue`; `layers[entity.Layer]` throws on a missing layer.
- Missing geometry (no solid, no top face, empty projection) skips that one element with a reason. It does not fail the run and does not fall back to a default and continue; a beam once got raised against the lowest point of the slab because the real face was missing.
- No input, no success. A command with nothing to export reports that, instead of finishing silently.

## 5. External data: per-item tolerance, explicit units, no clobbering

- Each item of a DWG, spreadsheet or JSON array in its own `try/catch`: skip, count, one warning at the end, continue. Seventeen dangling references once made a 5,000-entity drawing unreadable.
- Locked layers opened read-only with `GetObject(id, OpenMode.ForRead, false, true)`.
- Units converted explicitly, before recognition and tolerance checks. Drawings declaring inches are often drawn in millimetres; default to millimetres and offer "use the declared unit". Numbers in dimension text are millimetres and are not multiplied by block scale again.
- Numbers parsed with `TryParse` and an `InvariantCulture` fallback; NaN, infinity and overflow rejected.
- Unreadable entity types counted and reported, not dropped.
- Output never overwrites input: refuse an output path equal to the input or already existing.

## 6. Geometry and recognition: edges, not diagonals

- A rectangle is four edges: adjacent edges orthogonal, opposite edges equal. Its direction is an edge direction, never the diagonal angle; rotated columns once all came out wrong.
- "Straight profile" is decided on the outer loop only. Inner loops (openings, sleeve cut-outs) do not count; one round hole once disqualified a whole wall face.
- Tolerances are named constants with their source (`ShortCurveTolerance` about 0.78 mm, `1e-9` ft), not scattered literals.
- Linked elements are transformed into host coordinates first; rotated and mirrored instances use the clicked instance's `Transform`.
- Containment uses the right measure: a round hole against a rectangular demand compares with the half diagonal, not a 2R by 2R box.

## 7. Names resolved at run time stay untouched

An obfuscated release keeps public types and members and does not rename properties, fields or events, so:

- XAML `{Binding}`, `x:Name` and `ElementName` work as written. Types referenced through `{x:Type local:...}` are `public`.
- Command classes may be `internal` when the ribbon reads `typeof(Command).FullName` at run time.
- No new string-based reflection on own types (`Type.GetType("My...")`, `Activator.CreateInstance("...")`), no `TypeNameHandling` in JSON. One such line breaks the obfuscated build.
- The `IExternalApplication` named in the `.addin` stays public.

## 8. Version branches and dependencies

- Every `#if R20xx_OR_GREATER` branch carries a comment naming the API it selects and the release that introduced it. Keep existing comments in their language.
- `new ElementId(int)` and `ElementId.IntegerValue` are gone after 2024; route them through one helper pair. `Document.NewFloor` is gone from 2023.
- Revit 2025 and later cannot load .NET Framework mixed-mode libraries; such readers run in an out-of-process helper for every year, and the 2025+ configurations carry no reference to them.
- No new NuGet dependency without a decision. When removing one, grep the whole repository for leftovers and diff the output directory to see which DLLs disappeared.
- A reduced first release is a compile symbol defined by an MSBuild property (for example `-p:RibbonProfile=FirstRelease` defining `FIRST_RELEASE`, the ribbon builder picking the layout under `#if`; use the property and symbol names your project defines), not deleted commands. The default build keeps every command and there is one code base.

## 9. Sign-in and network clients

- Only 401 and 403 revoke the cached token. 5xx, 429, timeouts and malformed replies are treated as offline, and the token stays valid for the offline grace period.
- A cached token past `exp` but inside the grace period is usable now and refreshed in the background; it does not demand the password again.
- The server's `message` is shown verbatim; "cannot connect" is reserved for network errors and timeouts.
- Tokens and passwords never appear in logs, result windows or exception text; the password is cleared as soon as it has been used.
- The add-in's log opens every session with one line naming its version, the loaded DLL path and the Revit release, and account names appear as digests; a user's log then answers "which version, from where" without a screenshot.
- No network call on the UI thread; after every `await`, check the `CancellationToken` again.
- A local queue written by several Revit processes is serialized with a named `Mutex`.

## 10. Out-of-process helpers

- Exit codes are a contract: 0 success, 1 read/dependency/output error, 2 bad arguments. Errors on stderr, progress as JSON lines on stderr, the result in the `--out` file.
- The caller kills the helper and removes temporary files on timeout and on user cancel, and checks cancellation once more after reading the result.
- The helper resolves dependencies from its own directory only; no PATH changes, no registry.
- Before handing over, run the helper **as installed in the package**, with the caller's real arguments, on the known samples and confirm the counts are unchanged.
- A health probe of several local ports connects in parallel with a short connect timeout. On Windows a connect to a local port nobody listens on takes about two seconds to fail; three sequential probes exceeded a caller's three-second limit and marked a healthy machine as down.

## 11. Commits that pass review

- One behaviour change per commit. Pure refactoring (moves, renames, extractions) goes in separate commits before the behaviour change.
- The commit body says what behaviour changed and where it was verified (which manual-check row, which offline check, which evidence file). "Builds" is not verification.
- Fix a defect your own branch introduced with a `fixup` into that commit; fix a pre-existing defect in its own commit.
- After the branch is under review: no reword, no rebase. Append commits; record corrections to earlier messages in an errata section.
- New files are UTF-8 without BOM. Do not add a BOM to a file that had none; remove an existing BOM only in the first commit that touches the file and never bring it back.
- No incidental re-indentation or formatting of unrelated code; it buries the diff and is flagged as churn.
- Stay inside the files the task names; ask before touching others.

## 12. Hand-over checklist

- [ ] Every supported Revit year rebuilds with 0 errors; warnings compared per file and code against the base branch show nothing new (same verbosity on both sides)
- [ ] Every code commit on the branch builds on its own
- [ ] Translation table complete: every new source string has an entry with an identical key
- [ ] No silent catch; Escape returns `Cancelled`; one dialog per error, `message` left empty when you showed it
- [ ] Every failure path inside a transaction rolls back; no product leaves no types behind
- [ ] `HasValue`, `TryGetValue` and `Set` return values checked
- [ ] External data read per item; units explicit
- [ ] No new string reflection; XAML-referenced types public
- [ ] New files without BOM; no unrelated formatting; changes within scope
- [ ] Behaviour table's "before" verified against the base branch; four-cell table has no empty cells; every "not tested" also listed under open items
- [ ] Evidence contains no drawings or binaries; screenshots cropped
- [ ] Loaded and run in a real Revit on one `net48` year and one .NET year from a private copy; input file hashes unchanged

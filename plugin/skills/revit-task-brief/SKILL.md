---
name: revit-task-brief
description: Rewrite a designer's short request for a Revit add-in or model change into a precise, testable brief before any code is written - numbered behaviour, inputs with units and defaults, cancel and failure rules, facts that must come from the open project, deliverables, verification and the decisions taken. 把设计师的一句话需求改写成可验收的任务说明。Use at the start of every Revit development task; the brief is what the coding skills then implement.
---

# Revit Task Brief

A designer writes one or two sentences: "I pick two crossing pipes, a dialog asks for the jog height, and the first pipe gets jogged over the other one." That sentence is the intent; it is not a specification. Before any code, rewrite it into a brief that a developer, a tester and the designer would all read the same way. Then implement the brief, not the sentence.

The brief is written in English with Revit API names, because the coding skills and the API reference are English. The summary shown to the designer is written in the designer's language.

## What the brief must contain

Write every section, in this order. A section with nothing to say states that in one line rather than disappearing.

1. **Name and kind.** The add-in or command name (the one the designer gave, otherwise a short PascalCase name), and the kind of deliverable: a standalone `IExternalCommand` add-in, a ribbon command, or a bridge snippet. One sentence of purpose.
2. **Behaviour as numbered steps.** What the user selects (element class or category, host requirements, how many), what the command computes, what the dialog asks, what changes in the model, what is shown at the end. One action per step, in the order the user experiences it. Every quantity carries a unit (mm, degrees, m2) and every input a default and a valid range.
3. **Rejections and edge cases.** Which selections are refused and with what message (curtain wall, in-place family, parallel pipes, position outside the element). What Escape and Cancel do: `Result.Cancelled` and no change. What a failure inside the transaction does: roll back, `Result.Failed`, the reason in the message. Overlaps, empty selection, missing families, no room on one side.
4. **Facts taken from the open project.** Everything that must be derived from the project rather than typed into the code: family and type names, materials, levels, phases, existing Mark sequences, rooms, units. Name each one and say how it is chosen ("the window family type with the most instances; ties by name"). When the bridge is reachable, these are read with `query` and `get_project_snapshot`, not guessed.
5. **Transaction scope.** One transaction (or one transaction group) for the whole command, the transaction mode, and what must be true after rollback.
6. **Deliverables and environment.** Files and their names (sources, `<Name>.csproj`, `<Name>.addin`), assembly name, root namespace, a new `AddInId`, the target Revit release and the framework it fixes (table below), where the API assemblies come from, `Private=false`, the build command and the required result (zero errors). Whether to install (default: do not install; the evaluator does).
7. **Verification.** What the implementer verifies alone (the build, a bridge prototype of the core logic on the open model when the tools reach it, a `check_code` compile of snippets), what only a person in Revit can verify (picks, dialogs, the visual result), and what the final report must contain: build command and result, verified items with how, unverified items.
8. **Decisions taken.** Every choice the designer did not make, with the value the brief picks and one reason. Unattended runs proceed on these; attended runs show them to the designer first.

Revit release to framework: 2024 `net48`; 2025 and 2026 `net8.0-windows`; 2025.5, 2026.5 and 2027 `net10.0-windows`. Read the installed `RevitAPI.dll` file version and `RevitAPI.runtimeconfig.json` when the machine is reachable instead of trusting the year alone; the add-in skill has the details.

## Rules for the rewrite

- Keep the designer's intent and scope. Add precision, not features: no extra options, no "also export a schedule". When the sentence is ambiguous, pick the reading a careful colleague would, record it under Decisions, and go on.
- Every step must be testable by someone who did not write the code: a selection, a number, an element that exists afterwards, a message.
- Numbers get units and defaults; names that depend on the project get a rule, never a literal value from memory.
- Prefer the behaviour Revit users expect: pick with `PickObject` and a selection filter, confirm numeric input in one dialog, finish with a summary `TaskDialog`, leave the model untouched on cancel.
- Say what the command must not touch (the other pipe, other walls of the same type, existing instances).
- Keep the brief under about 600 words. Longer briefs get skimmed; shorter ones leave the decisions to chance.
- Do not invent the environment. State the facts that are known (an open Revit with a test model, the release) and let the implementer discover the rest with its tools; when a fact is required and unknown, write it as a question under Decisions with the default that will be used if nobody answers.

## Rules every brief carries

These come from hand-tested batches of generated add-ins (36 builds, 2026-10): the same five faults caused most functional failures, with or without the bridge. Write them into the brief's steps, rejections and verification; do not leave them to the implementer.

- **Transactions are closed on every path.** One owner (the command) opens one `Transaction` or `TransactionGroup`; every exit commits after all edits succeeded or rolls back, including exceptions (`using` plus an explicit `Commit`/`RollBack`). No model change happens before `Start` or after `Commit`; a pending transaction is never left behind. Set `FailureHandlingOptions` with a preprocessor that resolves warnings and turns errors into a rollback with a message.
- **Inserts never overlap.** Before the first door or window is placed, every planned opening is compared with every insert already on the host (`FindInserts`) and with the other planned openings; an overlap is refused or moved with a message, never left to Revit's error dialog.
- **Rebuilt elements keep their properties.** A pipe, duct or wall segment created to replace another copies diameter, system, level, type and connections from the original; for MEP prefer splitting in place (`PlumbingUtils.BreakCurve`) over delete-and-recreate.
- **The UI tells the truth.** One dialog validates its inputs and says why a value is refused; OK either acts or explains; Cancel and Escape return `Result.Cancelled` with no change; no failure is swallowed by a silent `catch` - every failure reaches the user as a message with the reason; the command ends with a summary.
- **The core logic is run in the open model before it is delivered.** When the bridge reaches a Revit, the brief's verification section names the prototype to run there. When the bridge's confirmation protocol needs a human and nobody is present, the report says "not verified in Revit: no confirmation available" instead of skipping the step silently.

## How to use it

1. Read the request. List the nouns (elements, types, rooms, numbers) and the verbs (pick, create, modify, flip, report).
2. Fill the eight sections. For section 4 run the bridge reads if a Revit is reachable and note what was found; otherwise write the rule and mark it "to read from the project at run time".
3. Attended: show the designer the summary in their language (steps, inputs with defaults, decisions) and ask for one round of answers. Unattended: proceed and keep the decisions in the report.
4. Hand the brief to the implementation skill (`revit-addin-dev` for an add-in, `revit-bridge` for a model change through the bridge). The brief is the acceptance list: the final report walks through its steps and says for each whether it was verified, and how.

[Template](references/brief-template.md) gives the skeleton; [worked example](references/example-pipe-jog.md) shows a one-sentence pipe request turned into a brief.

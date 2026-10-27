# Worked example: a pipe jog

## The designer's request

> Make me a Revit 2026 add-in: I pick two crossing pipes, then one or two spots on the first pipe, a dialog asks for the jog height and the angles on both sides, and the first pipe gets jogged over (or under) the other one with the elbows connected properly. Name it PipeJog.

## The brief

```
# PipeJog - reroute one pipe over or under a crossing pipe with angled bends

Kind: standalone IExternalCommand add-in. Transaction mode: Manual.

## Behaviour
1. The user picks two pipes (Autodesk.Revit.DB.Plumbing.Pipe, selection filter on the class) that cross in plan at the same elevation: the first pipe (the one to reroute) and the crossing pipe. Parallel or non-crossing centerlines: message, Result.Failed.
2. The command computes the intersection of the two centerlines projected to plan.
3. The user picks one or two points on the first pipe (PickObject with ObjectType.PointOnElement, one pick per point, no multi-pick Finish step). Two points are the start and end of the jog; one point is the start and the end is mirrored about the intersection.
4. A dialog asks for: jog height (mm, default 300, non-zero; positive up, negative down); bend angle, first side (degrees, default 45, 0 < a <= 90); bend angle, second side (same).
5. In one transaction the command splits the first pipe at the jog start and end, replaces the middle by a rising segment, a straight offset segment at the new elevation and a descending segment inclined at the given angles, and connects the four joints with elbow fittings from the pipe type's routing preferences (Document.Create.NewElbowFitting). The crossing pipe is not modified; the first pipe's far ends keep their connections.
6. At the end it shows a TaskDialog with the new segment count, the offset and the angles, and returns Result.Succeeded.

## Rejections and edge cases
- Escape at any pick or Cancel in the dialog: Result.Cancelled, no change.
- Failure inside the transaction (no elbow for the angle, split failed): roll back, Result.Failed, the reason in the message.
- Jog points not on the first pipe, in the wrong order, or so close to the ends or to each other that a segment would be shorter than the elbow: message before any change.

## Facts taken from the open project
- Pipe type, system type and level: taken from the picked pipe, never from names.
- Available elbow angles: from the pipe type's routing preferences; read through the bridge (query family_types, OST_PipeFitting) when reachable.

## Transaction scope
One transaction "PipeJog reroute"; after rollback both pipes are as picked.

## Deliverables and environment
- Files in the current directory: Command.cs, JogDialog.cs, PipeJog.csproj, PipeJog.addin; assembly, namespace and add-in name PipeJog; new AddInId.
- Target: Revit 2026.5 -> net10.0-windows (RevitAPI.dll 26.5.x); RevitAPI.dll and RevitAPIUI.dll from the installed Revit 2026 folder, Private=false.
- Build: dotnet build PipeJog.csproj -c Release with a .NET 10 SDK; zero errors. Do not install; the evaluator installs and tests on the open test model.
- Known environment: a Revit 2026 with a test model (two crossing pipes) is open and reachable through the bridge tools.

## Verification
- Implementer: build; prototype the split, segment creation and NewElbowFitting through execute_code on the open model, read the result back (segment count, Z of the middle segment, fitting count, unconnected connectors), then port the logic into the command; check_code for every snippet first.
- Only in Revit by a person: the picks, the dialog, the visual result, cancel at each step.
- Report: build command and result; steps 1-6 each marked verified / not verified and how.

## Decisions taken
- One pick per point instead of a multi-pick with Finish: the designer said "one or two spots"; a Finish button is easy to miss (seen in testing).
- Default height 300 mm and angles 45/45: common offset practice; all three remain inputs.
- Positive height means up: stated in the dialog label.
```

What changed between the sentence and the brief: the selection got a class and a failure rule, the picks got a mechanism that does not need a Finish button, every number got a unit, a default and a range, cancel and failure got defined results, the data that must come from the model got a rule, and the verification split into what the implementer can prove and what a person must try.

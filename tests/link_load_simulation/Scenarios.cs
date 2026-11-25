using System;
using System.Linq;
using System.Text.Json;
using Api = Autodesk.Revit.Exceptions;

static class Scenarios
{
    static void Check(bool ok, string message)
    {
        if (!ok) throw new Exception(message);
    }
    static Document Project()
    {
        var doc = new Document();
        var first = doc.Add(new Level { Name = "L1" });
        var second = doc.Add(new Level { Name = "L2" });
        doc.ActiveView = doc.Add(new ViewPlan { GenLevel = first });
        doc.Add(new ViewPlan { GenLevel = second, IsTemplate = true });
        doc.Add(new ViewPlan { GenLevel = second, CanBePrinted = false });
        doc.Add(new ViewPlan { GenLevel = second });
        return doc;
    }
    static void Refused(Func<Document, object> run, Document doc, string reason, string next, int fileCalls = 0)
    {
        int before = doc.FileCalls;
        try { run(doc); }
        catch (InvalidOperationException ex)
        {
            Check(ex.Message.StartsWith("REFUSED: ") && ex.Message.Contains(reason) && ex.Message.Contains(next),
                  "Unexpected refusal: " + ex.Message);
            Check(doc.FileCalls == before + fileCalls, "Unexpected native load during refusal: " + reason);
            return;
        }
        throw new Exception("Expected refusal: " + reason);
    }

    static void Linked(object result, Document doc)
    {
        var json = JsonSerializer.SerializeToElement(result);
        Check(json.GetProperty("Status").GetString() == "Linked" &&
              json.GetProperty("ElementId").GetInt64() == doc.Elements.Last().Id.Value &&
              json.GetProperty("TypeId").GetInt64() == doc.Elements[^2].Id.Value, "linked instance and type IDs");
    }
    static void Loaded(object result, Family expected)
    {
        var json = JsonSerializer.SerializeToElement(result);
        Check(json.GetProperty("Status").GetString() == "Loaded" &&
              json.GetProperty("Family").GetString() == expected.Name &&
              json.GetProperty("ElementId").GetInt64() == expected.Id.Value, "loaded family name and ID");
    }

    static void Models()
    {
        var doc = Project();
        doc.Add(new RevitLinkType { Reference = new(@"X:\Other\Campus.rvt") });
        Linked(Rendered.Origin(doc), doc);
        Check(doc.LastPath == @"X:\Synthetic\Campus.rvt" && doc.InstanceCalls == 1 &&
              doc.ModelPlacement == ImportPlacement.Origin, "origin fixture and same filename in another folder");
        Refused(Rendered.Origin, doc, "already linked", "Manage Links");
        Linked(Rendered.Shared(doc), doc);
        Check(doc.LastPath == @"\\example.invalid\synthetic\Survey.RVT" &&
              doc.ModelPlacement == ImportPlacement.Shared, "shared fixture");
        doc = Project();
        // A type alone, including an unloaded link, is sufficient for duplicate detection.
        doc.Add(new RevitLinkType { Reference = new("x:/SYNTHETIC/archive/.././campus.RVT") });
        Refused(Rendered.Origin, doc, "already linked", "Manage Links");
        Refused(Rendered.Centered, doc, "positioning", "origin or shared");
        doc = Project();
        doc.IsFamilyDocument = true;
        Refused(Rendered.Origin, doc, "require a project", "Open");
        doc = Project();
        doc.Status = LinkLoadResultType.LinkNotFound;
        Refused(Rendered.Shared, doc, "LinkNotFound", "Check", 1);
        Check(doc.InstanceCalls == 0, "failed link type cannot be placed");
        doc = Project();
        doc.Status = LinkLoadResultType.LinkExists;
        Refused(Rendered.Origin, doc, "already linked", "Manage Links", 1);
        Check(doc.InstanceCalls == 0, "existing link type cannot be placed");
        doc = Project();
        doc.PlacementError = new Api.InvalidOperationException();
        Refused(Rendered.Shared, doc, "place the loaded link", "shared coordinates", 1);
        doc = Project();
        doc.Add(new RevitLinkType { Reference = new(null) });
        Refused(Rendered.Origin, doc, "no resolved path", "Repair");
    }

    static void Drawings()
    {
        var doc = Project();
        var active = doc.ActiveView;
        doc.Add(new CADLinkType());   // embedded import, no external reference
        Linked(Rendered.ViewDrawing(doc), doc);
        Check(doc.Target == active && doc.DrawingOptions.ThisViewOnly &&
              doc.DrawingOptions.Unit == ImportUnit.Millimeter && doc.DrawingOptions.Placement == ImportPlacement.Origin,
              "DWG in active view");
        Refused(Rendered.ViewDrawing, doc, "already linked", "Manage Links");
        Linked(Rendered.LevelDrawing(doc), doc);
        Check(doc.Target.GenLevel.Name == "L2" && !doc.Target.IsTemplate && doc.Target.CanBePrinted &&
              !doc.DrawingOptions.ThisViewOnly && doc.DrawingOptions.Unit == ImportUnit.Meter &&
              doc.DrawingOptions.Placement == ImportPlacement.Shared && doc.ActiveView == active, "DXF on named level");
        doc = Project();
        doc.Add(new CADLinkType { Reference = new("//example.invalid/SYNTHETIC/temporary/../Survey.dxf") });
        Refused(Rendered.LevelDrawing, doc, "already linked", "Manage Links");
        Refused(Rendered.BadUnits, doc, "units", "Choose");
        Refused(Rendered.BadPlacement, doc, "positioning", "origin or shared");
        Refused(Rendered.ConflictingLevel, doc, "active view", "Open");
        doc.ActiveView = doc.Add(new View3D());
        Refused(Rendered.ViewDrawing, doc, "target view", "2D");
        doc.Elements.RemoveAll(e => e is ViewPlan plan && plan.GenLevel?.Name == "L2");
        Refused(Rendered.LevelDrawing, doc, "No printable plan", "create its plan");
        doc = Project();
        doc.OverrideBaseLevel = doc.ActiveView.GenLevel.Id;
        Refused(Rendered.LevelDrawing, doc, "requested level", "Check its base level", 1);
        doc.WrongScope = true;
        Refused(Rendered.ViewDrawing, doc, "scope", "Check", 1);
        doc = Project();
        doc.Status = LinkLoadResultType.UsedExisting;
        Refused(Rendered.LevelDrawing, doc, "already linked", "Manage Links", 1);
        doc = Project();
        doc.LoadError = new Api.OptionalFunctionalityNotAvailableException();
        Refused(Rendered.ViewDrawing, doc, "module", "Install", 1);
        doc.LoadError = new Api.InvalidOperationException();
        Refused(Rendered.LevelDrawing, doc, "link the drawing", "shared coordinate", 1);
        doc = Project();
        doc.Status = LinkLoadResultType.LinkNotOpenable;
        Refused(Rendered.LevelDrawing, doc, "LinkNotOpenable", "file version", 1);
    }

    static void Families()
    {
        var doc = Project();
        doc.FamilyResult = new Family { Name = "Sample Desk" };
        Loaded(Rendered.Load(doc), doc.FamilyResult);
        Check(doc.LastPath == @"X:\Synthetic\Sample Desk.rfa", "RFA drive path");
        Refused(Rendered.Load, doc, "already loaded", "Resolve the existing family in Revit");
        doc.FamilyResult = new Family { Name = "Sample Pump" };
        Loaded(Rendered.OtherFamily(doc), doc.FamilyResult);
        Check(doc.LastPath == @"\\example.invalid\synthetic\Sample Pump.RFA", "RFA UNC path");
        doc = Project();
        var existing = doc.Add(new Family { Name = "SAMPLE DESK" });
        Refused(Rendered.Load, doc, "already loaded", "in Revit");
        // A renamed source whose native load reports an old ID or name must abort the transaction.
        doc.FamilyResult = existing;
        Refused(Rendered.OtherFamily, doc, "already loaded", "in Revit", 1);
        doc.FamilyResult = new Family { Name = "Sample Desk" };
        Refused(Rendered.OtherFamily, doc, "already loaded", "in Revit", 1);
        doc = Project();
        doc.LoadSucceeded = false;
        Refused(Rendered.Load, doc, "did not load", "opens in this version", 1);
        doc.LoadSucceeded = true;
        Refused(Rendered.Load, doc, "did not load", "opens in this version", 1);
    }

    static void Main()
    {
        Models();
        Drawings();
        Families();
        foreach (var method in typeof(Rendered).GetMethods().Where(m => m.Name.StartsWith("Invalid_")))
            Refused(method.CreateDelegate<Func<Document, object>>(), Project(), "absolute", "drive or UNC");
        foreach (var run in new Func<Document, object>[] { Rendered.Origin, Rendered.ViewDrawing, Rendered.Load })
        {
            foreach (var (error, reason, next) in new (Exception, string, string)[] {
                (new Api.FileArgumentNotFoundException(), "find the file", "Check the path"),
                (new Api.FileNotFoundException(), "find the file", "Check the path"),
                (new Api.FileAccessException(), "access", "permissions"),
                (new Api.CorruptModelException(), "damaged", "backup"),
                (new Api.OperationCanceledException(), "cancelled", "Retry"),
                (new Api.ModificationForbiddenException(), "model change", "Resolve"),
                (new Api.ModificationOutsideTransactionException(), "model change", "Resolve"),
                (new Api.RegenerationFailedException(), "model change", "Resolve"),
                (new Api.ArgumentException(), "ArgumentException", "Check")
            })
            {
                var doc = Project();
                doc.LoadError = error;
                Refused(run, doc, reason, next, 1);
            }
        }
        Console.WriteLine("Link/load scenarios passed");
    }
}

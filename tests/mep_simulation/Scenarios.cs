using System;
using System.Linq;
using System.Text.Json;
using Autodesk.Revit.DB.Mechanical;
using Autodesk.Revit.DB.Plumbing;

static class Scenarios
{
    static JsonElement Result(object result) => JsonSerializer.SerializeToElement(result);
    static void Check(bool condition, string message)
    {
        if (!condition) throw new Exception(message);
    }
    static void Refuses(Func<Document, object> run, Document doc, params string[] reasons)
    {
        try { run(doc); }
        catch (InvalidOperationException ex) when (ex.Message.StartsWith("REFUSED: ") && reasons.All(ex.Message.Contains)) { return; }
        throw new Exception("Expected refusal containing " + string.Join(", ", reasons));
    }
    static Document Model()
    {
        var doc = new Document();
        doc.Add(new Level { Name = "L1" });
        doc.Add(new PipeType { Name = "Sample Pipe" });
        doc.Add(new PipingSystemType { Name = "Sample Supply" });
        doc.Add(new DuctType { Name = "Sample Round", Shape = ConnectorProfileType.Round });
        doc.Add(new DuctType { Name = "Sample Rectangular", Shape = ConnectorProfileType.Rectangular });
        doc.Add(new MechanicalSystemType { Name = "Sample Air" });
        return doc;
    }
    static void Segments()
    {
        var doc = Model();
        Rendered.Pipe(doc);
        var pipe = doc.Elements.OfType<Pipe>().Single();
        Check(Math.Abs(pipe.get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM).Value * 304.8 - 25) < 1e-8, "diameter");
        var line = ((LocationCurve)pipe.Location).Curve;
        Check(Math.Abs(line.End.X * 304.8 - 1500) < 1e-8 && Math.Abs(line.Start.Z * 304.8 - 250) < 1e-8, "pipe coordinates");
        Rendered.Round(doc);
        Rendered.Rectangular(doc);
        var ducts = doc.Elements.OfType<Duct>().ToList();
        Check(Math.Abs(ducts[0].get_Parameter(BuiltInParameter.RBS_CURVE_DIAMETER_PARAM).Value * 304.8 - 250) < 1e-8, "round");
        Check(Math.Abs(ducts[1].get_Parameter(BuiltInParameter.RBS_CURVE_WIDTH_PARAM).Value * 304.8 - 400) < 1e-8
            && Math.Abs(ducts[1].get_Parameter(BuiltInParameter.RBS_CURVE_HEIGHT_PARAM).Value * 304.8 - 200) < 1e-8, "rectangular");
        Refuses(Rendered.MixedDimensions, doc, "round");
        Refuses(Rendered.UnknownDuct, doc, "Sample Rectangular");
        Refuses(Rendered.UnknownDuctSystem, doc, "Sample Air");
        Refuses(Rendered.UnknownSystem, doc, "Sample Supply");
        for (int i = 0; i < 15; i++) doc.Add(new PipeType { Name = "Type" + i });
        try { Rendered.UnknownType(doc); throw new Exception("missing refusal"); }
        catch (InvalidOperationException ex)
        {
            Check(ex.Message.StartsWith("REFUSED: ") && ex.Message.Contains("Sample Pipe")
                && ex.Message.Contains("Type10") && !ex.Message.Contains("Type11"), "bounded available names");
        }
        Refuses(Rendered.ShortPipe, doc, "too close");
        Refuses(Rendered.BadSize, doc, "positive");
        var empty = new Document();
        foreach (var run in new Func<Document, object>[] { Rendered.Pipe, Rendered.Round, Rendered.Route, Rendered.Join, Rendered.Inventory })
            Refuses(run, empty, "no MEP types loaded");
    }
    static void Issues(JsonElement result, string key, params int[] indices)
    {
        var items = result.GetProperty(key).EnumerateArray().ToList();
        Check(items.Select(i => i.GetProperty("Index").GetInt32()).SequenceEqual(indices), key);
        Check(items.All(i => !string.IsNullOrWhiteSpace(i.GetProperty("Reason").GetString())), "issue reasons");
    }
    static void Routes()
    {
        var doc = Model();
        var ok = Result(Rendered.Route(doc));
        Check(ok.GetProperty("Status").GetString() == "Created" && ok.GetProperty("ElbowIds").GetArrayLength() == 1, "elbow");
        Issues(ok, "CornerIssues");
        doc.RejectElbow = true;
        var rejected = Result(Rendered.Route(doc));
        Check(rejected.GetProperty("Status").GetString() == "Partial" && rejected.GetProperty("ElementIds").GetArrayLength() == 2, "partial route");
        Issues(rejected, "CornerIssues", 1);
        Check(doc.Elements.OfType<FamilyInstance>().Count() == 1, "failed attempt left a fitting behind");
        Check(doc.Elements.OfType<Pipe>().Skip(2).SelectMany(p => p.ConnectorManager.Connectors)
            .All(c => !c.IsConnected), "failed attempt left connected ends");
        var detached = Model();
        detached.DetachedElbow = true;
        Issues(Result(Rendered.Route(detached)), "CornerIssues", 1);
        Check(!detached.Elements.OfType<FamilyInstance>().Any(), "detached elbow left behind");
        var rolledBack = Model();
        rolledBack.RejectElbowCommit = true;
        Issues(Result(Rendered.Route(rolledBack)), "CornerIssues", 1);
        Check(rolledBack.Elements.OfType<Pipe>().Count() == 2 && !rolledBack.Elements.OfType<FamilyInstance>().Any(),
            "Revit rolled back only the fitting");
        var fatal = Model();
        fatal.FatalElbow = true;
        try { Rendered.Route(fatal); throw new Exception("fatal failure was treated as a missing elbow"); }
        catch (Autodesk.Revit.Exceptions.RegenerationFailedException ex) { Check(ex.Message == "abort", "fatal error lost"); }
        doc = Model();
        doc.RejectSize = true;
        Refuses(Rendered.Route, doc, "No pipe could be created");
        Check(!doc.Elements.OfType<Pipe>().Any(), "unsized pipes left behind");
    }
    static void Preflight()
    {
        var doc = Model();
        Refuses(Rendered.Gap, doc, "Segment 1", "too short");
        Refuses(Rendered.BadVertex, doc, "Vertex 2", "three coordinates");
        Refuses(Rendered.NoSegments, doc, "Segment 0", "Segment 1", "too short");
        Refuses(Rendered.MixedErrors, doc, "Vertex 0", "Segment 1", "Vertex 3", "finite");
        Check(doc.PipesCreated == 0 && doc.ElbowAttempts == 0, "invalid input reached Revit creation");
    }
    static void StraightRun()
    {
        var doc = Model();
        var result = Result(Rendered.Straight(doc));
        Check(doc.PipesCreated == 2 && doc.ElbowAttempts == 1, "merge straight run before placing the corner");
        var line = ((LocationCurve)doc.Elements.OfType<Pipe>().First().Location).Curve;
        Check(line.Start == new XYZ(0,0,0) && line.End == new XYZ(1500/304.8,1500/304.8,1500/304.8), "merged endpoints");
        Check(result.GetProperty("MergedVertexIndices").EnumerateArray().Select(v => v.GetInt32())
            .SequenceEqual(new[] {1, 2}), "merged original vertex indexes");
        Issues(result, "CornerIssues");
        doc = Model();
        doc.RejectElbow = true;
        Issues(Result(Rendered.Straight(doc)), "CornerIssues", 3);
    }
    static void Diameters()
    {
        foreach (var mode in new[] { Snap.InsideSet, Snap.AtRegeneration })
        {
            var doc = Model();
            doc.FirstPipeSnap = mode;
            Refuses(Rendered.Pipe, doc, "32 mm");
        }
        foreach (var mode in new[] { Snap.InsideSet, Snap.AtRegeneration, Snap.AfterElbowCommit })
        {
            var doc = Model();
            doc.FirstPipeSnap = mode;
            doc.OffsetReadback = true;
            var result = Result(Rendered.Route(doc));
            Check(result.GetProperty("Status").GetString() == "Partial", "diameter mismatch reported as created: " + mode);
            Issues(result, "SegmentIssues", 0);
            Issues(result, "CornerIssues", 1);
            var issue = result.GetProperty("SegmentIssues")[0];
            Check(Math.Abs(issue.GetProperty("ActualDiameterMm").GetDouble() - 32) < 1e-8, "rejected diameter");
            var pipe = doc.Elements.OfType<Pipe>().Single();
            double actual = pipe.get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM).AsDouble() * 304.8;
            var segment = result.GetProperty("Segments")[0];
            Check(result.GetProperty("ElementIds").GetArrayLength() == 1 &&
                segment.GetProperty("ElementId").GetInt64() == pipe.Id.Value &&
                segment.GetProperty("DiameterMm").GetDouble() == actual && actual != 25, "regenerated segment diameter");
            Check(!doc.Elements.OfType<FamilyInstance>().Any() && result.GetProperty("ElbowIds").GetArrayLength() == 0,
                "orphan fitting after size rejection");
        }
        var single = Model();
        single.OffsetReadback = true;
        var created = Result(Rendered.Pipe(single));
        double stored = single.Elements.OfType<Pipe>().Single().get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM).AsDouble();
        Check(created.GetProperty("DiameterMm").GetDouble() == stored * 304.8 && stored * 304.8 != 25, "single pipe readback");
    }
    static void RejectedDependents()
    {
        var doc = Model();
        doc.FirstPipeSnap = Snap.InsideSet;
        doc.WithDependents = true;
        var result = Result(Rendered.Route(doc));
        Issues(result, "SegmentIssues", 0);
        Check(doc.Elements.OfType<Pipe>().Count() == 1 && !doc.Elements.OfType<MEPSystem>().Any()
            && !doc.Elements.OfType<ConnectorElement>().Any(), "remove only the rejected pipe and its dependents");
        doc = Model();
        doc.FirstPipeSnap = Snap.InsideSet;
        doc.WithUnrelatedDeletion = true;
        doc.Add(new PipingSystem());
        Refuses(Rendered.Route, doc, "unrelated", "routing");
    }
    static Document Pair(XYZ b0, XYZ b1)
    {
        var doc = new Document();
        var system = new ElementId(103);
        var level = new ElementId(105);
        doc.Curve<Pipe>(system, level, new(0,0,0), new(2,0,0));
        doc.Curve<Pipe>(system, level, b0, b1);
        doc.Add(new PipingSystemType());
        doc.Add(new PipeType());
        doc.Add(new Level());
        return doc;
    }
    static void Connections()
    {
        var union = Pair(new(5,0,0), new(6,0,0));
        Rendered.Join(union);
        var a = ((LocationCurve)union.Elements[0].Location).Curve;
        Check(a.Start == new XYZ(0,0,0) && a.End == new XYZ(5,0,0) && union.FittingKind == "union", "collinear extension");
        var elbow = Pair(new(5,2,0), new(5,4,0));
        Rendered.Join(elbow);
        var b = ((LocationCurve)elbow.Elements[1].Location).Curve;
        Check(b.Start == new XYZ(5,0,0) && b.End == new XYZ(5,4,0) && elbow.FittingKind == "elbow", "extension to elbow");
        var transition = Pair(new(2,0,0), new(3,0,0));
        transition.Elements[1].get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM).Value = 0.2;
        Rendered.Join(transition);
        Check(transition.FittingKind == "transition", "coincident reduction");
        Refuses(Rendered.Join, Pair(new(5,2,1), new(5,4,1)), "No free ends");  // skew in Z
        Refuses(Rendered.Join, Pair(new(1,-1,0), new(1,1,0)), "No free ends"); // would require cutting
        Refuses(Rendered.Join, union, "No free ends");                       // already joined
        var different = Pair(new(2,0,0), new(3,0,0));
        different.Elements[1].get_Parameter(BuiltInParameter.RBS_PIPING_SYSTEM_TYPE_PARAM).IdValue = new(201);
        Refuses(Rendered.Join, different, "system type");
    }
    static void Placement()
    {
        var doc = new Document();
        doc.Add(new Level { Name = "L2", Elevation = 10 });
        var family = new Family { Name = "Sample Pump" };
        var symbol = doc.Add(new FamilySymbol { Name = "Small", Family = family });
        foreach (var kind in new[] { FamilyPlacementType.WorkPlaneBased, FamilyPlacementType.Adaptive, FamilyPlacementType.CurveBased })
        {
            family.FamilyPlacementType = kind;
            Refuses(Rendered.Place, doc, kind.ToString());
        }
        family.FamilyPlacementType = FamilyPlacementType.OneLevelBasedHosted;
        symbol.Category = Category.GetCategory(doc, BuiltInCategory.OST_Doors);
        Refuses(Rendered.Place, doc, "create_door_on_wall");
        Check(!symbol.IsActive && !doc.Elements.OfType<FamilyInstance>().Any(), "invalid families activated or placed");
        family.FamilyPlacementType = FamilyPlacementType.OneLevelBased;
        symbol.Category = null;
        Rendered.Place(doc);
        var point = (LocationPoint)doc.Elements.OfType<FamilyInstance>().Single().Location;
        Check(Math.Abs(point.Point.Z * 304.8 - 3500) < 1e-8 && Math.Abs(point.Rotation - Math.PI / 2) < 1e-8, "placement");
    }
    static void Inventory()
    {
        var doc = Model();
        doc.Add(new PipeType { Name = "Other" });
        doc.Add(new PipeType { Name = "Third" });
        doc.Add(new PipeSegment { Name = "Example", Sizes = { new(1), new(2, false), new(3) } });
        var result = Result(Rendered.Inventory(doc));
        Check(result.GetProperty("PipeTypes").GetProperty("Truncated").GetBoolean(), "type limit");
        var segment = result.GetProperty("PipeDiametersBySegment").GetProperty("Items")[0];
        Check(segment.GetProperty("PipeSegmentName").GetString() == "Example", "pipe segment associated with sizes");
        var sizes = segment.GetProperty("NominalDiametersMm");
        Check(!sizes.GetProperty("Truncated").GetBoolean() && sizes.GetProperty("Items").GetArrayLength() == 2
            && Math.Abs(sizes.GetProperty("Items")[1].GetDouble() - 914.4) < 1e-8, "nominal size lists");
        Check(result.GetProperty("RoundDuctDiametersMm").GetProperty("Items")[0].GetDouble() == 152.4, "duct diameter");
    }
    public static void Main()
    {
        Segments();
        Routes();
        Preflight();
        StraightRun();
        Diameters();
        RejectedDependents();
        Connections();
        Placement();
        Inventory();
        Console.WriteLine("MEP scenarios passed");
    }
}

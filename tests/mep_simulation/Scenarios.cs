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
    static void Refuses(Func<Document, object> run, Document doc, string reason)
    {
        try { run(doc); }
        catch (InvalidOperationException ex) when (ex.Message.StartsWith("REFUSED: ") && ex.Message.Contains(reason)) { return; }
        throw new Exception("Expected refusal containing " + reason);
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
        var detached = Model();
        detached.DetachedElbow = true;
        Issues(Result(Rendered.Route(detached)), "CornerIssues", 1);
        Check(!detached.Elements.OfType<FamilyInstance>().Any(), "detached elbow left behind");
        var fatal = Model();
        fatal.FatalElbow = true;
        try { Rendered.Route(fatal); throw new Exception("fatal failure was treated as a missing elbow"); }
        catch (Autodesk.Revit.Exceptions.RegenerationFailedException ex) { Check(ex.Message == "abort", "fatal error lost"); }
        var gap = Result(Rendered.Gap(doc));
        Issues(gap, "SegmentIssues", 1);
        Issues(gap, "CornerIssues", 1, 2);
        var malformed = Result(Rendered.BadVertex(doc));
        Issues(malformed, "SegmentIssues", 1, 2);
        Issues(malformed, "CornerIssues", 1, 2, 3);
        Refuses(Rendered.NoSegments, doc, "No pipe could be created");
        doc = Model();
        doc.RejectSize = true;
        Refuses(Rendered.Route, doc, "No pipe could be created");
        Check(!doc.Elements.OfType<Pipe>().Any(), "unsized pipes left behind");
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
        var sizes = result.GetProperty("PipeSegmentSizes").GetProperty("Items")[0].GetProperty("DiametersMm");
        Check(!sizes.GetProperty("Truncated").GetBoolean() && sizes.GetProperty("Items").GetArrayLength() == 2
            && Math.Abs(sizes.GetProperty("Items")[1].GetDouble() - 914.4) < 1e-8, "nominal size lists");
        Check(result.GetProperty("RoundDuctDiametersMm").GetProperty("Items")[0].GetDouble() == 152.4, "duct diameter");
    }
    public static void Main()
    {
        Segments();
        Routes();
        Connections();
        Placement();
        Inventory();
        Console.WriteLine("MEP scenarios passed");
    }
}

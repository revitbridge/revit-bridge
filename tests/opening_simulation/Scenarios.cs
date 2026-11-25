// Model lengths in millimetres in this fixture. The wall curve and the regenerated
// family point deliberately differ. Set the instance and type dimensions independently.
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;

namespace Autodesk.Revit.DB.Structure { enum StructuralType { NonStructural } }
enum BuiltInCategory : long { OST_Doors, OST_Windows }
enum BuiltInParameter { DOOR_WIDTH, FAMILY_WIDTH_PARAM, WINDOW_WIDTH, DOOR_HEIGHT, FAMILY_HEIGHT_PARAM,
                        WINDOW_HEIGHT, INSTANCE_SILL_HEIGHT_PARAM, INSTANCE_HEAD_HEIGHT_PARAM }
enum WallKind { Basic, Curtain, Stacked }
enum StorageType { Double }
enum FamilyPlacementType { OneLevelBasedHosted }
enum UnitTypeId { Millimeters }
static class UnitUtils {
    public static double ConvertToInternalUnits(double n, UnitTypeId _) => n;
    public static double ConvertFromInternalUnits(double n, UnitTypeId _) => n;
}
record ElementId(long Id) { public override string ToString() => Id.ToString(); }
record XYZ(double X, double Y, double Z) {
    public static bool IsWithinLengthLimits(XYZ _) => true;
    public double DistanceTo(XYZ p) => Math.Sqrt(Math.Pow(X-p.X, 2) + Math.Pow(Y-p.Y, 2) + Math.Pow(Z-p.Z, 2));
}
record BoundingBoxXYZ(XYZ Min, XYZ Max);
record IntersectionResult(XYZ XYZPoint);
class Curve {
    public double Length => 5000;
    public XYZ Evaluate(double f, bool _) => new(f * Length, 0, 0);
    public XYZ GetEndPoint(int i) => Evaluate(i, true);
    public IntersectionResult Project(XYZ p) => new(new XYZ(p.X, 0, 0));
}
class Line : Curve { }
class Arc : Curve { }
record LocationCurve(Curve Curve);
record LocationPoint(XYZ Point);
class Parameter {
    double value;
    public Parameter(double n) { value = n; }
    public bool HasValue => true;
    public StorageType StorageType => StorageType.Double;
    public bool IsReadOnly => false;
    public double AsDouble() => value;
    public bool Set(double n) { value = n; return true; }
}
class Element {
    public ElementId Id;
    public string Name;
    public object Location;
    public Dictionary<BuiltInParameter, Parameter> Parameters = new();
    public Parameter get_Parameter(BuiltInParameter key) => Parameters.GetValueOrDefault(key);
}
class Level : Element { public double ProjectElevation; }
class WallType { public WallKind Kind => WallKind.Basic; }
class Wall : Element {
    public WallType WallType = new();
    public ElementId LevelId = new(1);
    public double Width => 200;
    public BoundingBoxXYZ Box = new(new(0,0,0), new(5000,200,6000));
    public BoundingBoxXYZ get_BoundingBox(object _) => Box;
}
class Family { public FamilyPlacementType FamilyPlacementType => FamilyPlacementType.OneLevelBasedHosted; }
class FamilySymbol : Element {
    public string FamilyName => "Family";
    public Family Family = new();
    public bool IsActive => true;
    public void Activate() { }
}
class FamilyInstance : Element {
    public Wall Host;
    public bool CanFlipFacing => true;
    public bool Flipped;
    public bool flipFacing() { Flipped = true; return true; }
}
class FilteredElementCollector : List<Element> {
    public FilteredElementCollector(Document d) : base(new Element[] { d.Symbol, d.Base, d.Upper }) { }
    public FilteredElementCollector OfClass(Type t) { RemoveAll(e => !t.IsInstanceOfType(e)); return this; }
    public FilteredElementCollector OfCategory(BuiltInCategory _) => this;
}
class Application { public double ShortCurveTolerance => 0.01; }
class Document {
    public Wall Wall = new() { Id = new(101), Location = new LocationCurve(new Arc()) };
    public Level Base = new() { Id = new(1), Name = "Base" };
    public Level Upper = new() { Id = new(2), Name = "Upper", ProjectElevation = 3000 };
    public FamilySymbol Symbol = new() { Id = new(7), Name = "Opening" };
    public FamilyInstance Instance;
    public XYZ Asked;
    public Application Application = new();
    public double? InstanceHeight, Head = 2100;
    public bool PointAvailable = true;
    public Document Create => this;
    public Element GetElement(ElementId id) => id == Wall.Id ? Wall : id == Base.Id ? Base : null;
    public FamilyInstance NewFamilyInstance(XYZ p, FamilySymbol _, Wall wall, Level level, Autodesk.Revit.DB.Structure.StructuralType usage) {
        Asked = p;
        Instance = new FamilyInstance { Id = new(201), Host = wall };
        Instance.Parameters[BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM] = new(p.Z - level.ProjectElevation);
        if (Head.HasValue) Instance.Parameters[BuiltInParameter.INSTANCE_HEAD_HEIGHT_PARAM] = new(Head.Value + p.Z - level.ProjectElevation);
        if (InstanceHeight.HasValue) Instance.Parameters[BuiltInParameter.DOOR_HEIGHT] = new(InstanceHeight.Value);
        return Instance;
    }
    public void Regenerate() {
        if (Instance != null && PointAvailable)
            Instance.Location = new LocationPoint(new XYZ(Asked.X - (Instance.Flipped ? 42 : 35), Asked.Y + 12, Asked.Z));
    }
}
class Program {
    static void Expect(bool ok) { if (!ok) throw new Exception("Unexpected placement or read-back"); }
    static void Refused(Document doc, string message, bool beforePlacement) {
        try { Rendered.Default(doc); }
        catch (InvalidOperationException ex) when (ex.Message.StartsWith("REFUSED:") && ex.Message.Contains(message)) {
            Expect(beforePlacement == (doc.Instance == null)); return;
        }
        throw new Exception("Expected refusal: " + message);
    }
    public static void Main() {
        var doc = new Document();
        var result = JsonSerializer.SerializeToElement(Rendered.Default(doc));
        bool window = result.TryGetProperty("SillHeightMm", out var sill);
        Expect(result.GetProperty("HostId").GetInt64() == 101 && doc.Asked.X == 1500);
        Expect(result.GetProperty("XMm").GetDouble() == 1465);
        Expect(result.GetProperty("YMm").GetDouble() == 12);
        Expect(result.GetProperty("ZMm").GetDouble() == (window ? 900 : 0));
        Console.WriteLine("PASS default placement and final point");
        var upper = new Document();
        result = JsonSerializer.SerializeToElement(Rendered.Upper(upper));
        Expect(result.GetProperty("Level").GetString() == "Upper");
        Expect(result.GetProperty("XMm").GetDouble() == 1458 && result.GetProperty("YMm").GetDouble() == 12);
        Expect(result.GetProperty("ZMm").GetDouble() == upper.Asked.Z);
        Console.WriteLine("PASS actual XYZ after flip");
        Console.WriteLine("PASS cross-level placement and flip");
        Refused(new Document { PointAvailable = false }, "location", false);
        if (window) return;
        var raised = new Document();
        raised.Wall.Box = new BoundingBoxXYZ(new(0,0,725), new(5000,200,3000));
        Refused(raised, "725 mm above the placement level", true);
        var unknownWall = new Document();
        unknownWall.Wall.Box = null;
        Refused(unknownWall, "verify the requested door elevation", true);
        var shortWall = new Document { Head = 6001 };
        Refused(shortWall, "opening extends", false);
        Rendered.Default(new Document { Head = 6000 });
        Refused(new Document { Head = 0, InstanceHeight = 2100 }, "head must be above", false);
        Refused(new Document { Head = -15, InstanceHeight = 2100 }, "head must be above", false);
        var instance = new Document { Head = null, InstanceHeight = 2100 };
        Rendered.Default(instance);
        instance.InstanceHeight = 6001;
        Refused(instance, "opening extends", false);
        var type = new Document { Head = null, InstanceHeight = 0 };
        type.Symbol.Parameters[BuiltInParameter.DOOR_HEIGHT] = new(0);
        type.Symbol.Parameters[BuiltInParameter.FAMILY_HEIGHT_PARAM] = new(2100);
        Rendered.Default(type);
        var tallType = new Document { Head = null };
        tallType.Symbol.Parameters[BuiltInParameter.FAMILY_HEIGHT_PARAM] = new(6001);
        Refused(tallType, "opening extends", false);
        // A smaller placed opening can fit even when the nominal type height would not.
        tallType.Head = 2100;
        Rendered.Default(tallType);
        tallType.Head = null;
        tallType.InstanceHeight = 2100;
        Rendered.Default(tallType);
        Refused(new Document { Head = null, InstanceHeight = 0 }, "verify the opening height", false);
        Console.WriteLine("PASS wall bottom, head, fallback heights and unreadable dimensions");
    }
}

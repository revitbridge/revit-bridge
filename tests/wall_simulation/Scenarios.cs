using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;

record ElementId(long Value) { public static readonly ElementId InvalidElementId = new(-1); }
enum BuiltInParameter { WALL_BASE_CONSTRAINT, WALL_BASE_OFFSET, WALL_HEIGHT_TYPE, WALL_TOP_OFFSET, WALL_USER_HEIGHT_PARAM }
enum UnitTypeId { Millimeters }
static class UnitUtils {
    public static double ConvertToInternalUnits(double mm, UnitTypeId _) => mm / 304.8;
    public static double ConvertFromInternalUnits(double feet, UnitTypeId _) => feet * 304.8;
}
class Element {
    public ElementId Id;
    public virtual Parameter get_Parameter(BuiltInParameter key) => null;
}
class Level : Element { public string Name; public double ProjectElevation; }
class Wall : Element {
    public Dictionary<BuiltInParameter, Parameter> Parameters = new();
    public override Parameter get_Parameter(BuiltInParameter key) => Parameters[key];
}
class Parameter {
    object value;
    readonly Action onSet;
    public Parameter(object initial, Action onSet) { value = initial; this.onSet = onSet; }
    public bool HasValue => true;
    public bool IsReadOnly => false;
    public ElementId AsElementId() => (ElementId)value;
    public double AsDouble() => (double)value;
    public bool Set(ElementId id) { value = id; onSet(); return true; }
    public bool Set(double d) { value = d; onSet(); return true; }
}
class FilteredElementCollector : List<Element> {
    public FilteredElementCollector(Document document) : base(document.Levels) { }
    public FilteredElementCollector OfClass(Type _) => this;
}
class Document {
    public readonly List<Level> Levels = new();
    public int WriteCount { get; private set; }
    readonly Wall wall = new() { Id = new(101) };
    public Document(bool bound = true, bool highBase = false, bool lowTop = false) {
        for (int i = 0; i < 4; i++) {
            Levels.Add(new Level { Id = new(i + 1), Name = "L" + (i + 1), ProjectElevation = Mm(i * 3000) });
        }
        Add(BuiltInParameter.WALL_BASE_CONSTRAINT, Levels[0].Id);
        Add(BuiltInParameter.WALL_BASE_OFFSET, Mm(highBase ? 2000 : lowTop ? 1000 : 0));
        Add(BuiltInParameter.WALL_HEIGHT_TYPE, bound ? Levels[lowTop ? 3 : 1].Id : ElementId.InvalidElementId);
        Add(BuiltInParameter.WALL_TOP_OFFSET, Mm(lowTop ? -6000 : 180));
        Add(BuiltInParameter.WALL_USER_HEIGHT_PARAM, Mm(3000));
    }
    public Element GetElement(ElementId id) => id == wall.Id ? wall : Levels.FirstOrDefault(level => level.Id == id);
    static double Mm(double value) => value / 304.8;
    void Add(BuiltInParameter key, object value) {
        wall.Parameters[key] = new Parameter(value, () => WriteCount++);
    }
    public void Commit() {
        var p = wall.Parameters;
        var baseElevation = ((Level)GetElement(p[BuiltInParameter.WALL_BASE_CONSTRAINT].AsElementId())).ProjectElevation
                + p[BuiltInParameter.WALL_BASE_OFFSET].AsDouble();
        var level = GetElement(p[BuiltInParameter.WALL_HEIGHT_TYPE].AsElementId()) as Level;
        double top = level != null ? level.ProjectElevation + p[BuiltInParameter.WALL_TOP_OFFSET].AsDouble()
                                   : baseElevation + p[BuiltInParameter.WALL_USER_HEIGHT_PARAM].AsDouble();
        if (top <= baseElevation) throw new Exception("Cannot commit an inverted wall");
    }
    public void Regenerate() { }
}
class Program {
    static void Check(string name, Func<Document, object> operation, bool bound, string level, double? offset, double height,
                      bool ignored = false, bool highBase = false, bool lowTop = false) {
        var doc = new Document(bound, highBase, lowTop);
        var result = JsonSerializer.SerializeToElement(operation(doc));
        doc.Commit();
        var reportedOffset = result.GetProperty("TopOffsetMm");
        bool correctOffset = offset.HasValue
            ? Math.Abs(reportedOffset.GetDouble() - offset.Value) < 1e-6
            : reportedOffset.ValueKind == JsonValueKind.Null;
        if (result.GetProperty("TopLevel").GetString() != level ||
            result.GetProperty("HeightIgnored").GetBoolean() != ignored || !correctOffset ||
            Math.Abs(result.GetProperty("HeightMm").GetDouble() - height) > 1e-6) {
            throw new Exception(name + ": " + result);
        }
        Console.WriteLine("PASS " + name);
    }
    public static void Main() {
        Check("raise base past old top", Rendered.Raise, true, "L4", 180, 3180, true);
        Check("repeating top level keeps its offset", Rendered.RepeatTop, true, "L2", 180, 2980);
        Check("bind previously unconnected wall and clear stale offset", Rendered.Bind, false, "L3", 0, 6000);
        Check("explicit top offset", Rendered.Offset, true, "L3", 125, 6125);
        Check("base only keeps top offset", Rendered.BaseOnly, true, "L2", 180, 2980);
        Check("unconnected height has no top offset", Rendered.Height, true, null, null, 2000);
        Check("bind lower top and clear negative offset", Rendered.LowerTop, true, "L3", 0, 2000, lowTop: true);
        Check("raise base level and lower positive offset", Rendered.RaiseBase, true, "L2", 180, 1180, highBase: true);
        var doc = new Document();
        try { Rendered.Invalid(doc); throw new Exception("invalid target was accepted"); }
        catch (InvalidOperationException ex) when (ex.Message.StartsWith("REFUSED:") && doc.WriteCount == 0) {
            Console.WriteLine("PASS inverted target refused before write");
        }
    }
}

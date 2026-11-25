using System;
using System.Collections.Generic;
using System.Linq;

// Small in-memory model for the rendered pack bodies. Real API compatibility is
// checked separately against the six Autodesk reference assemblies.
record ElementId(long Value)
{
    public static readonly ElementId InvalidElementId = new(-1);
    public override string ToString() => Value.ToString();
}
enum BuiltInCategory : long { OST_Doors, OST_Windows, OST_StructuralFraming, OST_Columns, OST_StructuralColumns }
enum BuiltInParameter { RBS_PIPE_DIAMETER_PARAM, RBS_CURVE_DIAMETER_PARAM, RBS_CURVE_WIDTH_PARAM,
    RBS_CURVE_HEIGHT_PARAM, RBS_PIPING_SYSTEM_TYPE_PARAM }
enum ConnectorType { End }
enum ConnectorProfileType { Round, Rectangular, Oval }
enum StorageType { Double, ElementId }
enum Snap { None, InsideSet, AtRegeneration, AfterElbowCommit }
enum UnitTypeId { Millimeters }
enum FamilyPlacementType { OneLevelBased, OneLevelBasedHosted, WorkPlaneBased, CurveBased, Adaptive, TwoLevelsBased, CurveDrivenStructural }
static class UnitUtils
{
    public static double ConvertToInternalUnits(double mm, UnitTypeId unit) => mm / 304.8;
    public static double ConvertFromInternalUnits(double feet, UnitTypeId unit) => feet * 304.8;
}
record XYZ(double X, double Y, double Z)
{
    public static readonly XYZ BasisZ = new(0, 0, 1);
    public static XYZ operator +(XYZ a, XYZ b) => new(a.X + b.X, a.Y + b.Y, a.Z + b.Z);
    public static XYZ operator -(XYZ a, XYZ b) => new(a.X - b.X, a.Y - b.Y, a.Z - b.Z);
    public static XYZ operator -(XYZ a) => new(-a.X, -a.Y, -a.Z);
    public static XYZ operator *(double f, XYZ a) => new(f * a.X, f * a.Y, f * a.Z);
    public double DotProduct(XYZ b) => X * b.X + Y * b.Y + Z * b.Z;
    public XYZ CrossProduct(XYZ b) => new(Y * b.Z - Z * b.Y, Z * b.X - X * b.Z, X * b.Y - Y * b.X);
    public double GetLength() => Math.Sqrt(DotProduct(this));
    public XYZ Normalize() => (1 / GetLength()) * this;
    public double DistanceTo(XYZ b) => (this - b).GetLength();
    public bool IsAlmostEqualTo(XYZ b) => DistanceTo(b) < 1e-9;
    public static bool IsWithinLengthLimits(XYZ p) => p.GetLength() < 100000;
}
record Line(XYZ Start, XYZ End)
{
    public XYZ GetEndPoint(int i) => i == 0 ? Start : End;
    public static Line CreateBound(XYZ start, XYZ end) => new(start, end);
    public static Line CreateUnbound(XYZ start, XYZ direction) => new(start, start + direction);
}
class LocationPoint { public XYZ Point; public double Rotation; }
class LocationCurve { public Line Curve; }
class Parameter
{
    public StorageType StorageType { get; init; }
    public ElementId IdValue;
    public double Value;
    public bool HasValue = true;
    public bool IsReadOnly;
    public ElementId AsElementId() => IdValue;
    public double AsDouble() => Value;
    public Snap SnapMode;
    public static readonly double SnappedSize = 32 / 304.8;
    public const double ReadbackOffset = 1e-9;
    public bool OffsetReadback;
    public bool Set(double value)
    {
        Value = SnapMode == Snap.InsideSet ? SnappedSize : value + (OffsetReadback ? ReadbackOffset : 0);
        return true;
    }
}
class Category
{
    public ElementId Id;
    public static Category GetCategory(Document document, BuiltInCategory category) => new() { Id = new((long)category) };
}
class Element
{
    public ElementId Id, LevelId;
    public ElementId GroupId = ElementId.InvalidElementId;
    public string Name;
    public object Location;
    public Category Category;
    public bool Pinned { get; set; }
    public List<ElementId> Dependents = new();
    public List<ElementId> GetDependentElements(object filter) => Dependents.ToList();
    public readonly Dictionary<BuiltInParameter, Parameter> Parameters = new();
    public Parameter get_Parameter(BuiltInParameter key) => Parameters.GetValueOrDefault(key);
}
class ElementType : Element { }
class Level : Element { public double Elevation; }
class Family : Element { public FamilyPlacementType FamilyPlacementType; }
class FamilySymbol : ElementType
{
    public Family Family;
    public string FamilyName => Family.Name;
    public bool IsActive;
    public void Activate() => IsActive = true;
}
static class AdaptiveComponentFamilyUtils
{
    public static bool IsAdaptiveComponentFamily(Family family) => family.FamilyPlacementType == FamilyPlacementType.Adaptive;
}
class FamilyInstance : Element { public MEPModel MEPModel; }
class MEPModel { public ConnectorManager ConnectorManager = new(); }
class ConnectorManager { public List<Connector> Connectors = new(); }
class MEPSystem : Element { }
class ConnectorElement : Element { }
class MEPCurve : Element
{
    public ConnectorManager ConnectorManager = new();
    public MEPSystem MEPSystem;
}
class Connector
{
    public Element Owner;
    public MEPSystem MEPSystem => (Owner as MEPCurve)?.MEPSystem;
    public int Index;
    public ConnectorType ConnectorType => ConnectorType.End;
    public XYZ Origin => ((LocationCurve)Owner.Location).Curve.GetEndPoint(Index);
    public (XYZ BasisZ, int Index) CoordinateSystem =>
        ((Origin - ((LocationCurve)Owner.Location).Curve.GetEndPoint(1 - Index)).Normalize(), Index);
    public double Radius => Owner.get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM).Value / 2;
    public List<Connector> AllRefs = new();
    public bool IsConnected => AllRefs.Count > 0;
    public bool IsConnectedTo(Connector other) => AllRefs.Contains(other);
}
class FilteredElementCollector : List<Element>
{
    public FilteredElementCollector(Document document) : base(document.Elements) { }
    public FilteredElementCollector OfClass(Type type) { RemoveAll(e => !type.IsInstanceOfType(e)); return this; }
    public ElementId FirstElementId() => this.FirstOrDefault()?.Id ?? ElementId.InvalidElementId;
}
class Application
{
    public double ShortCurveTolerance => 0.0026;
    public double VertexTolerance => 1e-7;
    public double AngleTolerance => 1e-7;
}
enum TransactionStatus { Started, Committed, RolledBack }
class SubTransaction(Document doc) : IDisposable
{
    List<Element> originalElements;
    public TransactionStatus Start()
    {
        originalElements = doc.Elements.ToList();
        return TransactionStatus.Started;
    }
    public TransactionStatus Commit()
    {
        if (doc.RejectElbowCommit) return RollBack();
        originalElements = null;
        doc.ElbowCommitted = true;
        return TransactionStatus.Committed;
    }
    public TransactionStatus RollBack()
    {
        foreach (var element in doc.Elements.Except(originalElements).ToList()) doc.Delete(element.Id);
        originalElements = null;
        return TransactionStatus.RolledBack;
    }
    public void Dispose() { if (originalElements != null) RollBack(); }
}
class Document
{
    public readonly List<Element> Elements = new();
    public Application Application = new();
    public Document Create => this;
    public string FittingKind;
    public bool RejectElbow, RejectElbowCommit, RejectSize, DetachedElbow, FatalElbow;
    public Snap FirstPipeSnap;
    public bool OffsetReadback;
    public bool WithDependents, WithUnrelatedDeletion, ElbowCommitted;
    public int PipesCreated, ElbowAttempts;
    long nextId = 101;
    public T Add<T>(T element) where T : Element
    {
        element.Id = new ElementId(nextId++);
        Elements.Add(element);
        return element;
    }
    public Element GetElement(ElementId id) => Elements.SingleOrDefault(e => e.Id == id);
    public List<ElementId> Delete(ElementId id)
    {
        var element = GetElement(id);
        if (element is FamilyInstance fitting)
            foreach (var c in fitting.MEPModel.ConnectorManager.Connectors)
                foreach (var other in c.AllRefs) other.AllRefs.Remove(c);
        var removed = element.Dependents.Append(id).ToList();
        if (element is MEPCurve curve && curve.MEPSystem != null) removed.Add(curve.MEPSystem.Id);
        if (WithUnrelatedDeletion && element is Autodesk.Revit.DB.Plumbing.Pipe)
            removed.Add(Elements.OfType<Autodesk.Revit.DB.Plumbing.PipingSystem>().First().Id);
        Elements.RemoveAll(e => removed.Contains(e.Id));
        return removed;
    }
    public void Regenerate()
    {
        foreach (var pipe in Elements.OfType<Autodesk.Revit.DB.Plumbing.Pipe>())
        {
            var size = pipe.get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM);
            if (size.SnapMode == Snap.AtRegeneration || (size.SnapMode == Snap.AfterElbowCommit && ElbowCommitted))
                size.Value = Parameter.SnappedSize;
        }
    }
    public T Curve<T>(ElementId system, ElementId level, XYZ start, XYZ end) where T : MEPCurve, new()
    {
        var curve = Add(new T { Location = new LocationCurve { Curve = new(start, end) }, LevelId = level });
        foreach (var key in Enum.GetValues<BuiltInParameter>())
            curve.Parameters[key] = new Parameter { IsReadOnly = RejectSize };
        curve.Parameters[BuiltInParameter.RBS_PIPE_DIAMETER_PARAM].OffsetReadback = OffsetReadback;
        if (curve is Autodesk.Revit.DB.Plumbing.Pipe && ++PipesCreated == 1)
        {
            curve.Parameters[BuiltInParameter.RBS_PIPE_DIAMETER_PARAM].SnapMode = FirstPipeSnap;
            if (WithDependents)
            {
                curve.MEPSystem = Add(new Autodesk.Revit.DB.Plumbing.PipingSystem());
                curve.Dependents.Add(Add(new ConnectorElement()).Id);
            }
        }
        curve.Parameters[BuiltInParameter.RBS_PIPING_SYSTEM_TYPE_PARAM] = new Parameter
            { StorageType = StorageType.ElementId, IdValue = system };
        for (int i = 0; i < 2; i++) curve.ConnectorManager.Connectors.Add(new Connector { Owner = curve, Index = i });
        return curve;
    }
    public FamilyInstance NewFamilyInstance(XYZ point, FamilySymbol symbol, Level level, Autodesk.Revit.DB.Structure.StructuralType type)
        => Add(new FamilyInstance { Location = new LocationPoint { Point = point + level.Elevation * XYZ.BasisZ }, LevelId = level.Id });
    FamilyInstance Fit(Connector a, Connector b, string kind)
    {
        FittingKind = kind;
        var fitting = Add(new FamilyInstance { MEPModel = new() });
        foreach (var end in new[] { a, b })
        {
            var fittingEnd = new Connector { Owner = fitting };
            fitting.MEPModel.ConnectorManager.Connectors.Add(fittingEnd);
            end.AllRefs.Add(fittingEnd);
            fittingEnd.AllRefs.Add(end);
        }
        return fitting;
    }
    public FamilyInstance NewElbowFitting(Connector a, Connector b)
    {
        ElbowAttempts++;
        if (RejectElbow)
        {
            Fit(a, b, "elbow");
            throw new Autodesk.Revit.Exceptions.InvalidOperationException("sample failure after modifying connectors");
        }
        if (DetachedElbow) return Add(new FamilyInstance { MEPModel = new() });
        if (FatalElbow) throw new Autodesk.Revit.Exceptions.RegenerationFailedException();
        return Fit(a, b, "elbow");
    }
    public FamilyInstance NewUnionFitting(Connector a, Connector b) => Fit(a, b, "union");
    public FamilyInstance NewTransitionFitting(Connector a, Connector b) => Fit(a, b, "transition");
}
static class ElementTransformUtils
{
    public static void MoveElement(Document doc, ElementId id, XYZ move) => ((LocationPoint)doc.GetElement(id).Location).Point += move;
    public static void RotateElement(Document doc, ElementId id, Line axis, double angle) => ((LocationPoint)doc.GetElement(id).Location).Rotation += angle;
}
namespace Autodesk.Revit.DB.Structure { enum StructuralType { NonStructural } }
namespace Autodesk.Revit.Exceptions
{
    class ArgumentException : Exception { }
    class InvalidOperationException : Exception { public InvalidOperationException(string message) : base(message) { } }
    class RegenerationFailedException : InvalidOperationException { public RegenerationFailedException() : base("abort") { } }
}
record MEPSize(double NominalDiameter, bool UsedInSizeLists = true);
namespace Autodesk.Revit.DB.Plumbing
{
    class PipeType : ElementType { }
    class PipingSystemType : ElementType { }
    class PipingSystem : MEPSystem { }
    class PipeSegment : Element { public List<MEPSize> Sizes = new(); public IEnumerable<MEPSize> GetSizes() => Sizes; }
    class Pipe : MEPCurve
    {
        public bool IsPlaceholder { get; set; }
        public static Pipe Create(Document doc, ElementId system, ElementId type, ElementId level, XYZ a, XYZ b)
            => doc.Curve<Pipe>(system, level, a, b);
    }
}
namespace Autodesk.Revit.DB.Mechanical
{
    class DuctType : ElementType { public ConnectorProfileType Shape; }
    class MechanicalSystemType : ElementType { }
    class Duct : MEPCurve
    {
        public static Duct Create(Document doc, ElementId system, ElementId type, ElementId level, XYZ a, XYZ b)
            => doc.Curve<Duct>(system, level, a, b);
    }
    enum DuctShape { Round }
    class DuctSizeSettings
    {
        public static DuctSizeSettings GetDuctSizeSettings(Document doc) => new();
        public int GetSizeCount(DuctShape shape) => this[shape].Count;
        public List<MEPSize> this[DuctShape shape] => new() { new(0.5) };
    }
}

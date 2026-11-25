using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;

record ElementId(long Value)
{
    public ElementId(BuiltInParameter p) : this((long)p) { }
    public override string ToString() => Value.ToString();
}
enum BuiltInCategory : long { OST_Walls = -2, OST_Doors = -3 }
enum BuiltInParameter { ELEM_TYPE_PARAM }
enum UnitTypeId { Millimeters }
static class UnitUtils
{
    public static double ConvertToInternalUnits(double value, UnitTypeId unit) => value / 304.8;
}
class Category
{
    public static Category GetCategory(Document d, BuiltInCategory c) => new();
}
record XYZ(double X, double Y, double Z)
{
    public static XYZ BasisX => new(1, 0, 0);
    public static XYZ BasisY => new(0, 1, 0);
    public static XYZ BasisZ => new(0, 0, 1);
    public static XYZ operator +(XYZ a, XYZ b) => new(a.X + b.X, a.Y + b.Y, a.Z + b.Z);
    public static XYZ operator -(XYZ a) => new(-a.X, -a.Y, -a.Z);
    public static XYZ operator -(XYZ a, XYZ b) => a + (-b);
    public static XYZ operator *(XYZ a, double n) => new(a.X * n, a.Y * n, a.Z * n);
    public double DotProduct(XYZ b) => X * b.X + Y * b.Y + Z * b.Z;
    public XYZ CrossProduct(XYZ b) => new(Y * b.Z - Z * b.Y, Z * b.X - X * b.Z, X * b.Y - Y * b.X);
    public bool IsZeroLength() => DotProduct(this) < 1e-18;
    public XYZ Normalize() => IsZeroLength() ? this : this * (1 / Math.Sqrt(DotProduct(this)));
    public static bool IsWithinLengthLimits(XYZ p) => p.DotProduct(p) < 1e14;
}
class BoundingBoxXYZ { public XYZ Min, Max; }
record Outline(XYZ Min, XYZ Max);
class Element(long id, string name)
{
    public ElementId Id = new(id);
    public string Name = name;
    public BuiltInCategory? Category;
    public long LevelId, TypeId;
    public bool Visible = true, ViewSpecific;
    public BoundingBoxXYZ Box;
    public BoundingBoxXYZ get_BoundingBox(View view) => Box;
}
class Level(long id, string name) : Element(id, name) { }
class ElementType(long id, string name, string family) : Element(id, name)
{
    public string FamilyName = family;
}
class View() : Element(1, "Working view")
{
    public bool IsTemplate => false;
    public XYZ RightDirection = XYZ.BasisX, UpDirection = XYZ.BasisY;
}
record ViewOrientation3D(XYZ EyePosition, XYZ UpDirection, XYZ ForwardDirection);
class View3D : View
{
    public bool IsPerspective, IsLocked, IsSectionBoxActive;
    public BoundingBoxXYZ SectionBox;
    public ViewOrientation3D Orientation;
    public void SetSectionBox(BoundingBoxXYZ box) => SectionBox = box;
    public ViewOrientation3D GetOrientation() => Orientation;
    public void SetOrientation(ViewOrientation3D camera) => Orientation = camera;
    public void OrientTo(XYZ forward) => Orientation = new(new XYZ(0, 0, 0), XYZ.BasisZ, forward);
}
class UIView(ElementId id)
{
    public ElementId ViewId = id;
    public XYZ First, Second;
    public void ZoomAndCenterRectangle(XYZ first, XYZ second) { First = first; Second = second; }
}
class Selection
{
    private HashSet<ElementId> ids = new();
    public ICollection<ElementId> GetElementIds() => ids.ToList();
    public void SetElementIds(ICollection<ElementId> elements) => ids = elements.ToHashSet();
}
class Document
{
    public View ActiveView = new View3D();
    public UIView Window = new(new ElementId(1));
    public Selection Selection = new();
    public Dictionary<ElementId, Element> Elements = new();
    public Element GetElement(ElementId id) => Elements.GetValueOrDefault(id);
}
class UIDocument(Document d)
{
    public Selection Selection => d.Selection;
    public IEnumerable<UIView> GetOpenUIViews() => new[] { d.Window };
    public void RefreshActiveView() { }
}
abstract class ElementFilter { public abstract bool Matches(Element e); }
class BoundingBoxIsInsideFilter(Outline o) : ElementFilter
{
    public override bool Matches(Element e) => e.Box != null &&
        e.Box.Min.X >= o.Min.X && e.Box.Min.Y >= o.Min.Y && e.Box.Min.Z >= o.Min.Z &&
        e.Box.Max.X <= o.Max.X && e.Box.Max.Y <= o.Max.Y && e.Box.Max.Z <= o.Max.Z;
}
class BoundingBoxIntersectsFilter(Outline o) : ElementFilter
{
    public override bool Matches(Element e) => e.Box != null &&
        e.Box.Min.X <= o.Max.X && e.Box.Min.Y <= o.Max.Y && e.Box.Min.Z <= o.Max.Z &&
        e.Box.Max.X >= o.Min.X && e.Box.Max.Y >= o.Min.Y && e.Box.Max.Z >= o.Min.Z;
}
class ElementLevelFilter(ElementId level) : ElementFilter
{
    public override bool Matches(Element e) => e.LevelId == level.Value;
}
class ElementClassFilter(Type type, bool inverted) : ElementFilter
{
    public override bool Matches(Element e) => type.IsInstanceOfType(e) != inverted;
}
static class ParameterFilterRuleFactory
{
    public static ElementId CreateEqualsRule(ElementId parameter, ElementId type) => type;
}
class ElementParameterFilter(ElementId type) : ElementFilter
{
    public override bool Matches(Element e) => e.TypeId == type.Value;
}
class LogicalOrFilter(List<ElementFilter> filters) : ElementFilter
{
    public override bool Matches(Element e) => filters.Any(f => f.Matches(e));
}
class FilteredElementCollector : IEnumerable<Element>
{
    IEnumerable<Element> items;
    public FilteredElementCollector(Document d) { items = d.Elements.Values; }
    public FilteredElementCollector(Document d, ElementId view) : this(d) { items = items.Where(e => e.Visible); }
    public static bool IsViewValidForElementIteration(Document d, ElementId id) => d.ActiveView.Id == id;
    FilteredElementCollector Apply(Func<Element, bool> test) { items = items.Where(test); return this; }
    public FilteredElementCollector OfClass(Type cls) => Apply(cls.IsInstanceOfType);
    public FilteredElementCollector OfCategory(BuiltInCategory cat) => Apply(e => e.Category == cat);
    public FilteredElementCollector WhereElementIsElementType() => Apply(e => e is ElementType);
    public FilteredElementCollector WhereElementIsNotElementType() => Apply(e => e is not ElementType);
    public FilteredElementCollector WhereElementIsViewIndependent() => Apply(e => !e.ViewSpecific);
    public FilteredElementCollector WherePasses(ElementFilter filter) => Apply(filter.Matches);
    public int GetElementCount() => items.Count();
    public ICollection<ElementId> ToElementIds() => items.Select(e => e.Id).ToList();
    public IEnumerator<Element> GetEnumerator() => items.GetEnumerator();
    IEnumerator IEnumerable.GetEnumerator() => GetEnumerator();
}

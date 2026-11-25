using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Threading;

namespace Autodesk.Revit.Exceptions
{
    class ArgumentException(string message = "") : Exception(message) { }
    class ArgumentNullException : ArgumentException { }
    class InvalidOperationException : Exception { }
}

record ElementId(long Value)
{
    public override string ToString() => Value.ToString();
}

enum BuiltInCategory { OST_IOSModelGroups = -2000095, OST_StairsRailingBaluster, OST_RailingSystemBaluster, OST_RailingSupport, OST_StairsSupports }
enum CategoryType { Model, Annotation, Internal, AnalyticalModel, Invalid }
record Category(CategoryType CategoryType, ElementId Id);

class Element
{
    public ElementId Id { get; }
    readonly Category category;
    public bool Unreadable, Pinned;
    public Document Document;
    public List<ElementId> LogicalChildren = new();

    public Element(long id, Category category)
    {
        Id = new ElementId(id);
        this.category = category;
    }

    public Category Category
    {
        get
        {
            Document.RecordRead();
            if (Unreadable) throw new Autodesk.Revit.Exceptions.InvalidOperationException();
            return category;
        }
    }

    public IList<ElementId> GetDependentElements(object filter)
    {
        if (filter != null) throw new Exception("test model expects unrestricted logical dependents");
        Document.RecordRead();
        Document.DependentQueries.Add(Id.Value);
        return LogicalChildren;
    }
}

class ElementType(long id, Category c) : Element(id, c) { }
class View(long id, Category c) : Element(id, c) { }
class Level(long id, Category c) : Element(id, c) { }
class Sketch(long id, Category c) : Element(id, c) { }
class HostObject(long id, Category c) : Element(id, c) { }
class Floor(long id, Category c) : HostObject(id, c) { }
class RoofBase(long id, Category c) : HostObject(id, c) { }
class Ceiling(long id, Category c) : HostObject(id, c) { }
class CurtainSystem(long id, Category c) : Element(id, c) { }
class WallSweep(long id, Category c) : Element(id, c) { }
class SlabEdge(long id, Category c) : Element(id, c) { }
class WallFoundation(long id, Category c) : Element(id, c) { }
class MEPCurve(long id, Category c) : Element(id, c) { }
class BeamSystem(long id, Category c) : Element(id, c) { }
class Toposolid(long id, Category c) : Element(id, c) { }
class Family(long id, Category c) : Element(id, c) { }
class Opening(long id, Category c) : Element(id, c) { }
class Panel(long id, Category c) : FamilyInstance(id, c) { }
class Mullion(long id, Category c) : FamilyInstance(id, c) { }
class CurtainGridLine(long id, Category c) : Element(id, c) { }
class IndependentTag(long id, Category c) : Element(id, c) { }
class LinearDimension(long id, Category c) : Element(id, c) { }
class ModelLine(long id, Category c) : Element(id, c) { }
class FamilyInstance(long id, Category c) : Element(id, c)
{
    public Element SuperComponent, Host;
    public List<ElementId> Subcomponents = new();
    public ICollection<ElementId> GetSubComponentIds() { Document.RecordRead(); return Subcomponents; }
}

namespace Autodesk.Revit.DB.Architecture
{
    class Fascia(long id, Category c) : Element(id, c) { }
    class Gutter(long id, Category c) : Element(id, c) { }
    class TopographySurface(long id, Category c) : Element(id, c) { }
    class BuildingPad(long id, Category c) : Element(id, c) { }
    class Stairs(long id, Category c) : Element(id, c)
    {
        public List<ElementId> Runs = new(), Landings = new(), Supports = new();
        public ICollection<ElementId> GetStairsRuns() => Runs;
        public ICollection<ElementId> GetStairsLandings() => Landings;
        public ICollection<ElementId> GetStairsSupports() => Supports;
    }
    class StairsRun(long id, Category c) : Element(id, c) { }
    class StairsLanding(long id, Category c) : Element(id, c) { }
    class MultistoryStairs(long id, Category c) : Element(id, c) { }
    class Railing(long id, Category c, long host) : Element(id, c)
    {
        public ElementId HostId => new ElementId(host);
    }
    class ContinuousRail(long id, Category c, long owner) : Element(id, c)
    {
        public ElementId HostRailingId => new ElementId(owner);
    }
}

namespace Autodesk.Revit.DB.Structure
{
    class Rebar(long id, Category c) : Element(id, c) { }
    class RebarInSystem(long id, Category c) : Element(id, c) { }
    class RebarContainer(long id, Category c) : Element(id, c) { }
    class AreaReinforcement(long id, Category c) : Element(id, c) {
        public List<ElementId> Bars = new();
        public ICollection<ElementId> GetRebarInSystemIds() => Bars;
    }
    class PathReinforcement(long id, Category c) : Element(id, c) {
        public List<ElementId> Bars = new();
        public ICollection<ElementId> GetRebarInSystemIds() => Bars;
    }
    class FabricSheet(long id, Category c) : Element(id, c) { }
    class FabricArea(long id, Category c) : Element(id, c) {
        public List<ElementId> Sheets = new();
        public ICollection<ElementId> GetFabricSheetElementIds() => Sheets;
    }
}
class CurtainGrid
{
    public List<ElementId> Panels = new(), Mullions = new();
    public ICollection<ElementId> GetPanelIds() => Panels;
    public ICollection<ElementId> GetMullionIds() => Mullions;
}
class Wall(long id, Category c) : HostObject(id, c)
{
    public CurtainGrid CurtainGrid;
    public List<ElementId> Walls = new();
    public bool IsStackedWall => Walls.Count > 0;
    public IList<ElementId> GetStackedWallMemberIds()
    {
        Document.RecordRead();
        if (!IsStackedWall) throw new Exception("ordinary wall has no stacked members");
        return Walls;
    }
}
class Group(long id, Category c) : Element(id, c)
{
    public List<ElementId> Members = new();
    public ICollection<ElementId> GetMemberIds() { Document.RecordRead(); return Members; }
}

// This collection delays its first iteration to exercise the real post-delete budget.
class SlowReport(ICollection<ElementId> source) : List<ElementId>(source), IEnumerable<ElementId>
{
    IEnumerator<ElementId> IEnumerable<ElementId>.GetEnumerator()
    {
        Thread.Sleep(300);
        return base.GetEnumerator();
    }
}

class Document
{
    public Dictionary<ElementId, Element> Live;
    public Dictionary<long, long[]> ActualChildren = new();
    public readonly List<long> DependentQueries = new();
    public readonly HashSet<ElementId> UnreadableIds = new();
    public int DeleteCalls;
    public bool OmitRequested, LeaveSurvivor, SlowAfterDelete, SlowBeforeDelete, CannotDelete;

    public Document(IEnumerable<Element> elements)
    {
        Live = elements.ToDictionary(e => e.Id);
        foreach (var element in Live.Values) element.Document = this;
    }
    public void RecordRead()
    {
        if (DeleteCalls != 0) throw new Exception("classification must precede deletion");
        if (SlowBeforeDelete) Thread.Sleep(300);
    }
    public Element GetElement(ElementId id) => UnreadableIds.Contains(id) ? null : Live.GetValueOrDefault(id);
    public ICollection<ElementId> Delete(ICollection<ElementId> requested)
    {
        if (++DeleteCalls != 1) throw new Exception("second deletion attempt");
        if (CannotDelete) throw new Autodesk.Revit.Exceptions.ArgumentException("Simulated refusal for this stair.");
        var effects = new List<ElementId>();
        var pending = new Queue<ElementId>(requested);
        while (pending.TryDequeue(out var id))
        {
            if (!Live.Remove(id)) continue;
            effects.Add(id);
            if (ActualChildren.TryGetValue(id.Value, out var children))
                foreach (long child in children) pending.Enqueue(new ElementId(child));
        }
        if (LeaveSurvivor) Live[requested.First()] = new Element(requested.First().Value, null);
        if (OmitRequested) effects.Remove(requested.First());
        return SlowAfterDelete ? new SlowReport(effects) : effects;
    }
}

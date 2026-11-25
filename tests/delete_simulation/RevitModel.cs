using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Threading;

namespace Autodesk.Revit.Exceptions
{
    class ArgumentException : Exception { }
    class ArgumentNullException : ArgumentException { }
    class InvalidOperationException : Exception { }
}

record ElementId(long Value)
{
    public override string ToString() => Value.ToString();
}

enum BuiltInCategory { OST_IOSModelGroups = -2000095 }
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
class FamilyInstance(long id, Category c) : Element(id, c)
{
    public List<ElementId> Subcomponents = new();
    public ICollection<ElementId> GetSubComponentIds() { Document.RecordRead(); return Subcomponents; }
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
    public int DeleteCalls;
    public bool OmitRequested, LeaveSurvivor, SlowAfterDelete, SlowBeforeDelete;

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
    public Element GetElement(ElementId id) => Live.GetValueOrDefault(id);
    public ICollection<ElementId> Delete(ICollection<ElementId> requested)
    {
        if (++DeleteCalls != 1) throw new Exception("second deletion attempt");
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

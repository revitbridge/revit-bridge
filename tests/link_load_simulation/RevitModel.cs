using System;
using System.Collections.Generic;
using System.Linq;

// Record the native calls made by the shipped templates. All paths are just strings.
record ElementId(long Value)
{
    public static readonly ElementId InvalidElementId = new(-1);
    public override string ToString() => Value.ToString();
}
class ApiObject : IDisposable { public void Dispose() { } }
class FilePath(string path) : ApiObject
{
    public string Path { get; } = path;
    public bool Empty => string.IsNullOrEmpty(Path);
    public bool CloudPath => false;
    public bool ServerPath => false;
}
class ExternalFileReference(string absolute) : ApiObject
{
    public FilePath GetAbsolutePath() => absolute == null ? null : new FilePath(absolute);
}
static class ModelPathUtils
{
    public static string ConvertModelPathToUserVisiblePath(FilePath path) => path.Path;
}
class Element
{
    public ElementId Id = ElementId.InvalidElementId;
    public string Name;
    public ExternalFileReference Reference;
    public bool IsExternalFileReference() => Reference != null;
    public ExternalFileReference GetExternalFileReference() => Reference;
}
class Family : Element { }
class Level : Element { }
class View : Element
{
    public Level GenLevel;
    public bool IsTemplate;
    public bool CanBePrinted = true;
}
class ViewPlan : View { }
class View3D : View { }
enum ImportPlacement { Origin, Shared }
enum ImportUnit { Default, Millimeter, Centimeter, Decimeter, Meter, Inch, Foot }
enum BuiltInParameter { IMPORT_BASE_LEVEL }
enum LinkLoadResultType { LinkLoaded, LinkExists, UsedExisting, LinkNotFound, LinkNotOpenable,
                          SameModelAsHost, SameCentralModelAsHost, LinkOpenAsHost }
class LinkLoadResult : ApiObject
{
    public ElementId ElementId;
    public LinkLoadResultType LoadResult;
}
class RevitLinkOptions(bool relative) : ApiObject { public bool Relative { get; } = relative; }
class DWGImportOptions : ApiObject
{
    public ImportUnit Unit;
    public ImportPlacement Placement;
    public bool ThisViewOnly, OrientToView;
}
class FilteredElementCollector(Document doc) : List<Element>(doc.Elements)
{
    public FilteredElementCollector OfClass(Type type) { RemoveAll(e => !type.IsInstanceOfType(e)); return this; }
    public int GetElementCount() => Count;
}
class Document
{
    public List<Element> Elements = new();
    public View ActiveView;
    public bool IsFamilyDocument;
    public LinkLoadResultType Status = LinkLoadResultType.LinkLoaded;
    public Exception LoadError, PlacementError;
    public string LastPath;
    public View Target;
    public DWGImportOptions DrawingOptions;
    public ImportPlacement? ModelPlacement;
    public ElementId OverrideBaseLevel;
    public bool WrongScope;
    public bool LoadSucceeded = true;
    public Family FamilyResult;
    public int FileCalls, InstanceCalls;
    public int AttachedInstances;
    public readonly List<RevitLinkInstance> PendingInstances = new();
    public bool MissingInstance, WrongType;
    public Element GetElement(ElementId id) => Elements.SingleOrDefault(e => e.Id == id);
    public void Regenerate()
    {
        foreach (var instance in PendingInstances) Add(instance);
        PendingInstances.Clear();
    }

    public T Add<T>(T element) where T : Element
    {
        element.Id = new(1 + Elements.Count);
        Elements.Add(element);
        return element;
    }
    public void ReadSource(string path)
    {
        FileCalls++;
        LastPath = path;
        if (LoadError != null) throw LoadError;
    }
    public LinkLoadResult Result(Element type) => new() { ElementId = type.Id, LoadResult = Status };
    public bool LoadFamily(string path, out Family family)
    {
        ReadSource(path);
        family = FamilyResult;
        if (LoadSucceeded && family != null && !Elements.Contains(family)) Add(family);
        return LoadSucceeded;
    }
}
class RevitLinkType : Element
{
    public static LinkLoadResult Create(Document doc, FilePath path, RevitLinkOptions options)
    {
        doc.ReadSource(path.Path);
        var type = doc.Add(new RevitLinkType { Reference = new(path.Path) });
        return doc.Result(type);
    }
}
class RevitLinkInstance(ElementId type) : Element
{
    public ElementId GetTypeId() => type;
    public static RevitLinkInstance Create(Document doc, ElementId type, ImportPlacement placement)
    {
        doc.InstanceCalls++;
        doc.ModelPlacement = placement;
        if (doc.PlacementError != null) throw doc.PlacementError;
        var parent = new RevitLinkInstance(doc.WrongType ? ElementId.InvalidElementId : type);
        if (!doc.MissingInstance) doc.Add(parent);
        for (int i = 0; i < doc.AttachedInstances; i++)
            doc.PendingInstances.Add(new RevitLinkInstance(doc.Add(new RevitLinkType()).Id));
        return parent;
    }
}
class CADLinkType : Element { }
class Parameter(ElementId id) { public ElementId AsElementId() => id; }
class ImportInstance(ElementId type) : Element
{
    public bool IsLinked = true, ViewSpecific;
    public ElementId OwnerViewId, LevelId;
    public ElementId GetTypeId() => type;
    public Parameter get_Parameter(BuiltInParameter key) => new(LevelId);
    public static ImportInstance Create(Document doc, View view, string path, DWGImportOptions options,
                                        out LinkLoadResult result)
    {
        doc.ReadSource(path);
        doc.DrawingOptions = options;
        doc.Target = view;
        var type = doc.Add(new CADLinkType { Reference = new(path) });
        result = doc.Result(type);
        return doc.Add(new ImportInstance(type.Id) {
            ViewSpecific = doc.WrongScope ? !options.ThisViewOnly : options.ThisViewOnly,
            OwnerViewId = options.ThisViewOnly ? view.Id : ElementId.InvalidElementId,
            LevelId = doc.OverrideBaseLevel ?? view.GenLevel?.Id
        });
    }
}
namespace Autodesk.Revit.Exceptions
{
    class ApplicationException(string message = null) : Exception(message) { }
    class SourceException(string path) : ApplicationException("Cannot read " + path) { }
    class ArgumentException : ApplicationException { }
    class FileArgumentNotFoundException : ArgumentException { }
    class FileNotFoundException : ApplicationException { }
    class FileAccessException : ApplicationException { }
    class CorruptModelException : ApplicationException { }
    class OperationCanceledException : ApplicationException { }
    class InvalidOperationException : ApplicationException { }
    class ModificationForbiddenException : InvalidOperationException { }
    class ModificationOutsideTransactionException : InvalidOperationException { }
    class RegenerationFailedException : InvalidOperationException { }
    class OptionalFunctionalityNotAvailableException : InvalidOperationException { }
}

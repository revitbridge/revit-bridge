using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;

static class Scenarios
{
    // The stub exposes no Settings.Categories or model-wide collector; subcategories
    // and ordinary categories can be classified only by reading the actual candidate.
    static readonly Category Model = new(CategoryType.Model, new ElementId(1));
    static readonly Category Annotation = new(CategoryType.Annotation, new ElementId(2));
    static readonly Category HiddenModel = new(CategoryType.Model, new ElementId(3));
    static readonly Category HiddenMark = new(CategoryType.Annotation, new ElementId(4));
    static readonly Category GroupCategory = new(CategoryType.Model, new ElementId((long)BuiltInCategory.OST_IOSModelGroups));

    static List<ElementId> Ids(params long[] numbers) => numbers.Select(i => new ElementId(i)).ToList();
    static void Check(bool ok, string message)
    {
        if (!ok) throw new Exception(message);
    }

    // actual is the cascade returned by Revit; logical is what GetDependentElements
    // exposes. Scenarios can change either graph independently.
    static void Run(string name, Func<Document, object> invoke, Element[] elements, long[] actual,
                    long[] kept, long[] requested, long[] extras, string failure = null, int calls = 1,
                    Action<Document> setup = null, long[] logical = null)
    {
        var doc = new Document(elements);
        foreach (var e in doc.Live.Values) e.LogicalChildren = new();
        doc.ActualChildren[10] = actual;
        doc.Live[new ElementId(10)].LogicalChildren = Ids(logical ?? actual);
        setup?.Invoke(doc);
        var before = new Dictionary<ElementId, Element>(doc.Live);
        object result = null;
        Exception error = null;
        // The bridge commits when a pack returns and rolls its transaction back when it throws.
        try { result = invoke(doc); }
        catch (Exception ex) { error = ex; doc.Live = before; }
        Check(doc.DeleteCalls == calls, name + ": delete call count " + doc.DeleteCalls + ", error " + error);
        Check(doc.Live.Keys.Select(id => id.Value).Order().SequenceEqual(kept.Order()), name + ": wrong live ids");
        if (failure != null)
        {
            Check(result == null && error != null && error.Message.StartsWith(failure, StringComparison.Ordinal),
                  name + ": unexpected failure: " + error);
            Check(doc.Live.Count == elements.Length, name + ": transaction not restored");
        }
        else
        {
            Check(error == null, name + ": " + error);
            var body = JsonSerializer.SerializeToElement(result);
            Check(body.GetProperty("Status").GetString() == "Deleted", name + ": status");
            Check(body.GetProperty("Count").GetInt32() == requested.Length, name + ": count");
            Check(body.GetProperty("ElementIds").EnumerateArray().Select(e => e.GetInt64()).SequenceEqual(requested), name + ": requested ids");
            Check(body.GetProperty("AlsoDeletedCount").GetInt32() == extras.Length, name + ": extra count");
            var shown = body.GetProperty("AlsoDeleted").EnumerateArray().Select(e => e.GetInt64()).ToArray();
            Check(shown.Length == Math.Min(100, extras.Length) && shown.Distinct().Count() == shown.Length && shown.All(extras.Contains),
                  name + ": extra ids");
        }
        Console.WriteLine("PASS " + name);
    }

    static void Main()
    {
        var wall = new Wall(10, Model);
        var door = new FamilyInstance(11, Model);
        var tag = new FamilyInstance(12, Annotation);
        const string Model11 = "REFUSED: Deletion also removes model element 11";
        const string Unknown = "REFUSED: Could not classify dependent element";
        const string Budget = "REFUSED: The dependency check exceeded";

        Run("floor and category-less sketch", Rendered.One, [new HostObject(10, Model), new Sketch(12, null)], [12], [], [10], [12]);
        Run("sketch with unreadable category", Rendered.One, [new HostObject(10, Model), new Sketch(12, null) { Unreadable = true }], [12], [], [10], [12]);
        Run("door and tag", Rendered.One, [new FamilyInstance(10, Model), tag], [12], [], [10], [12]);
        Run("unlisted hosted door", Rendered.One, [wall, door, tag], [11, 12], [10, 11, 12], [10], [], Model11);
        Run("explicit door and indirect tag", Rendered.Both, [wall, door, tag], [11], [], [10, 11], [12],
            setup: d => { d.ActualChildren[11] = [12]; door.LogicalChildren = Ids(12); });
        Run("model subcategory absent from settings", Rendered.One, [wall, new FamilyInstance(11, HiddenModel)], [11], [10, 11], [10], [], Model11);
        Run("annotation subcategory absent from settings", Rendered.One, [wall, new FamilyInstance(11, HiddenMark)], [11], [], [10], [11]);
        Run("no category catalogue", Rendered.One, [wall, new HostObject(11, HiddenModel)], [11], [10, 11], [10], [], Model11);
        var unknown = new Element(13, null);
        var broken = new FamilyInstance(14, HiddenModel) { Unreadable = true };
        Run("null category cascade", Rendered.One, [wall, unknown], [13], [10, 13], [10], [], Unknown);
        Run("unreadable category cascade", Rendered.One, [wall, broken], [14], [10, 14], [10], [], Unknown);
        Run("unknown unrelated or surviving dependencies", Rendered.One, [wall, unknown, broken], [], [13, 14], [10], [], logical: [13]);
        var extras = Enumerable.Range(1000, 107).Select(i => (long)i).ToArray();
        Run("107 extras count and 100 displayed", Rendered.One, new Element[] { wall }.Concat(extras.Select(i => new Element(i, Annotation))).ToArray(),
            extras, [], [10], extras);
        Run("empty input", Rendered.Empty, [wall], [], [10], [], [], "REFUSED: Give the element ids", calls: 0);
        Run("pinned input", Rendered.One, [new HostObject(10, Model) { Pinned = true }], [], [10], [10], [], "REFUSED: Nothing was deleted", calls: 0);
        Run("unreported requested id", Rendered.One, [wall], [], [10], [10], [], "The deletion result did not contain", setup: d => d.OmitRequested = true);
        Run("requested survivor", Rendered.One, [wall], [], [10], [10], [], "Revit did not delete", setup: d => d.LeaveSurvivor = true);
        Run("post-delete timeout rolls back", Rendered.One, [wall], [], [10], [10], [], Budget, setup: d => d.SlowAfterDelete = true);
        Run("pre-delete timeout", Rendered.One, [wall], [], [10], [10], [], Budget, calls: 0, setup: d => d.SlowBeforeDelete = true);
        Run("non-family model dependent", Rendered.One, [wall, new Element(11, Model)], [11], [10, 11], [10], [], Model11);
        Run("unreported dependency rolls back even for a tag", Rendered.One, [wall, tag], [12], [10, 12], [10], [], Unknown, logical: []);
        Run("invalid category is unknown", Rendered.One, [wall, new Element(11, new Category(CategoryType.Invalid, new ElementId(7)))],
            [11], [10, 11], [10], [], Unknown);
        var inner = new FamilyInstance(11, Model) { Subcomponents = Ids(12) };
        var outer = new FamilyInstance(10, Model) { Subcomponents = Ids(11) };
        Run("recursive nested family ownership", Rendered.One, [outer, inner, new FamilyInstance(12, Model)], [11], [], [10], [11, 12],
            setup: d => d.ActualChildren[11] = [12], logical: []);
        var curtain = new Wall(10, Model) { CurtainGrid = new CurtainGrid { Panels = Ids(11), Mullions = Ids(12) } };
        Run("curtain panels and mullions", Rendered.One, [curtain, door, new FamilyInstance(12, Model)], [11, 12], [], [10], [11, 12], logical: []);
        var stacked = new Wall(10, Model) { Walls = Ids(11) };
        Run("stacked wall members", Rendered.One, [stacked, new Wall(11, Model)], [11], [], [10], [11], logical: []);
        var group = new Group(10, GroupCategory) { Members = Ids(11) };
        Run("model group members", Rendered.One, [group, new Element(11, Model)], [11], [], [10], [11], logical: []);
        var detailGroup = new Group(10, Annotation) { Members = Ids(11) };
        Run("other groups cannot authorize a model dependent", Rendered.One, [detailGroup, door], [11], [10, 11], [10], [], Model11);
        Run("external railing of a stacked component", Rendered.One, [stacked, new Wall(11, Model), new Element(12, Model)],
            [11], [10, 11, 12], [10], [], "REFUSED: Deletion also removes model element 12",
            setup: d => { d.ActualChildren[11] = [12]; d.Live[new ElementId(11)].LogicalChildren = Ids(12); });
        Run("dimension and internal dependent", Rendered.One, [wall, new Element(11, Annotation), new Element(12, new Category(CategoryType.Internal, new ElementId(9)))],
            [11, 12], [], [10], [11, 12]);
    }
}

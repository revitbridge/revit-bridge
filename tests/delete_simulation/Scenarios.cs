using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using Autodesk.Revit.DB.Architecture;
using Autodesk.Revit.DB.Structure;

static class Scenarios
{
    static readonly Category Model = new(CategoryType.Model, new ElementId(1));
    static readonly Category Annotation = new(CategoryType.Annotation, new ElementId(2));
    static Category BuiltIn(BuiltInCategory id) => new(CategoryType.Internal, new ElementId((long)id));
    static readonly Category GroupCategory = BuiltIn(BuiltInCategory.OST_IOSModelGroups);

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
        foreach (var e in doc.Live.Values) e.LogicalChildren = new() { e.Id };
        doc.ActualChildren[10] = actual;
        doc.Live[new ElementId(10)].LogicalChildren.AddRange(Ids(logical ?? actual));
        setup?.Invoke(doc);
        var before = new Dictionary<ElementId, Element>(doc.Live);
        object result = null;
        Exception error = null;
        // The bridge commits when a pack returns and rolls its transaction back when it throws.
        try { result = invoke(doc); }
        catch (Exception ex) { error = ex; doc.Live = before; }
        Check(doc.DeleteCalls == calls, name + ": delete call count " + doc.DeleteCalls + ", error " + error);
        Check(doc.DependentQueries.All(requested.Contains), name + ": queried a non-input's dependents");
        Check(doc.DependentQueries.Distinct().Count() == doc.DependentQueries.Count, name + ": queried an input twice");
        if (calls != 0) Check(requested.All(doc.DependentQueries.Contains), name + ": input not queried before delete");
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
            var expected = extras.Select(id => new ElementId(id))
                .GroupBy(id => doc.UnreadableIds.Contains(id) ? "Unknown" : before[id].GetType().Name)
                .ToDictionary(g => g.Key, g => g.Count());
            var counts = body.GetProperty("AlsoDeletedByClass").EnumerateObject().ToDictionary(p => p.Name, p => p.Value.GetInt32());
            Check(counts.Count == expected.Count && expected.All(p => counts.GetValueOrDefault(p.Key) == p.Value), name + ": class counts");
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
        var tag = new IndependentTag(12, Annotation);
        var floor = new Floor(10, Model);
        var group = new Group(10, GroupCategory) { Members = Ids(11, 12) };
        var stairs = new Stairs(10, Model) { Runs = Ids(11), Landings = Ids(12), Supports = Ids(13) };
        const string Unknown = "REFUSED: Deletion returned an id outside the classified dependency set";
        const string Budget = "REFUSED: The dependency check exceeded";

        Run("floor sketch, model-category sketch lines and dimension", Rendered.One,
            [floor, new Sketch(11, null), new ModelLine(12, Model), new ModelLine(13, Model), new LinearDimension(14, Annotation)],
            [11, 12, 13, 14], [], [10], [11, 12, 13, 14]);
        Run("door with two tags", Rendered.One, [new FamilyInstance(10, Model), tag, new IndependentTag(13, Annotation)],
            [12, 13], [], [10], [12, 13]);
        Run("unlisted hosted door", Rendered.One, [wall, door, tag], [11, 12], [10, 11, 12], [10], [],
            "REFUSED: Deletion also removes FamilyInstance 11", calls: 0);
        Run("unlisted independently placeable dependent refused even if it would survive", Rendered.One, [wall, door],
            [], [10, 11], [10], [], "REFUSED: Deletion also removes FamilyInstance 11", calls: 0, logical: [11]);
        Run("explicit door and indirect tag", Rendered.Both, [wall, door, tag], [11], [], [10, 11], [12],
            setup: d => { d.ActualChildren[11] = [12]; door.LogicalChildren.AddRange(Ids(12)); });
        Run("unknown class with invalid category is reported by class", Rendered.One,
            [wall, new Element(11, new Category(CategoryType.Invalid, new ElementId(7)))], [11], [], [10], [11]);
        Run("internal element with unreadable category", Rendered.One, [floor, new Element(11, null) { Unreadable = true }],
            [11], [], [10], [11]);
        Run("prelisted internal id with no API object counts as Unknown", Rendered.One, [floor, new Element(11, null)],
            [11], [], [10], [11], setup: d => d.UnreadableIds.Add(new ElementId(11)));
        Run("unrelated supplied dependency survives and is absent from counts", Rendered.One, [wall, tag],
            [], [12], [10], [], logical: [12]);
        var extras = Enumerable.Range(1000, 107).Select(i => (long)i).ToArray();
        Run("count all 107 extras by class; show 100 ids", Rendered.One,
            new Element[] { wall }.Concat(extras.Select(i => i % 2 == 0 ? (Element)new ModelLine(i, Model) : new IndependentTag(i, Annotation))).ToArray(),
            extras, [], [10], extras);
        Run("empty input", Rendered.Empty, [wall], [], [10], [], [], "REFUSED: Give the element ids", calls: 0);
        Run("pinned input", Rendered.One, [new Wall(10, Model) { Pinned = true }], [], [10], [10], [], "REFUSED: Nothing was deleted", calls: 0);
        Run("unreported requested id", Rendered.One, [wall], [], [10], [10], [], "The deletion result did not contain", setup: d => d.OmitRequested = true);
        Run("requested survivor", Rendered.One, [wall], [], [10], [10], [], "Revit did not delete", setup: d => d.LeaveSurvivor = true);
        Run("post-delete timeout rolls back", Rendered.One, [wall], [], [10], [10], [], Budget, setup: d => d.SlowAfterDelete = true);
        Run("pre-delete timeout", Rendered.One, [wall], [], [10], [10], [], Budget, calls: 0, setup: d => d.SlowBeforeDelete = true);
        Run("unreported dependency rolls back even for a tag", Rendered.One, [wall, tag], [12], [10, 12], [10], [], Unknown, logical: []);
        var inner = new FamilyInstance(11, Model) { Subcomponents = Ids(12) };
        var outer = new FamilyInstance(10, Model) { Subcomponents = Ids(11) };
        Run("recursive nested family ownership", Rendered.One, [outer, inner, new FamilyInstance(12, Model)],
            [11, 12], [], [10], [11, 12]);
        Run("nested family supercomponent is the input", Rendered.One, [wall, new FamilyInstance(11, Model) { SuperComponent = wall }],
            [11], [], [10], [11]);
        Run("owned id absent from input dependency set rolls back", Rendered.One, [outer, inner],
            [11], [10, 11], [10], [], Unknown, logical: []);
        var curtain = new Wall(10, Model) { CurtainGrid = new CurtainGrid { Panels = Ids(11, 12), Mullions = Ids(14) } };
        Run("curtain panels, a door used as a panel, mullions and grid lines", Rendered.One,
            [curtain, door, new Panel(12, Model), new CurtainGridLine(13, Model), new Mullion(14, Model)],
            [11, 12, 13, 14], [], [10], [11, 12, 13, 14]);
        var curtainHost = new Wall(10, Model) { CurtainGrid = new CurtainGrid() };
        var panelCategory = BuiltIn(BuiltInCategory.OST_CurtainWallPanels);
        var wallPanel = new Wall(11, panelCategory) { StackedWallOwnerId = curtainHost.Id };
        Run("owned wall panel omitted from curtain grid", Rendered.One, [curtainHost, wallPanel],
            [11], [], [10], [11]);
        var otherCurtain = new Wall(20, Model) { CurtainGrid = new CurtainGrid() };
        var foreignPanel = new Wall(11, panelCategory) { StackedWallOwnerId = otherCurtain.Id };
        Run("wall panel belongs to a different curtain", Rendered.One, [curtainHost, foreignPanel, otherCurtain],
            [11], [10, 11, 20], [10], [], "REFUSED: Deletion also removes Wall 11", calls: 0);
        Run("matching owner with a non-panel category", Rendered.One,
            [curtainHost, new Wall(11, Model) { StackedWallOwnerId = curtainHost.Id }],
            [11], [10, 11], [10], [], "REFUSED: Deletion also removes Wall 11", calls: 0);
        Run("panel reported only by another input", Rendered.Both,
            [curtainHost, new Floor(11, Model), new Wall(12, panelCategory) { StackedWallOwnerId = curtainHost.Id }],
            [], [10, 11, 12], [10, 11], [], "REFUSED: Deletion also removes Wall 12", calls: 0,
            setup: d => d.Live[new ElementId(11)].LogicalChildren.AddRange(Ids(12)));
        var stacked = new Wall(10, Model) { Walls = Ids(11) };
        Run("stacked wall members", Rendered.One, [stacked, new Wall(11, Model)], [11], [], [10], [11]);
        Run("internal-category model group's walls and openings", Rendered.One, [group, new Wall(11, Model), new Opening(12, Model), new LinearDimension(13, Annotation)],
            [11, 12, 13], [], [10], [11, 12, 13]);
        var railingGroup = new Group(10, GroupCategory) { Members = Ids(11) };
        var groupedRailing = new Railing(11, Model, 99);
        var groupedHandrail = new ContinuousRail(13, Model, 11);
        Run("model group with railing, baluster and handrail support", Rendered.One,
            [railingGroup, groupedRailing, new FamilyInstance(12, BuiltIn(BuiltInCategory.OST_StairsRailingBaluster)),
             groupedHandrail, new FamilyInstance(14, BuiltIn(BuiltInCategory.OST_RailingSupport)) { Host = groupedHandrail }],
            [11, 12, 13, 14], [], [10], [11, 12, 13, 14]);
        Run("grouped stair, hosted railing and support", Rendered.One,
            [railingGroup, new Stairs(11, Model), new Railing(12, Model, 11), new FamilyInstance(13, BuiltIn(BuiltInCategory.OST_StairsSupports))],
            [11, 12, 13], [], [10], [11, 12, 13]);
        var outside = new Railing(20, Model, 99);
        Run("support in group's report with a different identified host", Rendered.One,
            [railingGroup, groupedRailing, new FamilyInstance(12, BuiltIn(BuiltInCategory.OST_RailingSupport)) { Host = outside }, outside],
            [11, 12], [10, 11, 12, 20], [10], [], "REFUSED: Deletion also removes FamilyInstance 12", calls: 0);
        Run("unowned railing in group dependency report cannot authorize its parts", Rendered.One,
            [new Group(10, GroupCategory), groupedRailing, new FamilyInstance(12, BuiltIn(BuiltInCategory.OST_RailingSupport))],
            [11, 12], [10, 11, 12], [10], [], "REFUSED: Deletion also removes Railing", calls: 0);
        Run("detail group membership cannot authorize deleting a wall", Rendered.One,
            [new Group(10, Annotation) { Members = Ids(11) }, new Wall(11, Model)], [11], [10, 11], [10], [], "REFUSED: Deletion also removes Wall", calls: 0);
        Run("unqueried dependency of a member rolls back", Rendered.One, [group, door, tag],
            [11], [10, 11, 12], [10], [], Unknown, logical: [11],
            setup: d => { d.ActualChildren[11] = [12]; door.LogicalChildren.AddRange(Ids(12)); });
        Run("stair components, hosted railings, rail supports, balusters and element types", Rendered.One,
            [stairs, new StairsRun(11, Model), new StairsLanding(12, Model), new FamilyInstance(13, Model),
             new Railing(14, Model, 10), new Railing(15, Model, 11), new Railing(16, Model, 12),
             new FamilyInstance(17, BuiltIn(BuiltInCategory.OST_StairsRailingBaluster)),
             new FamilyInstance(18, BuiltIn(BuiltInCategory.OST_RailingSupport)), new ElementType(19, null),
             new ElementType(20, Model), new ModelLine(21, Model)],
            [11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21], [], [10], [11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]);
        Run("stair cannot authorize unrelated railing", Rendered.One, [stairs, new Railing(11, Model, 99)],
            [11], [10, 11], [10], [], "REFUSED: Deletion also removes Railing", calls: 0);
        Run("floor input and explicit hosted railing", Rendered.Both, [floor, new Railing(11, Model, 10), new ModelLine(12, Model)],
            [11, 12], [], [10, 11], [12]);
        Run("stair in the batch cannot authorize families only dependent on the other input", Rendered.Both,
            [stairs, new Wall(11, Model), new FamilyInstance(12, BuiltIn(BuiltInCategory.OST_RailingSupport))],
            [], [10, 11, 12], [10, 11], [], "REFUSED: Deletion also removes FamilyInstance", calls: 0,
            setup: d => d.Live[new ElementId(11)].LogicalChildren.AddRange(Ids(12)));
        Run("Revit cannot delete a stair", Rendered.One, [stairs], [], [10], [10], [],
            "REFUSED: Revit would not delete the supplied ids: Simulated refusal for this stair.", setup: d => d.CannotDelete = true);
        foreach (var independent in new Element[] { new Wall(11, null), new Floor(11, null), new RoofBase(11, null), new Ceiling(11, null),
                 new CurtainSystem(11, Model), new WallSweep(11, Model), new SlabEdge(11, Model),
                 new Fascia(11, Model), new Gutter(11, Model), new WallFoundation(11, Model), new MEPCurve(11, Model),
                 new TopographySurface(11, Model), new Toposolid(11, Model), new BuildingPad(11, Model), new BeamSystem(11, Model),
                 door, new Railing(11, Model, 10), new Stairs(11, null), new MultistoryStairs(11, null),
                 new Group(11, GroupCategory), new Opening(11, null), new Rebar(11, null), new RebarInSystem(11, null),
                 new RebarContainer(11, null), new AreaReinforcement(11, null), new PathReinforcement(11, null),
                 new FabricSheet(11, null), new FabricArea(11, null) })
            Run("protect dependent " + independent.GetType().Name, Rendered.One, [floor, independent], [11], [10, 11], [10], [],
                "REFUSED: Deletion also removes " + independent.GetType().Name + " 11", calls: 0);
        Run("loaded family", Rendered.One, [new Family(10, null)], [], [10], [10], [],
            "REFUSED: Nothing was deleted: 1 of 1 supplied ids cannot be deleted by this tool. 10 is a family", calls: 0);
        foreach (var system in new Element[] { new AreaReinforcement(10, Model) { Bars = Ids(11) },
                                               new PathReinforcement(10, Model) { Bars = Ids(11) } })
            Run("system bars of " + system.GetType().Name, Rendered.One, [system, new RebarInSystem(11, Model)],
                [11], [], [10], [11]);
        Run("sheets belonging to fabric area", Rendered.One, [new FabricArea(10, Model) { Sheets = Ids(11) },
            new FabricSheet(11, Model)], [11], [], [10], [11]);
    }
}

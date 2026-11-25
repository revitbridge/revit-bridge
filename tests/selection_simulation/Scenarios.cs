using System;
using System.Linq;

static class Scenarios
{
    static readonly Document Doc = new();
    static View3D View => (View3D)Doc.ActiveView;
    static ElementId Id(long n) => new(n);
    static void Check(bool condition, string name)
    {
        if (!condition) throw new Exception("Failed: " + name);
    }
    static void Selected(params long[] expected)
    {
        var actual = Doc.Selection.GetElementIds().Select(i => i.Value).OrderBy(n => n);
        Check(actual.SequenceEqual(expected.OrderBy(n => n)),
              $"expected selection [{string.Join(",", expected)}], got [{string.Join(",", actual)}]");
    }
    static void Refused(Func<Document, object> run, string reason)
    {
        var before = Doc.Selection.GetElementIds();
        try { run(Doc); }
        catch (InvalidOperationException ex) when (ex.Message.StartsWith("REFUSED: ") && ex.Message.Contains(reason))
        {
            Selected(before.Select(id => id.Value).ToArray());
            return;
        }
        throw new Exception("Expected refusal: " + reason);
    }
    static void Add(Element e) => Doc.Elements.Add(e.Id, e);
    static void Seed()
    {
        var walls = BuiltInCategory.OST_Walls;
        Add(new Level(20, "L1"));
        Add(new View { Id = Id(210), LevelId = 20 });
        Add(new Element(211, "Other view tag") {
            LevelId = 20, ViewSpecific = true, Visible = false,
            Box = new BoundingBoxXYZ { Min = new XYZ(0, 0, 0), Max = new XYZ(1, 1, 1) }
        });
        Add(new Element(212, "Current view tag") { LevelId = 20, ViewSpecific = true });
        Add(new ElementType(30, "Typical", "Basic") { Category = walls });
        for (int i = 0; i < 4; i++)
        {
            Add(new Element(101 + i, "Instance")
            {
                Category = i == 2 ? BuiltInCategory.OST_Doors : walls,
                Box = new BoundingBoxXYZ { Min = new XYZ(i, 0, 0), Max = new XYZ(i+2, 2, 2) },
                LevelId = 20, TypeId = i == 2 ? 0 : 30, Visible = i != 3
            });
        }
    }
    static int Main()
    {
        try { Run(); return 0; }
        catch (Exception ex) { Console.Error.WriteLine(ex); return 1; }
    }
    static void Run()
    {
        Seed();
        Rendered.Replace(Doc); Selected(101, 102);
        Rendered.Add(Doc); Selected(101, 102, 103);
        Rendered.Remove(Doc); Selected(102, 103);
        Refused(Rendered.Stale, "999");
        Rendered.Clear(Doc); Selected();
        Rendered.Clear(Doc); Selected();

        Rendered.Inside(Doc); Selected(101);
        Rendered.Intersect(Doc); Selected(101, 102);
        Refused(Rendered.MixedBox, "either");
        Rendered.Visible(Doc); Selected(101, 102);
        Rendered.Model(Doc); Selected(101, 102, 104);
        Rendered.ModelLevel(Doc); Selected(101, 102, 103, 104);
        Rendered.ViewLevel(Doc); Selected(101, 102, 103, 212);
        Refused(Rendered.MissingType, "Missing");

        Rendered.Section(Doc);
        Check(View.IsSectionBoxActive && View.SectionBox.Min == new XYZ(-1, -1, -1)
              && View.SectionBox.Max == new XYZ(4, 3, 3), "padded section box in feet");
        Refused(Rendered.Flat, "zero extent");
        Check(View.SectionBox.Max == new XYZ(4, 3, 3), "previous section box after refusal");
        Rendered.Reset(Doc); Check(!View.IsSectionBoxActive, "section reset");

        Rendered.Top(Doc);
        Check(View.Orientation.ForwardDirection == -XYZ.BasisZ, "top");
        Rendered.Axon(Doc);
        Check(View.Orientation.ForwardDirection == new XYZ(-1, -1, -1).Normalize(), "northeast");
        Rendered.Vertical(Doc);
        Check(View.Orientation.EyePosition == new XYZ(0, 0, 5) && View.Orientation.UpDirection == XYZ.BasisY
              && View.Orientation.ForwardDirection == -XYZ.BasisZ, "custom vertical camera");
        Refused(Rendered.Coincident, "coincide");
        View.IsLocked = true;
        Refused(Rendered.Top, "unlock");
        View.IsLocked = false;

        View.RightDirection = new XYZ(1, -1, 0).Normalize();
        View.UpDirection = XYZ.BasisZ;
        Doc.Selection.SetElementIds(new[] { Id(101) });
        Rendered.ZoomSelection(Doc);
        double right = Doc.Window.Second.DotProduct(View.RightDirection);
        Check(right > Math.Sqrt(2) && Doc.Window.First.DotProduct(View.RightDirection) < -Math.Sqrt(2),
              "zoom contains both off-diagonal corners");
        Rendered.ZoomPair(Doc);
        Check(Doc.Window.Second.DotProduct(View.RightDirection) > right, "zoom includes second element");
        Selected(101);

        View.IsPerspective = true;
        View.SetOrientation(new(new XYZ(0, 0, 0), XYZ.BasisY, XYZ.BasisZ));
        Refused(Rendered.ZoomPair, "behind the eye");
        View.IsPerspective = false;
        // 5000 is allowed. The next distinct id must be refused before replacing the selection.
        Doc.Selection.SetElementIds(Enumerable.Range(1000, 4999).Select(n => Id(n)).ToList());
        Rendered.AddOne(Doc);
        Check(Doc.Selection.GetElementIds().Count == 5000, "exact limit");
        Refused(Rendered.AddOther, "5001");
        for (int i = 0; i < 4998; i++)
            Add(new Element(1000 + i, "Extra") { Category = BuiltInCategory.OST_Walls, TypeId = 30, LevelId = 20 });
        Refused(Rendered.Model, "5001");
        Console.WriteLine("PASS selection, filters, section box, camera, zoom and limits");
    }
}

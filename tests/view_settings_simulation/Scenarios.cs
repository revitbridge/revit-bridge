using System;
using System.Linq;

static class Scenarios
{
    static readonly Document Doc = new();
    static void Check(bool ok, string context)
    {
        if (!ok) throw new Exception(context);
    }

    static string Refused(Func<Document, object> run, params string[] details)
    {
        var active = Doc.ActiveView;
        var before = Doc.Views.Values.Select(v => (v.DetailLevel, v.DisplayStyle, v.ViewTemplateId)).ToArray();
        try { run(Doc); }
        catch (InvalidOperationException ex) when (ex.Message.StartsWith("REFUSED: "))
        {
            Check(details.All(ex.Message.Contains), ex.Message);
            Check(Doc.ActiveView == active && before.SequenceEqual(Doc.Views.Values.Select(
                v => (v.DetailLevel, v.DisplayStyle, v.ViewTemplateId))), "Refusal changed a view");
            return ex.Message;
        }
        throw new Exception("Missing refusal: " + string.Join(", ", details));
    }

    static void Level(Func<Document, object> run, View target, string previous, string current, ViewDetailLevel expected)
    {
        var active = Doc.ActiveView;
        dynamic result = run(Doc);
        Check(result.View == target.Name && result.PreviousLevel == previous && result.NewLevel == current
              && target.DetailLevel == expected && Doc.ActiveView == active, "Detail level " + current);
    }

    static void Style(Func<Document, object> run, View target, string previous, string current, DisplayStyle expected)
    {
        var active = Doc.ActiveView;
        dynamic result = run(Doc);
        Check(result.View == target.Name && result.PreviousStyle == previous && result.NewStyle == current
              && target.DisplayStyle == expected && Doc.ActiveView == active, "Display style " + current);
    }

    static void Main()
    {
        var plan = Doc.Add("Plan", ViewType.FloorPlan);
        var named = Doc.Add("Named", ViewType.ThreeD);
        var template = Doc.Add("Template", ViewType.FloorPlan);
        template.IsTemplate = true;
        Doc.ActiveView = plan;
        Level(Rendered.Coarse, plan, "medium", "coarse", ViewDetailLevel.Coarse);
        Level(Rendered.Medium, plan, "coarse", "medium", ViewDetailLevel.Medium);
        Level(Rendered.LevelNamed, named, "medium", "fine", ViewDetailLevel.Fine);
        plan.DisplayStyle = DisplayStyle.ShadingWithEdges;
        Style(Rendered.Wireframe, plan, "ShadingWithEdges", "wireframe", DisplayStyle.Wireframe);
        Style(Rendered.HiddenLine, plan, "wireframe", "hidden_line", DisplayStyle.HLR);
        Style(Rendered.Shaded, plan, "hidden_line", "shaded", DisplayStyle.Shading);
        Style(Rendered.ConsistentColors, plan, "shaded", "consistent_colors", DisplayStyle.FlatColors);
        Style(Rendered.StyleNamed, named, "hidden_line", "realistic", DisplayStyle.Realistic);
        Check(plan.DetailLevel == ViewDetailLevel.Medium && plan.DisplayStyle == DisplayStyle.FlatColors, "Wrong target");

        // Each property can be included, excluded, or absent from the template independently.
        plan.ViewTemplateId = Doc.Views.Single(pair => pair.Value == template).Key;
        var detail = new ElementId(BuiltInParameter.VIEW_DETAIL_LEVEL);
        template.Parameters.Add(detail);
        plan.CanEditDetail = false;
        Refused(Rendered.Fine, "template 'Template'", "detail level", "Change it there or detach");
        plan.CanEditDetail = true;
        Refused(Rendered.Fine, "template 'Template'");
        Style(Rendered.Realistic, plan, "consistent_colors", "realistic", DisplayStyle.Realistic);
        template.Excluded.Add(detail);
        foreach (var parameter in new[] { BuiltInParameter.GRAPHIC_DISPLAY_OPTIONS_MODEL,
                     BuiltInParameter.MODEL_GRAPHICS_STYLE, BuiltInParameter.MODEL_GRAPHICS_STYLE_ANON_DRAFT })
        {
            var styleId = new ElementId(parameter);
            template.Parameters.Add(styleId);
            plan.CanEditStyle = false;
            Refused(Rendered.Shaded, "template 'Template'", "display style", "Change it there or detach");
            plan.CanEditStyle = true;
            Refused(Rendered.Shaded, "template 'Template'");
            template.Excluded.Add(styleId);
        }
        Level(Rendered.Fine, plan, "medium", "fine", ViewDetailLevel.Fine);
        Style(Rendered.Shaded, plan, "realistic", "shaded", DisplayStyle.Shading);
        Check(plan.ViewTemplateId != ElementId.InvalidElementId, "Template detached");
        Refused(Rendered.LevelTemplate, "is a view template");
        Refused(Rendered.StyleTemplate, "is a view template");
        Refused(Rendered.LevelInvalid, "coarse", "medium", "fine");
        Refused(Rendered.StyleInvalid, "wireframe", "hidden_line", "shaded", "consistent_colors", "realistic");
        plan.HasDetail = false;
        Refused(Rendered.Coarse, "detail level", "FloorPlan");
        plan.HasDetail = true;
        plan.CanEditDetail = false;
        Refused(Rendered.Coarse, "detail level", "FloorPlan");
        plan.HasStyle = false;
        Refused(Rendered.HiddenLine, "Supported styles: none");
        plan.HasStyle = true;
        plan.CanEditStyle = false;
        Refused(Rendered.HiddenLine, "Supported styles: none");

        foreach (var type in new[] { ViewType.Schedule, ViewType.DrawingSheet, ViewType.Legend })
        {
            Doc.ActiveView = Doc.Add("Unsupported", type);
            Refused(Rendered.Shaded, type.ToString(), "Supported styles: none");
        }
        var drafting = Doc.Add("Draft", ViewType.DraftingView);
        Doc.ActiveView = drafting;
        Style(Rendered.Wireframe, drafting, "hidden_line", "wireframe", DisplayStyle.Wireframe);
        Style(Rendered.HiddenLine, drafting, "wireframe", "hidden_line", DisplayStyle.HLR);
        Refused(Rendered.Shaded, "Supported styles: wireframe, hidden_line.");
        Refused(Rendered.ConsistentColors, "Supported styles: wireframe, hidden_line.");
        Refused(Rendered.Realistic, "Supported styles: wireframe, hidden_line.");

        for (int i = 0; i < 12; i++)
            Doc.Add(i % 2 == 0 ? "Duplicate" : "DUPLICATE", i % 2 == 0 ? ViewType.FloorPlan : ViewType.ThreeD);
        foreach (var run in new Func<Document, object>[] { Rendered.LevelDuplicate, Rendered.StyleDuplicate })
        {
            string msg = Refused(run, "Ambiguous", "Duplicate (FloorPlan)", "DUPLICATE (ThreeD)", "...");
            Check(msg.Count(c => c == '(') == 8, "Unbounded matches");
        }
        foreach (var run in new Func<Document, object>[] { Rendered.LevelMissing, Rendered.StyleMissing })
        {
            string msg = Refused(run, "No view named 'Missing'", "Plan (FloorPlan)", "Named (ThreeD)");
            Check(msg.Count(c => c == '(') == 9, "Unbounded candidates"); // Includes "(up to 8)".
        }
        Doc.ActiveView = null;
        Refused(Rendered.Fine, "No active view");
        Refused(Rendered.Shaded, "No active view");
        Console.WriteLine("PASS view settings");
    }
}

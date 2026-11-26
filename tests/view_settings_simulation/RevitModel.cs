using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;

enum BuiltInParameter { VIEW_DETAIL_LEVEL, MODEL_GRAPHICS_STYLE, MODEL_GRAPHICS_STYLE_ANON_DRAFT, GRAPHIC_DISPLAY_OPTIONS_MODEL }
record ElementId(int Value)
{
    public ElementId(BuiltInParameter parameter) : this((int)parameter) { }
    public static readonly ElementId InvalidElementId = new(-1);
}
enum ViewType { FloorPlan, ThreeD, DraftingView, DrawingSheet, Schedule, Legend }
enum DisplayStyle { Wireframe, HLR, Shading, FlatColors, Realistic, ShadingWithEdges }
enum ViewDetailLevel { Coarse, Medium, Fine }

class View(string name, ViewType type)
{
    public string Name => name;
    public ViewType ViewType => type;
    public bool IsTemplate;
    public ElementId ViewTemplateId = ElementId.InvalidElementId;
    public HashSet<ElementId> Parameters = new(), Excluded = new();
    public ICollection<ElementId> GetTemplateParameterIds() => Parameters;
    public ICollection<ElementId> GetNonControlledTemplateParameterIds() => Excluded;

    public bool HasDetail = true, CanEditDetail = true, HasStyle = true, CanEditStyle = true;
    public bool HasDetailLevel() => HasDetail;
    public bool CanModifyDetailLevel() => CanEditDetail;
    public bool HasDisplayStyle() => HasStyle;
    public bool CanModifyDisplayStyle() => CanEditStyle;
    public ViewDetailLevel DetailLevel { get; set; } = ViewDetailLevel.Medium;
    public DisplayStyle DisplayStyle { get; set; } = DisplayStyle.HLR;
}

class Document
{
    public View ActiveView;
    public Dictionary<ElementId, View> Views = new();
    public View GetElement(ElementId id) => Views.GetValueOrDefault(id);
    public View Add(string name, ViewType type)
    {
        var view = new View(name, type);
        Views.Add(new ElementId(100 + Views.Count), view);
        return view;
    }
}

class FilteredElementCollector(Document document) : IEnumerable<View>, IDisposable
{
    public FilteredElementCollector OfClass(Type type) =>
        type == typeof(View) ? this : throw new ArgumentException(nameof(type));
    public IEnumerator<View> GetEnumerator() => document.Views.Values.GetEnumerator();
    IEnumerator IEnumerable.GetEnumerator() => GetEnumerator();
    public void Dispose() { }
}

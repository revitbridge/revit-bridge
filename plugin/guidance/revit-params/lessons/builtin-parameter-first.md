---
schema_version: 1
id: revit-params/builtin-parameter-first
revision: 1
title: "Built-in parameters: get_Parameter(BuiltInParameter), not LookupParameter(name)"
summary: "`get_Parameter(BuiltInParameter.X)` instead of a translated display name"
stage: [write, fix]
kind: convention
revit: ">=2024 <2028"
order: 20
triggers:
  symbols: [LookupParameter, get_Parameter, BuiltInParameter, GetParameters]
---

## Scenario

`LookupParameter("Unconnected Height")` matches the translated display name: it finds nothing in a Revit running in another language, and a project or shared parameter with the same name can be returned instead. In C#, `element.get_Parameter(BuiltInParameter.X)` (the indexed property `Parameter[BuiltInParameter]`) finds a built-in parameter in any language. Shared parameters: `get_Parameter(Guid)`. Every parameter with a given name: `GetParameters(name)`.

## Wrong

```csharp
Parameter height = wall.LookupParameter("Unconnected Height");   // English Revit only
```

## Right

```csharp
Element wall = new FilteredElementCollector(document).OfClass(typeof(Wall)).FirstElement();
if (wall == null) return new { Found = false };
Parameter height = wall.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM);
Parameter comments = wall.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
double? heightMm = height == null ? (double?)null
    : UnitUtils.ConvertFromInternalUnits(height.AsDouble(), UnitTypeId.Millimeters);
return new { Found = true, HeightMm = heightMm, Comments = comments != null ? comments.AsString() : null };
```

`get_Parameter` returns null when the element does not have that parameter: check before use. Validators and packs name built-in parameters the same way (`WALL_USER_HEIGHT_PARAM`, not "Unconnected Height").

## Versions

`Element.get_Parameter(BuiltInParameter | Definition | Guid)`, `GetParameters`, and the `BuiltInParameter` members `WALL_USER_HEIGHT_PARAM`, `ALL_MODEL_INSTANCE_COMMENTS` and `ALL_MODEL_MARK` are present in 2024.0-2027.2.

## Source

- revit-api-docs data set (Revit 2026): `Element.LookupParameter` remarks: "built-in parameter names are translated, and this method matches the translation"; safer approaches listed there: `GetParameters(String)`, `get_Parameter(Guid)`, `get_Parameter(BuiltInParameter)` "to find a built-in parameter in a language-independent way". `ParameterTypeId.WallUserHeightParam` is labelled "Unconnected Height". Revit 2026 SDK samples read wall height with `get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM).AsDouble()`.
- Metadata of the Revit API assemblies 2024.0, 2024.3, 2025.0, 2025.4, 2026.0, 2026.4, 2027.2 and the installed 2026.5 and 2027.0 (2025.5 not checked). The built-in packs `create_wall` and `modify_wall_height` use `WALL_USER_HEIGHT_PARAM`.

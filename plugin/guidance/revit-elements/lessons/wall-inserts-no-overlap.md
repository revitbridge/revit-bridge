---
schema_version: 1
id: revit-elements/wall-inserts-no-overlap
revision: 1
title: "A door or window must not overlap another insert on its wall: check FindInserts before NewFamilyInstance"
summary: "check every insert from `wall.FindInserts` for overlap before `NewFamilyInstance`: Revit rejects overlapping doors and windows and the transaction fails"
stage: [write, fix]
kind: pitfall
revit: ">=2024 <2028"
order: 20
triggers:
  categories: [OST_Doors, OST_Windows]
  symbols: [NewFamilyInstance, FindInserts, HostObject, FamilyInstance, FamilySymbol]
---

## Scenario

Revit refuses two inserts that overlap on one wall: the second placement fails and takes the transaction with it. Of 20 generated wall add-ins only one checked for this. Compare every planned opening with every existing insert and with each other before the first `NewFamilyInstance`; refuse or move it with a message.

## Wrong

```csharp
// a window at the door's spot: overlap, the transaction fails
document.Create.NewFamilyInstance(mid, windowType, wall, level, StructuralType.NonStructural);
```

## Right

```csharp
Wall wall = document.GetElement(new ElementId(Convert.ToInt64(parameters[0]))) as Wall;
FamilySymbol symbol = document.GetElement(new ElementId(Convert.ToInt64(parameters[1]))) as FamilySymbol;
if (wall == null || symbol == null) return new { Placed = false };
double along = UnitUtils.ConvertToInternalUnits(Convert.ToDouble(parameters[2]), UnitTypeId.Millimeters);
Line axis = (Line)((LocationCurve)wall.Location).Curve;
XYZ start = axis.GetEndPoint(0);
Func<FamilySymbol, double> widthOf = s => { Parameter p = s.get_Parameter(BuiltInParameter.FAMILY_WIDTH_PARAM); return p != null ? p.AsDouble() : 0; };
foreach (ElementId id in wall.FindInserts(true, false, false, false))
{
    FamilyInstance other = document.GetElement(id) as FamilyInstance;
    LocationPoint lp = other != null ? other.Location as LocationPoint : null;
    if (lp == null) continue;
    double t = (lp.Point - start).DotProduct(axis.Direction);
    if (Math.Abs(t - along) < (widthOf(symbol) + widthOf(other.Symbol)) / 2) return new { Placed = false, Overlaps = id.Value };
}
if (!symbol.IsActive) symbol.Activate();
var placed = document.Create.NewFamilyInstance(start + axis.Direction * along, symbol, wall, Autodesk.Revit.DB.Structure.StructuralType.NonStructural);
return new { Placed = true, Id = placed.Id.Value };
```

## Versions

`FindInserts`, the host `NewFamilyInstance` overload and `FAMILY_WIDTH_PARAM` are unchanged in 2024-2027.

## Source

- revit-api-docs data set (Revit 2026): `HostObject.FindInserts(bool, bool, bool, bool)`; `ItemFactoryBase.NewFamilyInstance(XYZ, FamilySymbol, Element host, StructuralType)`.
- dsh evaluation 2026-10-27, WallKit: 19 of 20 builds had no check; Right code passes `check_code`, Revit 2026.5.

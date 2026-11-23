"""``query``: list_model_items where it is needed, the 0.8.2 paths where they suffice, and link_id."""
from __future__ import annotations

import asyncio
import re
import socket

import pytest

from revit_bridge.revit.client import RevitClient
from revit_bridge.snapshot.query import (
    CATEGORY_RE,
    LEVELS_CODE,
    LINK_NOTE,
    ModelItems,
    ModelItemsError,
    RevitQueryError,
    RevitQueryExecutor,
    element_id_or_none,
    in_link,
    link_items,
    parse_model_items,
    run_query,
)

from tests.fake_revit import FakeModel, FakeRevit


def ask_requests(handler, kind, args=None):
    """``run_query`` against a fake add-in; (reply, the requests it got)."""
    async def go():
        async with FakeRevit(handler) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            try:
                return await run_query(RevitQueryExecutor(client), kind, args), fake.requests
            finally:
                await client.disconnect()
    return asyncio.run(go())


def ask(handler, kind, args=None):
    """(reply, the methods the add-in was asked)."""
    reply, requests = ask_requests(handler, kind, args)
    return reply, [r["method"] for r in requests]


NEW = FakeModel()                                     # offers list_model_items
OLD = FakeModel(list_model_items=False)               # capability reply without the method
OLDEST = FakeModel(list_model_items=False, capabilities="unsupported")   # no get_bridge_capabilities at all

LEVELS = [{"id": 311, "name": "F1", "elevation_mm": 0.0}, {"id": 312, "name": "F2", "elevation_mm": 3000.0},
          {"id": 313, "name": "Roof", "elevation_mm": 9000.0}]

# The C# origin/main (0.8.2) sends for these reads, byte for byte.
LEVELS_0_8_2 = ('var levels = new FilteredElementCollector(document)\n    .OfClass(typeof(Level))\n'
                '    .Cast<Level>()\n    .OrderBy(l => l.Elevation)\n'
                '    .Select(l => new { Id = l.Id.Value, Name = l.Name, ElevationMm = Math.Round(l.Elevation * 304.8, 1) })\n'
                '    .ToList();\nreturn levels;')
COUNTS_0_8_2 = ('var result = new List<object>();\n'
                'foreach (var name in new string[] { "OST_Walls", "OST_Doors" }) {\n    try {\n'
                '        var bic = (BuiltInCategory)Enum.Parse(typeof(BuiltInCategory), name);\n'
                '        var n = new FilteredElementCollector(document).OfCategory(bic)\n'
                '            .WhereElementIsNotElementType().GetElementCount();\n'
                '        result.Add(new { Category = name, Count = n });\n    } catch (Exception ex) {\n'
                '        result.Add(new { Category = name, Count = -1, Error = ex.Message });\n    }\n}\nreturn result;')
ELEMENTS_0_8_2 = ('var bic = (BuiltInCategory)Enum.Parse(typeof(BuiltInCategory), "OST_Walls");\n'
                  'var total = new FilteredElementCollector(document).OfCategory(bic)\n'
                  '    .WhereElementIsNotElementType().GetElementCount();\n'
                  'var items = new FilteredElementCollector(document).OfCategory(bic)\n'
                  '    .WhereElementIsNotElementType().Take(5)\n    .Select(e => {\n'
                  '        var t = document.GetElement(e.GetTypeId());\n        var lv = document.GetElement(e.LevelId);\n'
                  '        return new { Id = e.Id.Value, Name = e.Name,\n'
                  '                     Category = e.Category != null ? e.Category.Name : "",\n'
                  '                     Type = t != null ? t.Name : "",\n'
                  '                     Level = lv != null ? lv.Name : null };\n'
                  '    }).ToList();\nreturn new { Total = total, Items = items };')


# -- the reads that keep their 0.8.2 path ------------------------------------------------------

def test_levels_without_link_id_keep_the_0_8_2_request_on_every_add_in():
    for handler in (NEW, OLD, OLDEST):
        reply, requests = ask_requests(handler, "levels")
        assert reply == {"kind": "levels", "items": LEVELS}
        assert [r["method"] for r in requests] == ["send_code_to_revit"]     # no capability probe either
        assert requests[0]["params"]["code"] == LEVELS_0_8_2 == LEVELS_CODE


def test_family_types_without_link_id_keep_one_call_and_its_order_on_every_add_in():
    """Two categories: one get_available_family_types call, the add-in's order kept (a call per
    category would put the walls first)."""
    class Interleaving(FakeModel):
        def __call__(self, request):
            if request["method"] == "get_available_family_types":
                rows = [{"FamilyTypeId": 411, "FamilyName": "Single-Flush", "TypeName": "915 x 2134mm",
                         "Category": "Doors"},
                        {"FamilyTypeId": 401, "FamilyName": "Basic Wall", "TypeName": "Generic - 200mm",
                         "Category": "Walls"}]
                return FakeRevit.ok(request["id"], rows)
            return super().__call__(request)

    for with_method in (True, False):
        reply, requests = ask_requests(Interleaving(list_model_items=with_method), "family_types",
                                       {"categories": ["OST_Walls", "OST_Doors"]})
        assert [r["method"] for r in requests] == ["get_available_family_types"], with_method
        assert requests[0]["params"] == {"categoryList": ["OST_Walls", "OST_Doors"]}
        assert [i["id"] for i in reply["items"]] == [411, 401], with_method
        assert reply == {"kind": "family_types", "categories": ["OST_Walls", "OST_Doors"], "items": [
            {"id": 411, "family": "Single-Flush", "name": "915 x 2134mm", "category": "Doors"},
            {"id": 401, "family": "Basic Wall", "name": "Generic - 200mm", "category": "Walls"}]}


def test_counts_and_elements_without_link_id_send_the_0_8_2_code():
    for handler in (NEW, OLD):
        reply, requests = ask_requests(handler, "counts", {"categories": ["OST_Walls", "OST_Doors"]})
        assert reply == {"kind": "counts", "items": [{"category": "OST_Walls", "count": 12},
                                                     {"category": "OST_Doors", "count": 3}]}
        assert [r["method"] for r in requests] == ["send_code_to_revit"]
        assert requests[0]["params"]["code"] == COUNTS_0_8_2
        reply, requests = ask_requests(handler, "elements", {"category": "OST_Walls", "limit": 5})
        assert reply["total"] == 12 and [r["method"] for r in requests] == ["send_code_to_revit"]
        assert requests[0]["params"]["code"] == ELEMENTS_0_8_2


# -- the capability probe ----------------------------------------------------------------------

def test_the_capability_is_asked_once_per_connection():
    async def go():
        async with FakeRevit(NEW) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            executor = RevitQueryExecutor(client)
            try:
                for _ in range(3):
                    await run_query(executor, "links")
            finally:
                await client.disconnect()
            return [r["method"] for r in fake.requests]
    methods = asyncio.run(go())
    assert methods.count("get_bridge_capabilities") == 1 and methods.count("list_model_items") == 3


def test_a_method_that_was_advertised_but_is_missing_falls_back():
    reply, methods = ask(FakeModel(advertise_only=True), "links")
    assert reply["count"] == 3
    assert methods == ["get_bridge_capabilities", "list_model_items", "send_code_to_revit"]
    reply, _ = ask(FakeModel(advertise_only=True), "levels", {"link_id": 501})
    assert reply["error"] == "unsupported_addin"


# -- list_model_items replies --------------------------------------------------------------------

def test_add_in_warnings_reach_the_reply_and_are_not_dropped():
    for kind in ("links", "type_categories"):
        reply, _ = ask(FakeModel(warnings=["w"]), kind)
        assert reply["warnings"] == ["w"], kind
    reply, _ = ask(FakeModel(warnings=["w"]), "levels", {"link_id": 501})   # the link read's and the levels'
    assert reply["warnings"] == ["w", "w"]
    reply, _ = ask(FakeModel(warnings=["type 7 skipped: boom"]), "family_types",
                   {"categories": ["OST_StructuralFraming"], "link_id": 501})
    assert reply["warnings"] == ["type 7 skipped: boom", "type 7 skipped: boom"]


def test_a_failed_list_model_items_is_mapped_onto_the_query_errors():
    for code in ("no_document", "busy", "timeout", "failed"):
        reply, _ = ask(FakeModel(fail=code), "links")
        assert reply == {"error": "revit_error", "kind": "links", "message": f"{code}: fake failure {code}"}, code
    for code in ("bad_params", "bad_category", "unknown_kind"):
        reply, _ = ask(FakeModel(fail=code), "type_categories")
        assert reply["error"] == "invalid_args" and reply["message"].startswith(code), code
    reply, _ = ask(NEW, "family_types", {"categories": ["OST_Nope"], "link_id": 501})   # Revit has no such category
    assert reply["error"] == "invalid_args" and reply["message"].startswith("bad_category: ")


def test_unexpected_list_model_items_replies_are_errors_not_empty_lists():
    for bad in ("not json", 42, None, [{"Id": 1}], {"Items": []}, {"Success": "yes", "Items": []},
                {"Success": True, "Items": {}}):
        with pytest.raises(RevitQueryError):
            parse_model_items(bad)
    data = parse_model_items('{"Success": true, "Kind": "levels", "Items": [{"Id": 1}, 7], "Total": 2, '
                             '"Truncated": true, "Warnings": ["w"], "Error": null}')
    assert data.rows == [{"Id": 1}] and data.total == 2 and data.truncated is True
    assert data.warnings == ["w", "row 1: not an object (int)"]
    with pytest.raises(ModelItemsError) as info:
        parse_model_items({"Success": False, "Items": [], "Warnings": ["x"],
                           "Error": {"Code": "unknown_link", "Message": "no such link"}})
    assert info.value.code == "unknown_link" and info.value.warnings == ["x"]
    assert info.value.reply("levels", 9) == {"error": "unknown_link", "kind": "levels", "link_id": 9,
                                             "message": "unknown_link: no such link", "warnings": ["x"]}
    with pytest.raises(ModelItemsError) as info:
        parse_model_items({"Success": False, "Error": None})
    assert info.value.code == "failed"


def test_a_truncated_reply_says_so():
    class Truncating(FakeModel):
        def _list_model_items(self, rid, params):
            reply = super()._list_model_items(rid, params)
            reply["result"]["Truncated"] = True
            return reply

    reply, _ = ask(Truncating(), "links")
    assert reply["truncated"] is True and reply["count"] == 3


# -- links and type_categories --------------------------------------------------------------

LINK_ITEMS = [
    {"id": 501, "name": "Structure.rvt : 1 : location <Not Shared>", "type_id": 500, "type_name": "Structure.rvt",
     "loaded": True, "document_title": "Structure", "origin_mm": {"x": 0.0, "y": 0.0, "z": 0.0}, "rotated": False},
    {"id": 502, "name": "Site.rvt : 1 : location <Not Shared>", "type_id": 510, "type_name": "Site.rvt",
     "loaded": True, "document_title": "Site", "origin_mm": {"x": 12000.0, "y": -500.0, "z": 0.0}, "rotated": True},
    {"id": 503, "name": "MEP.rvt : 1 : location <Not Shared>", "type_id": 520, "type_name": "MEP.rvt",
     "loaded": False, "document_title": None, "origin_mm": {"x": 0.0, "y": 0.0, "z": 0.0}, "rotated": False},
]


def test_links_on_both_paths_include_the_unloaded_one():
    reply, methods = ask(NEW, "links")
    assert reply == {"kind": "links", "count": 3, "items": LINK_ITEMS, "warnings": []}
    assert methods == ["get_bridge_capabilities", "list_model_items"]
    reply, requests = ask_requests(OLD, "links")
    assert reply == {"kind": "links", "count": 3, "items": LINK_ITEMS, "warnings": []}
    assert [r["method"] for r in requests] == ["get_bridge_capabilities", "send_code_to_revit"]
    code = requests[-1]["params"]["code"]
    for part in ("typeof(RevitLinkInstance)", "RevitLinkType.IsLoaded(document, typeId)", "try {",
                 "Rotated = !t.IsTranslation", "Success = true"):
        assert part in code, part


def test_link_rows_are_read_one_by_one():
    data = ModelItems(rows=[{"Id": 7, "Name": "A", "Loaded": "yes", "Origin": [1, 2, 3]},
                            {"Name": "no id"},
                            {"Id": "8", "Origin": {"x": 1, "y": None, "z": 0}, "Rotated": True}],
                      warnings=["add-in: link 9 skipped: boom"])
    items, warnings = link_items(data)
    assert [i["id"] for i in items] == [7, 8]
    assert items[0]["loaded"] is None                     # an unknown load state is not "not loaded"
    assert items[0]["origin_mm"] == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert items[1]["origin_mm"] is None and items[1]["rotated"] is True and items[1]["name"] == ""
    assert warnings == ["add-in: link 9 skipped: boom", "link row 1: no id"]


def test_type_categories_on_both_paths():
    reply, methods = ask(NEW, "type_categories")
    assert reply == {"kind": "type_categories", "count": 4, "warnings": [], "items": [
        {"category": "OST_Doors", "display_name": "Doors", "count": 1},
        {"category": "OST_PipeCurves", "display_name": "Pipes", "count": 1},
        {"category": "OST_StructuralFraming", "display_name": "Structural Framing", "count": 1},
        {"category": "OST_Walls", "display_name": "Walls", "count": 2}]}
    assert methods == ["get_bridge_capabilities", "list_model_items"]
    reply, requests = ask_requests(OLD, "type_categories")
    # the fallback lists the categories the old type command can list: no pipes
    assert [i["category"] for i in reply["items"]] == ["OST_Doors", "OST_StructuralFraming", "OST_Walls"]
    code = requests[-1]["params"]["code"]
    for cls in ("typeof(FamilySymbol)", "typeof(WallType)", "typeof(FloorType)", "typeof(RoofType)",
                "typeof(CeilingType)", "typeof(CurtainSystemType)", "StringComparer.OrdinalIgnoreCase"):
        assert cls in code, cls


def test_a_type_category_that_is_not_an_ost_name_is_dropped_with_a_warning():
    model = FakeModel()
    model.model["types"]["OST_Bad.x"] = [{"FamilyTypeId": 1, "TypeName": "x", "Category": "x"}]
    model.model["types"]["OST_IOS_Geo2"] = [{"FamilyTypeId": 2, "TypeName": "y", "Category": "y"}]
    reply, _ = ask(model, "type_categories")
    categories = [i["category"] for i in reply["items"]]
    assert "OST_Bad.x" not in categories and "OST_IOS_Geo2" in categories     # digits and underscores are fine
    assert len(reply["warnings"]) == 1
    assert re.fullmatch(r"type category row \d: 'OST_Bad\.x' is not an OST_\* name", reply["warnings"][0])


def test_new_kinds_check_their_args_and_report_revit_errors():
    reply, methods = ask(NEW, "links", {"limit": 5})
    assert reply["error"] == "invalid_args" and methods == []
    reply, methods = ask(NEW, "links", {"link_id": 501})                # a link has no links
    assert reply["error"] == "invalid_args" and methods == []

    def failing(request):
        if request["method"] == "get_bridge_capabilities":
            return FakeRevit.error(request["id"], -32601, "not found")
        return FakeRevit.code_result(request["id"], None, success=False, error="no document open")

    for kind in ("links", "type_categories"):
        reply, _ = ask(failing, kind)
        assert reply == {"error": "revit_error", "kind": kind, "message": "no document open"}


# -- category names ------------------------------------------------------------------------------

def test_category_names_take_letters_digits_and_underscores_and_nothing_else():
    for good in ("OST_Walls", "OST_IOS_GeoSite", "OST_Fake_2", "OST_3"):
        assert CATEGORY_RE.fullmatch(good), good
    for bad in ("OST_Walls\n", "OST_Walls.x", "OST_Walls x", 'OST_Walls"', "OST_", "Walls", "OST_Wall;s",
                "ost_Walls", " OST_Walls"):
        assert not CATEGORY_RE.fullmatch(bad), bad
    reply, requests = ask_requests(NEW, "counts", {"categories": ["OST_Fake_2"]})
    assert reply["items"] == [{"category": "OST_Fake_2", "count": 0}]
    assert 'new string[] { "OST_Fake_2" }' in requests[0]["params"]["code"]
    for bad in ("OST_Walls\n", "OST_Walls.x", "OST_Walls x", 'OST_Walls"', "OST_Walls\"); File.Delete(\"x"):
        for kind, args in (("counts", {"categories": [bad]}), ("elements", {"category": bad}),
                           ("family_types", {"categories": [bad]})):
            reply, methods = ask(NEW, kind, args)
            # a trailing newline is stripped like any surrounding white space (0.8.2 too); the rest is refused
            if bad == "OST_Walls\n":
                assert "error" not in reply, (kind, bad)
                continue
            assert reply["error"] == "invalid_args" and methods == [], (kind, bad)


# -- link_id ---------------------------------------------------------------------------------

LINK_501 = {"id": 501, "name": "Structure.rvt : 1 : location <Not Shared>", "document_title": "Structure",
            "origin_mm": {"x": 0.0, "y": 0.0, "z": 0.0}, "rotated": False}


def test_levels_and_family_types_of_a_link():
    reply, requests = ask_requests(NEW, "levels", {"link_id": 501})
    assert reply == {"kind": "levels", "items": [{"id": 11, "name": "S-B1", "elevation_mm": -3500.0},
                                                 {"id": 12, "name": "S-F1", "elevation_mm": -100.0}],
                     "link": LINK_501, "coordinates": "link", "note": LINK_NOTE}
    assert [r["params"] for r in requests[1:]] == [{"kind": "links", "ids": [501]},
                                                   {"kind": "levels", "link_id": 501}]

    reply, requests = ask_requests(NEW, "family_types", {"categories": ["OST_StructuralFraming"], "link_id": "501"})
    assert reply["items"] == [{"id": 901, "family": "Concrete-Rectangular Beam", "name": "300x600mm",
                               "category": "Structural Framing"}]
    assert reply["link"] == LINK_501 and reply["coordinates"] == "link"
    assert requests[-1]["params"] == {"kind": "types", "category": "OST_StructuralFraming", "link_id": 501}

    reply, _ = ask(NEW, "levels", {"link_id": 502})
    assert reply["link"]["rotated"] is True and reply["link"]["origin_mm"] == {"x": 12000.0, "y": -500.0, "z": 0.0}


def test_the_link_note_says_coordinates_and_ids_belong_to_the_link():
    assert "own coordinate system" in LINK_NOTE
    assert ("Element, level and type ids read through link_id belong to the linked document: they name no "
            "element of the open model and must not be passed to tools that act on the open model") in LINK_NOTE


def test_type_categories_of_a_link():
    reply, requests = ask_requests(NEW, "type_categories", {"link_id": 501})
    assert reply["items"] == [{"category": "OST_StructuralFraming", "display_name": "Structural Framing", "count": 1}]
    assert reply["link"] == LINK_501 and reply["coordinates"] == "link"
    assert requests[-1]["params"] == {"kind": "type_categories", "link_id": 501}
    reply, methods = ask(OLD, "type_categories", {"link_id": 501})
    assert reply["error"] == "unsupported_addin" and methods == ["get_bridge_capabilities"]


def test_an_add_in_that_ignores_ids_gives_the_same_answer():
    for kind, args in (("levels", {"link_id": 501}), ("counts", {"categories": ["OST_StructuralFraming"],
                                                                "link_id": 501})):
        assert ask(FakeModel(honour_ids=False), kind, args)[0] == ask(NEW, kind, args)[0], kind
    reply, _ = ask(FakeModel(honour_ids=False), "levels", {"link_id": 999})
    assert reply["error"] == "unknown_link"


def test_counts_and_elements_of_a_link_run_in_the_link_document():
    reply, requests = ask_requests(NEW, "counts", {"categories": ["OST_StructuralFraming"], "link_id": 501})
    assert reply == {"kind": "counts", "items": [{"category": "OST_StructuralFraming", "count": 42}],
                     "link": LINK_501, "coordinates": "link", "note": LINK_NOTE}
    assert [r["method"] for r in requests] == ["get_bridge_capabilities", "list_model_items", "send_code_to_revit"]
    code = requests[-1]["params"]["code"]
    assert "l.Id.Value == 501" in code and "FilteredElementCollector(linkDoc)" in code
    assert "FilteredElementCollector(document).OfCategory" not in code

    reply, requests = ask_requests(NEW, "elements", {"category": "OST_StructuralFraming", "link_id": 501,
                                                     "limit": 3})
    assert reply["total"] == 42 and reply["limit"] == 3 and reply["link"] == LINK_501
    assert reply["items"] == [{"id": 9001, "name": "300x600mm", "category": "Structural Framing",
                               "type": "300x600mm", "level": "S-F1"}]
    code = requests[-1]["params"]["code"]
    assert "linkDoc.GetElement(e.GetTypeId())" in code and ".Take(3)" in code


def test_a_link_that_goes_away_between_the_check_and_the_read_is_refused():
    """The link passed the list_model_items check, then the send_code block finds it unloaded or gone."""
    class Racing(FakeModel):
        def __init__(self, link_error):
            super().__init__()
            self.link_error = link_error

        def _send_code(self, rid, code):
            if "LinkError" in code:
                return FakeRevit.code_result(rid, {"LinkError": self.link_error})
            return super()._send_code(rid, code)

    for link_error in ("link_not_loaded", "unknown_link"):
        for kind, args in (("elements", {"category": "OST_StructuralFraming"}),
                           ("counts", {"categories": ["OST_StructuralFraming"]})):
            reply, methods = ask(Racing(link_error), kind, dict(args, link_id=501))
            assert reply["error"] == link_error and reply["kind"] == kind and reply["link_id"] == 501
            assert methods == ["get_bridge_capabilities", "list_model_items", "send_code_to_revit"]


def test_an_unknown_link_id_is_refused_before_any_linked_read():
    for kind, args in (("levels", {}), ("family_types", {"categories": ["OST_Walls"]}),
                       ("elements", {"category": "OST_Walls"}), ("counts", {"categories": ["OST_Walls"]}),
                       ("type_categories", {})):
        reply, requests = ask_requests(NEW, kind, dict(args, link_id=999))
        assert reply == {"error": "unknown_link", "kind": kind, "link_id": 999,
                         "message": "999 is not a link instance of the open model (query kind links lists them)"}
        assert [r["method"] for r in requests] == ["get_bridge_capabilities", "list_model_items"]
        assert requests[-1]["params"] == {"kind": "links", "ids": [999]}


def test_the_add_ins_own_link_refusal_is_kept():
    class Racing(FakeModel):
        """The link was unloaded between the link read and the levels read."""
        def _list_model_items(self, rid, params):
            if params.get("link_id") is not None:
                return self._failed(rid, params["kind"], "link_not_loaded", "The link 501 (S) is not loaded.")
            return super()._list_model_items(rid, params)

    reply, _ = ask(Racing(), "levels", {"link_id": 501})
    assert reply == {"error": "link_not_loaded", "kind": "levels", "link_id": 501,
                     "message": "link_not_loaded: The link 501 (S) is not loaded."}


def test_an_unloaded_link_is_refused_with_its_summary():
    reply, requests = ask_requests(NEW, "levels", {"link_id": 503})
    assert reply["error"] == "link_not_loaded" and reply["link_id"] == 503
    assert reply["link"]["name"].startswith("MEP.rvt") and reply["link"]["document_title"] is None
    assert "not loaded" in reply["message"] and len(requests) == 2


def with_closed_workset_link():
    """A link whose type is loaded but whose document is not there (a closed workset)."""
    model = FakeModel()
    model.model["links"].append({"Id": 504, "Name": "Core.rvt : 1 : location <Not Shared>", "TypeId": 530,
                                 "TypeName": "Core.rvt", "Loaded": True, "DocumentTitle": None,
                                 "Origin": {"X": 0.0, "Y": 0.0, "Z": 0.0}, "Rotated": False})
    return model


def test_a_loaded_link_without_its_document_is_not_readable():
    for kind, args in (("levels", {}), ("counts", {"categories": ["OST_Walls"]})):
        reply, requests = ask_requests(with_closed_workset_link(), kind, dict(args, link_id=504))
        assert reply["error"] == "link_not_loaded" and reply["link"]["document_title"] is None, kind
        assert len(requests) == 2, kind                               # refused before any linked read
    reply, _ = ask(with_closed_workset_link(), "links")
    assert reply["items"][-1]["loaded"] is True and reply["items"][-1]["document_title"] is None


def test_link_id_on_an_add_in_without_list_model_items_is_unsupported():
    for handler in (OLD, OLDEST):
        for kind, args in (("levels", {}), ("family_types", {"categories": ["OST_Walls"]}),
                           ("elements", {"category": "OST_Walls"}), ("counts", {"categories": ["OST_Walls"]}),
                           ("type_categories", {})):
            reply, methods = ask(handler, kind, dict(args, link_id=501))
            assert reply["error"] == "unsupported_addin" and reply["kind"] == kind
            assert reply["method"] == "list_model_items" and "newer add-in" in reply["message"]
            assert methods == ["get_bridge_capabilities"]            # nothing read, no code sent


def test_link_id_with_unknown_capabilities_is_a_revit_error_not_unsupported():
    def busy(request):
        return FakeRevit.error(request["id"], -32000, "Unauthorized: invalid or missing token")

    reply, _ = ask(busy, "levels", {"link_id": 501})
    assert reply["error"] == "revit_error" and "capabilities could not be read (auth:" in reply["message"]


def test_bad_link_ids_never_reach_revit():
    arabic_indic_12 = chr(0x0661) + chr(0x0662)             # digits to str.isdigit, not to the id check
    for bad in (0, -5, "abc", "5x", 1.5, True, [501], {"id": 501}, 2 ** 63, 2 ** 70, 1e30,
                "9999999999999999999", arabic_indic_12):
        reply, methods = ask(NEW, "levels", {"link_id": bad})
        assert reply["error"] == "invalid_args" and "invalid link_id" in reply["message"], bad
        assert methods == [], bad
    reply, _ = ask(NEW, "levels", {"link_id": None})                      # null is "no link"
    assert reply == {"kind": "levels", "items": LEVELS}


def test_the_link_code_renames_only_the_document_it_reads():
    code = in_link("var n = new FilteredElementCollector(document).GetElementCount();\n"
                   "var documented = 1; return document.Title;", 501)
    assert code.count("FilteredElementCollector(document)") == 1          # the prelude's, finding the link
    assert "FilteredElementCollector(linkDoc).GetElementCount()" in code
    assert "var documented = 1; return linkDoc.Title;" in code
    assert '"unknown_link" : "link_not_loaded"' in code
    for bad in ("501; File.Delete", 0, 2 ** 63, 1.5, True):
        with pytest.raises(ValueError):
            in_link("x", bad)


# -- element ids ---------------------------------------------------------------------------------

def test_element_ids_are_whole_numbers_in_int64():
    for good, value in ((1, 1), (312, 312), (312.0, 312), ("312", 312), (2 ** 63 - 1, 2 ** 63 - 1)):
        assert element_id_or_none(good) == value, good
    for bad in (0, -1, 1.9, 0.5, True, False, None, 2 ** 63, 10 ** 30, 1e30, "9999999999999999999", "1e3",
                "12a", float("nan"), float("inf"), [1]):
        assert element_id_or_none(bad) is None, bad


def test_ids_are_checked_before_they_go_into_a_request():
    async def go():
        async with FakeRevit(NEW) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            ex = RevitQueryExecutor(client)
            try:
                with pytest.raises(ValueError, match="at most 200 ids"):
                    await ex.list_model_items("levels", ids=list(range(1, 202)))
                for bad in ([2 ** 63], [10 ** 30], [0], [1.5], [True]):
                    with pytest.raises(ValueError, match="invalid element id"):
                        await ex.list_model_items("levels", ids=bad)
                with pytest.raises(ValueError, match="invalid element id"):
                    await ex.list_model_items("levels", link_id=2 ** 63)
            finally:
                await client.disconnect()
            return fake.requests
    assert asyncio.run(go()) == []


def test_the_fake_refuses_ids_outside_int64_like_the_add_in():
    fake = FakeModel()
    for params in ({"kind": "levels", "ids": [312, 2 ** 63]}, {"kind": "types", "ids": [-2 ** 63 - 1]},
                   {"kind": "levels", "link_id": 2 ** 63}):
        result = fake._list_model_items(1, params)["result"]
        assert result["Success"] is False and result["Error"]["Code"] == "bad_params", params
    assert fake._list_model_items(1, {"kind": "levels", "ids": [312]})["result"]["Items"][0]["Id"] == 312


def test_the_fake_types_follow_the_add_in():
    """FakeModel against ListModelItemsEventHandler.ReadTypes / ModelItemsRequest.Parse (add-in 207df8a):
    ids in their order and of any category, the intersection with category, ids: [] gives no rows,
    no category and no ids is bad_params, ids with type_categories is bad_params."""
    async def go():
        async with FakeRevit(NEW) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            ex = RevitQueryExecutor(client)
            try:
                by_ids = await ex.list_model_items("types", ids=[431, 401, 431, 999])
                both = await ex.list_model_items("types", category="OST_Walls", ids=[431, 401])
                empty = await ex.list_model_items("types", ids=[])
                errors = []
                for kind, params in (("types", {}), ("type_categories", {"ids": [1]}), ("links", {"link_id": 501})):
                    try:
                        await ex.list_model_items(kind, **params)
                    except ModelItemsError as exc:
                        errors.append((exc.code, exc.message))
            finally:
                await client.disconnect()
            return by_ids, both, empty, errors
    by_ids, both, empty, errors = asyncio.run(go())
    assert [r["FamilyTypeId"] for r in by_ids.rows] == [431, 401]
    assert [r["BuiltInCategory"] for r in by_ids.rows] == ["OST_PipeCurves", "OST_Walls"]
    assert [r["FamilyTypeId"] for r in both.rows] == [401]
    assert empty.rows == [] and empty.total == 0
    assert errors == [("bad_params", "kind types needs category or ids."),
                      ("bad_params", "ids is not used with kind type_categories."),
                      ("bad_params", "link_id is not used with kind links.")]


# -- transport failures propagate ---------------------------------------------------------------

def closed_port() -> int:
    """A local port nothing listens on: connecting to it is refused."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_on(port, fn):
    async def go():
        client = RevitClient(host="127.0.0.1", port=port, timeout=2, connect_timeout=2)
        try:
            return await fn(RevitQueryExecutor(client))
        finally:
            await client.disconnect()
    return asyncio.run(go())


LINK_QUERIES = (("levels", {"link_id": 501}), ("family_types", {"categories": ["OST_Walls"], "link_id": 501}),
                ("counts", {"categories": ["OST_Walls"], "link_id": 501}), ("type_categories", {"link_id": 501}),
                ("links", None))


def test_a_refused_connection_propagates_from_link_queries():
    port = closed_port()
    for kind, args in LINK_QUERIES:
        with pytest.raises(OSError):
            run_on(port, lambda ex, kind=kind, args=args: run_query(ex, kind, args))


def test_a_connection_closed_mid_call_propagates_from_link_queries():
    """The add-in closes the connection on the capability probe, or on the link check after it."""
    class ClosingOn(FakeModel):
        def __init__(self, method):
            super().__init__()
            self.method = method

        def __call__(self, request):
            if request["method"] == self.method:
                return FakeRevit.DROP
            return super().__call__(request)

    async def ask_closing(method, kind, args):
        async with FakeRevit(ClosingOn(method)) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            try:
                return await run_query(RevitQueryExecutor(client), kind, args)
            finally:
                await client.disconnect()

    for method in ("get_bridge_capabilities", "list_model_items"):
        for kind, args in LINK_QUERIES:
            with pytest.raises(OSError):
                asyncio.run(ask_closing(method, kind, args))

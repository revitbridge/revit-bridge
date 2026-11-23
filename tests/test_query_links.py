"""``query``: list_model_items when the add-in offers it, the fallbacks when it does not, and link_id."""
from __future__ import annotations

import asyncio
import re

import pytest

from revit_bridge.revit.client import RevitClient
from revit_bridge.snapshot.query import (
    LINK_NOTE,
    ModelItems,
    ModelItemsError,
    RevitQueryError,
    RevitQueryExecutor,
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


# -- the two capability paths -------------------------------------------------------------

def test_levels_take_list_model_items_when_offered_and_the_old_path_otherwise():
    reply, requests = ask_requests(NEW, "levels")
    assert reply == {"kind": "levels", "items": LEVELS}
    assert [r["method"] for r in requests] == ["get_bridge_capabilities", "list_model_items"]
    assert requests[-1]["params"] == {"kind": "levels"}                # all rows: no q, no limit
    for handler in (OLD, OLDEST):
        reply, methods = ask(handler, "levels")
        assert reply == {"kind": "levels", "items": LEVELS}           # the same reply on both paths
        assert methods == ["get_bridge_capabilities", "send_code_to_revit"]


def test_the_capability_is_asked_once_per_connection():
    async def go():
        async with FakeRevit(NEW) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            executor = RevitQueryExecutor(client)
            try:
                for _ in range(3):
                    await run_query(executor, "levels")
            finally:
                await client.disconnect()
            return [r["method"] for r in fake.requests]
    methods = asyncio.run(go())
    assert methods.count("get_bridge_capabilities") == 1 and methods.count("list_model_items") == 3


def test_family_types_ask_per_category_on_the_new_path_and_reach_more_categories():
    reply, requests = ask_requests(NEW, "family_types", {"categories": ["OST_Walls", "OST_PipeCurves"]})
    assert reply == {"kind": "family_types", "categories": ["OST_Walls", "OST_PipeCurves"], "items": [
        {"id": 401, "family": "Basic Wall", "name": "Generic - 200mm", "category": "Walls"},
        {"id": 402, "family": "Basic Wall", "name": "Generic - 300mm", "category": "Walls"},
        {"id": 431, "family": "Pipe Types", "name": "Standard", "category": "Pipes"}]}
    assert [r["params"] for r in requests[1:]] == [{"kind": "types", "category": "OST_Walls"},
                                                   {"kind": "types", "category": "OST_PipeCurves"}]

    reply, methods = ask(OLD, "family_types", {"categories": ["OST_Walls", "OST_PipeCurves"]})
    assert [i["id"] for i in reply["items"]] == [401, 402]             # the old command has no pipes
    assert methods == ["get_bridge_capabilities", "get_available_family_types"]


def test_add_in_warnings_reach_the_reply_and_are_not_dropped():
    reply, _ = ask(FakeModel(warnings=["level 9 skipped: boom"]), "levels")
    assert reply == {"kind": "levels", "items": LEVELS, "warnings": ["level 9 skipped: boom"]}
    reply, _ = ask(FakeModel(warnings=["type 7 skipped: boom"]), "family_types", {"categories": ["OST_Walls"]})
    assert reply["warnings"] == ["type 7 skipped: boom"]
    for kind in ("links", "type_categories"):
        reply, _ = ask(FakeModel(warnings=["w"]), kind)
        assert reply["warnings"] == ["w"], kind
    reply, _ = ask(FakeModel(warnings=["w"]), "levels", {"link_id": 501})   # the link read's and the levels'
    assert reply["warnings"] == ["w", "w"]


def test_a_method_that_was_advertised_but_is_missing_falls_back():
    reply, methods = ask(FakeModel(advertise_only=True), "levels")
    assert reply == {"kind": "levels", "items": LEVELS}
    assert methods == ["get_bridge_capabilities", "list_model_items", "send_code_to_revit"]
    reply, _ = ask(FakeModel(advertise_only=True), "levels", {"link_id": 501})
    assert reply["error"] == "unsupported_addin"


def test_counts_and_elements_without_link_id_never_ask_for_the_capability():
    reply, methods = ask(NEW, "counts", {"categories": ["OST_Walls", "OST_Doors"]})
    assert reply == {"kind": "counts", "items": [{"category": "OST_Walls", "count": 12},
                                                 {"category": "OST_Doors", "count": 3}]}
    assert methods == ["send_code_to_revit"]
    reply, methods = ask(NEW, "elements", {"category": "OST_Walls", "limit": 5})
    assert reply["total"] == 12 and methods == ["send_code_to_revit"]


# -- list_model_items failures -----------------------------------------------------------------

def test_a_failed_list_model_items_is_mapped_onto_the_query_errors():
    for code in ("no_document", "busy", "timeout", "failed"):
        reply, _ = ask(FakeModel(fail=code), "levels")
        assert reply == {"error": "revit_error", "kind": "levels", "message": f"{code}: fake failure {code}"}, code
    for code in ("bad_params", "bad_category", "unknown_kind"):
        reply, _ = ask(FakeModel(fail=code), "type_categories")
        assert reply["error"] == "invalid_args" and reply["message"].startswith(code), code
    reply, _ = ask(NEW, "family_types", {"categories": ["OST_Nope"]})   # an OST_ name Revit does not have
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

    reply, _ = ask(Truncating(), "levels")
    assert reply["truncated"] is True and len(reply["items"]) == 3


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
    model.model["types"]["OST_Bad1"] = [{"FamilyTypeId": 1, "TypeName": "x", "Category": "x"}]
    reply, _ = ask(model, "type_categories")
    assert "OST_Bad1" not in [i["category"] for i in reply["items"]]
    assert len(reply["warnings"]) == 1
    assert re.fullmatch(r"type category row \d: 'OST_Bad1' is not an OST_\* name", reply["warnings"][0])


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
    assert "own coordinate system" in LINK_NOTE

    reply, requests = ask_requests(NEW, "family_types", {"categories": ["OST_StructuralFraming"], "link_id": "501"})
    assert reply["items"] == [{"id": 901, "family": "Concrete-Rectangular Beam", "name": "300x600mm",
                               "category": "Structural Framing"}]
    assert reply["link"] == LINK_501 and reply["coordinates"] == "link"
    assert requests[-1]["params"] == {"kind": "types", "category": "OST_StructuralFraming", "link_id": 501}

    reply, _ = ask(NEW, "levels", {"link_id": 502})
    assert reply["link"]["rotated"] is True and reply["link"]["origin_mm"] == {"x": 12000.0, "y": -500.0, "z": 0.0}


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
    for bad in (0, -5, "abc", "5x", 1.5, True, [501], {"id": 501}, 2 ** 70, arabic_indic_12):
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
    with pytest.raises((TypeError, ValueError)):
        in_link("x", "501; File.Delete")


def test_ids_are_capped_at_200_per_call():
    async def go():
        async with FakeRevit(NEW) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            try:
                with pytest.raises(ValueError, match="at most 200 ids"):
                    await RevitQueryExecutor(client).list_model_items("levels", ids=list(range(201)))
            finally:
                await client.disconnect()
            return fake.requests
    assert asyncio.run(go()) == []


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

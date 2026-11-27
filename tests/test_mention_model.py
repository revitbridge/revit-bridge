"""Elements of the whole model for the picker, and the rough shape of one."""
from __future__ import annotations

import asyncio
import re

import pytest

from revit_bridge import mention_model
from revit_bridge.mention_model import element_preview
from revit_bridge.mentions import mention_candidates
from revit_bridge.revit.client import RevitClient
from revit_bridge.revit.sandbox import review
from revit_bridge.snapshot.query import RevitQueryError, RevitQueryExecutor

from tests.fake_revit import FakeRevit

ROWS = [
    {"id": "101", "name": "Concrete 200mm", "family": "Basic Wall", "category": "OST_Walls", "shown": "Walls"},
    {"id": "102", "name": "Brick 240", "family": "Basic Wall", "category": "OST_Walls", "shown": "Walls"},
    {"id": "201", "name": "M_Single-Flush 0915", "family": "M_Single-Flush", "category": "OST_Doors", "shown": "Doors"},
    {"id": "301", "name": "Standard", "family": "Pipe Types", "category": "OST_PipeCurves", "shown": "Pipes"},
]


def run(handler, fn, *args, **kwargs):
    async def go():
        async with FakeRevit(handler) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            try:
                result = await fn(RevitQueryExecutor(client), *args, **kwargs)
            finally:
                await client.disconnect()
            return result, [r["params"]["code"] for r in fake.requests]
    return asyncio.run(go())


def model(rows=ROWS, *, selected=(), complete=True, index_error=None):
    """A fake add-in: a selection (ids), the model index, and the detail read by ids."""
    def handler(request):
        assert request["method"] == "send_code_to_revit"
        code, parameters = request["params"]["code"], request["params"]["parameters"]
        assert review(code) == (True, []), code
        if "Selection.GetElementIds()" in code:
            ids = list(selected)
            snapshot = {"view": "example", "members": [str(i) for i in ids], "total": len(ids),
                        "page": [str(ids[i]) for i in parameters if i < len(ids)]}
            if "document.GetElement(" not in code:
                return FakeRevit.code_result(request["id"], snapshot)
            items = [dict(row, level=None, length=None) for row in rows if int(row["id"]) in ids]
            return FakeRevit.code_result(request["id"], {**snapshot, "items": items, "warnings": []})
        if "FilteredElementCollector" in code:
            assert parameters == [mention_model.INDEX_BUDGET_MS, mention_model.INDEX_ROWS]
            if index_error:
                return FakeRevit.code_result(request["id"], None, success=False, error=index_error)
            return FakeRevit.code_result(request["id"], {"items": rows, "warnings": [], "complete": complete,
                                                         "document_id": "example-document"})
        ids = [int(n) for n in re.findall(r"new ElementId\((\d+)\)", code)]
        assert 0 < len(ids) <= 50
        items = [dict(row, level="L1", length=None) for number in ids for row in rows if int(row["id"]) == number]
        return FakeRevit.code_result(request["id"], {"view": None, "items": items, "warnings": [],
                                                     "document_id": "example-document"})
    return handler


def candidates(handler, **kwargs):
    return run(handler, mention_candidates, "element", **kwargs)[0]


def test_nothing_selected_searches_the_whole_model():
    out = candidates(model())
    assert out["source"] == "model" and out["kind"] == "element"
    assert [i["id"] for i in out["items"]] == [101, 102, 201, 301] and out["total"] == 4
    assert out["truncated"] is False and "facets_partial" not in out and "warnings" not in out
    assert out["facets"] == [{"category": "OST_Doors", "label": "Doors", "count": 1},
                             {"category": "OST_PipeCurves", "label": "Pipes", "count": 1},
                             {"category": "OST_Walls", "label": "Walls", "count": 2}]
    first = out["items"][0]
    assert first["name"] == "Concrete 200mm" and first["family"] == "Basic Wall" and first["level"] == "L1"
    assert first["document_id"] == "example-document" and first["category"] == "OST_Walls"


@pytest.mark.parametrize("q,ids", [("brick", [102]), ("wall", [101, 102]), ("墙", [101, 102]), ("flush", [201]),
                                   ("door 0915", [201]), ("concrte", [101]), ("zzz", [])])
def test_model_search_ranks_names_families_categories_and_aliases(q, ids):
    out = candidates(model(), q=q)
    assert out["source"] == "model" and sorted(i["id"] for i in out["items"]) == ids and out["total"] == len(ids)


def test_a_whole_number_finds_the_element_with_that_id_first():
    out = candidates(model(), q="301")
    assert out["items"][0]["id"] == 301
    # "240" is not an id in the model: it is still a search term (the type "Brick 240").
    assert [i["id"] for i in candidates(model(), q="240")["items"]] == [102]


def test_a_selection_is_offered_instead_of_the_model_even_when_no_selected_element_matches():
    selected = candidates(model(selected=[102, 201]))
    assert selected["source"] == "selection" and selected["selected"] == 2
    assert sorted(i["id"] for i in selected["items"]) == [102, 201]
    none = candidates(model(selected=[102, 201]), q="pipe")
    assert none["source"] == "selection" and none["items"] == [] and none["selected"] == 2


def test_source_chooses_the_selection_or_the_model():
    assert candidates(model(), source="selection") | {"items": []} == {
        "kind": "element", "source": "selection", "selected": 0, "items": [], "offset": 0, "total": 0,
        "truncated": False, "category_matched": [], "facets": []}
    out, codes = run(model(selected=[102]), mention_candidates, "element", source="model", q="pipe")
    assert out["source"] == "model" and [i["id"] for i in out["items"]] == [301]
    assert not any("Selection.GetElementIds()" in code for code in codes)       # the selection was not read


def test_pages_and_limit():
    rows = [{"id": str(n), "name": f"Type {n:03d}", "family": "Basic Wall", "category": "OST_Walls", "shown": "Walls"}
            for n in range(1, 121)]
    first = candidates(model(rows), limit=200)
    assert len(first["items"]) == 50 and first["total"] == 120 and first["truncated"] is True
    last = candidates(model(rows), offset=100, limit=50)
    assert [i["id"] for i in last["items"]] == list(range(101, 121)) and last["truncated"] is False


def test_an_index_that_stopped_at_its_budget_says_so():
    out = candidates(model(complete=False), q="wall")
    assert out["truncated"] is True and out["facets_partial"] is True
    assert out["warnings"] == [mention_model._INCOMPLETE]


def test_a_failed_read_is_an_error_not_an_empty_list():
    out = candidates(model(index_error="Revit is busy"))
    assert out == {"error": "revit_error", "kind": "element", "message": "Revit is busy"}


# -- the rough shape ---------------------------------------------------------------------------

BOX = {"found": True, "name": "Concrete 200mm", "family": "Basic Wall", "category": "OST_Walls", "shown": "Walls",
       "size": [4000, 200, 3000], "vertices": [0, 0, 0, 4000, 0, 0, 4000, 200, 3000], "triangles": [0, 1, 2],
       "partial": False, "document_id": "example-document"}


def preview(payload, element_id=101, **kwargs):
    def handler(request):
        code = request["params"]["code"]
        assert review(code) == (True, []), code
        assert f"new ElementId({element_id})" in code
        if isinstance(payload, str):
            return FakeRevit.code_result(request["id"], None, success=False, error=payload)
        return FakeRevit.code_result(request["id"], payload)
    return run(handler, element_preview, element_id, **kwargs)


def test_preview_returns_the_mesh_in_millimetres():
    out, codes = preview(BOX)
    assert out == {"kind": "element", "id": 101, "document_id": "example-document", "name": "Concrete 200mm",
                   "family": "Basic Wall", "category": "OST_Walls", "category_label": "Walls", "shape": "mesh",
                   "size_mm": [4000, 200, 3000], "vertices": BOX["vertices"], "triangles": [0, 1, 2],
                   "partial": False, "approximate": True}
    assert len(codes) == 1 and "Transaction" not in codes[0]


def test_preview_budget_is_bounded():
    def parameters(max_triangles):
        seen = []

        def handler(request):
            seen.append(request["params"]["parameters"])
            return FakeRevit.code_result(request["id"], BOX)
        run(handler, element_preview, 101, max_triangles=max_triangles)
        return seen[0]
    assert parameters(10 ** 9) == [mention_model.PREVIEW_TRIANGLES_MAX, mention_model.PREVIEW_BUDGET_MS]
    assert parameters(0) == [1, mention_model.PREVIEW_BUDGET_MS]
    assert parameters("many") == [mention_model.PREVIEW_TRIANGLES, mention_model.PREVIEW_BUDGET_MS]


@pytest.mark.parametrize("change", [{"vertices": [], "triangles": []}, {"triangles": [0, 1, 9]},
                                    {"triangles": [0, 1]}, {"vertices": [0, 0, 0.5, 1, 1, 1, 2, 2, 2]},
                                    {"vertices": None}])
def test_a_mesh_that_cannot_be_drawn_falls_back_to_the_box(change):
    out, _ = preview({**BOX, **change, "partial": True})
    assert out["shape"] == "box" and out["vertices"] == [] and out["triangles"] == []
    assert out["size_mm"] == [4000, 200, 3000] and out["partial"] is False


def test_preview_of_a_missing_element_and_of_a_bad_id():
    assert preview({"found": False})[0] == {"error": "not_found", "kind": "element", "id": 101}
    # Found, but without a bounding box: nothing to draw.
    assert preview({"found": True, "name": "Example", "category": None})[0]["error"] == "not_found"
    for bad in ("abc", -1, 0, None, 1.5):
        out, codes = run(lambda request: pytest.fail("reached Revit"), element_preview, bad)
        assert out["error"] == "invalid_args" and codes == []


def test_preview_raises_when_the_model_could_not_be_read():
    with pytest.raises(RevitQueryError, match="Revit is busy"):
        preview("Revit is busy")

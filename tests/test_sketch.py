"""A sketch on a capture of a plan view: the frame, pixels to millimetres, snapping, the text."""
from __future__ import annotations

import asyncio
import base64
import random

import pytest

from revit_bridge import sketch
from revit_bridge.evidence import Ledger
from revit_bridge.revit.client import RevitClient
from revit_bridge.revit.sandbox import review
from revit_bridge.snapshot.query import RevitQueryError, RevitQueryExecutor

from tests.fake_revit import FakeRevit

FEET = 304.8
# The view shows x from -6000 to 10000 mm and y from -2000 to 6000 mm: 10 mm per pixel at 1600 x 800.
RAW = {"document_id": "example-document", "view_id": "32", "view_uid": "example-view", "view_name": "L1",
       "view_type": "FloorPlan", "level": "L1", "elevation": 0.0,
       "c0": [-6000 / FEET, -2000 / FEET, 0.0], "c1": [10000 / FEET, 6000 / FEET, 0.0],
       "right": [1.0, 0.0, 0.0], "up": [0.0, 1.0, 0.0], "direction": [0.0, 0.0, 1.0]}
FRAME = sketch._frame(RAW)
# A wall that stops at (-3000, 5000), and a long wall along x = 0.
WALLS = [{"id": "11", "straight": True, "a": [-3000 / FEET, 1000 / FEET], "b": [-3000 / FEET, 5000 / FEET]},
         {"id": "12", "straight": True, "a": [0.0, -1000 / FEET], "b": [0.0, 6000 / FEET]},
         {"id": "13", "straight": False, "a": [5000 / FEET, 0.0], "b": [7000 / FEET, 2000 / FEET]}]
PNG = base64.b64encode(bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10]) + bytes(16)).decode()   # only the signature is checked


def run(handler, fn, *args, **kwargs):
    async def go():
        async with FakeRevit(handler) as fake:
            client = RevitClient(host="127.0.0.1", port=fake.port, timeout=2)
            try:
                return await fn(client, *args, **kwargs), fake.requests
            finally:
                await client.disconnect()
    return asyncio.run(go())


def addin(*, frames=None, walls=WALLS, complete=True, walls_error=None):
    frames = list(frames or [RAW])

    def handler(request):
        method, params = request["method"], request.get("params") or {}
        if method == "get_bridge_capabilities":
            return FakeRevit.ok(request["id"], {"protocol": 2, "methods": ["export_view_image_in_revit", "send_code_to_revit"]})
        if method == "export_view_image_in_revit":
            return FakeRevit.ok(request["id"], {"png_base64": PNG, "width": 1600, "height": 800, "view_name": "L1"})
        code = params["code"]
        assert review(code) == (True, []), code
        if "GetZoomCorners" in code:
            return FakeRevit.code_result(request["id"], frames.pop(0) if len(frames) > 1 else frames[0])
        assert params["parameters"] == ["example-view", "example-document"]
        if walls_error:
            return FakeRevit.code_result(request["id"], None, success=False, error=walls_error)
        return FakeRevit.code_result(request["id"], {"walls": walls, "complete": complete})
    return handler


def resolve(strokes, **kwargs):
    async def go(client):
        return await sketch.resolve_sketch(RevitQueryExecutor(client), FRAME, 1600, 800, strokes)
    return run(addin(**kwargs), go)[0]


def test_the_frame_is_the_view_rectangle_in_millimetres():
    assert FRAME == {"document_id": "example-document", "view_id": 32, "view_uid": "example-view", "view_name": "L1",
                     "view_type": "FloorPlan", "level": "L1", "elevation_mm": 0.0,
                     "u": [-6000.0, 10000.0], "v": [-2000.0, 6000.0], "right": [1.0, 0.0], "up": [0.0, 1.0]}
    # The corners in either order, and a view turned by a quarter.
    swapped = sketch._frame({**RAW, "c0": RAW["c1"], "c1": RAW["c0"]})
    assert swapped["u"] == FRAME["u"] and swapped["v"] == FRAME["v"]
    turned = sketch._frame({**RAW, "right": [0.0, 1.0, 0.0], "up": [-1.0, 0.0, 0.0]})
    assert turned["u"] == [-2000.0, 6000.0] and turned["v"] == [-10000.0, 6000.0]


@pytest.mark.parametrize("change", [{"view_type": "ThreeD"}, {"view_type": "Section"}, {"direction": [1.0, 0.0, 0.0]},
                                    {"c0": [0, 0]}, {"view_id": "x"}, {"c1": RAW["c0"]}])
def test_a_view_that_is_not_a_plan_or_a_bad_reading_is_refused(change):
    with pytest.raises(RevitQueryError):
        sketch._frame({**RAW, **change})


def test_pixels_become_model_millimetres():
    lines = sketch.sketch_lines(FRAME, 1600, 800, [[[0, 0], [1600, 800]], [[600, 600], [600, 100]]])
    assert [[tuple(round(v) for v in p) for p in line] for line in lines] == [
        [(-6000, 6000), (10000, -2000)], [(0, 0), (0, 5000)]]


def test_a_shaky_line_drawn_over_twice_is_one_segment_in_the_direction_drawn():
    random.seed(7)
    there = [[300 + i * 3, 100 + random.uniform(-3, 3)] for i in range(100)]
    back = [[597 - i * 3, 103 + random.uniform(-3, 3)] for i in range(100)]
    (line,) = sketch.sketch_lines(FRAME, 1600, 800, [there + back[:60]])
    assert len(line) == 2
    (x0, y0), (x1, y1) = line
    assert abs(x0 - -3000) < 60 and abs(x1 - -30) < 60 and abs(y0 - 5000) < 60 and abs(y1 - 5000) < 60
    assert x0 < x1                                               # it began on the left


def test_a_bent_stroke_is_a_polyline():
    stroke = [[100 + i * 4, 400] for i in range(60)] + [[336, 400 + i * 4] for i in range(1, 50)]
    (line,) = sketch.sketch_lines(FRAME, 1600, 800, [stroke])
    assert [tuple(round(v) for v in p) for p in line] == [(-5000, 2000), (-2640, 2000), (-2640, 40)]


@pytest.mark.parametrize("strokes,why", [
    ([], "1 to 8"), ([[[0, 0], [10, 10]]] * 9, "1 to 8"), ("no", "1 to 8"), ([[[0, 0]]], "list of"),
    ([[[0, 0], [1, "2"]]], "list of"), ([[[0, 0], [float("nan"), 2]]], "list of"), ([[[0, 0], [5000, 0]]], "leaves"),
    ([[[100, 100], [102, 101]]], "too short")])
def test_strokes_that_are_not_lines_are_refused(strokes, why):
    with pytest.raises(ValueError, match=why):
        sketch.sketch_lines(FRAME, 1600, 800, strokes)
    assert resolve(strokes)["error"] == "invalid_args"


def test_a_line_from_a_wall_end_to_another_wall_snaps_and_meets_it_squarely():
    # Drawn from near the end of wall 11 (-3000, 5000) to near wall 12 (x = 0), a little off and sloping.
    out = resolve([[[310, 108], [590, 90]]])
    assert out["lines"] == [{"line": 1, "length_mm": 3000, "pixels": [[300.0, 100.0], [600.0, 100.0]], "points": [
        {"x": -3000, "y": 5000, "snap": {"kind": "wall_end", "wall_id": 11}},
        {"x": 0, "y": 5000, "snap": {"kind": "on_wall", "wall_id": 12}}]}]
    assert sketch.pixel_of(FRAME, 1600, 800, -6000, 6000) == [0.0, 0.0]
    assert sketch.pixel_of(FRAME, 1600, 800, 10000, -2000) == [1600.0, 800.0]
    assert out["view_name"] == "L1" and out["level"] == "L1" and out["tolerance_mm"] == 250 and out["warnings"] == []


def test_a_free_line_nearly_along_an_axis_is_made_parallel_and_a_sloping_one_is_kept():
    flat = resolve([[[900, 600], [1300, 620]]])["lines"][0]["points"]
    assert flat == [{"x": 3000, "y": 0}, {"x": 7000, "y": 0}]
    sloping = resolve([[[900, 600], [1300, 450]]])["lines"][0]["points"]
    assert sloping == [{"x": 3000, "y": 0}, {"x": 7000, "y": 1500}]


def test_a_curved_wall_gives_its_ends_but_no_point_on_it():
    out = resolve([[[1100, 590], [1100, 300]]])           # starts at the curved wall's end (5000, 0)
    first, second = out["lines"][0]["points"]
    assert first == {"x": 5000, "y": 0, "snap": {"kind": "wall_end", "wall_id": 13}} and "snap" not in second


def test_an_incomplete_wall_read_is_said_and_a_view_that_is_gone_raises():
    assert resolve([[[900, 600], [1300, 600]]], complete=False)["warnings"] == [
        "The view has more walls than one read covers; some ends may not have snapped."]
    with pytest.raises(RevitQueryError, match="no longer open"):
        resolve([[[900, 600], [1300, 600]]], walls_error="The view of the sketch is no longer open; capture the view again.")


def test_the_block_names_every_line_and_how_to_use_it():
    out = resolve([[[310, 108], [590, 90]], [[100 + i * 4, 400] for i in range(60)] + [[336, 400 + i * 4] for i in range(1, 50)]])
    text = sketch.sketch_block(out)
    assert text == ("Designer's sketch (drawn by the designer on a capture of the plan view \"L1\", level \"L1\"; the host "
                    "converted it to model coordinates, x and y in millimetres): line 1 from (-3000, 5000) [the end of wall "
                    "id 11] to (0, 5000) [on wall id 12], length 3000 mm; polyline 2 (2 segments) from (-5000, 2000) to "
                    "(-2640, 2000) to (-2640, 40), length 4320 mm. These coordinates are the designer's own input: use them "
                    "as given, with source designer and evidence naming the line, e.g. \"sketch line 1 start\". A level name is "
                    "not: read it from Revit (source tool).")
    assert sketch.sketch_block({"lines": []}) == ""
    marked = sketch.sketch_block({**out, "view_name": 'Plan "A"\nL1', "level": None})
    assert "plan view \"Plan 'A' L1\";" in marked


def test_capture_for_sketch_returns_the_picture_with_its_frame(tmp_path):
    out, requests = run(addin(), sketch.capture_for_sketch, 1600, ledger=Ledger(tmp_path / "ledger.jsonl"))
    assert out["error"] is None and out["frame"] == FRAME and out["png_base64"] == PNG and out["width"] == 1600
    codes = [r["params"]["code"] for r in requests if r["method"] == "send_code_to_revit"]
    assert len(codes) == 2 and all("GetZoomCorners" in code for code in codes)        # before and after the export


def test_capture_for_sketch_refuses_a_view_that_moved_or_is_not_a_plan(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    moved = {**RAW, "c1": [10500 / FEET, 6000 / FEET, 0.0]}
    out, _ = run(addin(frames=[RAW, moved]), sketch.capture_for_sketch, 1600, ledger=ledger)
    assert out["error"] == "view_changed" and "frame" not in out and out.get("png_base64") is None
    out, requests = run(addin(frames=[{**RAW, "view_type": "ThreeD"}]), sketch.capture_for_sketch, 1600, ledger=ledger)
    assert out["error"] == "unsupported_view" and "plan view" in out["hint"]
    assert not any(r["method"] == "export_view_image_in_revit" for r in requests)    # nothing was exported

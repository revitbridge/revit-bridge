"""Sketches on plan and orthographic 3D captures: frames, projection, snapping and text."""
from __future__ import annotations

import asyncio
import base64
import math
import random
import time

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
       "view_type": "FloorPlan", "level": "L1", "elevation": 0.0, "is_3d": False, "is_perspective": False, "levels": [],
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


def addin(*, frames=None, walls=WALLS, grids=(), columns=(), framing=(), legacy=False, complete=True, walls_error=None,
          expected_level=""):
    frames = list(frames or [RAW])

    def handler(request):
        method, params = request["method"], request.get("params") or {}
        if method == "get_bridge_capabilities":
            return FakeRevit.ok(request["id"], {"protocol": 2, "methods": ["export_view_image_in_revit", "send_code_to_revit"]})
        if method == "export_view_image_in_revit":
            return FakeRevit.ok(request["id"], {"png_base64": PNG, "width": 1600, "height": 800, "view_name": frames[0]["view_name"]})
        code = params["code"]
        assert review(code) == (True, []), code
        if "GetZoomCorners" in code:
            return FakeRevit.code_result(request["id"], frames.pop(0) if len(frames) > 1 else frames[0])
        # Consume the level selector on a copy so FakeRevit retains the original request.
        params = dict(params, parameters=params["parameters"].copy())
        level = params["parameters"].pop()
        assert level == expected_level
        assert params["parameters"] == ["example-view", "example-document"]
        if walls_error:
            return FakeRevit.code_result(request["id"], None, success=False, error=walls_error)
        result = {"walls": walls, "complete": complete}
        if not legacy:
            result.update(grids=grids, columns=columns, framing=framing)
        if level:
            for group in ("walls", "columns", "framing"):
                result[group] = [row for row in result[group] if row["level"] == level]
        return FakeRevit.code_result(request["id"], result)
    return handler


def resolve(strokes, *, frame=FRAME, level=None, **kwargs):
    async def go(client):
        return await sketch.resolve_sketch(RevitQueryExecutor(client), frame, 1600, 800, strokes, level)
    return run(addin(**kwargs), go)[0]


def test_the_frame_is_the_view_rectangle_in_millimetres():
    assert FRAME == {"document_id": "example-document", "view_id": 32, "view_uid": "example-view", "view_name": "L1",
                     "view_type": "FloorPlan", "kind": "plan", "level": "L1", "elevation_mm": 0.0,
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
                    "(-2640, 2000) to (-2640, 40), length 4320 mm. For walls along a polyline use create_walls_by_points "
                    "with points as \"x,y;x,y;...\" in millimetres. These coordinates are the designer's own input: use them "
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
    perspective = {**RAW, "view_type": "ThreeD", "is_3d": True, "is_perspective": True}
    out, requests = run(addin(frames=[perspective]), sketch.capture_for_sketch, 1600, ledger=ledger)
    assert out["error"] == "unsupported_view" and "plan view" in out["hint"]
    assert not any(r["method"] == "export_view_image_in_revit" for r in requests)    # nothing was exported


def drawn(*points):
    return [sketch.pixel_of(FRAME, 1600, 800, x, y) for x, y in points]


# Only the extensions of these two grid segments cross, at (0, 0).
GRIDS = [{"name": "A", "a": [4, 0], "b": [7, 0]}, {"name": "1", "a": [0, 4], "b": [0, 7]}]
COLUMN = {"id": "123", "point": [0, 0]}
BEAM = {"id": "456", "a": [0, 0], "b": [5, 5]}


@pytest.mark.parametrize("targets,hit,miss,snap,label", [
    ({"grids": GRIDS}, (140, 140), (260, 260), {"kind": "grid_intersection", "grids": ["1", "A"]},
     "[the crossing of grids 1 and A]"),
    ({"columns": [COLUMN]}, (0, 240), (0, 260), {"kind": "column", "element_id": 123}, "[column id 123]"),
    ({"framing": [BEAM]}, (0, 240), (0, 260), {"kind": "element_end", "element_id": 456}, "[the end of element id 456]"),
    ({"grids": GRIDS[:1]}, (0, 240), (0, 260), {"kind": "on_grid", "grid": "A"}, "[on grid A]"),
])
def test_snap_tolerance_and_block_text_for_new_targets(targets, hit, miss, snap, label):
    out = resolve([drawn(hit, (3030, 2410)), drawn(miss, (3030, 2410))], walls=[], **targets)
    assert out["lines"][0]["points"][0] == {"x": 0, "y": 0, "snap": snap}
    assert "snap" not in out["lines"][1]["points"][0]
    assert "(0, 0) " + label in sketch.sketch_block(out)


def test_points_beat_lines_and_the_nearest_point_wins_with_wall_ends_first_on_ties():
    wall = {"id": "17", "a": [0, 0], "b": [-2, -3], "straight": True}
    column = {"id": "18", "point": [210 / FEET, 0]}
    out = resolve([drawn((20, 0), (3030, 2410)), drawn((190, 0), (3030, 2410))],
                  walls=[wall], grids=GRIDS, columns=[column])
    assert [line["points"][0]["snap"] for line in out["lines"]] == [
        {"kind": "wall_end", "wall_id": 17}, {"kind": "column", "element_id": 18}]


def test_parallel_grids_have_no_crossing_and_the_nearest_line_wins():
    parallel = [GRIDS[0], {"name": "B", "a": [4, 200 / FEET], "b": [7, 200 / FEET]}]
    wall = {"id": "19", "a": [-5, 100 / FEET], "b": [5, 100 / FEET], "straight": True}
    out = resolve([drawn((0, 180), (3030, 2410)), drawn((0, 90), (3030, 2410))], walls=[wall], grids=parallel)
    assert [line["points"][0] for line in out["lines"]] == [
        {"x": 0, "y": 200, "snap": {"kind": "on_grid", "grid": "B"}},
        {"x": 0, "y": 100, "snap": {"kind": "on_wall", "wall_id": 19}}]


@pytest.mark.parametrize("target,snap", [
    ({"walls": [], "grids": GRIDS[:1]}, {"kind": "on_grid", "grid": "A"}),
    ({"walls": [{"id": "20", "a": [-6, 0], "b": [6, 0], "straight": True}]}, {"kind": "on_wall", "wall_id": 20}),
])
def test_a_segment_from_a_column_meets_a_line_target_at_a_right_angle(target, snap):
    out = resolve([drawn((0, 2010), (180, 30))], columns=[{"id": "21", "point": [0, 2000 / FEET]}], **target)
    assert out["lines"][0]["points"] == [
        {"x": 0, "y": 2000, "snap": {"kind": "column", "element_id": 21}}, {"x": 0, "y": 0, "snap": snap}]


def test_one_read_for_all_strokes_and_wall_only_replies_still_work():
    strokes = [[[310, 108], [590, 90]], drawn((2000, 500), (4000, 1500))]

    async def go(client):
        return await sketch.resolve_sketch(RevitQueryExecutor(client), FRAME, 1600, 800, strokes)

    out, requests = run(addin(complete=False), go)
    assert len([r for r in requests if r["method"] == "send_code_to_revit"]) == 1
    assert out == resolve(strokes, legacy=True, complete=False)
    assert out["warnings"]


def test_jittered_box_closes_once_and_its_length_includes_the_fourth_edge():
    stroke = drawn((1000, 1000), (4010, 1100), (3980, 3040), (920, 3000), (1060, 1050))
    out = resolve([stroke], walls=[])
    assert out["lines"] == [{
        "line": 1, "closed": True, "length_mm": 2 * (3010 + 2040),
        "points": [{"x": 1000, "y": 1000}, {"x": 4010, "y": 1000}, {"x": 4010, "y": 3040}, {"x": 1000, "y": 3040}],
        "pixels": drawn((1000, 1000), (4010, 1000), (4010, 3040), (1000, 3040)),
    }]
    text = sketch.sketch_block(out)
    assert ("closed polyline 1 (4 segments) through (1000, 1000) to (4010, 1000) to (4010, 3040) to "
            "(1000, 3040), length 10100 mm. For walls along a polyline use create_walls_by_points with points as "
            "\"x,y;x,y;...\" in millimetres, and closed true for a closed line. These coordinates") in text


def test_closure_uses_the_drawn_distance_including_the_tolerance_boundary():
    for gap, closed in ((250, True), (251, False)):
        stroke = drawn((0, 0), (2300, 0), (1100, 2000), (0, gap))
        line, = resolve([stroke], walls=[])["lines"]
        assert ("closed" in line) == closed
        assert len(line["points"]) == (3 if closed else 4)


def test_an_open_l_is_squared_and_an_oblique_bend_is_left_as_drawn():
    out = resolve([drawn((0, 0), (2000, 100), (2100, 1600)), drawn((0, 0), (2000, 0), (3000, 1732))], walls=[])
    square, oblique = out["lines"]
    assert square["points"] == [{"x": 0, "y": 0}, {"x": 2000, "y": 0}, {"x": 2000, "y": 1600}]
    assert oblique["points"] == [{"x": 0, "y": 0}, {"x": 2000, "y": 0}, {"x": 3000, "y": 1732}]
    assert "closed" not in square and "closed" not in oblique
    assert sketch.sketch_block(out).count("For walls along a polyline") == 1


def test_squaring_preserves_snapped_corners_and_leaves_a_fixed_end_in_place():
    column = {"id": "26", "point": [2000 / FEET, 100 / FEET]}
    out = resolve([drawn((0, 0), (1980, 80), (2100, 1600)), drawn((0, 2000), (2100, 2000), (1980, 80))],
                  walls=[], columns=[column])
    corner = {"x": 2000, "y": 100, "snap": {"kind": "column", "element_id": 26}}
    a, b, c = out["lines"][0]["points"]
    assert b == corner and a == {"x": 0, "y": 0}
    # Coordinates are given to a thousandth of a millimetre: over sides of about two metres the
    # dot product of a right angle may be off by a few square millimetres (about 1e-6 radians).
    assert (b["x"] - a["x"]) * (c["x"] - b["x"]) + (b["y"] - a["y"]) * (c["y"] - b["y"]) == pytest.approx(0, abs=5)
    assert out["lines"][1]["points"] == [{"x": 0, "y": 2000}, {"x": 2100, "y": 2000}, corner]


def test_rectangle_is_square_in_the_axes_of_a_rotated_view():
    angle = math.radians(28)
    frame = {**FRAME, "right": [math.cos(angle), math.sin(angle)], "up": [-math.sin(angle), math.cos(angle)]}
    stroke = [[400, 200], [710, 208], [703, 455], [391, 450], [407, 203]]

    async def go(client):
        return await sketch.resolve_sketch(RevitQueryExecutor(client), frame, 1600, 800, [stroke])

    out, _ = run(addin(walls=[]), go)
    box, = out["lines"]
    assert box["closed"] and box["pixels"] == [[400, 200], [710, 200], [710, 455], [400, 455]]
    assert box["length_mm"] == 2 * (3100 + 2550)
    pts = [(p["x"], p["y"]) for p in box["points"]]
    for i, (x, y) in enumerate(pts):
        a, b = pts[i - 1], pts[(i + 1) % 4]
        assert (a[0] - x) * (b[0] - x) + (a[1] - y) * (b[1] - y) == pytest.approx(0, abs=5)     # thousandths, as above


def test_a_fixed_last_corner_sets_the_height_of_the_rectangle():
    stroke = drawn((1000, 1000), (4000, 1100), (3980, 3040), (920, 3000), (1060, 1050))
    column = {"id": "27", "point": [1000 / FEET, 3000 / FEET]}
    box, = resolve([stroke], walls=[], columns=[column])["lines"]
    assert box["closed"] and box["length_mm"] == 10000
    assert box["points"] == [{"x": 1000, "y": 1000}, {"x": 4000, "y": 1000}, {"x": 4000, "y": 3000},
                             {"x": 1000, "y": 3000, "snap": {"kind": "column", "element_id": 27}}]


def test_single_segments_have_no_polyline_guidance():
    out = resolve([drawn((0, 0), (2000, 500)), drawn((500, 500), (4000, 1500))], walls=[])
    assert "create_walls_by_points" not in sketch.sketch_block(out)


def test_a_short_line_along_the_wall_it_starts_from_keeps_its_length():
    # From the end of wall 11 (-3000, 5000) back along that wall: nothing to meet at a right angle.
    out = resolve([[[300, 100], [301, 140]]])
    first, second = out["lines"][0]["points"]
    assert first["snap"] == {"kind": "wall_end", "wall_id": 11} and second["snap"] == {"kind": "on_wall", "wall_id": 11}
    assert (first["x"], first["y"]) == (-3000, 5000) and (second["x"], second["y"]) == (-3000, 4600)
    assert out["lines"][0]["length_mm"] == 400


def test_a_box_drawn_between_pixels_comes_out_on_whole_millimetres():
    # Real captures give fractional pixels: the squared corners must not carry long decimals.
    box = [[400.37, 300.21], [700.64, 303.4], [698.2, 500.77], [403.9, 498.3], [401.1, 303.0]]
    (line,) = resolve([box])["lines"]
    assert line.get("closed") is True and len(line["points"]) == 4
    for point in line["points"]:
        assert type(point["x"]) is int and type(point["y"]) is int, point
    xs = sorted({p["x"] for p in line["points"]})
    ys = sorted({p["y"] for p in line["points"]})
    assert len(xs) == 2 and len(ys) == 2                          # an axis-aligned rectangle


# A 3-4-5 view tilted toward the model. Both corners satisfy -.6*y + .8*z = 4000 mm.
TILTED = {**RAW, "view_type": "ThreeD", "view_name": "Tilted box", "is_3d": True,
          "level": None, "elevation": None, "levels": [{"name": "Shelf", "elevation": 2000 / FEET},
                                                      {"name": "Base", "elevation": 0}],
          "right": [1, 0, 0], "up": [0, .8, .6], "direction": [0, -.6, .8],
          "c0": [-6000 / FEET, -4000 / FEET, 2000 / FEET], "c1": [10000 / FEET, 2400 / FEET, 6800 / FEET]}


def oriented_view(azimuth, altitude, roll):
    az, alt, roll = map(math.radians, (azimuth, altitude, roll))
    horizontal = [math.cos(az), math.sin(az), 0]
    vertical = [-math.sin(az) * math.sin(alt), math.cos(az) * math.sin(alt), math.cos(alt)]
    direction = [math.sin(az) * math.cos(alt), -math.cos(az) * math.cos(alt), math.sin(alt)]
    right = [math.cos(roll) * r + math.sin(roll) * v for r, v in zip(horizontal, vertical)]
    up = [-math.sin(roll) * r + math.cos(roll) * v for r, v in zip(horizontal, vertical)]

    def corner(u, v):
        return [(u * r + v * a + 4000 * d) / FEET for r, a, d in zip(right, up, direction)]

    return {**TILTED, "right": right, "up": up, "direction": direction,
            "c0": corner(-6000, -2000), "c1": corner(10000, 6000)}


@pytest.mark.parametrize("angles", [(45, 35.264, 0), (107, 42, 34), (203, 89.8, 0)])
def test_pixel_model_pixel_round_trip_at_two_elevations(angles):
    frame = sketch._frame(oriented_view(*angles))
    strokes = [[[0, 0], [1600, 800]], [[125.3, 734.2], [1300.6, 77.1]]]
    for z in (0, 2000):
        lines = sketch.sketch_lines(frame, 1600, 800, strokes, elevation_mm=z)
        for line, stroke in zip(lines, strokes):
            for (x, y), pixel in zip(line, stroke):
                assert sketch.pixel_of(frame, 1600, 800, x, y, elevation_mm=z) == pytest.approx(pixel, abs=.1)


def test_tilted_view_intersects_each_level_at_known_model_points():
    frame = sketch._frame(TILTED)
    assert frame == {**FRAME, "view_name": "Tilted box", "view_type": "ThreeD", "kind": "axonometric",
                     "right": [1, 0, 0], "up": [0, .8, .6], "direction": [0, -.6, .8], "w0": pytest.approx(4000),
                     "level": None, "elevation_mm": None, "levels": [{"name": "Base", "elevation_mm": 0},
                                                                     {"name": "Shelf", "elevation_mm": 2000}]}
    # u = x, v = .8*y + .6*z. Top left has (u, v) = (-6000, 6000), bottom right (10000, -2000).
    for z, top, bottom in ((0, 7500, -2500), (2000, 6000, -4000)):
        (line,) = sketch.sketch_lines(frame, 1600, 800, [[[0, 0], [1600, 800]]], elevation_mm=z)
        assert line[0] == pytest.approx((-6000, top))
        assert line[1] == pytest.approx((10000, bottom))
        assert sketch.pixel_of(frame, 1600, 800, -6000, top, z) == [0, 0]
    with pytest.raises(ValueError, match="elevation"):
        sketch.pixel_of(frame, 1600, 800, 0, 0)
    with pytest.raises(ValueError, match="elevation"):
        sketch.sketch_lines(frame, 1600, 800, [[[0, 0], [1600, 800]]])


@pytest.mark.parametrize("altitude", [0, 2.8, -2.8])
def test_an_almost_horizontal_3d_view_is_refused(altitude):
    with pytest.raises(RevitQueryError, match="horizontally"):
        sketch._frame(oriented_view(53, altitude, 0))


def test_perspective_refusal_names_supported_views():
    with pytest.raises(RevitQueryError, match="orthographic 3D view or a plan"):
        sketch._frame({"view_type": "ThreeD", "is_3d": True, "is_perspective": True})


@pytest.mark.parametrize("bad", [
    {"right": [1, 0]}, {"up": [0, math.nan, .6]}, {"direction": [1, 0, .8]}, {"c0": [0, math.inf, 0]},
    {"is_perspective": "false"}, {"levels": None}, {"levels": [{"name": "Shelf"}]},
    {"levels": [{"name": None, "elevation": 0}]}, {"levels": [{"name": "Shelf", "elevation": math.inf}]},
    {"levels": [{"name": "Shelf", "elevation": 10**400}]}, {"levels": TILTED["levels"] * 101},
])
def test_bad_3d_readings_are_refused(bad):
    with pytest.raises(RevitQueryError, match="^Invalid view frame$"):
        sketch._frame({**TILTED, **bad})


@pytest.mark.parametrize("level", [None, "Missing shelf"])
def test_3d_sketch_requires_an_available_level_before_reading_targets(level):
    frame = sketch._frame(TILTED)
    # Leave the executor absent: rejecting the level should need no Revit request.
    result = asyncio.run(sketch.resolve_sketch(None, frame, 1600, 800, [[[0, 0], [100, 100]]], level))
    assert result["error"] == "invalid_args"
    assert "Base" in result["message"] and "Shelf" in result["message"]


def test_available_level_message_is_limited_to_twenty_names():
    levels = [{"name": f"Shelf-{n:02d}", "elevation": n} for n in range(22)]
    frame = sketch._frame({**TILTED, "levels": levels})
    result = asyncio.run(sketch.resolve_sketch(None, frame, 1600, 800, [[[0, 0], [100, 100]]]))
    assert result["error"] == "invalid_args"
    assert result["message"].count("Shelf-") == 20 and "Shelf-20" not in result["message"]


def test_legacy_plan_frame_and_supplied_level_preserve_plan_sketch():
    legacy = FRAME.copy()
    del legacy["kind"]
    strokes = [[[310, 108], [590, 90]]]
    expected = resolve(strokes)
    assert expected["view_kind"] == "plan"
    assert resolve(strokes, frame=legacy, level="Shelf") == resolve(strokes, level="Shelf") == expected


def test_3d_wall_snaps_square_on_the_chosen_level_and_project_back_to_the_capture():
    frame = sketch._frame(TILTED)
    # At z=2000, wall 11 ends at (-3000, 5000), pixel (300, 80). Wall 12 runs along x=0.
    # A wall on Base ends exactly at the first drawn pixel and would otherwise win the snap.
    walls = [dict(row, level="Shelf") for row in WALLS]
    walls.append({"level": "Base", "id": "99", "straight": True, "a": [-2900 / FEET, 4900 / FEET], "b": [-4, 1]})
    out = resolve([[[310, 88], [590, 70]]], frame=frame, level="Shelf", expected_level="Shelf", walls=walls)
    assert (out["view_kind"], out["level"], out["elevation_mm"]) == ("axonometric", "Shelf", 2000)
    assert out["lines"] == [{"line": 1, "length_mm": 3000, "pixels": [[300, 80], [600, 80]], "points": [
        {"x": -3000, "y": 5000, "snap": {"kind": "wall_end", "wall_id": 11}},
        {"x": 0, "y": 5000, "snap": {"kind": "on_wall", "wall_id": 12}}]}]
    assert sketch.sketch_block(out).startswith(
        'Designer\'s sketch (drawn by the designer on a capture of the 3D view "Tilted box", projected onto level "Shelf"; '
        'the host converted it to model coordinates, x and y in millimetres): line 1 from (-3000, 5000) [the end of wall id 11]')
    assert sketch.sketch_block(out) == sketch.sketch_block({**out, "view_type": "FloorPlan"})


def test_free_3d_lines_align_with_each_projected_axis_even_when_the_view_is_rolled():
    # Roll the 3-4-5 view so screen right and up project along (1, .6) and (-.6, .64).
    raw = oriented_view(0, math.degrees(math.asin(.8)), math.degrees(math.asin(.6)))
    frame = sketch._frame(raw)
    strokes = [[sketch.pixel_of(frame, 1600, 800, x, y, 0) for x, y in [(0, 0), end]]
               for end in ((2000, 1260), (-1500, 1700))]
    out = resolve(strokes, frame=frame, level="Base", expected_level="Base", walls=[])
    for line, axis, end in zip(out["lines"], ((1, .6), (-.6, .64)), ((2000, 1260), (-1500, 1700))):
        a, b = line["points"]
        assert a == {"x": 0, "y": 0}
        distance = sum(p * d for p, d in zip(end, axis)) / sum(d * d for d in axis)
        assert (b["x"], b["y"]) == pytest.approx([d * distance for d in axis], abs=2)


def test_capture_for_sketch_returns_the_3d_frame_and_detects_a_changed_level(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    out, _ = run(addin(frames=[TILTED]), sketch.capture_for_sketch, 1600, ledger=ledger)
    assert out["error"] is None and out["png_base64"] == PNG and out["frame"] == sketch._frame(TILTED)
    changed = {**TILTED, "levels": [{"name": "Base", "elevation": 1}]}
    out, _ = run(addin(frames=[TILTED, changed]), sketch.capture_for_sketch, 1600, ledger=ledger)
    assert out["error"] == "view_changed" and "frame" not in out


@pytest.mark.parametrize("advance", [False, True], ids=["two-fixed-points", "sawtooth"])
def test_2000_point_zigzag_is_rejected_quickly(advance):
    stroke = [[i if advance else 0, 700 * (i % 2)] for i in range(2000)]
    start = time.perf_counter()
    with pytest.raises(ValueError, match="too tangled"):
        sketch.sketch_lines(FRAME, 2000, 800, [stroke])
    assert time.perf_counter() - start < 0.5
    if advance:
        assert len(sketch._simplify(stroke, 65)) == sketch.MAX_SEGMENTS + 2


def test_stroke_over_the_2000_point_limit_is_refused_at_input():
    with pytest.raises(ValueError, match="list of"):
        sketch.sketch_lines(FRAME, 2000, 800, [[[i, 100] for i in range(2001)]])

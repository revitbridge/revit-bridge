"""A sketch the designer draws on a capture of a plan view, as model coordinates.

A host shows the designer a picture of the active plan view and lets them draw on it ("a
wall here"). A picture has no coordinates, so the host reads the view's frame with the
picture and keeps it; what was drawn in pixels is then converted here, exactly, instead of
being guessed from the image by a model.

``capture_for_sketch(client, width, *, ledger, host, scope)``
    ``capture_view_image`` of the active view plus its ``frame``: the model rectangle the
    picture shows (the view's zoom corners), read before and after the export and refused
    when the view moved in between. Plan views only (floor, ceiling, structural, area): a
    section or a 3D view has no single plane to draw on.
``sketch_lines(frame, width, height, strokes)``
    Pure. Each stroke (a list of ``[x, y]`` pixels of the capture) becomes a line in model
    millimetres: a stroke that is nearly straight - however shakily or often it was drawn
    over - is one segment, anything else a simplified polyline.
``resolve_sketch(executor, frame, width, height, strokes)``
    ``sketch_lines`` snapped to the walls of that view: an end within the tolerance of a
    wall's end takes it (``wall_end``), else of a wall's centre line a point on it
    (``on_wall``); a segment from a wall's end to another wall meets that wall at a right
    angle when that is what was nearly drawn, and a free segment that is nearly parallel to
    the view's axes is made parallel. Reads the model once; raises RevitQueryError when
    the view of the frame is no longer there. ``{view_name, level, lines: [{line, points:
    [{x, y, snap?}], length_mm, pixels}], tolerance_mm, warnings}``; ``pixels`` are the same
    points on the capture, for a host that draws what was understood over the picture.
``sketch_block(resolved)``
    The fixed text a host appends to the designer's message.

Nothing here writes to the model, and no image is read: only the pixel coordinates.
"""
from __future__ import annotations

import math

from revit_bridge import view_commands
from revit_bridge.evidence.ledger import LOCAL_SCOPE
from revit_bridge.snapshot.query import RevitQueryError, RevitQueryExecutor, element_id_or_none

PLAN_VIEW_TYPES = ("FloorPlan", "CeilingPlan", "EngineeringPlan", "AreaPlan")
MAX_STROKES = 8
MAX_STROKE_POINTS = 4000
MAX_SEGMENTS = 12
STRAIGHT_RATIO = 0.12          # a stroke this thin for its length is one straight line
MIN_STROKE_PX = 6              # shorter than this is a click, not a line
SNAP_PX = 14                   # the snap tolerance, in pixels of the capture ...
SNAP_MIN_MM = 250              # ... and never less than this in the model
AXIS_DEGREES = 6               # a free segment this close to an axis of the view is made parallel
FEET_MM = 304.8

_FRAME = """
UIDocument ui = new UIDocument(document);
View view = document.ActiveView;
if (view == null) throw new InvalidOperationException("No active view.");
UIView shown = ui.GetOpenUIViews().FirstOrDefault(v => v.ViewId == view.Id);
if (shown == null) throw new InvalidOperationException("The active view is not open in a window.");
IList<XYZ> corners = shown.GetZoomCorners();
XYZ right = view.RightDirection, up = view.UpDirection, direction = view.ViewDirection;
Level level = view.GenLevel;
return new {
    document_id = document.GetHashCode().ToString(), view_id = view.Id.ToString(), view_uid = view.UniqueId,
    view_name = view.Name, view_type = view.ViewType.ToString(),
    level = level?.Name, elevation = level?.ProjectElevation,
    c0 = new double[] { corners[0].X, corners[0].Y, corners[0].Z },
    c1 = new double[] { corners[1].X, corners[1].Y, corners[1].Z },
    right = new double[] { right.X, right.Y, right.Z }, up = new double[] { up.X, up.Y, up.Z },
    direction = new double[] { direction.X, direction.Y, direction.Z } };
"""

_WALLS = """
View view = document.GetElement((string)parameters[0]) as View;
if (view == null || document.GetHashCode().ToString() != (string)parameters[1])
    throw new InvalidOperationException("The view of the sketch is no longer open; capture the view again.");
var timer = System.Diagnostics.Stopwatch.StartNew();
var walls = new List<object>();
bool complete = true;
foreach (Element e in new FilteredElementCollector(document, view.Id).OfClass(typeof(Wall))) {
    if (timer.ElapsedMilliseconds >= 800 || walls.Count >= 5000) { complete = false; break; }
    LocationCurve location = e.Location as LocationCurve;
    if (location?.Curve == null) continue;
    XYZ a = location.Curve.GetEndPoint(0), b = location.Curve.GetEndPoint(1);
    walls.Add(new { id = e.Id.ToString(), straight = location.Curve is Line,
                    a = new double[] { a.X, a.Y }, b = new double[] { b.X, b.Y } });
}
return new { walls, complete };
"""


def _numbers(values, count):
    return (isinstance(values, list) and len(values) == count
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values))


def _frame(data) -> dict:
    """The add-in's reading as a frame in millimetres, or RevitQueryError."""
    if not isinstance(data, dict) or not all(_numbers(data.get(k), 3) for k in ("c0", "c1", "right", "up", "direction")):
        raise RevitQueryError("Invalid view frame")
    view_id = element_id_or_none(data.get("view_id"))
    if view_id is None or not isinstance(data.get("view_uid"), str) or not isinstance(data.get("view_name"), str):
        raise RevitQueryError("Invalid view frame")
    if data.get("view_type") not in PLAN_VIEW_TYPES or abs(abs(data["direction"][2]) - 1) > 1e-6:
        raise RevitQueryError("Sketching needs a plan view (floor, ceiling, structural or area plan). "
                              "Open one in Revit and capture the view again.")
    right, up = data["right"], data["up"]
    dot = lambda p, axis: sum(a * b for a, b in zip(p, axis))        # noqa: E731
    u = sorted((dot(data["c0"], right) * FEET_MM, dot(data["c1"], right) * FEET_MM))
    v = sorted((dot(data["c0"], up) * FEET_MM, dot(data["c1"], up) * FEET_MM))
    if u[1] - u[0] <= 0 or v[1] - v[0] <= 0:
        raise RevitQueryError("Invalid view frame")
    elevation = data.get("elevation")
    return {"document_id": str(data.get("document_id") or ""), "view_id": view_id, "view_uid": data["view_uid"],
            "view_name": data["view_name"], "view_type": data["view_type"],
            "level": data.get("level") if isinstance(data.get("level"), str) else None,
            "elevation_mm": round(elevation * FEET_MM, 3) if isinstance(elevation, (int, float)) else None,
            "u": [round(u[0], 3), round(u[1], 3)], "v": [round(v[0], 3), round(v[1], 3)],
            "right": [right[0], right[1]], "up": [up[0], up[1]]}


async def view_frame(executor: RevitQueryExecutor) -> dict:
    """The model rectangle the active plan view shows now (see the module)."""
    response = await executor.client.send_code(_FRAME)
    if not response.success:
        raise RevitQueryError(response.error or "Could not read the view frame")
    return _frame(response.result)


async def capture_for_sketch(client, width: int = 1600, *, ledger, host: str = "mcp", scope: str = LOCAL_SCOPE) -> dict:
    """``capture_view_image`` of the active view with its ``frame``; ``error`` set when the view is
    not a plan or moved while it was exported (``frame`` is then absent)."""
    executor = RevitQueryExecutor(client)
    try:
        before = await view_frame(executor)
    except RevitQueryError as exc:
        return view_commands.view_reply(error="unsupported_view", hint=str(exc))
    image = await view_commands.capture_view_image(client, width, ledger=ledger, host=host, scope=scope)
    if image.get("error"):
        return image
    try:
        after = await view_frame(executor)
    except RevitQueryError as exc:
        return view_commands.view_reply(error="unsupported_view", hint=str(exc))
    if before != after:
        return view_commands.view_reply(error="view_changed",
                                        hint="The view moved while it was captured. Leave it still and capture again.")
    return dict(image, frame=after)


# -- pixels to model ---------------------------------------------------------------------------

def _point(frame, width, height, x, y):
    u = frame["u"][0] + x / width * (frame["u"][1] - frame["u"][0])
    v = frame["v"][1] - y / height * (frame["v"][1] - frame["v"][0])
    return (frame["right"][0] * u + frame["up"][0] * v, frame["right"][1] * u + frame["up"][1] * v)


def pixel_of(frame: dict, width: int, height: int, x: float, y: float) -> list[float]:
    """Where the model point (x, y), in millimetres, lies on the capture: ``[px, py]``. For a host
    that draws what was understood over the picture."""
    u = x * frame["right"][0] + y * frame["right"][1]
    v = x * frame["up"][0] + y * frame["up"][1]
    return [round((u - frame["u"][0]) / (frame["u"][1] - frame["u"][0]) * width, 1),
            round((frame["v"][1] - v) / (frame["v"][1] - frame["v"][0]) * height, 1)]


def _simplify(points, epsilon):
    """Ramer-Douglas-Peucker, iteratively."""
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        (ax, ay), (bx, by) = points[first], points[last]
        length = math.hypot(bx - ax, by - ay)
        worst, index = 0.0, -1
        for i in range(first + 1, last):
            px, py = points[i]
            distance = (abs((bx - ax) * (ay - py) - (ax - px) * (by - ay)) / length if length
                        else math.hypot(px - ax, py - ay))
            if distance > worst:
                worst, index = distance, i
        if index >= 0 and worst > epsilon:
            keep[index] = True
            stack += [(first, index), (index, last)]
    return [p for p, kept in zip(points, keep) if kept]


def _stroke(points):
    """A drawn stroke in pixels as the corners of a line: two for a (nearly) straight one."""
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    sxx = sum((p[0] - cx) ** 2 for p in points)
    syy = sum((p[1] - cy) ** 2 for p in points)
    sxy = sum((p[0] - cx) * (p[1] - cy) for p in points)
    angle = 0.5 * math.atan2(2 * sxy, sxx - syy)             # the direction the stroke runs in
    ax, ay = math.cos(angle), math.sin(angle)
    along = [(p[0] - cx) * ax + (p[1] - cy) * ay for p in points]
    across = [-(p[0] - cx) * ay + (p[1] - cy) * ax for p in points]
    length, thickness = max(along) - min(along), max(across) - min(across)
    if length < MIN_STROKE_PX:
        return None
    if thickness <= max(STRAIGHT_RATIO * length, 4):
        # Drawn over twice or shakily: the line through the middle, from one end to the other.
        ends = [(cx + ax * t, cy + ay * t) for t in (min(along), max(along))]
        # Keep the direction it was drawn in.
        return ends if along[-1] >= along[0] else ends[::-1]
    corners = _simplify(points, max(0.03 * math.hypot(length, thickness), 6))
    return corners if len(corners) - 1 <= MAX_SEGMENTS else None


def sketch_lines(frame: dict, width: int, height: int, strokes) -> list[list[tuple[float, float]]]:
    """The strokes as lines in model millimetres; ValueError for strokes that are not ones."""
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError("Invalid capture size")
    if not isinstance(strokes, list) or not 1 <= len(strokes) <= MAX_STROKES:
        raise ValueError(f"Draw 1 to {MAX_STROKES} lines")
    lines = []
    for stroke in strokes:
        if not isinstance(stroke, list) or not 2 <= len(stroke) <= MAX_STROKE_POINTS or not all(_numbers(p, 2) for p in stroke):
            raise ValueError("A line is a list of [x, y] pixel points")
        if any(not (-2 <= p[0] <= width + 2 and -2 <= p[1] <= height + 2) for p in stroke):
            raise ValueError("A line leaves the capture")
        corners = _stroke([(float(p[0]), float(p[1])) for p in stroke])
        if corners is None:
            raise ValueError("A line is too short or too tangled to read; draw it again")
        lines.append([_point(frame, width, height, x, y) for x, y in corners])
    return lines


# -- snapping to walls -------------------------------------------------------------------------

def _foot(point, a, b):
    """The nearest point to ``point`` on the segment a-b, and its distance."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    span = dx * dx + dy * dy
    t = 0.0 if not span else max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / span))
    foot = (a[0] + dx * t, a[1] + dy * t)
    return foot, math.hypot(point[0] - foot[0], point[1] - foot[1])


def _snap(point, walls, tolerance):
    best = None
    for wall in walls:
        for end in (wall["a"], wall["b"]):
            distance = math.hypot(point[0] - end[0], point[1] - end[1])
            if distance <= tolerance and (best is None or distance < best[0]):
                best = (distance, end, {"kind": "wall_end", "wall_id": wall["id"]})
    if best:
        return best[1], best[2]
    for wall in walls:
        if not wall["straight"]:
            continue
        foot, distance = _foot(point, wall["a"], wall["b"])
        if distance <= tolerance and (best is None or distance < best[0]):
            best = (distance, foot, {"kind": "on_wall", "wall_id": wall["id"]})
    return (best[1], best[2]) if best else (point, None)


def _square_up(points, snaps, walls, frame, tolerance):
    """Tidy each segment after snapping (see the module)."""
    by_id = {wall["id"]: wall for wall in walls}
    limit = math.tan(math.radians(AXIS_DEGREES))
    for i in range(len(points) - 1):
        for fixed, loose in ((i, i + 1), (i + 1, i)):
            # From a wall's end to a point on another wall: meet that wall at a right angle,
            # when the foot of the perpendicular is where the designer nearly drew to.
            if (snaps[fixed] or {}).get("kind") == "wall_end" and (snaps[loose] or {}).get("kind") == "on_wall":
                wall = by_id[snaps[loose]["wall_id"]]
                foot, _ = _foot(points[fixed], wall["a"], wall["b"])
                if math.hypot(foot[0] - points[loose][0], foot[1] - points[loose][1]) <= 2 * tolerance:
                    points[loose] = foot
        if snaps[i] is None or snaps[i + 1] is None:
            # A free end: a segment nearly along an axis of the view runs along it.
            # Where the line began stays, unless only the beginning is free.
            moving, anchor = (i + 1, i) if snaps[i + 1] is None else (i, i + 1)
            dx, dy = points[moving][0] - points[anchor][0], points[moving][1] - points[anchor][1]
            along = dx * frame["right"][0] + dy * frame["right"][1]
            across = dx * frame["up"][0] + dy * frame["up"][1]
            if along and abs(across / along) <= limit:
                across = 0.0
            elif across and abs(along / across) <= limit:
                along = 0.0
            else:
                continue
            if snaps[i] is None and snaps[i + 1] is None and len(points) > 2:
                continue                                       # a free corner of a polyline stays where it was drawn
            points[moving] = (points[anchor][0] + along * frame["right"][0] + across * frame["up"][0],
                              points[anchor][1] + along * frame["right"][1] + across * frame["up"][1])


async def resolve_sketch(executor: RevitQueryExecutor, frame: dict, width: int, height: int, strokes) -> dict:
    """The designer's strokes as model lines snapped to the view's walls (see the module).
    ``{"error": "invalid_args", "message"}`` for strokes that cannot be read."""
    try:
        lines = sketch_lines(frame, width, height, strokes)
    except ValueError as exc:
        return {"error": "invalid_args", "message": str(exc)}
    response = await executor.client.send_code(_WALLS, parameters=[frame["view_uid"], frame["document_id"]])
    data = response.result
    if not response.success or not isinstance(data, dict) or not isinstance(data.get("walls"), list):
        raise RevitQueryError(response.error or "Could not read the walls of the view")
    walls = []
    for row in data["walls"]:
        number = element_id_or_none(row.get("id")) if isinstance(row, dict) else None
        if number is None or not _numbers(row.get("a"), 2) or not _numbers(row.get("b"), 2):
            continue
        walls.append({"id": number, "straight": row.get("straight") is True,
                      "a": (row["a"][0] * FEET_MM, row["a"][1] * FEET_MM),
                      "b": (row["b"][0] * FEET_MM, row["b"][1] * FEET_MM)})
    per_pixel = (frame["u"][1] - frame["u"][0]) / width
    tolerance = max(SNAP_MIN_MM, SNAP_PX * per_pixel)
    out = []
    for number, line in enumerate(lines, start=1):
        points, snaps = [], []
        for point in line:
            snapped, snap = _snap(point, walls, tolerance)
            points.append(snapped)
            snaps.append(snap)
        _square_up(points, snaps, walls, frame, tolerance)
        length = sum(math.hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])
                     for i in range(len(points) - 1))
        if length < 1:
            return {"error": "invalid_args", "message": "A line has no length after snapping; draw it longer"}
        out.append({"line": number, "length_mm": round(length),
                    "points": [{"x": round(x), "y": round(y), **({"snap": snap} if snap else {})}
                               for (x, y), snap in zip(points, snaps)],
                    "pixels": [pixel_of(frame, width, height, x, y) for x, y in points]})
    warnings = [] if data.get("complete") is True else [
        "The view has more walls than one read covers; some ends may not have snapped."]
    return {"view_name": frame["view_name"], "view_type": frame["view_type"], "level": frame["level"],
            "elevation_mm": frame["elevation_mm"], "tolerance_mm": round(tolerance), "lines": out, "warnings": warnings}


# -- the text for the model ---------------------------------------------------------------------

def _where(point) -> str:
    text = f"({point['x']}, {point['y']})"
    snap = point.get("snap")
    if snap:
        text += (f" [the end of wall id {snap['wall_id']}]" if snap["kind"] == "wall_end"
                 else f" [on wall id {snap['wall_id']}]")
    return text


def sketch_block(resolved: dict) -> str:
    """The fixed text for the designer's sketch; "" when there is nothing drawn."""
    lines = resolved.get("lines") or []
    if not lines:
        return ""
    name = " ".join(str(resolved.get("view_name") or "").split()).replace('"', "'")[:200]
    level = " ".join(str(resolved.get("level") or "").split()).replace('"', "'")[:200]
    parts = []
    for line in lines:
        points = line["points"]
        path = " to ".join(_where(p) for p in points)
        kind = (f"line {line['line']}" if len(points) == 2
                else f"polyline {line['line']} ({len(points) - 1} segments)")
        parts.append(f"{kind} from {path}, length {line['length_mm']} mm")
    return (f"Designer's sketch (drawn by the designer on a capture of the plan view \"{name}\""
            + (f", level \"{level}\"" if level else "")
            + "; the host converted it to model coordinates, x and y in millimetres): " + "; ".join(parts)
            + ". These coordinates are the designer's own input: use them as given, with source designer and "
              "evidence naming the line, e.g. \"sketch line 1 start\". A level name is not: read it from Revit "
              "(source tool).")

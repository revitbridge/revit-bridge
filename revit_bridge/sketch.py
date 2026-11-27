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
    ``sketch_lines`` snapped to the nearest wall end (``wall_end``), crossing of straight
    grids (``grid_intersection``), column location (``column``) or framing end (``element_end``)
    within tolerance. Otherwise a point can snap to a straight wall's centre line
    (``on_wall``) or a straight grid (``on_grid``). A segment from a point target to a wall
    or grid meets it at a right angle when that is what was nearly drawn. A free segment
    nearly parallel to the view's axes is made parallel. Polylines that return within
    tolerance of their start are closed. Nearly square corners are squared with snapped
    vertices fixed. Reads the model once; raises RevitQueryError when the view of the frame
    is no longer there. ``{view_name, level, lines: [{line, points: [{x, y, snap?}],
    length_mm, pixels, closed?}], tolerance_mm, warnings}``; model and pixel points list
    each corner once, and the length includes the closing edge of a closed line.
``sketch_block(resolved)``
    The fixed text a host appends to the designer's message.

Nothing here writes to the model, and no image is read: only the pixel coordinates.
"""
from __future__ import annotations

import math
from itertools import combinations

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
RIGHT_ANGLE_DEGREES = 8
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

_TARGETS = """
View view = document.GetElement((string)parameters[0]) as View;
if (view == null || document.GetHashCode().ToString() != (string)parameters[1])
    throw new InvalidOperationException("The view of the sketch is no longer open; capture the view again.");
var timer = System.Diagnostics.Stopwatch.StartNew();
var walls = new List<object>();
var grids = new List<object>();
var columns = new List<object>();
var framing = new List<object>();
var columnCategories = new ElementMulticategoryFilter(new List<BuiltInCategory> {
    BuiltInCategory.OST_StructuralColumns, BuiltInCategory.OST_Columns });
var filter = new LogicalOrFilter(new List<ElementFilter> {
    new ElementClassFilter(typeof(Wall)), new ElementClassFilter(typeof(Grid)),
    columnCategories, new ElementCategoryFilter(BuiltInCategory.OST_StructuralFraming) });
double[] XY(XYZ p) => new double[] { p.X, p.Y };
bool complete = true;
foreach (Element e in new FilteredElementCollector(document, view.Id).WherePasses(filter).WhereElementIsNotElementType()) {
    if (timer.ElapsedMilliseconds >= 800 || walls.Count + grids.Count + columns.Count + framing.Count >= 5000) {
        complete = false;
        break;
    }
    if (e is Grid grid) {
        if (grid.Curve is Line axis && axis.IsBound)
            grids.Add(new { name = grid.Name, a = XY(axis.GetEndPoint(0)), b = XY(axis.GetEndPoint(1)) });
    } else if (columnCategories.PassesFilter(e)) {
        if (e.Location is LocationPoint location)
            columns.Add(new { id = e.Id.ToString(), point = XY(location.Point) });
    } else if ((e.Location as LocationCurve)?.Curve is Curve curve && curve.IsBound) {
        var a = XY(curve.GetEndPoint(0));
        var b = XY(curve.GetEndPoint(1));
        if (e is Wall) walls.Add(new { id = e.Id.ToString(), straight = curve is Line, a, b });
        else framing.Add(new { id = e.Id.ToString(), a, b });
    }
}
return new { walls, grids, columns, framing, complete };
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


# -- snapping to model geometry ----------------------------------------------------------------

def _xy(row, key):
    value = row.get(key)
    if _numbers(value, 2):
        return (value[0] * FEET_MM, value[1] * FEET_MM)
    return None


def _targets(data):
    """Point targets (wall ends first for ties), straight wall segments and infinite grid lines."""
    ends, walls, grids = [], {}, {}
    for group, kind, id_key in (("walls", "wall_end", "wall_id"), ("framing", "element_end", "element_id")):
        for row in data.get(group, []):
            number = element_id_or_none(row.get("id")) if isinstance(row, dict) else None
            if number is None:
                continue
            a, b = _xy(row, "a"), _xy(row, "b")
            if a is not None and b is not None:
                snap = {"kind": kind, id_key: number}
                ends.extend(((a, snap), (b, snap)))
                if group == "walls" and row.get("straight") is True:
                    walls[number] = (a, b)
    for row in data.get("columns", []):
        number = element_id_or_none(row.get("id")) if isinstance(row, dict) else None
        if number is not None and (point := _xy(row, "point")) is not None:
            ends.append((point, {"kind": "column", "element_id": number}))
    for row in data.get("grids", []):
        if isinstance(row, dict) and isinstance(row.get("name"), str):
            a, b = _xy(row, "a"), _xy(row, "b")
            if a is not None and b is not None and a != b:
                grids[row["name"]] = (a, b)
    return ends, walls, grids


def _foot(point, a, b, *, infinite=False):
    """The nearest point on a-b and its distance. Only grid lines extend beyond their ends."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    span = dx * dx + dy * dy
    t = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / span if span else 0.0
    if not infinite:
        t = max(0.0, min(1.0, t))
    foot = (a[0] + dx * t, a[1] + dy * t)
    return foot, math.hypot(point[0] - foot[0], point[1] - foot[1])


def _crossing(a, b, c, d):
    dx, dy = b[0] - a[0], b[1] - a[1]
    ex, ey = d[0] - c[0], d[1] - c[1]
    if math.isclose(dx * ey, dy * ex, rel_tol=1e-14):
        return None
    t = ((c[0] - a[0]) * ey - (c[1] - a[1]) * ex) / (dx * ey - dy * ex)
    return a[0] + t * dx, a[1] + t * dy


def _snap(point, ends, walls, grids, tolerance):
    best_distance = math.inf
    best = (point, None)
    for end, snap in ends:
        distance = math.dist(point, end)
        if distance == 0:
            return end, snap
        if distance <= tolerance and distance < best_distance:
            best_distance, best = distance, (end, snap)

    # A crossing within tolerance must lie on two grids that both pass within tolerance.
    nearby = {}
    for name, axis in grids.items():
        foot, distance = _foot(point, *axis, infinite=True)
        if distance <= tolerance and distance < best_distance:
            nearby[name] = (foot, distance)
    for first, second in combinations(nearby, 2):
        crossing = _crossing(*grids[first], *grids[second])
        if crossing is not None:
            distance = math.dist(point, crossing)
            if distance <= tolerance and distance < best_distance:
                best_distance = distance
                best = (crossing, {"kind": "grid_intersection", "grids": sorted((first, second))})
                if distance == 0:
                    return best
    if best[1] is not None:
        return best

    for number, segment in walls.items():
        foot, distance = _foot(point, *segment)
        if distance <= tolerance and distance < best_distance:
            best_distance, best = distance, (foot, {"kind": "on_wall", "wall_id": number})
    for name, (foot, distance) in nearby.items():
        if distance < best_distance:
            best_distance, best = distance, (foot, {"kind": "on_grid", "grid": name})
    return best


def _axis_end(anchor, end, frame):
    limit = math.tan(math.radians(AXIS_DEGREES))
    dx, dy = end[0] - anchor[0], end[1] - anchor[1]
    along = dx * frame["right"][0] + dy * frame["right"][1]
    across = dx * frame["up"][0] + dy * frame["up"][1]
    if along and abs(across / along) <= limit:
        axis, distance = frame["right"], along
    elif across and abs(along / across) <= limit:
        axis, distance = frame["up"], across
    else:
        return end
    return anchor[0] + axis[0] * distance, anchor[1] + axis[1] * distance


def _square_up(points, snaps, walls, grids, frame, tolerance):
    """Align a single segment to the view or to the wall or grid at its loose end."""
    if None in snaps:
        moving = 1 if snaps[1] is None else 0
        points[moving] = _axis_end(points[1 - moving], points[moving], frame)
        return
    for fixed, loose in ((0, 1), (1, 0)):
        if snaps[fixed]["kind"] not in ("wall_end", "grid_intersection", "column", "element_end"):
            continue
        snap = snaps[loose]
        if snap["kind"] == "on_wall":
            if snaps[fixed].get("wall_id") == snap["wall_id"]:
                continue                       # along the wall it starts from: no perpendicular to drop
            foot, _ = _foot(points[fixed], *walls[snap["wall_id"]])
        elif snap["kind"] == "on_grid":
            foot, _ = _foot(points[fixed], *grids[snap["grid"]], infinite=True)
        else:
            continue
        if math.dist(foot, points[loose]) <= 2 * tolerance:
            points[loose] = foot


def _nearly_square(incoming, outgoing):
    dx, dy = incoming
    ex, ey = outgoing
    lengths = math.hypot(dx, dy) * math.hypot(ex, ey)
    return lengths > 0 and abs(dx * ex + dy * ey) <= lengths * math.sin(math.radians(RIGHT_ANGLE_DEGREES))


def _square_polyline(points, snaps, frame, closed):
    """Use the angles before any edits to decide which corners to square."""
    edge_ends = list(range(1, len(points)))
    if closed:
        edge_ends.append(0)
    directions = [(points[j][0] - a[0], points[j][1] - a[1]) for a, j in zip(points, edge_ends)]
    square = [_nearly_square(directions[i - 1], direction) for i, direction in enumerate(directions)]
    square[0] = closed and square[0]
    if not any(square):
        return False

    if snaps[0] is None and snaps[1] is None:
        points[1] = _axis_end(points[0], points[1], frame)
        directions[0] = (points[1][0] - points[0][0], points[1][1] - points[0][1])
    if not closed:
        # Walk in drawing order. If a snap fixes the next vertex, its actual incoming
        # direction becomes the reference for the following corner.
        for i in range(1, len(directions)):
            if square[i] and snaps[i + 1] is None:
                x, y = points[i]
                dx, dy = x - points[i - 1][0], y - points[i - 1][1]
                if dx or dy:
                    points[i + 1], _ = _foot(points[i + 1], (x, y), (x - dy, y + dx), infinite=True)
        return True

    # A closed path must fit both sides of the join. Carry the first edge's direction
    # through each run of square corners, including the run that leads back to the start.
    for i in range(1, len(directions)):
        if square[i]:
            dx, dy = directions[i - 1]
            directions[i] = (-dy, dx)
    for i in reversed(range(1, len(directions))):
        j = edge_ends[i]
        if not square[j]:
            break
        dx, dy = directions[j]
        directions[i] = (-dy, dx)

    edges = []
    for i, (dx, dy) in enumerate(directions):
        j = edge_ends[i]
        anchor = points[j] if j == 0 or snaps[j] is not None else points[i]
        edges.append((anchor, (anchor[0] + dx, anchor[1] + dy)))
    for i in range(1, len(points)):
        if snaps[i] is not None:
            continue
        corner = _crossing(*edges[i - 1], *edges[i])
        if corner is not None:
            points[i] = corner
    return True


def _whole_mm(point, frame):
    right, up = frame["right"], frame["up"]
    u = round(point[0] * right[0] + point[1] * right[1])
    v = round(point[0] * up[0] + point[1] * up[1])
    return right[0] * u + up[0] * v, right[1] * u + up[1] * v


def _coordinate_mm(value, squared):
    # Whole millimetres, except where squaring put a corner between them (a view that is turned,
    # or a corner tied to a snapped point that is not on a whole millimetre): rounding such a
    # corner would make the right angle slightly oblique, so it keeps thousandths.
    if squared:
        value = round(value, 3)
        if value != round(value):
            return value
    return round(value)


async def resolve_sketch(executor: RevitQueryExecutor, frame: dict, width: int, height: int, strokes) -> dict:
    """The designer's strokes as model lines snapped to elements in the view (see the module).
    ``{"error": "invalid_args", "message"}`` for strokes that cannot be read."""
    try:
        lines = sketch_lines(frame, width, height, strokes)
    except ValueError as exc:
        return {"error": "invalid_args", "message": str(exc)}
    response = await executor.client.send_code(_TARGETS, parameters=[frame["view_uid"], frame["document_id"]])
    data = response.result
    if not response.success or not isinstance(data, dict) or not isinstance(data.get("walls"), list):
        raise RevitQueryError(response.error or "Could not read the walls of the view")
    ends, walls, grids = _targets(data)
    per_pixel = (frame["u"][1] - frame["u"][0]) / width
    tolerance = max(SNAP_MIN_MM, SNAP_PX * per_pixel)
    out = []
    for number, line in enumerate(lines, start=1):
        closed = len(line) >= 4 and math.dist(line[-1], line[0]) <= tolerance
        points, snaps = [], []
        for point in (line[:-1] if closed else line):
            snapped, snap = _snap(point, ends, walls, grids, tolerance)
            # A free point is where a hand was: whole millimetres along the axes of the view, so
            # that what is squared from it along those axes stays on whole millimetres too.
            points.append(snapped if snap else _whole_mm(snapped, frame))
            snaps.append(snap)
        squared = False
        if len(points) == 2:
            _square_up(points, snaps, walls, grids, frame, tolerance)
        else:
            squared = _square_polyline(points, snaps, frame, closed)
        length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
        if closed:
            length += math.dist(points[-1], points[0])
        if length < 1:
            return {"error": "invalid_args", "message": "A line has no length after snapping; draw it longer"}
        out.append({"line": number, "length_mm": round(length), **({"closed": True} if closed else {}),
                    "points": [{"x": _coordinate_mm(x, squared), "y": _coordinate_mm(y, squared),
                                **({"snap": snap} if snap else {})} for (x, y), snap in zip(points, snaps)],
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
        kind = snap["kind"]
        if kind == "wall_end":
            return f"{text} [the end of wall id {snap['wall_id']}]"
        if kind == "on_wall":
            return f"{text} [on wall id {snap['wall_id']}]"
        if kind == "grid_intersection":
            return f"{text} [the crossing of grids {' and '.join(snap['grids'])}]"
        if kind == "column":
            return f"{text} [column id {snap['element_id']}]"
        if kind == "element_end":
            return f"{text} [the end of element id {snap['element_id']}]"
        if kind == "on_grid":
            return f"{text} [on grid {snap['grid']}]"
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
        if line.get("closed"):
            start = f"closed polyline {line['line']} ({len(points)} segments) through"
        elif len(points) > 2:
            start = f"polyline {line['line']} ({len(points) - 1} segments) from"
        else:
            start = f"line {line['line']} from"
        parts.append(f"{start} {path}, length {line['length_mm']} mm")
    guidance = ""
    if any(len(line["points"]) > 2 for line in lines):
        guidance = ' For walls along a polyline use create_walls_by_points with points as "x,y;x,y;..." in millimetres'
        if any(line.get("closed") for line in lines):
            guidance += ", and closed true for a closed line"
        guidance += "."
    return (f"Designer's sketch (drawn by the designer on a capture of the plan view \"{name}\""
            + (f", level \"{level}\"" if level else "")
            + "; the host converted it to model coordinates, x and y in millimetres): " + "; ".join(parts)
            + "." + guidance + " These coordinates are the designer's own input: use them as given, with source designer and "
              "evidence naming the line, e.g. \"sketch line 1 start\". A level name is not: read it from Revit "
              "(source tool).")

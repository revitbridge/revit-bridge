"""A designer's sketch on a plan or orthographic 3D capture, as model coordinates.

A host shows the designer a picture of the active view and lets them draw on it ("a
wall here"). A picture has no coordinates, so the host reads the view's frame with the
picture and keeps it; what was drawn in pixels is then converted here, exactly, instead of
being guessed from the image by a model.

``capture_for_sketch(client, width, *, ledger, host, scope)``
    ``capture_view_image`` of the active view plus its ``frame``: the model rectangle the
    picture shows (the view's zoom corners), read before and after the export and refused
    when the view moved in between. An orthographic 3D frame includes the document's levels.
``sketch_lines(frame, width, height, strokes, elevation_mm=None)``
    Pure. Each stroke (a list of ``[x, y]`` pixels of the capture) becomes a line in model
    millimetres. A shaky straight stroke can be traced over and still give one segment.
    Bent strokes become polylines with up to 12 segments. In 3D, each pixel's line of
    sight meets the horizontal plane at the supplied elevation.
``resolve_sketch(executor, frame, width, height, strokes, level=None)``
    ``sketch_lines`` snapped to the nearest wall end (``wall_end``), crossing of straight
    grids (``grid_intersection``), column location (``column``) or framing end (``element_end``)
    within tolerance. Otherwise a point can snap to a straight wall's centre line
    (``on_wall``) or a straight grid (``on_grid``). A segment from a point target to a wall
    or grid meets it at a right angle when that is what was nearly drawn. A free segment
    nearly parallel to a plan's axes or the model axes in 3D is made parallel. Polylines
    that return within tolerance of their start are closed. Nearly square corners can be
    squared. A closed outline must qualify at every corner. In 3D the designer names a
    level for projection and element snapping. Grids apply on every level. Reads the model
    once and raises RevitQueryError when the view of the frame is no longer there.
    ``{view_name, view_kind, level, lines: [{line, points: [{x, y, snap?}],
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
MAX_STROKE_POINTS = 2000
MAX_SEGMENTS = 12
STRAIGHT_RATIO = 0.12          # a stroke this thin for its length is one straight line
MIN_STROKE_PX = 6              # shorter than this is a click, not a line
SNAP_PX = 14                   # the snap tolerance, in pixels of the capture ...
SNAP_MIN_MM = 250              # ... and never less than this in the model
AXIS_DEGREES = 6               # a free segment this close to an alignment axis is made parallel
RIGHT_ANGLE_DEGREES = 8
FEET_MM = 304.8

_FRAME = """
UIDocument ui = new UIDocument(document);
View view = document.ActiveView;
if (view == null) throw new InvalidOperationException("No active view.");
View3D view3d = view as View3D;
bool isPerspective = view3d?.IsPerspective == true;
UIView shown = ui.GetOpenUIViews().FirstOrDefault(v => v.ViewId == view.Id);
if (shown == null) throw new InvalidOperationException("The active view is not open in a window.");
IList<XYZ> corners = isPerspective ? null : shown.GetZoomCorners();
double[] components(XYZ p) => p == null ? null : new double[] { p.X, p.Y, p.Z };
Level level = view.GenLevel;
var timer = System.Diagnostics.Stopwatch.StartNew();
var levels = new FilteredElementCollector(document).OfClass(typeof(Level)).Cast<Level>().Select(l => {
    if (timer.ElapsedMilliseconds >= 800) throw new InvalidOperationException("Could not read levels in time.");
    return new { name = l.Name, elevation = l.ProjectElevation };
}).OrderBy(l => l.elevation).ThenBy(l => l.name, StringComparer.Ordinal).Take(200).ToList();
return new {
    document_id = document.GetHashCode().ToString(), view_id = view.Id.ToString(), view_uid = view.UniqueId,
    view_name = view.Name, view_type = view.ViewType.ToString(), is_3d = view3d != null, is_perspective = isPerspective, levels,
    level = level?.Name, elevation = level?.ProjectElevation,
    c0 = components(corners?[0]), c1 = components(corners?[1]), right = components(view.RightDirection),
    up = components(view.UpDirection), direction = components(view.ViewDirection) };
"""

_TARGETS = """
View view = document.GetElement((string)parameters[0]) as View;
if (view == null || document.GetHashCode().ToString() != (string)parameters[1])
    throw new InvalidOperationException("The view of the sketch is no longer open; capture the view again.");
string levelName = (string)parameters[2];
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
var levelNames = new Dictionary<ElementId, string>();
bool onLevel(Element e) {
    ElementId id = e.LevelId;
    if (!(e is Wall) && (id == null || id == ElementId.InvalidElementId)) {
        id = e.get_Parameter(BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM)?.AsElementId();
        if (id == null || id == ElementId.InvalidElementId)
            id = e.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM)?.AsElementId();
    }
    if (id == null || id == ElementId.InvalidElementId) return false;
    if (!levelNames.TryGetValue(id, out string name)) {
        name = (document.GetElement(id) as Level)?.Name;
        levelNames[id] = name;
    }
    return name == levelName;
}
bool complete = true;
foreach (Element e in new FilteredElementCollector(document, view.Id).WherePasses(filter).WhereElementIsNotElementType()) {
    if (timer.ElapsedMilliseconds >= 800 || walls.Count + grids.Count + columns.Count + framing.Count >= 5000) {
        complete = false;
        break;
    }
    if (levelName != "" && !(e is Grid) && !onLevel(e)) continue;
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


def _finite(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # A JSON integer too large to represent as a double.
        return False


def _numbers(values, count):
    return isinstance(values, list) and len(values) == count and all(_finite(v) for v in values)


def _levels_mm(levels):
    if not isinstance(levels, list) or len(levels) > 200:
        raise RevitQueryError("Invalid view frame")
    choices = []
    for row in levels:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]:
            raise RevitQueryError("Invalid view frame")
        if not _finite(row.get("elevation")) or not math.isfinite(elevation_mm := row["elevation"] * FEET_MM):
            raise RevitQueryError("Invalid view frame")
        choices.append({"name": row["name"], "elevation_mm": round(elevation_mm, 3)})
    return sorted(choices, key=lambda row: row["elevation_mm"])


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _frame(data) -> dict:
    """The add-in's reading as a frame in millimetres, or RevitQueryError."""
    if not isinstance(data, dict):
        raise RevitQueryError("Invalid view frame")
    axonometric = data.get("view_type") == "ThreeD"
    if axonometric:
        if data.get("is_3d") is not True or type(data.get("is_perspective")) is not bool:
            raise RevitQueryError("Invalid view frame")
        if data["is_perspective"]:
            raise RevitQueryError("Sketching needs an orthographic 3D view or a plan view. Open one and capture again.")
    if not all(_numbers(data.get(k), 3) for k in ("c0", "c1", "right", "up", "direction")):
        raise RevitQueryError("Invalid view frame")
    view_id = element_id_or_none(data.get("view_id"))
    if view_id is None or not isinstance(data.get("view_uid"), str) or not isinstance(data.get("view_name"), str):
        raise RevitQueryError("Invalid view frame")
    right, up, direction = data["right"], data["up"], data["direction"]
    if axonometric:
        axes = (right, up, direction)
        if (any(abs(math.hypot(*axis) - 1) > 1e-6 for axis in axes)
                or any(abs(_dot(a, b)) > 1e-6 for a, b in combinations(axes, 2))):
            raise RevitQueryError("Invalid view frame")
        if abs(direction[2]) < 0.05:
            raise RevitQueryError("This view looks almost horizontally. Tilt the 3D view toward the level and capture again.")
    elif data.get("view_type") not in PLAN_VIEW_TYPES or abs(abs(direction[2]) - 1) > 1e-6:
        raise RevitQueryError("Sketching needs a plan view (floor, ceiling, structural or area plan) or an orthographic "
                              "3D view. Open one in Revit and capture the view again.")
    u = sorted((_dot(data["c0"], right) * FEET_MM, _dot(data["c1"], right) * FEET_MM))
    v = sorted((_dot(data["c0"], up) * FEET_MM, _dot(data["c1"], up) * FEET_MM))
    if not all(map(math.isfinite, u + v)) or u[1] - u[0] <= 0 or v[1] - v[0] <= 0:
        raise RevitQueryError("Invalid view frame")
    frame = {"document_id": str(data.get("document_id") or ""), "view_id": view_id, "view_uid": data["view_uid"],
             "view_name": data["view_name"], "view_type": data["view_type"],
             "u": [round(u[0], 3), round(u[1], 3)], "v": [round(v[0], 3), round(v[1], 3)]}
    if axonometric:
        w0 = _dot(data["c0"], direction) * FEET_MM
        if not math.isfinite(w0) or frame["u"][0] == frame["u"][1] or frame["v"][0] == frame["v"][1]:
            raise RevitQueryError("Invalid view frame")
        return dict(frame, kind="axonometric", right=right, up=up, direction=direction, w0=w0,
                    levels=_levels_mm(data.get("levels")), level=None, elevation_mm=None)
    elevation = data.get("elevation")
    return dict(frame, kind="plan", right=right[:2], up=up[:2],
                level=data.get("level") if isinstance(data.get("level"), str) else None,
                elevation_mm=round(elevation * FEET_MM, 3) if isinstance(elevation, (int, float)) else None)


async def view_frame(executor: RevitQueryExecutor) -> dict:
    """The active view's projection frame in millimetres (see the module)."""
    response = await executor.client.send_code(_FRAME)
    if not response.success:
        raise RevitQueryError(response.error or "Could not read the view frame")
    return _frame(response.result)


async def capture_for_sketch(client, width: int = 1600, *, ledger, host: str = "mcp", scope: str = LOCAL_SCOPE) -> dict:
    """``capture_view_image`` of the active view with its ``frame``; ``error`` set when the view is
    unsupported or moved while it was exported (``frame`` is then absent)."""
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

def _point(frame, width, height, x, y, elevation_mm=None):
    u = frame["u"][0] + x / width * (frame["u"][1] - frame["u"][0])
    v = frame["v"][1] - y / height * (frame["v"][1] - frame["v"][0])
    if frame.get("kind") == "axonometric":
        if not _finite(elevation_mm):
            raise ValueError("A 3D sketch needs a finite level elevation in millimetres")
        direction = frame["direction"]
        origin = [r * u + a * v + d * frame["w0"] for r, a, d in zip(frame["right"], frame["up"], direction)]
        t = (origin[2] - elevation_mm) / direction[2]
        return origin[0] - direction[0] * t, origin[1] - direction[1] * t
    return (frame["right"][0] * u + frame["up"][0] * v, frame["right"][1] * u + frame["up"][1] * v)


def pixel_of(frame: dict, width: int, height: int, x: float, y: float, elevation_mm=None) -> list[float]:
    """Project a model point to capture pixels. In 3D its level elevation in millimetres is required."""
    u = x * frame["right"][0] + y * frame["right"][1]
    v = x * frame["up"][0] + y * frame["up"][1]
    if frame.get("kind") == "axonometric":
        if not _finite(elevation_mm):
            raise ValueError("A 3D sketch needs a finite level elevation in millimetres")
        u += elevation_mm * frame["right"][2]
        v += elevation_mm * frame["up"][2]
    return [round((u - frame["u"][0]) / (frame["u"][1] - frame["u"][0]) * width, 1),
            round((frame["v"][1] - v) / (frame["v"][1] - frame["v"][0]) * height, 1)]


def _simplify(points, epsilon):
    """Ramer-Douglas-Peucker, iteratively."""
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    kept_count = 2
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
            kept_count += 1
            if kept_count > MAX_SEGMENTS + 1:
                break
            stack += [(first, index), (index, last)]
    out = [p for p, kept in zip(points, keep) if kept]
    # The simplifier splits at the farthest point, which on a closed or sharply bent stroke
    # can be a wobble on an edge rather than a corner; the real corners are then kept as well
    # and the wobble stays between them. Drop a kept point that lies within the tolerance of
    # the straight line between its neighbours.
    i = 1
    while len(out) <= MAX_SEGMENTS + 1 and i < len(out) - 1:
        (ax, ay), (px, py), (bx, by) = out[i - 1], out[i], out[i + 1]
        dx, dy = bx - ax, by - ay
        span = dx * dx + dy * dy
        s = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / span)) if span else 0.0
        if math.hypot(px - (ax + dx * s), py - (ay + dy * s)) <= epsilon:
            del out[i]
            i = max(1, i - 1)
        else:
            i += 1
    return out


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
        # Exact end-to-end reversals have no lateral spread for the simplifier to detect.
        if len(points) > MAX_SEGMENTS + 1 and all(p == points[i % 2] for i, p in enumerate(points)):
            return None
        # Drawn over twice or shakily: the line through the middle, from one end to the other.
        ends = [(cx + ax * t, cy + ay * t) for t in (min(along), max(along))]
        # Keep the direction it was drawn in.
        return ends if along[-1] >= along[0] else ends[::-1]
    corners = _simplify(points, max(0.03 * math.hypot(length, thickness), 6))
    return corners if len(corners) - 1 <= MAX_SEGMENTS else None


def sketch_lines(frame: dict, width: int, height: int, strokes, elevation_mm=None) -> list[list[tuple[float, float]]]:
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
        lines.append([_point(frame, width, height, x, y, elevation_mm) for x, y in corners])
    return lines


# -- snapping to model geometry ----------------------------------------------------------------

def _xy(row, key):
    value = row.get(key)
    if _numbers(value, 2):
        return (value[0] * FEET_MM, value[1] * FEET_MM)
    return None


def _targets(data):
    """Point targets (wall ends first for ties), straight wall segments and infinite grid lines."""
    def rows(group):
        value = data.get(group)
        return value if isinstance(value, list) else []

    ends, walls, grids = [], {}, {}
    for group, kind, id_key in (("walls", "wall_end", "wall_id"), ("framing", "element_end", "element_id")):
        for row in rows(group):
            number = element_id_or_none(row.get("id")) if isinstance(row, dict) else None
            if number is None:
                continue
            a, b = _xy(row, "a"), _xy(row, "b")
            if a is not None and b is not None:
                snap = {"kind": kind, id_key: number}
                ends.extend(((a, snap), (b, snap)))
                if group == "walls" and row.get("straight") is True:
                    walls[number] = (a, b)
    for row in rows("columns"):
        number = element_id_or_none(row.get("id")) if isinstance(row, dict) else None
        if number is not None and (point := _xy(row, "point")) is not None:
            ends.append((point, {"kind": "column", "element_id": number}))
    for row in rows("grids"):
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


def _alignment_axes(frame):
    """On the level plane, align to the model in 3D and to the view in plan."""
    if frame.get("kind") == "axonometric":
        return (1, 0), (0, 1)
    return frame["right"], frame["up"]


def _axis_end(anchor, end, axes):
    limit = math.tan(math.radians(AXIS_DEGREES))
    dx, dy = end[0] - anchor[0], end[1] - anchor[1]
    right, up = axes
    closest = 0.0
    result = end
    for (ax, ay), (nx, ny) in ((right, up), (up, right)):
        along, across = dx * ax + dy * ay, dx * nx + dy * ny
        if abs(along) > closest and abs(across / along) <= limit:
            closest = abs(along)
            result = anchor[0] + ax * along, anchor[1] + ay * along
    return result


def _square_up(points, snaps, walls, grids, axes, tolerance):
    """Align a single segment to an axis or to the wall or grid at its loose end."""
    if None in snaps:
        moving = 1 if snaps[1] is None else 0
        points[moving] = _axis_end(points[1 - moving], points[moving], axes)
    elif adjustment := _perpendicular_snap(points, snaps, 0, 1, walls, grids, tolerance):
        loose, foot = adjustment
        points[loose] = foot


def _perpendicular_snap(points, snaps, a, b, walls, grids, tolerance):
    """Return the loose vertex and its perpendicular foot from the opposite point snap."""
    if snaps[a] is None or snaps[b] is None:
        return None
    for fixed, loose in ((a, b), (b, a)):
        if snaps[fixed]["kind"] not in ("wall_end", "grid_intersection", "column", "element_end"):
            continue
        snap = snaps[loose]
        if snap["kind"] == "on_wall":
            foot, distance = _foot(points[fixed], *walls[snap["wall_id"]])
        elif snap["kind"] == "on_grid":
            foot, distance = _foot(points[fixed], *grids[snap["grid"]], infinite=True)
        else:
            continue
        if distance > 1 and math.dist(foot, points[loose]) <= 2 * tolerance:
            return loose, foot
    return None


def _nearly_square(incoming, outgoing, degrees=RIGHT_ANGLE_DEGREES):
    dx, dy = incoming
    ex, ey = outgoing
    lengths = math.hypot(dx, dy) * math.hypot(ex, ey)
    return lengths > 0 and abs(dx * ex + dy * ey) <= lengths * math.sin(math.radians(degrees))


def _edge_vectors(points, closed):
    ends = points[1:] + (points[:1] if closed else [])
    return [(bx - ax, by - ay) for (ax, ay), (bx, by) in zip(points, ends)]


def _square_closed(points, snaps):
    # Every corner qualifies. Alternate between the first edge and its perpendicular,
    # anchoring each edge at a snapped end or the start of the stroke when possible.
    dx, dy = points[1][0] - points[0][0], points[1][1] - points[0][1]
    edges = []
    for i in range(len(points)):
        j = (i + 1) % len(points)
        anchor = points[j] if j == 0 or snaps[j] is not None else points[i]
        edges.append((anchor, (anchor[0] + dx, anchor[1] + dy)))
        dx, dy = -dy, dx
    for i in range(1, len(points)):
        if snaps[i] is not None:
            continue
        corner = _crossing(*edges[i - 1], *edges[i])
        if corner is None:
            return False
        points[i] = corner
    return True


def _valid_square(points, candidate, original_edges, square, closed, tolerance):
    if not all(math.isfinite(v) for point in candidate for v in point):
        return False
    edges = _edge_vectors(candidate, closed)
    lengths = [math.hypot(*edge) for edge in original_edges]
    if any(math.hypot(*edge) < tolerance <= length for edge, length in zip(edges, lengths)):
        return False
    for i, (before, after) in enumerate(zip(points, candidate)):
        incoming = lengths[i - 1] if i or closed else 0
        outgoing = lengths[i] if i < len(lengths) else 0
        if math.dist(before, after) > max(2 * tolerance, 0.25 * max(incoming, outgoing)):
            return False
    return all(_nearly_square(edges[i - 1], edges[i], degrees=1) for i in square)


def _square_polyline(points, snaps, walls, grids, axes, closed, tolerance):
    """Use the snapped angles to select corners. Commit the edits only if the whole line fits."""
    edges = _edge_vectors(points, closed)
    square = [i for i in range(0 if closed else 1, len(edges)) if _nearly_square(edges[i - 1], edges[i])]
    mixed_outline = closed and len(square) != len(points)
    if mixed_outline:
        square = []

    candidate = points.copy()
    if snaps[1] is None and (not mixed_outline or snaps[0] is None):
        candidate[1] = _axis_end(points[0], points[1], axes)
    if not closed:
        first = _perpendicular_snap(points, snaps, 0, 1, walls, grids, tolerance)
        last = _perpendicular_snap(points, snaps, len(points) - 2, len(points) - 1, walls, grids, tolerance)
        # A two-segment stroke can ask for different feet at the same middle vertex.
        if first and last and first[0] == last[0] and first[1] != last[1]:
            return False
        for adjustment in (first, last):
            if adjustment:
                loose, foot = adjustment
                candidate[loose] = foot
        for i in square:
            if snaps[i + 1] is None:
                x, y = candidate[i]
                dx, dy = x - candidate[i - 1][0], y - candidate[i - 1][1]
                candidate[i + 1], _ = _foot(candidate[i + 1], (x, y), (x - dy, y + dx), infinite=True)
    elif square and not _square_closed(candidate, snaps):
        return False

    if not square and candidate == points:
        return False
    if not _valid_square(points, candidate, edges, square, closed, tolerance):
        return False
    points[:] = candidate
    return True


def _whole_mm(point, axes):
    right, up = axes
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


async def resolve_sketch(executor: RevitQueryExecutor, frame: dict, width: int, height: int, strokes, level=None) -> dict:
    """The designer's strokes as model lines snapped to elements in the view (see the module).
    ``{"error": "invalid_args", "message"}`` for strokes that cannot be read."""
    kind = frame.get("kind", "plan")
    if kind == "axonometric":
        levels = frame["levels"]
        chosen = next((row for row in levels if row["name"] == level), None)
        if chosen is None:
            names = ", ".join(_display_name(row["name"], 60) for row in levels[:20]) or "(none)"
            return {"error": "invalid_args", "message": f"Choose a level for the 3D sketch. Available levels: {names}."}
        elevation_mm = chosen["elevation_mm"]
    else:
        level, elevation_mm = frame["level"], frame["elevation_mm"]
    try:
        lines = sketch_lines(frame, width, height, strokes, elevation_mm)
    except ValueError as exc:
        return {"error": "invalid_args", "message": str(exc)}
    response = await executor.client.send_code(_TARGETS, parameters=[frame["view_uid"], frame["document_id"],
                                                                    level if kind == "axonometric" else ""])
    data = response.result
    if not response.success or not isinstance(data, dict):
        raise RevitQueryError(response.error or "Could not read the walls of the view")
    ends, walls, grids = _targets(data)
    per_pixel = (frame["u"][1] - frame["u"][0]) / width
    tolerance = max(SNAP_MIN_MM, SNAP_PX * per_pixel)
    axes = _alignment_axes(frame)
    out = []
    for number, line in enumerate(lines, start=1):
        closed = len(line) >= 4 and math.dist(line[-1], line[0]) <= tolerance
        points, snaps = [], []
        for point in (line[:-1] if closed else line):
            snapped, snap = _snap(point, ends, walls, grids, tolerance)
            # Round only free points. Snapped geometry keeps its model coordinates.
            points.append(snapped if snap else _whole_mm(snapped, axes))
            snaps.append(snap)
        squared = False
        if len(points) == 2:
            _square_up(points, snaps, walls, grids, axes, tolerance)
        else:
            squared = _square_polyline(points, snaps, walls, grids, axes, closed, tolerance)
        length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
        if closed:
            length += math.dist(points[-1], points[0])
        if length < 1:
            return {"error": "invalid_args", "message": "A line has no length after snapping; draw it longer"}
        out.append({"line": number, "length_mm": round(length), **({"closed": True} if closed else {}),
                    "points": [{"x": _coordinate_mm(x, squared), "y": _coordinate_mm(y, squared),
                                **({"snap": snap} if snap else {})} for (x, y), snap in zip(points, snaps)],
                    "pixels": [pixel_of(frame, width, height, x, y, elevation_mm) for x, y in points]})
    warnings = [] if data.get("complete") is True else [
        "The view has more walls than one read covers; some ends may not have snapped."]
    return {"view_name": frame["view_name"], "view_type": frame["view_type"], "view_kind": kind, "level": level,
            "elevation_mm": elevation_mm, "tolerance_mm": round(tolerance), "lines": out, "warnings": warnings}


# -- the text for the model ---------------------------------------------------------------------

def _display_name(name, limit):
    return " ".join(str(name or "").split()).replace('"', "'")[:limit]


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
            names = " and ".join(_display_name(name, 60) for name in snap["grids"])
            return f"{text} [the crossing of grids {names}]"
        if kind == "column":
            return f"{text} [column id {snap['element_id']}]"
        if kind == "element_end":
            return f"{text} [the end of element id {snap['element_id']}]"
        if kind == "on_grid":
            return f"{text} [on grid {_display_name(snap['grid'], 60)}]"
    return text


def sketch_block(resolved: dict) -> str:
    """The fixed text for the designer's sketch; "" when there is nothing drawn."""
    lines = resolved.get("lines") or []
    if not lines:
        return ""
    name = _display_name(resolved.get("view_name"), 200)
    level = _display_name(resolved.get("level"), 200)
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
    if resolved.get("view_kind") == "axonometric":
        view = f'3D view "{name}", projected onto level "{level}"'
    else:
        view = f'plan view "{name}"' + (f', level "{level}"' if level else "")
    return (f"Designer's sketch (drawn by the designer on a capture of the {view}"
            + "; the host converted it to model coordinates, x and y in millimetres): " + "; ".join(parts)
            + "." + guidance + " These coordinates are the designer's own input: use them as given, with source designer and "
              "evidence naming the line, e.g. \"sketch line 1 start\". A level name is not: read it from Revit "
              "(source tool).")

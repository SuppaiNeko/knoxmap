"""The sea, found from the coastline ways."""
from __future__ import annotations

import math

from ..osm import OSMFeature
from .projection import Projector


def sea_polygons(feats: list[OSMFeature], proj: Projector) -> list:
    """The sea inside the map, as shapely polygons in tile coordinates.

    OSM maps the sea only by its shore: natural=coastline ways, each running
    with the land on its left and the water on its right. The map rectangle is
    cut along every shore line into faces, and each face is sea or land by
    which side of its nearest stretch of shore it lies on. That handles a
    headland, a bay, an island - any mix, as long as the shore is in view.
    A map with no shore in it is taken to be land.
    """
    from shapely.geometry import LineString, Point, box
    from shapely.ops import nearest_points, polygonize, unary_union

    w, h = proj.width, proj.height
    frame = box(0, 0, w, h)
    shores = []
    for feat in feats:
        if feat.kind != "way" or feat.tags.get("natural") != "coastline":
            continue
        pts = [proj.to_px(la, lo) for la, lo in feat.geometry]
        if len(pts) >= 2:
            shores.append(LineString(pts))
    if not shores:
        return []
    # Join the shore into as few lines as possible, then carry any end that
    # stops inside the map on in its own direction to beyond the edge. The
    # download holds only coast that touches the area, so a shore that wanders
    # out of it and back arrives in pieces - and a line that does not cross
    # the map cannot split it, which left whole bays as dry land.
    from shapely.ops import linemerge
    merged = linemerge(unary_union(shores)) if len(shores) > 1 else shores[0]
    pieces = [merged] if isinstance(merged, LineString) else list(getattr(merged, "geoms", []))
    far = 3 * (w + h)
    inner = frame.buffer(-1)
    shores = []
    for line in pieces:
        coords = list(line.coords)
        if len(coords) < 2:
            continue
        if coords[0] == coords[-1]:
            shores.append(line)          # an island: closed, nothing to extend
            continue
        for end, before in ((0, 1), (-1, -2)):
            ex, ey = coords[end]
            if inner.contains(Point(ex, ey)):
                bx, by = coords[before]
                dx, dy = ex - bx, ey - by
                length = math.hypot(dx, dy) or 1.0
                tip = (ex + dx / length * far, ey + dy / length * far)
                coords = [tip] + coords if end == 0 else coords + [tip]
        shores.append(LineString(coords))
    # Clip the shore to a little beyond the frame, so lines that only graze
    # the edge still split it, and the faces stay inside the map.
    reach = frame.buffer(2)
    clipped = [s.intersection(reach) for s in shores]
    lines = [g for c in clipped for g in (getattr(c, "geoms", None) or [c])
             if not g.is_empty and g.length > 0]
    if not lines:
        return []
    faces = list(polygonize(unary_union(lines + [frame.exterior])))

    def seaward(face) -> bool:
        spot = face.representative_point()
        best, best_d = None, None
        for line in lines:
            d = line.distance(spot)
            if best_d is None or d < best_d:
                best, best_d = line, d
        coords = list(best.coords)
        at = best.project(nearest_points(best, spot)[0])
        # The segment the nearest point falls on.
        run = 0.0
        for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
            seg = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            if run + seg >= at or (x2, y2) == coords[-1]:
                break
            run += seg
        cross = (x2 - x1) * (spot.y - y1) - (y2 - y1) * (spot.x - x1)
        # Tile y grows southward, which mirrors the plane: "right of the line"
        # has a positive cross product here, where it is negative on a map.
        return cross > 0

    return [f.intersection(frame) for f in faces
            if f.intersection(frame).area > 1 and seaward(f)]

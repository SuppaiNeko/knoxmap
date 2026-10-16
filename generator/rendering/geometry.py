"""Small geometry helpers and the pixel drawing primitives the passes share."""
from __future__ import annotations

import math

from PIL import Image, ImageDraw

from ..osm import OSMFeature
from .projection import Projector


def _pixel_width(metres: float, metres_per_tile: float) -> int:
    """Convert a real width to the nearest whole bitmap tile, never zero."""
    return max(1, int(math.floor(metres / max(0.05, metres_per_tile) + 0.5)))


def _feature_coords_px(feat: OSMFeature, proj: Projector) -> list[list[tuple[float, float]]]:
    """Project every ring/linestring in this feature to pixel space."""
    if feat.kind == "way":
        return [[proj.to_px(la, lo) for la, lo in feat.geometry]]
    if feat.kind == "relation":
        rings: list[list[tuple[float, float]]] = []
        for _role, coords in feat.role_geoms:
            rings.append([proj.to_px(la, lo) for la, lo in coords])
        return rings
    if feat.kind == "node":
        la, lo = feat.geometry[0]
        return [[proj.to_px(la, lo)]]
    return []


def _is_polygon(feat: OSMFeature) -> bool:
    """Treat as polygon if first/last point match (way) or it's a relation."""
    if feat.kind == "relation":
        return True
    if feat.kind == "way" and len(feat.geometry) >= 3:
        return feat.geometry[0] == feat.geometry[-1]
    return False


def _paint_multipolygon(image: Image.Image, feat: OSMFeature, proj: Projector,
                        fill: tuple[int, int, int]) -> None:
    """Fill a relation's outer rings and leave its inner rings as they were.

    An island in a lake or a clearing in a wood is an inner ring. Drawn like
    the outers it was flooded or planted over; here it is cut out of a mask
    the size of the relation, so whatever lies under it shows through.
    """
    rings = [(role, [proj.to_px(la, lo) for la, lo in ring])
             for role, ring in feat.role_geoms if len(ring) >= 3]
    if not rings:
        return
    xs = [x for _r, ring in rings for x, _y in ring]
    ys = [y for _r, ring in rings for _x, y in ring]
    x0, y0 = max(0, int(min(xs))), max(0, int(min(ys)))
    x1, y1 = min(image.width, int(max(xs)) + 2), min(image.height, int(max(ys)) + 2)
    if x1 <= x0 or y1 <= y0:
        return
    mask = Image.new("L", (x1 - x0, y1 - y0), 0)
    md = ImageDraw.Draw(mask)
    for role, ring in sorted(rings, key=lambda r: r[0] == "inner"):
        md.polygon([(x - x0, y - y0) for x, y in ring],
                   fill=0 if role == "inner" else 255)
    image.paste(Image.new("RGB", mask.size, fill), (x0, y0), mask)


def _draw_polygon(draw: ImageDraw.ImageDraw, rings: list[list[tuple[float, float]]],
                  fill: tuple[int, int, int]) -> None:
    for ring in rings:
        if len(ring) >= 3:
            draw.polygon(ring, fill=fill)


def _draw_line(draw: ImageDraw.ImageDraw, rings: list[list[tuple[float, float]]],
               fill: tuple[int, int, int], width_px: int) -> None:
    w = max(1, int(round(width_px)))
    for ring in rings:
        if len(ring) >= 2:
            draw.line(ring, fill=fill, width=w, joint="curve")
            # Round line caps so intersections look right.
            r = w // 2
            if r > 0:
                for x, y in ring:
                    draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)

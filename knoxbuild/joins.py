"""Where one map's roads reach its edge, so the next map's can meet them.

Two maps standing side by side in the game's world share a border, and a
street that stops dead at it is the thing that gives a hand-drawn map away.
The drawing board has no world under it - that is the point of it - so there
was nothing to draw towards.

This reads the map already built: how big it is in metres, and the place on
each of its four edges where a road runs off it, taken from the road picture
the renderer draws (`<name>_road_hierarchy.bmp`, which is the class of road
over its whole right of way). What comes back is in metres along that edge,
measured from its own corner, which is what the board draws on its own sheet.

Nothing here moves anything or writes anything: it answers "where do its
roads come out".
"""
from __future__ import annotations

import json
import os

from .bitmaps import read_gray

# A run of road tiles this short on an edge is a kerb or a corner clipped by
# the border, not a street leaving the map.
MIN_RUN_TILES = 2
# And one this long is not a street either: a map whose edge runs along a
# dual carriageway has a hundred tiles of tarmac on it.
MAX_RUN_TILES = 120

SIDES = ("N", "S", "W", "E")
# Which side of the new sheet meets it, for each side of the map joined.
FACING = {"N": "S", "S": "N", "W": "E", "E": "W"}


def _info(out_dir: str, map_name: str) -> dict | None:
    path = os.path.join(out_dir, f"{map_name}_info.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _runs(line) -> list[tuple[int, int, int]]:
    """Runs of road along one edge, as (first tile, last tile, class)."""
    out = []
    start = None
    rank = 0
    for i, value in enumerate(line):
        if value:
            if start is None:
                start = i
                rank = int(value)
            else:
                rank = max(rank, int(value))
        elif start is not None:
            out.append((start, i - 1, rank))
            start = None
    if start is not None:
        out.append((start, len(line) - 1, rank))
    return out


def edges(out_dir: str, map_name: str) -> dict:
    """This map's size in metres and where its roads leave each side.

    Each road is `{"at": metres along the edge to the middle of it, "width":
    how wide it is in metres, "rank": the road class the renderer painted}`.
    A map built before the road picture was written has no roads here, only
    its size, which is still enough to draw a sheet that fits beside it.
    """
    info = _info(out_dir, map_name) or {}
    metres = float(info.get("meters_per_tile") or 1.0)
    width = int(info.get("width_tiles") or 0)
    height = int(info.get("height_tiles") or 0)
    out = {"name": map_name, "metersPerTile": metres,
           "widthM": round(width * metres, 1), "heightM": round(height * metres, 1),
           "widthTiles": width, "heightTiles": height,
           "roads": {side: [] for side in SIDES}}
    path = os.path.join(out_dir, f"{map_name}_road_hierarchy.bmp")
    if not width or not height or not os.path.exists(path):
        return out
    try:
        roads = read_gray(path, crop=(height, width))
    except (OSError, ValueError):
        return out
    lines = {"N": roads[0, :], "S": roads[-1, :],
             "W": roads[:, 0], "E": roads[:, -1]}
    for side, line in lines.items():
        for first, last, rank in _runs(line):
            span = last - first + 1
            if span < MIN_RUN_TILES or span > MAX_RUN_TILES:
                continue
            out["roads"][side].append({
                "at": round((first + last + 1) / 2 * metres, 1),
                "width": round(span * metres, 1),
                "rank": rank,
            })
    return out


def sheet_joins(out_dir: str, map_name: str, side: str,
                sheet_w: float, sheet_h: float) -> dict:
    """The same roads, placed on a sheet that sits on that side of the map.

    `side` is which side of the built map the new sheet is on - "E" means the
    sheet is east of it - so the roads come out on the sheet's own western
    edge, at the same distance along the border. The sheet is lined up with
    that map's corner, which is what makes the two line up in the world.

    Points are (x, y) in metres on the sheet, y up from its bottom-left
    corner, which is how the board and generator/sketch.py measure it.
    """
    side = (side or "E").upper()
    if side not in SIDES:
        raise ValueError(f"A map has four sides, not {side!r}.")
    found = edges(out_dir, map_name)
    meets = FACING[side]
    points = []
    for road in found["roads"][meets]:
        at = road["at"]
        if meets in ("N", "S"):
            # Along the top or bottom: the distance is across, measured from
            # the west corner on both maps.
            x = at
            y = sheet_h if side == "S" else 0.0
        else:
            # Down a side: the picture's rows run north to south and the sheet
            # measures from its bottom, so the distance is flipped.
            x = sheet_w if side == "W" else 0.0
            y = max(0.0, found["heightM"] - at)
        if 0.0 <= x <= sheet_w and 0.0 <= y <= sheet_h:
            points.append({"x": round(x, 1), "y": round(y, 1),
                           "width": road["width"], "rank": road["rank"],
                           "side": meets})
    return {**found, "side": side, "meets": meets, "joins": points}

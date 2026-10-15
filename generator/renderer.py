"""Rasterize OSM features into Project Zomboid bitmaps.

Output contract (per the Mapping Guide):
  <name>.bmp              — landscape (11 palette colors)
  <name>_veg.bmp          — vegetation (must be same size as landscape)
    <name>_road_hierarchy.bmp — grayscale OSM road class across right-of-way
  <name>_ZombieSpawnMap.bmp — grayscale, 1/10th resolution

Dimensions are snapped up to the next multiple of 300 (PZ cell size). The
requested real-world bbox is expanded symmetrically to fit that grid so the
meters-per-tile scale stays consistent.
"""
# <imports>
from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from typing import Iterable

from PIL import Image, ImageDraw, ImageFilter

import knoxstop

from . import pz_colors as C
from . import structures
from .osm import FENCE_BARRIERS, GATE_BARRIERS, OSMFeature, classify
# </imports>

# <reexports>
# Everything that moved out is still importable from here: the tests, the
# tools and knoxbuild reach into this module by name.
from .rendering.furniture import (CRACK_SHARE, DRAIN_EVERY,
                                  EDGE_LINE_MIN_WIDTH, GRIME_SHARE,
                                  HYDRANT_EVERY, LAMP_EVERY, LITTER_SHARE,
                                  ROAD_FURNITURE_INTERVALS, SPEED_BY_WIDTH,
                                  SPEED_SIGN_EVERY, _mod_tiles_ready,
                                  _paint_street_furniture)  # noqa: F401
from .rendering.geometry import (_draw_line, _draw_polygon, _feature_coords_px,
                                 _is_polygon, _paint_multipolygon,
                                 _pixel_width)  # noqa: F401
from .rendering.gridangle import (ALIGN_MIN_STRENGTH, DISTRICT_CELL_M,
                                  DISTRICT_CLUSTER_DEG, DISTRICT_MIN_ROAD_M,
                                  GRID_ROADS, _grid_delta, dominant_road_angle)  # noqa: F401
from .rendering.landcover import (AREA_CATEGORIES, DENSE_COVERAGE,
                                  GREEN_CATEGORIES, LANDSCAPE_FILL,
                                  LANDSCAPE_ORDER, VEG_CATEGORIES,
                                  WATERWAY_WIDTH_M)  # noqa: F401
from .rendering.projection import (Projector, _ceil_to, _inside, cover_bbox,
                                   shape_px)  # noqa: F401
from .rendering.roads import (_paint_road_details, _pave_dense_ground,
                              _real_kerbs, _road_hierarchy_image,
                              _smooth_road_edges, _weather_roads)  # noqa: F401
from .rendering.sea import sea_polygons  # noqa: F401
from .rendering.shape import THROUGH_ROADS, _clip_to_shape  # noqa: F401
from .rendering.widths import (ROAD_WIDTHS_BY_HIERARCHY_M, ROAD_WIDTHS_M,
                               SIDEWALK_BY_HIERARCHY_M, SIDEWALK_M,
                               VERGE_BY_HIERARCHY_M, VERGE_M, _road_markings,
                               _sidewalk_width_m, _verge_width_m, _way_width_m)  # noqa: F401
# </reexports>


# --- main entry point ------------------------------------------------------

@dataclass
class RenderResult:
    landscape_path: str
    vegetation_path: str
    spawn_map_path: str
    preview_path: str
    buildings_geojson_path: str
    meta_path: str
    width: int
    height: int
    cells_x: int
    cells_y: int


def render(features: Iterable[OSMFeature], south: float, west: float,
           north: float, east: float, meters_per_tile: float,
           output_dir: str, map_name: str,
           spawn_density: int = 10,
           tree_density: float = 1.0,
           forest_density: float = 1.0,
           rotation: float = 0.0,
           osm_cache: str | None = None,
           osm_bbox: tuple[float, float, float, float] | None = None,
           shape: dict | None = None,
           straight_roads: bool = True,
           should_stop=None) -> RenderResult:
    proj = Projector.build(south, west, north, east, meters_per_tile, rotation)
    # Read more than once below - the buildings pass goes back over the
    # address points - so never leave this as a generator.
    features = list(features)
    if straight_roads:
        # Every road in straight runs at 45-degree steps (octilinear.py).
        from .octilinear import straighten_roads
        straighten_roads(features, proj, classify, _is_polygon)
    # A drawn polygon, circle or real outline rather than a rectangle: the map
    # still covers its bounding box in whole cells, but only what lies inside
    # the shape is built.
    clip = shape_px(shape, proj)
    landscape = Image.new("RGB", (proj.width, proj.height), C.DARK_GRASS)
    vegetation = Image.new("RGB", (proj.width, proj.height), C.VEG_NOTHING)
    l_draw = ImageDraw.Draw(landscape)

    # Bucket features so we paint in a deterministic order.
    buckets: dict[str, list[OSMFeature]] = {}
    vegetation_feats: list[OSMFeature] = []
    building_feats: list[OSMFeature] = []
    indoor_feats: list[OSMFeature] = []
    fence_feats: list[OSMFeature] = []
    gate_feats: list[OSMFeature] = []
    place_feats: list[OSMFeature] = []
    monument_feats: list[OSMFeature] = []
    for feat in features:
        # Fences and hedges are collected from any outline that carries one,
        # before and independently of what the outline is: the fence around a
        # schoolyard is still a fence when the way is also the schoolyard.
        if feat.kind == "node" and "population" in feat.tags and "place" in feat.tags:
            place_feats.append(feat)
            continue
        if structures.monument_kind(feat.tags):
            monument_feats.append(feat)
        if feat.kind == "node" and feat.tags.get("barrier") in GATE_BARRIERS:
            gate_feats.append(feat)
        if feat.kind == "node" and "natural" not in feat.tags:
            continue      # shops and cafes inside buildings: knoxbuild/uses.py
        # Rooms a mapper drew inside a building. classify() has no colour for
        # them - they are walls, not ground - so they are taken aside here and
        # attached to their building when the geojson is written.
        if feat.kind == "way" and feat.tags.get("indoor") in ("room", "corridor"):
            indoor_feats.append(feat)
            continue
        barrier = feat.tags.get("barrier")
        if barrier in FENCE_BARRIERS and feat.kind == "way":
            fence_feats.append(feat)
        elif barrier == "hedge" and feat.kind == "way":
            vegetation_feats.append(feat)
        cat = classify(feat.tags, _is_polygon(feat))
        if cat is None:
            continue
        if cat in {"fence", "hedge"}:
            continue          # the line itself is collected below
        if cat in VEG_CATEGORIES:
            vegetation_feats.append(feat)
            if cat in {"forest", "scrub", "tree_single", "hedge"}:
                continue
        if cat == "building":
            if clip is not None and not _inside(feat, proj, clip):
                continue
            building_feats.append(feat)
        buckets.setdefault(cat, []).append(feat)

    # Bridges lifted over the roads they cross, and monuments. See
    # generator/structures.py; what they leave out of the ground is cut here.
    lifted = structures.Plan()
    structures.plan_bridges(buckets, proj, meters_per_tile, _way_width_m, lifted)
    structures.plan_piers(buckets, proj, meters_per_tile, _way_width_m, lifted)
    structures.plan_monuments(monument_feats, proj, meters_per_tile, lifted)
    if lifted.not_buildings:
        building_feats = [f for f in building_feats if id(f) not in lifted.not_buildings]
        buckets["building"] = [f for f in buckets.get("building", [])
                               if id(f) not in lifted.not_buildings]

    # Streets where every home is an address point and not a drawn building.
    addressed = _houses_from_addresses(features, building_feats, proj)
    if addressed:
        building_feats = building_feats + addressed
        buckets["building"] = buckets.get("building", []) + addressed

    def ground_rings(feat: OSMFeature) -> list[list[tuple[float, float]]]:
        rings = _feature_coords_px(feat, proj)
        cut = lifted.cut.get(id(feat))
        if cut is None:
            return rings
        from shapely.geometry import LineString as _Line
        left = _Line(rings[0]).difference(cut)
        return [list(g.coords) for g in getattr(left, "geoms", [left])
                if g.geom_type == "LineString" and not g.is_empty]

    # The sea first, under everything: a pier or a beach mapped over it
    # paints on top.
    for sea in sea_polygons(buckets.get("coastline", []), proj):
        for part in getattr(sea, "geoms", None) or [sea]:
            if hasattr(part, "exterior"):
                l_draw.polygon(list(part.exterior.coords), fill=C.WATER)
                for hole in part.interiors:
                    l_draw.polygon(list(hole.coords), fill=C.DARK_GRASS)

    for cat in LANDSCAPE_ORDER:
        if cat == "railway":
            # Pavements, as one pass over every road class and after the land
            # use: painted first, a park or a lawn mapped up to the kerb erased
            # the pavement and the tarmac met the grass. One pass so a side
            # street's pavement cannot cut across the high street it joins.
            for road in ("road_service", "road_minor", "road_medium", "road_major"):
                for feat in buckets.get(road, []):
                    if _is_polygon(feat):
                        continue
                    margin = _sidewalk_width_m(feat, road)
                    width_px = _pixel_width(_way_width_m(feat, road) + 2 * margin,
                                            meters_per_tile)
                    _draw_line(l_draw, ground_rings(feat),
                               C.PALE_CONCRETE, width_px)
            for road in ("road_service", "road_minor", "road_medium", "road_major"):
                for feat in buckets.get(road, []):
                    if _is_polygon(feat):
                        continue
                    verge = _verge_width_m(feat, road)
                    width_px = _pixel_width(_way_width_m(feat, road) + 2 * verge,
                                            meters_per_tile)
                    _draw_line(l_draw, ground_rings(feat),
                               C.DARK_GRASS, width_px)
        fill = LANDSCAPE_FILL.get(cat)
        if fill is None:
            continue
        for feat in buckets.get(cat, []):
            rings = _feature_coords_px(feat, proj)
            if cat in ROAD_WIDTHS_M and not _is_polygon(feat):
                width_px = _pixel_width(_way_width_m(feat, cat), meters_per_tile)
                _draw_line(l_draw, ground_rings(feat), fill, width_px)
            elif feat.kind == "relation":
                _paint_multipolygon(landscape, feat, proj, fill)
            elif _is_polygon(feat):
                _draw_polygon(l_draw, rings, fill)
            else:
                # A river or canal mapped only as its centre line.
                metres = WATERWAY_WIDTH_M.get(feat.tags.get("waterway"), 3.0)
                _draw_line(l_draw, rings, fill, _pixel_width(metres, meters_per_tile))

    # Bridges over water squared to the tiles (generator/structures.py).
    for deck, cat in lifted.straight:
        l_draw.rectangle(deck.bounds, fill=LANDSCAPE_FILL.get(cat, C.MEDIUM_ASPHALT))
    _pave_dense_ground(landscape, building_feats, buckets, proj)
    for paved in lifted.paving:
        l_draw.polygon(list(paved.exterior.coords), fill=C.PAVING)
    if clip is not None:
        _clip_to_shape(landscape, clip, buckets, proj)
    _weather_roads(landscape, proj)

    knoxstop.check(should_stop, "the terrain")
    # The woods the mapper drew take both dials; the gardens and the scrub
    # over open ground take the overall one only.
    _paint_vegetation(vegetation, landscape, vegetation_feats, proj,
                      density=tree_density * forest_density)
    _paint_gardens(vegetation, landscape, building_feats, proj, density=tree_density)
    _paint_wild_growth(vegetation, landscape, proj, density=tree_density)
    _clear_building_vegetation(vegetation, building_feats, proj)
    _clear_road_vegetation(vegetation, landscape)
    _paint_road_details(vegetation, landscape, buckets, proj)
    road_hierarchy = _road_hierarchy_image(buckets, proj)
    _paint_street_furniture(vegetation, landscape, road_hierarchy)
    # Nothing grows through a deck or a ramp, and no lamp stands on one.
    veg_px = vegetation.load()
    for x, y in lifted.clear_veg:
        if 0 <= x < proj.width and 0 <= y < proj.height:
            veg_px[x, y] = C.VEG_NOTHING
    structures.rail_water(lifted, landscape, C.WATER)
    ground_px = landscape.load()
    under = {C.WATER, C.MEDIUM_ASPHALT, C.DARK_ASPHALT, C.DARKEST_ASPHALT,
             C.LIGHT_ASPHALT, C.PALE_CONCRETE}
    structures.settle_posts(lifted, lambda x, y: 0 <= x < proj.width and 0 <= y < proj.height
                            and ground_px[x, y] not in under)

    # --- zombie spawn map (10x smaller, grayscale) ---
    spawn_w = proj.width // C.SPAWN_MAP_SCALE
    spawn_h = proj.height // C.SPAWN_MAP_SCALE
    spawn_map = _build_spawn_map(landscape, spawn_w, spawn_h, spawn_density)

    # --- preview (landscape + vegetation blended) ---
    preview = _build_preview(landscape, vegetation)

    # --- output files ---
    os.makedirs(output_dir, exist_ok=True)
    landscape_path = os.path.join(output_dir, f"{map_name}.bmp")
    veg_path = os.path.join(output_dir, f"{map_name}_veg.bmp")
    road_hierarchy_path = os.path.join(output_dir, f"{map_name}_road_hierarchy.bmp")
    spawn_path = os.path.join(output_dir, f"{map_name}_ZombieSpawnMap.bmp")
    preview_path = os.path.join(output_dir, f"{map_name}_preview.png")
    buildings_path = os.path.join(output_dir, f"{map_name}_buildings.geojson")
    meta_path = os.path.join(output_dir, f"{map_name}_info.json")

    landscape.save(landscape_path, format="BMP")
    vegetation.save(veg_path, format="BMP")
    road_hierarchy.save(road_hierarchy_path, format="BMP")
    # The ground as drawn, before knoxbuild paints front paths into it
    # (knoxbuild/yards.py), so building again starts from clean ground.
    landscape.save(os.path.join(output_dir, f"{map_name}_ground_base.bmp"), format="BMP")
    vegetation.save(os.path.join(output_dir, f"{map_name}_veg_base.bmp"), format="BMP")
    spawn_map.save(spawn_path, format="BMP")
    preview.save(preview_path, format="PNG")

    with open(buildings_path, "w", encoding="utf-8") as f:
        json.dump(_buildings_geojson(building_feats, indoor_feats), f)
    with open(os.path.join(output_dir, f"{map_name}_areas.geojson"), "w", encoding="utf-8") as f:
        json.dump(_areas_geojson(buckets), f)
    with open(os.path.join(output_dir, f"{map_name}_fences.geojson"), "w", encoding="utf-8") as f:
        json.dump(_lines_geojson([f for f in fence_feats
                                  if clip is None or _inside(f, proj, clip)]), f)
    with open(os.path.join(output_dir, f"{map_name}_fence_openings.geojson"), "w",
              encoding="utf-8") as f:
        json.dump(_openings_geojson(gate_feats, buckets), f)
    with open(os.path.join(output_dir, f"{map_name}_structures.json"), "w", encoding="utf-8") as f:
        json.dump({"bridges": lifted.bridges, "monuments": lifted.monuments,
                   "tiles": lifted.rows()}, f)
    with open(os.path.join(output_dir, f"{map_name}_places.json"), "w",
              encoding="utf-8") as f:
        json.dump(_places(place_feats, proj), f, ensure_ascii=False)

    cells_x, cells_y = proj.cell_grid()
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({
            "map_name": map_name,
            "bbox": {"south": south, "west": west, "north": north, "east": east},
            "rotation": rotation,
            "osm_cache": osm_cache,
            "straight_roads": bool(straight_roads),
            "osm_bbox": list(osm_bbox) if osm_bbox else None,
            "shape": shape,
            "meters_per_tile": meters_per_tile,
            "width_tiles": proj.width,
            "height_tiles": proj.height,
            "cells_x": cells_x,
            "cells_y": cells_y,
            "spawn_density_max": spawn_density,
            "building_count": len(building_feats),
            "feature_count": sum(len(v) for v in buckets.values()),
            # How many homes came from an address with no building drawn for
            # it, so "it made none of mine" is a number rather than a guess.
            "houses_from_addresses": len(addressed),
            "guide_reference": "Thuztor Mapping Guide v0.2",
        }, f, indent=2)

    return RenderResult(
        landscape_path=landscape_path,
        vegetation_path=veg_path,
        spawn_map_path=spawn_path,
        preview_path=preview_path,
        buildings_geojson_path=buildings_path,
        meta_path=meta_path,
        width=proj.width,
        height=proj.height,
        cells_x=cells_x,
        cells_y=cells_y,
    )


def _paint_vegetation(veg: Image.Image, landscape: Image.Image,
                      feats: list[OSMFeature], proj: Projector,
                      density: float = 1.0) -> None:
    _paint_vegetation_extras(veg, landscape, feats, proj)
    _paint_woodland(veg, landscape,
                    [f for f in feats if classify(f.tags, _is_polygon(f)) in
                     {"forest", "scrub", "tree_single"}], proj, density)


def _paint_woodland(veg: Image.Image, landscape: Image.Image,
                    feats: list[OSMFeature], proj: Projector,
                    density: float = 1.0) -> None:
    """Paint trees on the vegetation bitmap.

    Forest polygons get full density (TREES), scrub becomes bushes+trees,
    single-tree nodes become small dots. Forest edges get downgraded to a
    mix with dark grass so the transition isn't a hard rectangle.

    `density` below 1 thins the woodland by stepping every tile down one
    grade - full trees become a mix with dark grass, a mix becomes sparse,
    sparse becomes nothing - and above 1 it does the reverse. Which matters
    because trees are cover: a map buried in forest plays completely
    differently from the same map with hedgerows.
    """
    mask = Image.new("L", veg.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    scrub_mask = Image.new("L", veg.size, 0)
    scrub_draw = ImageDraw.Draw(scrub_mask)

    for feat in feats:
        cat = classify(feat.tags, _is_polygon(feat))
        rings = _feature_coords_px(feat, proj)
        if cat == "forest":
            if _is_polygon(feat):
                for ring in rings:
                    if len(ring) >= 3:
                        mask_draw.polygon(ring, fill=255)
        elif cat == "scrub":
            if _is_polygon(feat):
                for ring in rings:
                    if len(ring) >= 3:
                        scrub_draw.polygon(ring, fill=255)
        elif cat == "tree_single":
            if rings and rings[0]:
                x, y = rings[0][0]
                r = max(1, int(2 / proj.meters_per_tile))
                mask_draw.ellipse((x - r, y - r, x + r, y + r), fill=255)

    # Build a "border band" of the forest mask so we can paint edges lighter.
    eroded = mask.filter(ImageFilter.MinFilter(5))
    veg_px = veg.load()
    mask_px = mask.load()
    eroded_px = eroded.load()
    scrub_px = scrub_mask.load()
    land_px = landscape.load()

    # Thickest to thinnest. `density` shifts every tile along this ladder.
    GRADES = [C.VEG_NOTHING, C.SPARSE_TREES, C.TREES_DARK_GRASS, C.TREES]

    def graded(colour) -> tuple[int, int, int]:
        if density == 1.0:
            return colour
        i = GRADES.index(colour)
        shift = round((density - 1.0) * 2)
        return GRADES[min(max(i + shift, 0), len(GRADES) - 1)]

    w, h = veg.size
    for y in range(h):
        for x in range(w):
            if scrub_px[x, y]:
                veg_px[x, y] = C.BUSHES_TREES_DARK_GRASS
            if mask_px[x, y]:
                # Only paint trees on grass / dirt. Skip water, roads, buildings.
                lp = land_px[x, y]
                if lp == C.WATER:
                    continue
                shade = graded(C.TREES if eroded_px[x, y]
                               else C.TREES_DARK_GRASS)
                if shade == C.VEG_NOTHING:
                    continue
                veg_px[x, y] = shade
                # Trees on a grass tile → switch the landscape to DARK_GRASS
                # so the PZ renderer is happy (trees sit on dark grass best).
                if lp in (C.MEDIUM_GRASS, C.LIGHT_GRASS):
                    land_px[x, y] = C.DARK_GRASS


def _paint_vegetation_extras(veg: Image.Image, landscape: Image.Image,
                             feats: list[OSMFeature], proj: Projector) -> None:
    """Hedges, orchards, cemeteries and wetland, which are not woodland.

    Hedges are rows of bushes a metre or two wide along their line. Orchard
    trees go in on a regular grid - what makes an orchard recognisable from
    the air is that its trees stand in rows. Cemeteries get scattered flowers
    and the odd tree, wetland patches of dense bush.
    """
    import random

    rng = random.Random(0x5EED)
    draw = ImageDraw.Draw(veg)
    land_draw = ImageDraw.Draw(landscape)
    vp = veg.load()
    lp = landscape.load()
    w, h = veg.size

    def polygon_mask(feat):
        mask = Image.new("1", veg.size, 0)
        md = ImageDraw.Draw(mask)
        for ring in _feature_coords_px(feat, proj):
            if len(ring) >= 3:
                md.polygon(ring, fill=1)
        return mask

    for feat in feats:
        cat = classify(feat.tags, _is_polygon(feat))
        if feat.tags.get("barrier") == "hedge":
            width = max(1, int(round(1.5 / proj.meters_per_tile)))
            for ring in _feature_coords_px(feat, proj):
                if len(ring) >= 2:
                    draw.line(ring, fill=C.BUSHES, width=width)
        elif cat in {"orchard", "cemetery", "wetland"} and _is_polygon(feat):
            mask = polygon_mask(feat)
            bbox = mask.getbbox()
            if not bbox:
                continue
            mp = mask.load()
            x0, y0, x1, y1 = bbox
            spacing = max(2, int(round(4 / proj.meters_per_tile)))
            for y in range(y0, y1):
                for x in range(x0, x1):
                    if not mp[x, y] or lp[x, y] == C.WATER:
                        continue
                    if cat == "orchard":
                        if x % spacing == 0 and y % spacing == 0:
                            vp[x, y] = C.TREES
                            lp[x, y] = C.DARK_GRASS
                    elif cat == "cemetery":
                        # A few flowers between the graves, not instead of
                        # them: the headstones themselves go in later, as
                        # props (knoxbuild/props.py).
                        roll = rng.random()
                        if roll < 0.012:
                            vp[x, y] = C.TREES
                            lp[x, y] = C.DARK_GRASS
                        elif roll < 0.04:
                            vp[x, y] = C.FLOWERS
                    else:
                        if rng.random() < 0.35:
                            vp[x, y] = C.DENSE_BUSHES_GRASS
                            lp[x, y] = C.DARK_GRASS


# Garden planting. Trees came only from mapped woods, orchards and single-tree
# nodes, and suburbs rarely map their garden trees, so a real leafy suburb
# came out as houses standing on bare lawn. Yards - grass within reach of a
# house, clear of roads and paths - get a tree about every GARDEN_TREE_EVERY
# tiles each way, and a few shrubs against the house walls.
GARDEN_REACH_TILES = 18        # how far from a house its yard reaches
GARDEN_TREE_EVERY = 9
GARDEN_TREE_CHANCE = 0.6
GARDEN_CLEAR_OF_HOUSE = 3      # a tree trunk this far off the wall at least
GARDEN_CLEAR_OF_PAVING = 2     # and off the kerb, drive or path
SHRUB_CHANCE = 0.12            # per tile of lawn along a house wall
# Tufts over open grass. A lawn of one flat grass tile looked like felt beside
# the vanilla map, whose grass is broken up by clumps everywhere.
SHORT_GRASS_SHARE = 0.2
# Long grass is off: its rule mixes in flowerbed tiles, which came out as pink
# and blue squares all over every lawn.
LONG_GRASS_SHARE = 0.0
GRASS_COLOURS = (C.DARK_GRASS, C.MEDIUM_GRASS, C.LIGHT_GRASS)


# Rows of the map worked on at a time in _box_any. A whole town's summed-area
# table is four bytes a tile twice over - a 6000 x 5400 map needed a third of a
# gigabyte for one call, which is what ran a 32-bit Python out of memory. In
# strips it is a few megabytes, for the same answer.
BOX_STRIP_ROWS = 512


def _box_any(mask, radius: int):
    """True wherever `mask` is true within `radius` tiles (a square reach)."""
    import numpy as np

    h, w = mask.shape
    out = np.zeros((h, w), dtype=bool)
    k = 2 * radius + 1
    for top in range(0, h, BOX_STRIP_ROWS):
        bottom = min(h, top + BOX_STRIP_ROWS)
        # The strip, plus the rows on either side that can reach into it. Past
        # the top and bottom of the map there is nothing, which is what the
        # padding stands for, exactly as it did for the whole map at once.
        y0, y1 = max(0, top - radius), min(h, bottom + radius)
        block = np.pad(mask[y0:y1].astype(np.int32), radius + 1)
        ii = block.cumsum(0)
        ii = ii.cumsum(1)
        rows = y1 - y0
        total = (ii[k:k + rows, k:k + w] - ii[0:rows, k:k + w]
                 - ii[k:k + rows, 0:w] + ii[0:rows, 0:w])
        out[top:bottom] = total[top - y0:bottom - y0] > 0
    return out


def _paint_gardens(veg: Image.Image, landscape: Image.Image,
                   building_feats: list[OSMFeature], proj: Projector,
                   density: float = 1.0) -> int:
    """Trees in yards and shrubs along house walls. Returns trees planted."""
    import numpy as np

    if not building_feats or density <= 0:
        return 0
    houses = Image.new("1", veg.size, 0)
    hd = ImageDraw.Draw(houses)
    for feat in building_feats:
        for ring in _feature_coords_px(feat, proj):
            if len(ring) >= 3:
                hd.polygon(ring, fill=1)
    house = np.array(houses, dtype=bool)
    del houses
    ground = np.array(landscape.convert("RGB"))
    grass = np.zeros(house.shape, dtype=bool)
    for colour in GRASS_COLOURS:
        grass |= np.all(ground == colour, axis=2)
    del ground                       # a town's worth of pixels; let it go
    vegp = np.array(veg.convert("RGB"))
    empty = np.all(vegp == 0, axis=2)
    del vegp
    lawn = grass & empty & ~house
    del empty
    paved_near = _box_any(~grass & ~house, GARDEN_CLEAR_OF_PAVING)

    yard = lawn & _box_any(house, GARDEN_REACH_TILES)         & ~_box_any(house, GARDEN_CLEAR_OF_HOUSE) & ~paved_near
    rng = np.random.default_rng(1234)
    step = GARDEN_TREE_EVERY
    chance = min(1.0, GARDEN_TREE_CHANCE * density)
    h, w = house.shape
    px = veg.load()
    planted = 0
    for y0 in range(0, h, step):
        for x0 in range(0, w, step):
            if rng.random() >= chance:
                continue
            ys, xs = np.nonzero(yard[y0:y0 + step, x0:x0 + step])
            if len(xs) == 0:
                continue
            i = rng.integers(len(xs))
            px[int(x0 + xs[i]), int(y0 + ys[i])] = C.TREES
            planted += 1

    # Shrubs: lawn right against a wall, not in front of the paving.
    # The dice are rolled for the tiles in question, not for every tile on the
    # map: one roll each way over a town is a quarter of a gigabyte of floats.
    beside = lawn & _box_any(house, 1) & ~house & ~paved_near
    ys, xs = np.nonzero(beside)
    keep = rng.random(len(xs)) < SHRUB_CHANCE * density
    for x, y in zip(xs[keep].tolist(), ys[keep].tolist()):
        px[x, y] = C.BUSHES

    open_grass = lawn & ~beside & ~_box_any(house, 0)
    ys, xs = np.nonzero(open_grass)
    roll = rng.random(len(xs))
    for colour, share, lo in ((C.SHORT_GRASS, SHORT_GRASS_SHARE, 0.0),
                              (C.GRASS_ON_DARK, LONG_GRASS_SHARE, SHORT_GRASS_SHARE)):
        pick = (roll >= lo) & (roll < lo + share)
        for x, y in zip(xs[pick].tolist(), ys[pick].tolist()):
            if px[x, y] == C.VEG_NOTHING:
                px[x, y] = colour
    return planted


# Growth on land nobody mapped.
#
# Trees came only from mapped woods, orchards, single-tree nodes and the
# gardens of houses, so any ground OpenStreetMap says nothing about came out
# as an unbroken lawn - "more often than not it's the program getting
# confused and not just a field of nothing". Real open country has scrub and
# stands of trees in it wherever nobody is farming or mowing.
#
# It is kept off light grass, which is what farmland and churchyards are
# painted, so a wheat field stays a wheat field. And it clumps: one roll per
# tile puts a tree every so often everywhere, which is an orchard, while a
# slow noise field over the map gives thickets here and open ground there.
WILD_TREE_EVERY_M = 11.0
WILD_CLUMP_M = 70.0            # how far a thicket or a clearing runs
WILD_TREE_CHANCE = 0.75        # at the thickest, per patch of that size
WILD_BUSH_SHARE = 0.45         # of what grows, this much is scrub
WILD_CLEAR_OF_PAVING = 2       # tiles off a kerb, a drive or a path
WILD_GROUND = (C.DARK_GRASS, C.MEDIUM_GRASS)


def _paint_wild_growth(veg: Image.Image, landscape: Image.Image,
                       proj: Projector, density: float = 1.0) -> int:
    """Scatter trees and scrub over open grass. Returns what was planted."""
    import numpy as np

    if density <= 0:
        return 0
    ground = np.array(landscape.convert("RGB"))
    open_ground = np.zeros(ground.shape[:2], dtype=bool)
    for colour in WILD_GROUND:
        open_ground |= np.all(ground == colour, axis=2)
    del ground
    vegp = np.array(veg.convert("RGB"))
    bare = np.all(vegp == 0, axis=2)
    del vegp
    free = open_ground & bare
    del bare
    # Not up against a road, a pavement or a building: those tiles are the
    # verge, and the road details pass has its own plans for them.
    free &= ~_box_any(~open_ground, WILD_CLEAR_OF_PAVING)
    del open_ground

    h, w = free.shape
    step = max(2, int(round(WILD_TREE_EVERY_M / proj.meters_per_tile)))
    clump = max(step * 2, int(round(WILD_CLUMP_M / proj.meters_per_tile)))
    rng = np.random.default_rng(9871)
    field = rng.random(((h // clump) + 2, (w // clump) + 2)) ** 1.6
    px = veg.load()
    planted = 0
    for y0 in range(0, h, step):
        for x0 in range(0, w, step):
            thickness = field[y0 // clump, x0 // clump]
            if rng.random() >= min(1.0, WILD_TREE_CHANCE * thickness * density):
                continue
            ys, xs = np.nonzero(free[y0:y0 + step, x0:x0 + step])
            if len(xs) == 0:
                continue
            i = int(rng.integers(len(xs)))
            px[int(x0 + xs[i]), int(y0 + ys[i])] = (
                C.BUSHES if rng.random() < WILD_BUSH_SHARE else C.TREES)
            planted += 1
    return planted


def _clear_building_vegetation(veg: Image.Image, feats: list[OSMFeature],
                               proj: Projector) -> None:
    """No trees or bushes inside a building.

    Woodland polygons and single-tree nodes are mapped independently of the
    buildings standing in them, so a tree painted under a house came out as a
    tree growing through its living-room floor.
    """
    mask = Image.new("1", veg.size, 0)
    md = ImageDraw.Draw(mask)
    for feat in feats:
        for ring in _feature_coords_px(feat, proj):
            if len(ring) >= 3:
                md.polygon(ring, fill=1)
    blank = Image.new("RGB", veg.size, C.VEG_NOTHING)
    veg.paste(blank, (0, 0), mask)


def _clear_road_vegetation(veg: Image.Image, landscape: Image.Image) -> None:
    """Keep mapped woodland and scrub off every rasterized road surface."""
    import numpy as np

    road_colours = np.array((C.DARK_ASPHALT, C.MEDIUM_ASPHALT, C.LIGHT_ASPHALT,
                             C.DARKEST_ASPHALT, C.DARK_POTHOLE,
                             C.LIGHT_POTHOLE), dtype=np.uint8)
    width, height = landscape.size
    for top in range(0, height, 256):
        bottom = min(height, top + 256)
        ground = np.asarray(landscape.crop((0, top, width, bottom)))
        road = np.zeros(ground.shape[:2], dtype=bool)
        for colour in road_colours:
            road |= np.all(ground == colour, axis=2)
        if not road.any():
            continue
        vegetation = np.array(veg.crop((0, top, width, bottom)))
        vegetation[road] = C.VEG_NOTHING
        veg.paste(Image.fromarray(vegetation), (0, top))


def _places(feats: list[OSMFeature], proj: Projector) -> list[dict]:
    """Named places with an official population, and where they sit."""
    out = []
    for f in feats:
        raw = str(f.tags.get("population", "")).replace(",", "").replace(" ", "")
        try:
            population = int(float(raw.split(";")[0]))
        except (ValueError, OverflowError):      # OverflowError: "inf", "1e999"
            continue
        la, lo = f.geometry[0]
        x, y = proj.to_px(la, lo)
        out.append({"name": f.tags.get("name", ""), "place": f.tags.get("place"),
                    "population": population, "tile_x": round(x), "tile_y": round(y),
                    "inside": 0 <= x < proj.width and 0 <= y < proj.height})
    return out


AREA_TAGS = {"landuse", "amenity", "leisure", "name", "religion"}


def _areas_geojson(buckets: dict[str, list[OSMFeature]]) -> dict:
    """Land-use polygons, with the category each was painted as."""
    features = []
    for cat in AREA_CATEGORIES:
        for f in buckets.get(cat, []):
            if not _is_polygon(f):
                continue
            if f.kind == "way":
                rings = [[[lon, lat] for lat, lon in f.geometry]]
            else:
                rings = [[[lon, lat] for lat, lon in ring]
                         for role, ring in f.role_geoms if role != "inner"]
            polys = [[r] for r in rings if len(r) >= 3]
            if not polys:
                continue
            features.append({
                "type": "Feature",
                "properties": {"category": cat,
                               **{k: v for k, v in f.tags.items() if k in AREA_TAGS}},
                "geometry": {"type": "MultiPolygon", "coordinates": polys},
            })
    return {"type": "FeatureCollection", "features": features}


LINE_TAGS = {"barrier", "fence_type", "material", "height", "wall"}


def _lines_geojson(feats: list[OSMFeature]) -> dict:
    """Fence and wall lines for knoxbuild to turn into fence tiles."""
    features = []
    for f in feats:
        if f.kind != "way" or len(f.geometry) < 2:
            continue
        features.append({
            "type": "Feature",
            "properties": {k: v for k, v in f.tags.items() if k in LINE_TAGS},
            "geometry": {"type": "LineString",
                         "coordinates": [[lon, lat] for lat, lon in f.geometry]},
        })
    return {"type": "FeatureCollection", "features": features}


# Ways people walk through a fence on: the path to the playing field, the
# track to the yard. A fence drawn across one is drawn with a gap.
OPENING_PATHS = {"paved_path", "dirt_path", "pedestrian"}


def _openings_geojson(gates: list[OSMFeature], buckets: dict) -> dict:
    """Where a fence should have a way through it: gates, and the paths that
    cross one. The crossing itself is worked out where the fence is built
    (knoxbuild/fences.py), which is the only place that knows where the line
    ended up on the grid."""
    features = []
    for f in gates:
        if not f.geometry:
            continue
        lat, lon = f.geometry[0]
        features.append({
            "type": "Feature",
            "properties": {"opening": "gate",
                           "barrier": f.tags.get("barrier") or "gate"},
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
        })
    for cat in sorted(OPENING_PATHS):
        for f in buckets.get(cat, ()):
            if f.kind != "way" or len(f.geometry) < 2 or _is_polygon(f):
                continue
            features.append({
                "type": "Feature",
                "properties": {"opening": "path", "category": cat},
                "geometry": {"type": "LineString",
                             "coordinates": [[lon, lat] for lat, lon in f.geometry]},
            })
    return {"type": "FeatureCollection", "features": features}


def _build_spawn_map(landscape: Image.Image, w: int, h: int,
                     max_density: int) -> Image.Image:
    """Grayscale spawn map. Dense in built-up areas, zero over water.

    The red channel is read as a raw zombie density, and Build 42 reads it on a
    much smaller scale than you would guess from a 0-255 byte. Sampling the
    vanilla Knox County spawn map: values run 1..10 and 97% of the map is 0.
    Writing 96 here - a quarter of the byte range - buries the map in thousands
    of zombies. Everything below is expressed as a fraction of `max_density`,
    which now defaults to vanilla's ceiling of 10.
    """
    scaled = landscape.resize((w, h), Image.Resampling.BILINEAR)
    out = Image.new("RGB", (w, h), (0, 0, 0))
    sp = scaled.load()
    op = out.load()
    rng = random.Random(0xABBA)

    def band(chance: float, lo: float, hi: float) -> int:
        """Density between two fractions of the ceiling, `chance` of the time.

        Sparsity matters as much as the ceiling. Vanilla leaves 97% of the map
        at zero and averages about 0.06 per pixel; filling every pixel with a
        mid value - even a small one - still produces a wall of zombies. Town
        maps are denser than county-wide wilderness, but the shape should be
        the same: crowds on the streets, almost nothing in the fields.
        """
        if rng.random() > chance:
            return 0
        return int(round(rng.uniform(max_density * lo, max_density * hi)))

    for y in range(h):
        for x in range(w):
            r, g, b = sp[x, y]
            if r == C.WATER[0] and g == C.WATER[1] and b == C.WATER[2]:
                v = 0
            elif (75 <= r <= 175 and abs(r - g) < 30 and abs(g - b) < 30)                     or (r, g, b) == C.PAVING:
                # Asphalt, pavement and paved squares: where crowds belong.
                # The low end has to reach 75 now that the widest roads are
                # painted street4 at 80,80,80 - a range starting at 95 left
                # every main road through town as quiet as a field.
                v = band(0.40, 0.4, 1.0)
            elif r > g and r > 90 and g < 110:
                # Dirt tracks, yards, building footprints.
                v = band(0.15, 0.2, 0.5)
            elif g > r and g > 80:
                # Open grass: the odd wanderer, nothing more.
                v = band(0.03, 0.1, 0.2)
            else:
                v = band(0.08, 0.1, 0.3)
            op[x, y] = (v, v, v)
    return out


def _build_preview(landscape: Image.Image, vegetation: Image.Image) -> Image.Image:
    """Human-friendly PNG: landscape with trees painted dark green."""
    preview = landscape.copy()
    lp = preview.load()
    vp = vegetation.load()
    w, h = preview.size
    tree_colors = {C.TREES, C.TREES_DARK_GRASS, C.SPARSE_TREES,
                   C.BUSHES_TREES_DARK_GRASS}
    for y in range(h):
        for x in range(w):
            v = vp[x, y]
            if v in tree_colors:
                lp[x, y] = (40, 75, 35) if v == C.TREES else (65, 95, 45)
    return preview


# What survives into the geojson knoxbuild reads. amenity/shop/leisure carry
# what a building actually is far more reliably than the building tag alone,
# and they pick its materials and room plan.
#
# building:levels earns its place twice over: it is the only thing in OSM that
# says how tall a building is, and dropping it meant every block of flats in a
# town was rebuilt as a bungalow no matter what the mapper had recorded.
# Houses where the map has an address but no building.
#
# In whole countries - and in most American suburbs - the houses are not drawn
# at all; what the survey left behind is one node per home carrying
# addr:housenumber. Those streets came out as roads through empty grass. A
# node with an address on it is a building by definition, so one of a
# household's size is put there, upright on the grid; the rest of the build
# treats it like any other footprint, so it is skipped if a real building
# already covers the spot and it moves off the road like the others do.
ADDRESS_HOUSE_M = (9.0, 11.0)    # frontage, depth: a small detached house
ADDRESS_CLEAR_M = 4.0            # keep this far off a mapped building
ADDRESS_APART_M = 7.0            # and this far from the next address point
MAX_ADDRESS_HOUSES = 40000
# How far the near wall of the house stands back from the edge of the road.
#
# An address is a point on a street, and mappers put it anywhere from the
# doorstep to the middle of the carriageway. A house dropped where the point
# is therefore sat on the road often enough to read as roads going missing.
# Each one is pushed away from the nearest road until it is clear of it, and
# dropped if it cannot be.
ADDRESS_SETBACK_M = 2.0
ADDRESS_PUSH_M = 18.0            # as far as a house is moved to get clear
# Every road a house has to stand clear of: the ones with a carriageway. A
# footpath or a drive may run right past the door.
ADDRESS_AVOIDS_ROADS = {"road_major", "road_medium", "road_minor", "road_service",
                        "pedestrian"}


def _off_the_road(x: float, y: float, need: float, roads, road_half,
                  road_tree, push_limit: float):
    """Move a point away from the road nearest it until the house round it
    clears the carriageway. None when it cannot be cleared."""
    from shapely.geometry import Point

    for _ in range(6):
        here = Point(x, y)
        worst, gap = None, 0.0
        for i in road_tree.query(here.buffer(need + max(road_half, default=0.0))):
            i = int(i)
            short = need + road_half[i] - roads[i].distance(here)
            if short > gap:
                worst, gap = i, short
        if worst is None:
            return x, y
        # Straight away from the road it is closest to.
        near = roads[worst].interpolate(roads[worst].project(here))
        dx, dy = x - near.x, y - near.y
        length = (dx * dx + dy * dy) ** 0.5
        if length < 1e-6:
            return None                  # on the centre line; no way to know
        step = gap + 0.5
        if step > push_limit:
            return None
        x += dx / length * step
        y += dy / length * step
        push_limit -= step
        if push_limit <= 0:
            return None
    return None


def _houses_from_addresses(feats: list["OSMFeature"], buildings: list["OSMFeature"],
                           proj: "Projector") -> list["OSMFeature"]:
    """A house-sized footprint for every address that has no building."""
    from shapely.geometry import LineString, Point, Polygon
    from shapely.strtree import STRtree

    points = [f for f in feats
              if f.kind == "node" and f.geometry and f.tags.get("addr:housenumber")
              and not f.tags.get("building")]
    if not points:
        return []
    shapes = []
    for f in buildings:
        rings = _feature_coords_px(f, proj)
        for ring in rings:
            if len(ring) >= 3:
                poly = Polygon(ring)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if not poly.is_empty:
                    shapes.append(poly)
    tree = STRtree(shapes) if shapes else None
    # The roads themselves, each as a band of its own width, so a house can
    # be pushed off the one it belongs to.
    roads, road_half = [], []
    for f in feats:
        cat = classify(f.tags, _is_polygon(f))
        if cat not in ADDRESS_AVOIDS_ROADS:
            continue
        for ring in _feature_coords_px(f, proj):
            if len(ring) >= 2:
                roads.append(LineString(ring))
                road_half.append(_way_width_m(f, cat) / proj.meters_per_tile / 2)
    road_tree = STRtree(roads) if roads else None
    setback = ADDRESS_SETBACK_M / proj.meters_per_tile
    push_limit = ADDRESS_PUSH_M / proj.meters_per_tile
    clear = ADDRESS_CLEAR_M / proj.meters_per_tile
    apart = ADDRESS_APART_M / proj.meters_per_tile
    half_w = (ADDRESS_HOUSE_M[0] / proj.meters_per_tile) / 2
    half_h = (ADDRESS_HOUSE_M[1] / proj.meters_per_tile) / 2
    taken: dict[tuple[int, int], list] = {}
    cell = max(1.0, apart)
    made = []
    for f in points:
        if len(made) >= MAX_ADDRESS_HOUSES:
            break
        lat, lon = f.geometry[0]
        x, y = proj.to_px(lat, lon)
        if not (-half_w <= x < proj.width + half_w and -half_h <= y < proj.height + half_h):
            continue
        here = Point(x, y)
        if tree is not None:
            near = tree.query(here.buffer(clear))
            if any(shapes[int(i)].distance(here) <= clear for i in near):
                continue
        if road_tree is not None:
            spot = _off_the_road(x, y, max(half_w, half_h) + setback,
                                 roads, road_half, road_tree, push_limit)
            if spot is None:
                continue                 # nowhere off the road to put it
            x, y = spot
            here = Point(x, y)
        key = (int(x // cell), int(y // cell))
        crowd = [p for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                 for p in taken.get((key[0] + dx, key[1] + dy), ())]
        if any(abs(px - x) < apart and abs(py - y) < apart for px, py in crowd):
            continue
        taken.setdefault(key, []).append((x, y))
        ring = [(x - half_w, y - half_h), (x + half_w, y - half_h),
                (x + half_w, y + half_h), (x - half_w, y + half_h),
                (x - half_w, y - half_h)]
        tags = {k: v for k, v in f.tags.items() if k in BUILDING_TAGS}
        tags.setdefault("building", "house")
        made.append(OSMFeature(osm_id=f.osm_id, kind="way", tags=tags,
                               geometry=[proj.to_latlon(px, py) for px, py in ring]))
    return made


BUILDING_TAGS = {
    "building", "building:levels", "building:part", "building:material",
    "building:use", "height", "levels", "roof:levels", "roof:shape",
    "name", "addr:housenumber", "addr:street", "addr:flats",
    "amenity", "shop", "leisure", "tourism", "industrial",
    "healthcare", "office", "craft", "man_made", "residential", "cuisine",
}


def _buildings_geojson(feats: list[OSMFeature],
                       indoor: list[OSMFeature] = ()) -> dict:
    """Export building footprints so the user knows where to drop .tbx lots.

    Ways tagged indoor=room are attached to the building they stand in, as
    a "rooms" property: the mapper's own interior walls, which knoxbuild
    cuts the ground floor along instead of guessing at it
    (knoxbuild/build.py, knoxbuild/layout._mapped_rooms).
    """
    from shapely.geometry import MultiPolygon, Polygon

    features = []
    polys = []        # each building's shape, only built when rooms must fit
    for f in feats:
        if f.kind == "way":
            coords = [[lon, lat] for lat, lon in f.geometry]
            if len(coords) >= 3:
                features.append({
                    "type": "Feature",
                    # amenity/shop/leisure carry what a building actually is
                    # far more reliably than the building tag alone, and
                    # knoxbuild uses them to pick materials and room plans.
                    "properties": {k: v for k, v in f.tags.items()
                                   if k in BUILDING_TAGS},
                    "geometry": {"type": "Polygon", "coordinates": [coords]},
                })
                if indoor:
                    g = Polygon(coords)
                    polys.append(g if g.is_valid else g.buffer(0))
        elif f.kind == "relation":
            rings = []
            for role, ring in f.role_geoms:
                if role == "inner":
                    continue
                coords = [[lon, lat] for lat, lon in ring]
                if len(coords) >= 3:
                    rings.append(coords)
            if rings:
                features.append({
                    "type": "Feature",
                    "properties": {k: v for k, v in f.tags.items()
                                   if k in BUILDING_TAGS},
                    "geometry": {"type": "MultiPolygon", "coordinates":
                                 [[r] for r in rings]},
                })
                if indoor:
                    g = MultiPolygon([Polygon(r) for r in rings])
                    polys.append(g if g.is_valid else g.buffer(0))
    if indoor and features:
        import numpy as np
        # One representative point per room, against the bounding boxes of all
        # buildings first: a city can hold thousands of each, and only the
        # handful of buildings whose box holds the point are asked properly.
        boxes = np.array([g.bounds for g in polys])
        for f in indoor:
            if len(f.geometry) < 3:
                continue
            ring = [[lon, lat] for lat, lon in f.geometry]
            room = Polygon(ring)
            if not room.is_valid:
                room = room.buffer(0)
            if room.is_empty:
                continue
            p = room.representative_point()
            near = np.flatnonzero((boxes[:, 0] <= p.x) & (p.x <= boxes[:, 2])
                                  & (boxes[:, 1] <= p.y) & (p.y <= boxes[:, 3]))
            for i in near.tolist():
                if polys[i].contains(p):
                    features[i]["properties"].setdefault("rooms", []).append(
                        {"ring": ring,
                         "room": f.tags.get("room"),
                         "amenity": f.tags.get("amenity")})
                    break
    return {"type": "FeatureCollection", "features": features}

"""Zones the mapper draws, each with its own settings.

One set of settings for a whole map makes one kind of town. A real one is not
one kind: the centre is taller and denser than the edge, the estate on the
hill is all the same brick, the industrial end has no houses in it and the
quarantine block has ten times the zombies. Until now the only way to get any
of that was to build two maps and hope they met in the middle.

A zone is a polygon with a few settings written on it. Anything inside it is
built to those; anything outside is built to the map's own. Only the settings
that can sensibly differ from one part of a town to another are allowed
(OVERRIDABLE) - a zone cannot change the scale or the seed, because those
decide what the whole map is - and a zone may also name the wall style its
buildings are built in, which is the one thing that is not an ordinary
setting.

Zones nest, and the smallest one containing a point wins, the way
knoxbuild/areas.py does it: a street of shops inside a town centre inside a
map is read from the inside out.
"""
from __future__ import annotations

import json
import os
from dataclasses import replace

from shapely import STRtree
from shapely.geometry import Point, Polygon

from .settings import LIMITS, Settings

# What a zone may say about itself. These are the settings whose answer can
# honestly differ between two parts of one town; the rest decide what the map
# is rather than what a district is like, and are left alone:
#
#   seed            the map is one map, and a zone with its own seed would
#                   rebuild differently every time the zone was nudged
#   align_streets   the whole map is turned, or none of it is
#   rotate_degrees  the same
#   straight_roads  the roads are drawn once, before any zone is read
#   fill_gaps       the download happens before there are zones to read
#   building_alignment  a footprint is squared up before its centre is known
OVERRIDABLE = (
    # --- what is built ---
    "max_levels", "apartment_chance", "apartment_footprint", "true_map",
    "min_size", "max_size", "room_size", "neighbourhood_tiles",
    "style_oddity", "vanilla_tiles",
    # --- who is in it ---
    "spawn_density", "zombies_per_resident", "m2_per_person",
    # --- what is around it ---
    "tree_density", "parking_density",
)

# A zone may also pin the walls its buildings are built in - "the estate on
# the hill is all the same brick" - which is not a setting but a style name
# out of the catalogue (knoxbuild/build.py, pick_style).
STYLE_KEY = "wall_style"
# And the hand-built buildings it draws from, by name (knoxbuild/presets.py).
# A footprint in the zone takes one that fits inside it; one that nothing fits
# is laid out by the generator as usual, because a town with holes in it where
# no preset fitted is worse than one with generated houses among the built.
PRESETS_KEY = "presets"


def overridable() -> list[dict]:
    """The settings a zone may set, with their limits, for the window."""
    base = Settings().to_dict()
    out = []
    for name in OVERRIDABLE:
        lo, hi = LIMITS[name]
        out.append({"id": name, "default": base[name],
                    "min": lo, "max": hi,
                    "int": isinstance(base[name], int)})
    return out


def clean(raw) -> list[dict]:
    """Zones from untrusted input: a ring and the settings on it.

    A zone is kept as drawn - a ring of (lon, lat) - because that is what it
    was drawn as and what it has to be drawn as again.
    """
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        ring = []
        for point in item.get("ring") or []:
            try:
                ring.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError, IndexError):
                ring = []
                break
        if len(ring) < 3:
            continue
        values = {}
        for key, value in (item.get("settings") or {}).items():
            if key not in OVERRIDABLE:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number != number:          # NaN compares false with everything
                continue
            lo, hi = LIMITS[key]
            values[key] = min(max(number, lo), hi)
        style = item.get(STYLE_KEY)
        zone = {"name": str(item.get("name") or "")[:60], "ring": ring,
                "settings": values}
        if isinstance(style, str) and style.strip():
            zone[STYLE_KEY] = style.strip()[:40]
        chosen = item.get(PRESETS_KEY)
        if isinstance(chosen, list):
            names = [str(n).strip()[:60] for n in chosen if str(n).strip()]
            if names:
                zone[PRESETS_KEY] = names[:200]
        out.append(zone)
    return out


def load(out_dir: str, map_name: str) -> list[dict]:
    path = os.path.join(out_dir, f"{map_name}_zones.json")
    try:
        with open(path, encoding="utf-8") as f:
            return clean(json.load(f))
    except (OSError, ValueError):
        return []


def save(out_dir: str, map_name: str, zones) -> None:
    path = os.path.join(out_dir, f"{map_name}_zones.json")
    os.makedirs(out_dir, exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean(zones), f)
    os.replace(tmp, path)


class Zoning:
    """The zones of one map, ready to be asked about a point.

    Built once and asked once per building, so the polygons are projected into
    tile coordinates up front and indexed; asking is a tree lookup and a
    `contains`, not a walk through every zone.
    """

    def __init__(self, items: list[tuple[Polygon, dict]], settings: Settings):
        self._items = items
        self._tree = STRtree([p for p, _ in items]) if items else None
        self._base = settings
        # The same zone is asked about over and over - every building in a
        # district, every chunk of the spawn map - and a Settings is frozen,
        # so each zone's answer is worked out once.
        self._settled: dict[int, Settings] = {}

    @classmethod
    def build(cls, zones, proj, settings: Settings) -> "Zoning":
        items = []
        for zone in clean(zones):
            ring = [proj.to_px(lat, lon) for lon, lat in zone["ring"]]
            shape = Polygon(ring)
            if not shape.is_valid:
                shape = shape.buffer(0)
            if shape.is_empty or shape.area <= 0:
                continue
            items.append((shape, zone))
        return cls(items, settings)

    @classmethod
    def load(cls, out_dir: str, map_name: str, proj,
             settings: Settings) -> "Zoning":
        return cls.build(load(out_dir, map_name), proj, settings)

    def __len__(self) -> int:
        return len(self._items)

    def at(self, x: float, y: float) -> dict | None:
        """The zone a point is in: the smallest one, so zones can nest."""
        if self._tree is None:
            return None
        point = Point(x, y)
        best = None
        for i in self._tree.query(point):
            shape, zone = self._items[int(i)]
            if shape.contains(point) and (best is None or shape.area < best[0]):
                best = (shape.area, zone)
        return best[1] if best else None

    def settings_at(self, x: float, y: float) -> Settings:
        """The map's settings, with whatever the zone here says on top."""
        zone = self.at(x, y)
        if zone is None or not zone["settings"]:
            return self._base
        key = id(zone)
        settled = self._settled.get(key)
        if settled is None:
            fields = {f: type(getattr(self._base, f))(v)
                      for f, v in zone["settings"].items()}
            settled = self._settled[key] = replace(self._base, **fields)
        return settled

    def style_at(self, x: float, y: float) -> str | None:
        """The wall style this zone pins its buildings to, if it pins one."""
        zone = self.at(x, y)
        return zone.get(STYLE_KEY) if zone else None

    def presets_at(self, x: float, y: float) -> list[str]:
        """The hand-built buildings this zone draws from, by name."""
        zone = self.at(x, y)
        return list(zone.get(PRESETS_KEY) or ()) if zone else []

    def any_presets(self) -> bool:
        """Whether any zone names a preset, so the library is read once."""
        return any(z.get(PRESETS_KEY) for _shape, z in self._items)

    def chunk_values(self, keys, gw: int, gh: int, tiles_per_chunk: int) -> dict:
        """One value per chunk for each named setting, as arrays.

        The spawn map is drawn a chunk at a time over the whole map, and
        asking `settings_at` for every chunk of a big one is millions of
        point-in-polygon tests. The zones are painted into the chunk grid once
        instead, biggest first so a zone inside another one wins, and the
        answer is read off.
        """
        import numpy as np
        from PIL import Image, ImageDraw

        out = {key: np.full((gh, gw), float(getattr(self._base, key)),
                            dtype=float) for key in keys}
        if not self._items:
            return out
        for shape, zone in sorted(self._items, key=lambda it: -it[0].area):
            values = {k: zone["settings"][k] for k in keys
                      if k in zone["settings"]}
            if not values:
                continue
            mask = Image.new("1", (gw, gh), 0)
            ring = [(x / tiles_per_chunk, y / tiles_per_chunk)
                    for x, y in shape.exterior.coords]
            if len(ring) < 3:
                continue
            ImageDraw.Draw(mask).polygon(ring, fill=1)
            inside = np.asarray(mask, dtype=bool)
            if not inside.any():
                continue
            for key, value in values.items():
                out[key][inside] = float(value)
        return out

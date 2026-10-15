"""Files that belong to one map folder: its settings, edits, sketch and design.
"""
# <imports>
from __future__ import annotations

import json
import os
from pathlib import Path

from generator import edits as mapedits
from generator import osm, renderer
from knoxbuild.settings import Settings
# </imports>


SETTINGS_FILE = "settings.json"


def _settings_path(map_dir: Path) -> Path:
    return map_dir / SETTINGS_FILE


def _load_settings(map_dir: Path) -> Settings:
    """This map's saved settings, or the defaults."""
    try:
        with open(_settings_path(map_dir), encoding="utf-8") as f:
            return Settings.from_dict(json.load(f))
    except (OSError, ValueError):
        return Settings()


def _save_settings(map_dir: Path, settings: Settings) -> None:
    try:
        with open(_settings_path(map_dir), "w", encoding="utf-8") as f:
            json.dump(settings.to_dict(), f, indent=2)
    except OSError:
        pass          # a map whose settings cannot be saved still builds


# ---- editing what a map was built from ------------------------------------------
#
# The download stays the download: what the mapper changed about it is kept
# beside the map as a short list of changes, keyed by each feature's own OSM
# id (generator/edits.py). A town is a hundred thousand features and nearly
# all of them are untouched, so this is a few lines where a copy would be
# megabytes - and a re-download does not throw the editing away.
def _osm_cache_for(map_dir: Path, bbox, meters_per_tile: float,
                   settings: Settings) -> tuple[str, tuple]:
    """Where this map's download lives, and the box it covers.

    A map turned to its street grid reaches past the drawn box at its corners
    and is fetched as the circle round it, under its own name. The editor and
    the generator both ask here, so they cannot look in different files.
    """
    south, west, north, east = bbox
    if bool(settings.align_streets) or bool(settings.rotate_degrees):
        return (osm.cache_path(str(map_dir), f"{map_dir.name}_turned"),
                renderer.cover_bbox(south, west, north, east, meters_per_tile))
    return osm.cache_path(str(map_dir), map_dir.name), tuple(bbox)


def _edits_path(map_dir: Path) -> Path:
    return map_dir / f"{map_dir.name}_edits.json"


def _load_edits(map_dir: Path) -> dict:
    try:
        with open(_edits_path(map_dir), encoding="utf-8") as f:
            return mapedits.clean(json.load(f))
    except (OSError, ValueError):
        return mapedits.empty()


def _save_edits(map_dir: Path, changes: dict) -> None:
    path = _edits_path(map_dir)
    try:
        map_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.part")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mapedits.clean(changes), f)
        os.replace(tmp, path)
    except OSError:
        pass          # a map whose edits cannot be saved still renders


# ---- the drawn map --------------------------------------------------------------
#
# What was drawn by hand for a map, as GeoJSON, next to the map it belongs to.
# It is kept rather than converted and thrown away because it is the only copy:
# the features it becomes go into the render and are gone, so without this a
# map could never be opened and its drawing changed.
def _sketch_path(map_dir: Path) -> Path:
    return map_dir / f"{map_dir.name}_sketch.geojson"


def _load_sketch(map_dir: Path) -> dict:
    try:
        with open(_sketch_path(map_dir), encoding="utf-8") as f:
            drawn = json.load(f)
    except (OSError, ValueError):
        return {"type": "FeatureCollection", "features": []}
    return drawn if isinstance(drawn, dict) else {
        "type": "FeatureCollection", "features": []}


def _save_sketch(map_dir: Path, drawn: dict | None) -> None:
    """Written whole through a temporary file, so a crash part way cannot
    leave a half-written drawing where the drawing was."""
    if not isinstance(drawn, dict):
        return
    path = _sketch_path(map_dir)
    try:
        map_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".geojson.part")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(drawn, f)
        os.replace(tmp, path)
    except OSError:
        pass          # a map whose drawing cannot be saved still renders


def _design_path(map_dir: Path) -> Path:
    return map_dir / f"{map_dir.name}_design.json"


def _load_design(map_dir: Path) -> dict | None:
    try:
        with open(_design_path(map_dir), encoding="utf-8") as f:
            design = json.load(f)
    except (OSError, ValueError):
        return None
    return design if isinstance(design, dict) else None


def _save_design(map_dir: Path, design: dict) -> None:
    path = _design_path(map_dir)
    try:
        map_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.part")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(design, f)
        os.replace(tmp, path)
    except OSError:
        pass          # a map whose sheet cannot be saved still renders

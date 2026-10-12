"""Offline golden-map regression: render, furnish and Rust-compile three fixed towns.

    python tools/regression_maps.py                 # compare against committed hashes
    python tools/regression_maps.py --record        # intentionally replace the hashes

Requires the installed map-tool Rules.txt/Blends.txt and a built knoxlots binary.
No OpenStreetMap requests, installed-map changes, or WorldEd source are involved.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from generator.osm import OSMFeature  # noqa: E402
from generator import pz_colors, renderer  # noqa: E402
from knoxbuild.build import build  # noqa: E402
from knoxbuild.settings import Settings  # noqa: E402
from knoxbuild.world import WORLD_ORIGIN_CELLS, origin, set_origin  # noqa: E402
from tools.compile_map import compile_rust, knoxlots_exe  # noqa: E402
from tools.terrain_source import rules_path  # noqa: E402

GOLDEN = ROOT / "tests" / "fixtures" / "regression_maps" / "hashes.json"
SOUTH, WEST = 40.0, 20.0
SIZE_M = 260
LAT_PER_M = 1 / 111320
LON_PER_M = 1 / (111320 * math.cos(math.radians(SOUTH)))

SCENARIOS = {
    "dense_city": {"seed": 10101, "preset": "city", "min_buildings": 12},
    "village": {"seed": 20202, "preset": "rural", "min_buildings": 6},
    "coast": {"seed": 30303, "preset": "town", "min_buildings": 6},
}


def scenario_features(name: str) -> list[OSMFeature]:
    """Features in local metres: no remote data, randomness or installation state."""
    result = []

    def add(tags, points):
        latlons = [(SOUTH + y * LAT_PER_M, WEST + x * LON_PER_M) for x, y in points]
        result.append(OSMFeature(len(result) + 1, "way", tags, latlons))

    def rectangle(tags, x, y, w, h):
        add(tags, [(x, y), (x + w, y), (x + w, y + h),
                   (x, y + h), (x, y)])

    if name == "dense_city":
        for n in (65, 130, 195):
            add({"highway": "secondary", "name": f"City Street {n}"}, [(n, 0), (n, 260)])
            add({"highway": "residential", "name": f"City Avenue {n}"}, [(0, n), (260, n)])
        for row, y in enumerate((10, 76, 141, 206)):
            for col, x in enumerate((8, 73, 138, 203)):
                for j, dx in enumerate((0, 30)):
                    tags = {"building": "apartments", "building:levels": str(3 + (row + col + j) % 3)}
                    if row == 1 and col == 1 and j == 0:
                        tags = {"building": "retail", "shop": "supermarket", "name": "Market Square"}
                    if row == 2 and col == 2 and j == 1:
                        tags = {"building": "yes", "amenity": "police", "name": "City Police"}
                    rectangle(tags, x + dx, y + 7, 24, 26)
        rectangle({"leisure": "park", "name": "Pocket Park"}, 78, 175, 42, 15)
    elif name == "village":
        add({"highway": "residential", "name": "Village Road"},
            [(0, 130), (80, 126), (130, 133), (260, 130)])
        add({"highway": "residential", "name": "Mill Lane"}, [(120, 0), (128, 260)])
        for i, (x, y) in enumerate(((20, 45), (50, 72), (75, 24), (155, 34),
                                    (192, 55), (223, 20), (20, 155), (55, 204),
                                    (157, 165), (195, 220), (225, 170))):
            rectangle({"building": "house", "name": f"Village House {i}"}, x, y, 15, 14)
        rectangle({"building": "church", "name": "Village Chapel"}, 22, 98, 22, 18)
        rectangle({"building": "school", "name": "Village School"}, 181, 95, 28, 23)
        rectangle({"landuse": "farmland"}, 5, 230, 100, 25)
        rectangle({"landuse": "forest"}, 210, 225, 48, 33)
    elif name == "coast":
        # West-to-east coast puts land to the north (left), sea to the south.
        add({"natural": "coastline"}, [(0, 65), (105, 72), (170, 56), (260, 72)])
        add({"highway": "residential", "name": "Harbour Road"},
            [(0, 132), (90, 138), (175, 128), (260, 135)])
        add({"highway": "residential", "name": "Coast Lane"},
            [(120, 75), (124, 160), (120, 260)])
        add({"man_made": "pier"}, [(222, 85), (222, 24)])
        rectangle({"landuse": "forest"}, 12, 217, 50, 38)
        for i, (x, y) in enumerate(((15, 158), (42, 191), (68, 148), (90, 207),
                                    (146, 170), (172, 211), (205, 155), (229, 190))):
            rectangle({"building": "house", "name": f"Coastal House {i}"}, x, y, 16, 14)
        rectangle({"building": "retail", "shop": "convenience", "name": "Harbour Shop"},
                  18, 96, 24, 19)
        rectangle({"building": "yes", "amenity": "restaurant", "name": "Harbour Cafe"},
                  165, 98, 25, 19)
    else:
        raise ValueError(f"Unknown regression map: {name}")
    return result


def compiled_hashes(folder: Path) -> dict[str, str]:
    """Hash the actual binary lot files, by relative filename for diagnostics."""
    files = sorted(p for p in folder.iterdir() if p.is_file() and
                   (p.name.endswith(".lotheader") or p.name.endswith(".lotpack")
                    or p.name.startswith("chunkdata_") and p.name.endswith(".bin")))
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def compare(actual: dict, expected: dict) -> list[str]:
    errors = []
    for name in sorted(set(actual) | set(expected)):
        if name not in actual:
            errors.append(f"missing {name}")
        elif name not in expected:
            errors.append(f"unexpected {name}")
        elif actual[name] != expected[name]:
            errors.append(f"changed {name}: expected {expected[name]}, got {actual[name]}")
    return errors


def run_case(name: str, work: Path) -> dict:
    spec = SCENARIOS[name]
    folder = work / name
    settings = Settings.from_dict({"preset": spec["preset"], "seed": spec["seed"],
                                   "vanilla_tiles": 1, "fill_gaps": 0})
    # Pin the world origin. The normal build avoids installed maps; that would
    # turn an identical test into a different set of lot filenames per machine.
    with patch("knoxbuild.world.choose_origin", return_value=WORLD_ORIGIN_CELLS), \
            patch.dict(os.environ, {"KNOXBUILD_WORKERS": "1"}):
        renderer.render(scenario_features(name), SOUTH, WEST,
                        SOUTH + SIZE_M * LAT_PER_M, WEST + SIZE_M * LON_PER_M,
                        meters_per_tile=1.0, output_dir=str(folder), map_name=name,
                        spawn_density=settings.spawn_density,
                        tree_density=settings.tree_density, straight_roads=bool(settings.straight_roads))
        if name == "coast":
            with Image.open(folder / f"{name}.bmp") as image:
                pixels = (image.get_flattened_data() if hasattr(image, "get_flattened_data")
                          else image.getdata())
                water = sum(pixel == pz_colors.WATER for pixel in pixels)
                if water < image.width * image.height * 0.05:
                    raise RuntimeError("coast: coastline did not produce enough sea")
        with contextlib.redirect_stdout(io.StringIO()):
            status = build(str(folder), settings=settings)
        if status != 0:
            raise RuntimeError(f"{name}: building generation returned {status}")
        rows = list(csv.DictReader((folder / f"{name}_placements.csv").open(encoding="utf-8")))
        if len(rows) < spec["min_buildings"]:
            raise RuntimeError(f"{name}: {len(rows)} buildings, expected at least {spec['min_buildings']}")
        cells = compile_rust(str(folder), workers=1)
    files = compiled_hashes(folder / "lots")
    if cells < 1 or len(files) != 3 * cells:
        raise RuntimeError(f"{name}: {cells} cells but {len(files)} compiled files")
    return {"seed": spec["seed"], "preset": spec["preset"],
            "buildings": len(rows), "cells": cells, "files": files}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--record", action="store_true", help="explicitly replace golden hashes")
    args = ap.parse_args(argv)
    if not knoxlots_exe():
        ap.error("knoxlots is required: build rust/knoxlots with cargo build --release")
    try:
        rules_path()
    except FileNotFoundError as exc:
        ap.error(f"map tools are required: {exc}")
    if not args.record and not GOLDEN.is_file():
        ap.error(f"missing golden hashes at {GOLDEN}; use --record to create them")
    expected = {} if args.record else json.loads(GOLDEN.read_text(encoding="utf-8"))
    failed = False
    previous_origin = origin()
    try:
        with tempfile.TemporaryDirectory(prefix="knoxmap-regression-") as temp:
            work = Path(temp)
            results = {}
            for name in SCENARIOS:
                print(f"{name}: rendering, furnishing and compiling...", flush=True)
                result = run_case(name, work)
                results[name] = result
                problems = [] if args.record else compare(result, expected.get(name, {}))
                if problems:
                    failed = True
                    for problem in problems:
                        print(f"  FAIL {problem}", flush=True)
                else:
                    print(f"  {'recorded' if args.record else 'PASS'}: "
                          f"{result['buildings']} buildings, {result['cells']} cells, "
                          f"{len(result['files'])} files", flush=True)
            if args.record:
                GOLDEN.parent.mkdir(parents=True, exist_ok=True)
                GOLDEN.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
                print(f"Recorded golden hashes in {GOLDEN}")
            elif set(expected) != set(SCENARIOS):
                failed = True
                print("FAIL regression manifest has missing or extra scenarios")
    finally:
        set_origin(previous_origin)
    if failed:
        print("Regression map hashes differ. Inspect the changes before --record.")
        return 1
    print("All three compiled regression maps match." if not args.record else "Golden hashes recorded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

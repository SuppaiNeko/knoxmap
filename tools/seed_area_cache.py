"""Put the downloads of maps already made into the shared store.

    python tools/seed_area_cache.py [output-folder]

Every map keeps its own Overpass download (and Overture's, if it used it)
beside it. The shared store (generator/areacache.py) is what a new map is
made from without downloading, and it starts empty; this fills it from the
maps on disk, so they are sources at once. Each file is copied with its
filter list or release and its date as they were, so a stale one is still
seen as stale and an older filter list is still fetched again online.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generator import areacache, osm, overture  # noqa: E402


def _header(path: str) -> dict | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, EOFError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def seed(output_dir: str) -> int:
    copied = 0
    for root, _dirs, files in os.walk(output_dir):
        for name in files:
            if name.endswith("_osm.json.gz"):
                store, key = osm.AREA_CACHE_DIR, "filters"
                tag = lambda v: f"f{v}"  # noqa: E731
            elif name.endswith("_overture.json.gz"):
                store, key = overture.AREA_CACHE_DIR, "release"
                tag = str
            else:
                continue
            src = os.path.join(root, name)
            payload = _header(src)
            box = (payload or {}).get("bbox")
            if not payload or payload.get(key) is None or not (
                    isinstance(box, list) and len(box) == 4):
                print(f"  skipped {src}: not a download KnoxMap can read")
                continue
            dest = areacache.path_for(store, tuple(box), tag(payload[key]))
            if os.path.exists(dest):
                continue
            os.makedirs(store, exist_ok=True)
            shutil.copy2(src, dest)
            copied += 1
            print(f"  {src} -> {os.path.basename(dest)}")
    return copied


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
    print(f"{seed(folder)} download(s) added to the shared store")

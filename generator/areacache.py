"""Downloads kept by the area they cover, for any map that lies inside one.

The download beside a map (osm.cache_path) is only ever that map's: it is
used when the box matches to the metre, and the box moves with almost every
setting - the scale, turning to the street grid, the map's name, a nudge of
the outline. Each of those fetched the whole town again, for data that had
not moved.

This store is shared by every map. A download is filed under the box it
covers, and a map that fits inside one is cut out of it. A map that reaches
past one takes what it can from it and fetches only the strips that are left,
so making a map a little bigger costs its edges, not the town.

The box and a tag (the filter list, or the Overture release) are in the file's
name, so finding one is a directory listing and there is no index to keep in
step with the files.
"""
from __future__ import annotations

import os
import time

# Boxes are named to 7 decimal places, about a centimetre. Anything closer
# than this is the same edge, and a strip thinner than it is not a strip.
EPS = 1e-7
SUFFIX = ".json.gz"


def path_for(directory: str, box: tuple, tag: str) -> str:
    coords = "_".join(f"{v:.7f}" for v in box)
    return os.path.join(directory, f"{coords}__{tag}{SUFFIX}")


def _parse(name: str) -> tuple[tuple, str] | None:
    if not name.endswith(SUFFIX) or "__" not in name:
        return None
    coords, tag = name[:-len(SUFFIX)].split("__", 1)
    try:
        box = tuple(float(v) for v in coords.split("_"))
    except ValueError:
        return None
    return (box, tag) if len(box) == 4 else None


def area(box: tuple) -> float:
    s, w, n, e = box
    return max(0.0, n - s) * max(0.0, e - w)


def contains(outer: tuple, inner: tuple) -> bool:
    return (outer[0] <= inner[0] + EPS and outer[1] <= inner[1] + EPS
            and outer[2] >= inner[2] - EPS and outer[3] >= inner[3] - EPS)


def intersection(a: tuple, b: tuple) -> tuple | None:
    box = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return box if box[2] - box[0] > EPS and box[3] - box[1] > EPS else None


def intersects(a: tuple, b: tuple) -> bool:
    # Touching counts here: this is for features, and a road along the edge
    # of the map is on the map.
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def subtract(box: tuple, covered: tuple) -> list[tuple]:
    """`box` less `covered`, as at most four boxes that do not overlap.

    The strip south of `covered` and the one north of it run the full width
    of `box`; the west and east strips fill in between them.
    """
    s, w, n, e = box
    inner = intersection(box, covered)
    if inner is None:
        return [box]
    cs, cw, cn, ce = inner
    strips = [(s, w, cs, e), (cn, w, n, e), (cs, w, cn, cw), (cs, ce, cn, e)]
    return [b for b in strips if b[2] - b[0] > EPS and b[3] - b[1] > EPS]


def find(directory: str, box: tuple, tags: list[str] | None = None,
         max_age_s: float | None = None) -> list[tuple[str, tuple, str]]:
    """The stored downloads that overlap `box`, best first, as (path, box, tag).

    Best is one that holds the whole of `box`, the smallest of those first, so
    a village is not cut out of a county; then the ones that hold the most of
    it. Within each, a tag earlier in `tags` wins. `tags` None takes any tag;
    `max_age_s` None takes any age.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    rank = {t: i for i, t in enumerate(tags or [])}
    found = []
    now = time.time()
    for name in names:
        parsed = _parse(name)
        if parsed is None:
            continue
        stored, tag = parsed
        if tags is not None and tag not in rank:
            continue
        overlap = intersection(stored, box)
        if overlap is None:
            continue
        path = os.path.join(directory, name)
        if max_age_s is not None:
            try:
                if now - os.path.getmtime(path) > max_age_s:
                    continue
            except OSError:
                continue
        whole = contains(stored, box)
        key = (0 if whole else 1, rank.get(tag, 0),
               area(stored) if whole else -area(overlap))
        found.append((key, path, stored, tag))
    found.sort(key=lambda f: f[0])
    return [(path, stored, tag) for _key, path, stored, tag in found]


def prune(directory: str, box: tuple, tag: str, keep: str) -> int:
    """Remove the downloads with this tag that `box` now covers, all but
    `keep`. A merged download replaces the pieces it was made from."""
    gone = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        parsed = _parse(name)
        path = os.path.join(directory, name)
        if parsed is None or parsed[1] != tag or os.path.normcase(path) == os.path.normcase(keep):
            continue
        if contains(box, parsed[0]):
            try:
                os.remove(path)
                gone += 1
            except OSError:
                pass      # in use by another map; it is only a spare copy
    return gone

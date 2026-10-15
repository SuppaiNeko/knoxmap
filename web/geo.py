"""Where things are: the map tiles, the place search, landmarks and the Overpass servers.
"""
# <imports bp=geo>
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse

from flask import Blueprint, jsonify, request, send_file

from generator import osm, places

from web.checks import BIG_LANDMARK_KM2
from web.common import BASE_DIR, _bbox_area_km2, _json_body, log

bp = Blueprint("geo", __name__)
# </imports>


# ---- map tiles, fetched the way the OSM tile policy asks -------------------------
#
# https://operations.osmfoundation.org/policies/tiles/ - an installed app must
# identify itself with its own User-Agent (never a browser's), honour the
# server's caching headers or keep tiles at least 7 days, and never pre-fetch
# or bulk-download. The page's map used to load tiles straight from
# tile.openstreetmap.org inside the app window, which sent WebView2's browser
# identity and cached nothing. Tiles now come through here: fetched only when
# the map shows them, with KnoxMap's User-Agent, kept on disk and revalidated.
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
CACHE_DIR = BASE_DIR / "cache"
TILE_CACHE = CACHE_DIR / "tiles"
TILE_MIN_AGE = 7 * 24 * 3600
# How many tiles are fetched at once. A map view is twenty or thirty tiles,
# and at two at a time they arrived in a slow ripple; a browser asking
# tile.openstreetmap.org directly opens six connections, so four through one
# proxy is no heavier than the page would be on its own. This is interactive
# browsing, not the bulk downloading the tile policy asks people not to do.
TILE_FETCHES = threading.BoundedSemaphore(4)
# One connection pool for all of them. Without it every tile paid for a new
# TCP connection and a TLS handshake - most of the wait, on a map of tiles
# that are only a few kilobytes each.
TILE_TIMEOUT = (5, 15)          # connect, read
_TILE_SESSION = None
_TILE_SESSION_LOCK = threading.Lock()
# Tiles being revalidated in the background, so a stale one is not fetched
# once per request while the first fetch is still running.
_TILE_REVALIDATING: set = set()


def _tile_session():
    global _TILE_SESSION
    with _TILE_SESSION_LOCK:
        if _TILE_SESSION is None:
            import requests
            from requests.adapters import HTTPAdapter

            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8)
            session.mount("https://", adapter)
            session.headers["User-Agent"] = places.HEADERS["User-Agent"]
            _TILE_SESSION = session
    return _TILE_SESSION


def _fetch_tile(z: int, x: int, y: int, path, meta, info: dict) -> bool:
    """Fetch or revalidate one tile. True when it is on disk afterwards."""
    import requests

    headers = {}
    if path.exists() and info.get("etag"):
        headers["If-None-Match"] = info["etag"]
    try:
        with TILE_FETCHES:
            r = _tile_session().get(TILE_URL.format(z=z, x=x, y=y),
                                    headers=headers, timeout=TILE_TIMEOUT)
    except requests.RequestException:
        return path.exists()
    if r.status_code not in (200, 304):
        return path.exists()
    max_age = TILE_MIN_AGE
    for part in (r.headers.get("Cache-Control") or "").split(","):
        if part.strip().startswith("max-age="):
            try:
                max_age = max(TILE_MIN_AGE, int(part.split("=", 1)[1]))
            except ValueError:
                pass
    if r.status_code == 200:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
        info["etag"] = r.headers.get("ETag")
    info["expires"] = time.time() + max_age
    try:
        meta.write_text(json.dumps(info), encoding="utf-8")
    except OSError:
        pass
    return path.exists()


@bp.route("/tiles/<int:z>/<int:x>/<int:y>.png")
def tile(z: int, x: int, y: int):
    if not (0 <= z <= 19 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return ("", 404)
    path = TILE_CACHE / str(z) / str(x) / f"{y}.png"
    meta = path.with_suffix(".json")
    info = {}
    if path.exists() and meta.exists():
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
    fresh = path.exists() and time.time() < info.get("expires", 0)
    if path.exists() and not fresh:
        # A tile we already have is served straight away and checked against
        # the server behind the page's back. Waiting on that round trip made
        # panning back over somewhere already visited as slow as the first
        # time, for a picture of a street that had not changed in a week.
        key = (z, x, y)
        if key not in _TILE_REVALIDATING:
            _TILE_REVALIDATING.add(key)

            def revalidate():
                try:
                    _fetch_tile(z, x, y, path, meta, info)
                finally:
                    _TILE_REVALIDATING.discard(key)

            threading.Thread(target=revalidate, name=f"tile-{z}-{x}-{y}",
                             daemon=True).start()
    elif not fresh and not _fetch_tile(z, x, y, path, meta, info):
        return ("", 502)
    resp = send_file(path, mimetype="image/png")
    resp.headers["Cache-Control"] = f"max-age={TILE_MIN_AGE}"
    return resp


# Nominatim asks that results are cached on the application's side and that
# the same query is not sent again and again
# (https://operations.osmfoundation.org/policies/nominatim/).
_SEARCH_CACHE: dict[tuple, tuple[float, list]] = {}
SEARCH_CACHE_SECONDS = 24 * 3600


@bp.route("/api/search")
def api_search():
    """Find a place by name, so the map can jump to it."""
    q = request.args.get("q", "")
    if not q.strip():
        return jsonify({"results": []})

    viewbox = None
    try:
        viewbox = (float(request.args["south"]), float(request.args["west"]),
                   float(request.args["north"]), float(request.args["east"]))
    except (KeyError, ValueError):
        pass
    bounded = request.args.get("bounded") == "1"

    key = (q.strip().lower(), viewbox and tuple(round(v, 3) for v in viewbox), bounded)
    cached = _SEARCH_CACHE.get(key)
    if cached and time.time() - cached[0] < SEARCH_CACHE_SECONDS:
        return jsonify({"results": cached[1]})
    try:
        results = places.search(q, viewbox=viewbox, bounded=bounded)
    except Exception as exc:  # network, rate limit, bad JSON
        return jsonify({"error": f"Search failed: {exc}"}), 502
    if len(_SEARCH_CACHE) > 500:
        _SEARCH_CACHE.clear()
    _SEARCH_CACHE[key] = (time.time(), results)
    return jsonify({"results": results})


@bp.route("/api/landmarks", methods=["POST"])
def api_landmarks():
    """List the named landmarks inside a bbox."""
    data = _json_body()
    try:
        south = float(data["south"])
        west = float(data["west"])
        north = float(data["north"])
        east = float(data["east"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Need south, west, north and east."}), 400

    landmark_area = _bbox_area_km2(south, west, north, east)
    if landmark_area > BIG_LANDMARK_KM2:
        # Slow, not impossible. It used to be refused outright.
        log.info("landmarks: %.1f km2, over the %g km2 this gets slow at",
                 landmark_area, BIG_LANDMARK_KM2)

    try:
        found = places.landmarks(south, west, north, east)
    except Exception as exc:
        return jsonify({"error": f"Lookup failed: {exc}"}), 502
    return jsonify({"landmarks": found})


@bp.route("/api/overpass", methods=["GET", "POST"])
def api_overpass():
    """Which Overpass servers map data is downloaded from.

    The public ones by default; your own (docs/SELF_HOSTING_OVERPASS.md) when
    named here. Saved as "overpass_endpoints" in knoxmap_config.json and used
    at once, so a download started after saving already goes there."""
    import knoxpaths

    if request.method == "POST":
        raw = _json_body().get("endpoints")
        if raw is None or raw == "":
            raw = []
        if isinstance(raw, str):
            raw = re.split(r"[\s,;]+", raw)
        if not isinstance(raw, list):
            return jsonify({"error": "Give the servers as a list of addresses."}), 400
        urls = []
        for item in raw:
            url = str(item).strip()
            if not url:
                continue
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return jsonify({"error": f"\"{url}\" is not an http(s) address."}), 400
            if url not in urls:
                urls.append(url)
        if len(urls) > 8:
            return jsonify({"error": "At most 8 servers."}), 400
        knoxpaths.update_config({"overpass_endpoints": urls})
        osm.set_endpoints(urls)
        log.info("overpass servers: %s", urls or "the public ones")
    try:
        configured = knoxpaths.load_config().get("overpass_endpoints")
    except Exception:  # noqa: BLE001
        configured = None
    return jsonify({"endpoints": [str(u) for u in configured] if isinstance(configured, list) else [],
                    "using": list(osm.OVERPASS_ENDPOINTS),
                    "public": list(osm._DEFAULT_ENDPOINTS)})

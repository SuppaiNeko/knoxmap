"""Knoxify — Flask entry point.

Real-world areas → Project Zomboid maps.

Run:
    source .venv/bin/activate
    python app.py

Then open http://127.0.0.1:5000/ in a browser.
"""
# <imports>
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path

from flask import (Flask, jsonify, render_template, request, send_file,
                   send_from_directory)

import knoxlog
import knoxstop
from generator import edits as mapedits
from generator import renderer, sketch
from knoxbuild import mapstate
from knoxbuild.settings import Settings

from web import common, geo, jobs, presets, system, updates
from web.checks import (BASE_BYTES, BUILD_BASE_BYTES, BUILD_BYTES_PER_TILE,
                        BYTES_PER_TILE, OVERPASS_TILE_KM2,
                        USUAL_METERS_PER_TILE, _normalise_bbox, _shape_rings)
from web.common import (SAFE_NAME, _bbox_area_km2, _cos_lat, _json_body,
                        _map_dir, _request_context, _worlded_exe, failed, log)
from web.geo import CACHE_DIR
from web.jobs import (_COMPILE, _PROGRESS_LOCK, _done_stopping, _set_progress,
                      _stopped, _stopper)
from web.mapfiles import (_load_design, _load_edits, _load_settings,
                          _load_sketch, _osm_cache_for, _save_design,
                          _save_edits, _save_settings, _save_sketch)
# </imports>


# Builds print place names in any script; a console on a legacy code page
# would raise mid-request on the first one it cannot show.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

app = Flask(__name__, template_folder="templates", static_folder="static")

# Only this PC may talk to the app. The server listens on 127.0.0.1, but a web
# page in any browser on the PC can still point a hostname of its own at that
# address ("DNS rebinding") and call the API as if it were the app's own page.
# Such a request carries the attacker's hostname, so anything not addressed to
# localhost is refused before it reaches a route.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


@app.errorhandler(Exception)
def _api_error(exc):
    """Every failure as JSON the page can show, with the details in the log.

    Flask's default answer to an exception is an HTML page. The page reads
    every reply as JSON, so a crash anywhere in a request showed only
    "Unexpected token '<', "<!doctype"... is not valid JSON", which says
    nothing about what went wrong or where.
    """
    from werkzeug.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        if not request.path.startswith("/api/"):
            return exc
        return jsonify({"error": f"{exc.code} {exc.name}"}), exc.code
    return failed(f"{type(exc).__name__}: {exc}", 500, exc)


@app.after_request
def _log_refusals(response):
    """Every error the API answers with goes in the log, not just crashes: a
    "Missing or invalid bbox" is as much a clue as a traceback."""
    if (request.path.startswith("/api/") and response.status_code >= 400
            and response.is_json):
        data = response.get_json(silent=True) or {}
        if data.get("stopped"):
            return response          # asked for; already in the log as a stop
        if not data.get("errorId"):
            eid = knoxlog.record(None, f"{request.method} {request.path} -> "
                                       f"{response.status_code}: {data.get('error')}",
                                 **_request_context())
            data["errorId"] = eid
            response.set_data(json.dumps(data))
    return response


@app.before_request
def _only_local():
    host = (request.host or "").rsplit(":", 1)[0] if not (request.host or "").startswith("[")         else (request.host or "").split("]")[0] + "]"
    if host not in LOCAL_HOSTS:
        return ("KnoxMap only answers requests from this computer.", 403)
    # A page open in the browser can send this server a plain POST with no body
    # (open the logs, restart for an update, change the language): the browser
    # allows it and the Host above is ours. Browsers say where such a request
    # came from, so one that came from somewhere else is refused. A request with
    # no Origin (the test client, curl) is not a browser's and passes.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin is not None:
            try:
                origin_host = urllib.parse.urlparse(origin).hostname
            except ValueError:
                origin_host = None
            if origin_host not in LOCAL_HOSTS:
                return ("KnoxMap does not take requests from other websites.", 403)
        elif request.headers.get("Sec-Fetch-Site") == "cross-site":
            return ("KnoxMap does not take requests from other websites.", 403)


# Source cells per side handed to one WorldEd process. 4x4 keeps its peak
# memory around a gigabyte; larger batches are faster per cell but climb.
COMPILE_BATCH = 4


@app.route("/")
def index():
    # The app window cannot download a file the way a browser does - clicking
    # a download link there did nothing at all - so the page asks the server
    # to save it and show it in Explorer instead. See /api/save.
    return render_template("index.html", version=knoxlog.version(),
                           in_window=os.environ.get("KNOXMAP_WINDOW") == "1")


@app.route("/api/generate", methods=["POST"])
def generate():
    data = _json_body()
    # A map drawn on the editor's own sheet brings its own size and its own
    # corner of the world; the box and the features both come out of it, so
    # there is nothing to draw on the world map and nothing to download.
    design = data.get("design")
    if isinstance(design, dict):
        south, west, north, east = sketch.design_bbox(design)
        data = {**data, "south": south, "west": west, "north": north,
                "east": east, "shape": None, "drawnOnly": True,
                "sketch": sketch.design_to_drawn(design)}
    shape, problem = _clean_shape(data.get("shape"))
    if problem:
        return jsonify({"error": problem}), 400
    if shape:
        # A drawn shape decides the box: its own bounds, whatever the page sent.
        lons = [p[0] for ring in _shape_rings(shape) for p in ring]
        lats = [p[1] for ring in _shape_rings(shape) for p in ring]
        data = {**data, "south": min(lats), "north": max(lats),
                "west": min(lons), "east": max(lons)}
    try:
        south = float(data["south"])
        west = float(data["west"])
        north = float(data["north"])
        east = float(data["east"])
        meters_per_tile = float(data.get("metersPerTile", 1.0))
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Missing or invalid bbox / scale."}), 400

    import math
    if not all(math.isfinite(v) for v in (south, west, north, east, meters_per_tile)):
        return jsonify({"error": "Missing or invalid bbox / scale."}), 400
    (south, west, north, east), problem = _normalise_bbox(
        south, west, north, east)
    if problem:
        return jsonify({"error": problem}), 400
    if not (MIN_METERS_PER_TILE <= meters_per_tile <= MAX_METERS_PER_TILE):
        return jsonify({"error": f"Metres per tile has to be a whole number "
                                 f"between {MIN_METERS_PER_TILE:g} and "
                                 f"{MAX_METERS_PER_TILE:g}."}), 400
    # Whole metres only: 1.5 would put every building, road and lot on a grid
    # that is not a multiple of the survey's own.
    meters_per_tile = tidy_scale(meters_per_tile)

    # Nothing below refuses the map. These are what the window has already
    # warned about; they are logged here so a report from somebody whose map
    # died halfway says how big it was.
    heavy: list[str] = []
    area_km2 = _bbox_area_km2(south, west, north, east)
    if area_km2 > BIG_AREA_KM2:
        heavy.append(f"{area_km2:.0f} km2, over the {BIG_AREA_KM2:g} km2 a map "
                     f"usually is")
    if not (USUAL_METERS_PER_TILE[0] <= meters_per_tile <= USUAL_METERS_PER_TILE[1]):
        heavy.append(f"{meters_per_tile:g} m a tile, outside the "
                     f"{USUAL_METERS_PER_TILE[0]:g}-{USUAL_METERS_PER_TILE[1]:g} "
                     f"the window offers")

    raw_name = data.get("mapName")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raw_name = f"knoxify_{int(time.time())}"
    map_name = SAFE_NAME.sub("_", raw_name).strip("_") or f"knoxify_{int(time.time())}"

    # Upper bound on the final bitmap size before we even hit Overpass.
    approx_w = ((east - west) * 111320 * _cos_lat((south + north) / 2)) / meters_per_tile
    approx_h = ((north - south) * 111320) / meters_per_tile
    if max(approx_w, approx_h) > BIG_TILES_PER_SIDE:
        heavy.append(f"{int(approx_w)}x{int(approx_h)} tiles, over the "
                     f"{BIG_TILES_PER_SIDE} a side that is comfortable")
    # The one size that is refused: WorldEd cannot compile it, so a map this big
    # would be made over most of an hour and then fail at the compile.
    from tools.compile_map import WORLDED_MAX_PIXELS, bitmap_pixels, default_backend, scale_that_fits
    pixels = bitmap_pixels(int(approx_w), int(approx_h))
    if pixels > WORLDED_MAX_PIXELS and default_backend() != "rust":
        fits = scale_that_fits(int(approx_w), int(approx_h), meters_per_tile)
        return jsonify({"error": (
            f"That map would be about {int(approx_w)} x {int(approx_h)} tiles "
            f"({pixels / 1e6:.0f} million), and WorldEd cannot compile a map past "
            f"{WORLDED_MAX_PIXELS / 1e6:.0f} million. "
            + (f"At {fits} m a tile it fits, or draw a smaller area."
               if fits else "Draw a smaller area."))}), 400
    tight = _too_big_for_memory(approx_w, approx_h)
    if tight:
        heavy.append(tight)
    if heavy:
        log.warning("generate %s: going ahead with a heavy map - %s",
                    map_name, "; ".join(heavy))

    t0 = time.time()
    log.info("generate %s: %.5f,%.5f,%.5f,%.5f at %s m/tile, %.2f km2", map_name,
             south, west, north, east, meters_per_tile, area_km2)
    map_dir = common.OUTPUT_DIR / map_name
    # A folder this request makes is taken away again if the download fails: it
    # holds nothing but the settings, and every failed attempt left one behind.
    made_here = not map_dir.exists()
    map_dir.mkdir(parents=True, exist_ok=True)

    def forget_empty_folder() -> None:
        if made_here and not (map_dir / f"{map_name}_info.json").exists():
            shutil.rmtree(map_dir, ignore_errors=True)

    bbox = (south, west, north, east)
    # Remembered next to the map, so Generate buildings and any later rebuild
    # use the same ones without the page having to send them again - and so a
    # map from last week can be reproduced exactly.
    settings = Settings.from_dict(data.get("settings"))
    _save_settings(map_dir, settings)

    # The drawn map, kept beside the real one. The page sends it when there is
    # one; when there is not, whatever was drawn for this map last time is used
    # again, so Generate map twice over does not quietly lose it.
    if isinstance(design, dict):
        _save_design(map_dir, design)
    drawn = data.get("sketch")
    if drawn is None:
        drawn = _load_sketch(map_dir)
    else:
        _save_sketch(map_dir, drawn)
    drawn_only = bool(data.get("drawnOnly"))

    # What to download. A map turned to its street grid (see
    # renderer.dominant_road_angle) reaches past the drawn box at its corners,
    # and the angle is only known once the streets are in. This used to fetch
    # the box, measure the angle, then fetch the turned map's bounds as well -
    # the same town twice over, about 2.4 times the data. Now one download
    # covers the map at any angle: the circle round it, as a box.
    cache, fetch_box = _osm_cache_for(map_dir, bbox, meters_per_tile, settings)

    # Regenerating the same area is the common case - it is how a map gets
    # re-rendered after the ground or road rules change - and a town's worth of
    # Overpass tiles takes minutes to download every time. The reply is kept on
    # disk and reused whenever the bbox matches to the metre.
    # A map drawn by hand has no town behind it to download. Overpass is not
    # asked at all, which is also the only way to make a map of a place that
    # is not on OpenStreetMap - one never surveyed, or one that does not exist.
    features = [] if drawn_only else osm.load_cache(cache, fetch_box)
    if features is None:
        _set_progress(map_name, stage="osm", done=0, total=1)
        def _progress(i, total, note=""):
            _set_progress(map_name, stage="osm", done=i - 1, total=total,
                          note=note)

        try:
            # Always through the tiled path, even for a small area: with one
            # tile it is the same single request, and it brings the retry that
            # quarters a bbox the servers call too heavy.
            features = osm.fetch_features_tiled(
                *fetch_box, max_tile_km2=OVERPASS_TILE_KM2, progress=_progress,
                should_stop=_stopper(map_name))
        except knoxstop.Stopped:
            forget_empty_folder()
            return _stopped(map_name, "generate")
        except Exception as exc:  # Overpass can be flaky — surface that clearly
            message = osm.explain(exc)
            _set_progress(map_name, stage="error", message=message)
            forget_empty_folder()
            return failed(message, 502, exc)
        try:
            osm.save_cache(cache, fetch_box, features)
        except OSError:
            pass  # a map that cannot be cached still renders

    # Buildings Overture has and OpenStreetMap has not, before anything is
    # measured from them: they are ordinary building features from here on,
    # so the street angle, the ground, the gardens and the .tbx all see them
    # exactly as they see a mapped one (generator/overture.py).
    gaps = {"added": 0}
    if settings.fill_gaps:
        from generator import overture
        _set_progress(map_name, stage="overture", done=0, total=1)
        try:
            features, gaps = overture.add_missing(
                features, fetch_box, str(map_dir), map_name,
                should_stop=_stopper(map_name))
        except knoxstop.Stopped:
            return _stopped(map_name, "generate")
        except Exception as exc:  # noqa: BLE001 - OSM alone still makes a map
            log.warning("overture %s: %s", map_name, exc)
            gaps = {"added": 0, "error": str(exc)}
        if gaps.get("added"):
            log.info("overture %s: %d buildings OSM had not got, from %d fetched",
                     map_name, gaps["added"], gaps.get("fetched", 0))

    # What the mapper drew, on top of whatever came down. It goes in here, with
    # the real features and before a single thing is measured from them, so the
    # street angle, the ground, the gardens, the buildings and the compiled
    # cells all see a drawn street exactly as they see a surveyed one.
    # What the mapper changed about the features themselves - a building
    # removed, a road dragged straight, a house retagged as a shop. Applied
    # here so nothing downstream ever sees the version that was rejected.
    changes = _load_edits(map_dir)
    if mapedits.count(changes):
        features, used = mapedits.apply(features, changes)
        log.info("generate %s: %d removed, %d moved, %d retagged by hand",
                 map_name, used["removed"], used["moved"], used["tags"])

    drawn_features = sketch.features(drawn)
    if drawn_features:
        features = list(features) + drawn_features
        log.info("generate %s: %d drawn features%s", map_name,
                 len(drawn_features), " (drawn map)" if drawn_only else "")

    rotation = 0.0
    if settings.align_streets:
        angle, strength = renderer.dominant_road_angle(features, *bbox)
        if strength >= renderer.ALIGN_MIN_STRENGTH and abs(angle) >= 0.5:
            rotation = -angle
    # And whatever turn the mapper asked for on top of that.
    rotation += float(settings.rotate_degrees)
    osm_cache_name = Path(cache).name
    osm_bbox = fetch_box

    osm_time = time.time() - t0
    _set_progress(map_name, stage="render", features=len(features))

    try:
        result = renderer.render(
            features, south, west, north, east,
            meters_per_tile=meters_per_tile,
            output_dir=str(map_dir),
            map_name=map_name,
            spawn_density=settings.spawn_density,
            tree_density=settings.tree_density,
            forest_density=settings.forest_density,
            rotation=rotation,
            osm_cache=osm_cache_name,
            osm_bbox=osm_bbox,
            shape=shape,
            straight_roads=bool(settings.straight_roads),
            should_stop=_stopper(map_name),
        )
    except knoxstop.Stopped:
        return _stopped(map_name, "generate")
    except MemoryError:
        # Nothing refuses a big map any more, so this is where one that really
        # was too big lands. Say what it would have taken rather than a
        # traceback: the numbers are the ones the window already showed.
        need = approx_w * approx_h * BYTES_PER_TILE + BASE_BYTES
        _set_progress(map_name, stage="error", message="ran out of memory")
        return failed(
            f"The map ran out of memory while it was being drawn. It needs about "
            f"{need / 1e9:.1f} GB for the ground and the greenery alone, at "
            f"{int(approx_w)}x{int(approx_h)} tiles. Close a few things and try "
            f"again, or draw it at a larger scale - 2 m a tile is a quarter of "
            f"the memory of 1 m. Everything downloaded is kept, so a second run "
            f"starts from the OpenStreetMap data already on disk.", 507)

    _write_readme(map_dir, map_name, result)
    _set_progress(map_name, stage="done")
    mapstate.stamp(str(map_dir), "generate")
    from_addresses = 0
    try:
        with open(map_dir / f"{map_name}_info.json", encoding="utf-8") as f:
            from_addresses = json.load(f).get("houses_from_addresses", 0)
    except (OSError, ValueError):
        pass
    log.info("generate %s: done, %d features, %dx%d tiles, rotation %.1f, "
             "%d houses from addresses, %d from Overture, %.1fs (download %.1fs)",
             map_name, len(features), result.width, result.height, rotation,
             from_addresses, gaps.get("added", 0), time.time() - t0, osm_time)

    return jsonify({
        "mapName": map_name,
        "width": result.width,
        "height": result.height,
        "cellsX": result.cells_x,
        "cellsY": result.cells_y,
        "featureCount": len(features),
        "rotation": round(rotation, 1),
        "osmSeconds": round(osm_time, 2),
        # What Overture put in that OpenStreetMap had not got, so the window
        # can say whether turning it on was worth it here.
        # What the window warned about and the mapper went ahead with anyway,
        # so it can say so beside the finished map too.
        "heavy": heavy,
        # As they were taken, clamped and saved - not as the page sent them -
        # so it can tell afterwards which knobs have really been moved since.
        "settings": settings.to_dict(),
        "fromOverture": gaps.get("added", 0),
        "overtureError": gaps.get("error") or gaps.get("why") or "",
        "files": {
            "landscape": f"/output/{map_name}/{Path(result.landscape_path).name}",
            "vegetation": f"/output/{map_name}/{Path(result.vegetation_path).name}",
            "spawn": f"/output/{map_name}/{Path(result.spawn_map_path).name}",
            "preview": f"/output/{map_name}/{Path(result.preview_path).name}",
            "buildings": f"/output/{map_name}/{Path(result.buildings_geojson_path).name}",
            "meta": f"/output/{map_name}/{Path(result.meta_path).name}",
            "zip": f"/download/{map_name}.zip",
            "readme": f"/output/{map_name}/README.txt",
        },
    })


def _map_summary(map_dir: Path) -> dict:
    """What the window shows for a map it did not just make."""
    info = {}
    try:
        with open(map_dir / f"{map_dir.name}_info.json", encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        pass
    needs = mapstate.needs(str(map_dir))
    stages = mapstate.done(str(map_dir))
    return {
        "mapName": map_dir.name,
        "width": info.get("width_tiles", 0),
        "height": info.get("height_tiles", 0),
        "cellsX": info.get("cells_x", 0),
        "cellsY": info.get("cells_y", 0),
        "featureCount": info.get("feature_count") or info.get("building_count", 0),
        "rotation": round(info.get("rotation", 0.0), 1),
        "updated": int(map_dir.stat().st_mtime),
        # What it was made from, so the window can run the steps again
        # over the same area without asking for it to be drawn afresh.
        "bbox": info.get("bbox"),
        "metersPerTile": info.get("meters_per_tile", 1.0),
        "shape": info.get("shape"),
        # The settings this map was made with. Upgrading it sent none at all,
        # so a map drawn with Knox County roads or a tree density of its own
        # came back with the defaults and looked like a different town.
        "settings": _load_settings(map_dir).to_dict(),
        "madeWith": mapstate.made_with(str(map_dir)),
        "current": knoxlog.version(),
        "stages": {k: v.get("version") for k, v in stages.items()},
        "needs": needs,
        "needsLabels": [mapstate.LABELS[s] for s in needs],
        "files": {
            "landscape": f"/output/{map_dir.name}/{map_dir.name}.bmp",
            "vegetation": f"/output/{map_dir.name}/{map_dir.name}_veg.bmp",
            "spawn": f"/output/{map_dir.name}/{map_dir.name}_ZombieSpawnMap.bmp",
            "preview": f"/output/{map_dir.name}/{map_dir.name}_preview.png",
            "buildings": f"/output/{map_dir.name}/{map_dir.name}_buildings.geojson",
            "meta": f"/output/{map_dir.name}/{map_dir.name}_info.json",
            "zip": f"/download/{map_dir.name}.zip",
            "readme": f"/output/{map_dir.name}/README.txt",
        },
    }


@app.route("/api/maps")
def api_maps():
    """Every map in output/, newest first, and whether an older KnoxMap made
    it - so one can be opened again instead of drawn from scratch."""
    maps = []
    for entry in common.OUTPUT_DIR.iterdir():
        if not entry.is_dir() or not (entry / f"{entry.name}_info.json").exists():
            continue
        maps.append(_map_summary(entry))
    maps.sort(key=lambda m: -m["updated"])
    return jsonify({"maps": maps, "current": knoxlog.version()})


@app.route("/api/maps/<map_name>")
def api_map(map_name: str):
    """One map, in the shape the page shows a freshly generated one in."""
    map_dir = _map_dir(map_name)
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    summary = _map_summary(map_dir)
    summary["settings"] = _load_settings(map_dir).to_dict()
    summary["population"] = _population(map_dir)
    return jsonify(summary)


@app.route("/output/<path:relpath>")
def serve_output(relpath: str):
    return send_from_directory(common.OUTPUT_DIR, relpath)


# Pictures of a finished map: drawn from the compiled cells the game itself
# loads, so they are the map rather than an impression of it. A town takes the
# best part of a minute, so it runs on its own thread and the page asks.
_PICTURES: dict[str, dict] = {}


@app.route("/api/pictures", methods=["POST"])
def api_pictures():
    """Draw the map, the way the game would."""
    data = _json_body()
    map_dir = _map_dir(data.get("mapName", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    from knoxbuild import picture as pictures

    if not pictures.compiled(map_dir):
        return jsonify({"error": "Compile the map first - there is nothing to "
                                 "draw until then."}), 400
    name = map_dir.name
    with _PROGRESS_LOCK:
        if _PICTURES.get(name, {}).get("state") == "running":
            return jsonify({"started": False, "state": "running"})
        _PICTURES[name] = {"state": "running", "error": None, "files": []}

    def worker() -> None:
        log.info("pictures %s: started", name)
        t0 = time.time()
        try:
            made = pictures.pictures_of(map_dir)
            files = [f"/output/{map_dir.name}/pictures/{p.name}" for p in made]
            log.info("pictures %s: %d in %.0fs", name, len(files), time.time() - t0)
            with _PROGRESS_LOCK:
                _PICTURES[name] = {"state": "done" if files else "error",
                                   "files": files,
                                   "error": None if files else "Nothing was drawn."}
        except Exception as exc:  # noqa: BLE001 - reported to the window
            eid = knoxlog.record(exc, f"pictures {name}: failed")
            with _PROGRESS_LOCK:
                _PICTURES[name] = {"state": "error", "errorId": eid,
                                   "error": str(exc), "files": []}

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/pictures-status")
def api_pictures_status():
    map_dir = _map_dir(request.args.get("map", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    with _PROGRESS_LOCK:
        return jsonify(dict(_PICTURES.get(map_dir.name, {"state": "idle", "files": []})))


@app.route("/api/buildings", methods=["POST"])
def api_buildings():
    """Run knoxbuild over a generated map."""
    from knoxbuild.build import build as build_buildings

    data = _json_body()
    map_dir = _map_dir(data.get("mapName", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    settings = Settings.from_dict(data.get("settings"))         if data.get("settings") else _load_settings(map_dir)
    _save_settings(map_dir, settings)
    log.info("buildings %s: started", map_dir.name)
    # Say so now if this PC cannot hold the map's bitmaps, rather than after
    # minutes of work. Free memory moves, so only a 32-bit Python's hard 2 GB
    # ceiling refuses outright; anything else is let through and caught below.
    info = _map_summary(map_dir)
    tight = _too_big_for_memory(info["width"], info["height"],
                                BUILD_BYTES_PER_TILE, BUILD_BASE_BYTES)
    if tight and sys.maxsize <= 2 ** 32:
        return jsonify({"error": tight}), 507
    if tight:
        log.warning("buildings %s: going ahead with a heavy map - %s",
                    map_dir.name, tight)
    t0 = time.time()
    out = io.StringIO()
    try:
        from contextlib import redirect_stdout
        def say(text: str, fraction: float | None) -> None:
            _set_progress(map_dir.name, stage="buildings", note=text,
                          fraction=fraction)

        say("Starting", 0.0)
        with redirect_stdout(out):
            build_buildings(str(map_dir), settings=settings,
                            should_stop=_stopper(map_dir.name), progress=say)
        _set_progress(map_dir.name, stage="buildings-done", note="", fraction=1.0)
    except knoxstop.Stopped:
        return _stopped(map_dir.name, "buildings")
    except MemoryError as exc:
        need = info["width"] * info["height"] * BUILD_BYTES_PER_TILE + BUILD_BASE_BYTES
        return failed(
            f"Generate buildings ran out of memory. A map of "
            f"{info['width']}x{info['height']} tiles needs about "
            f"{need / 1e9:.1f} GB for this step. Close a few things and try "
            f"again, or draw the map at a larger scale: 2 m a tile is a quarter "
            f"of the memory of 1 m. Nothing is lost: the map itself is saved, "
            f"so Generate buildings can simply be run again.", 507, exc)
    except Exception as exc:
        log.info("buildings %s output before the error:\n%s", map_dir.name,
                 out.getvalue()[-4000:])
        return failed(f"Building generation failed: {exc}", 500, exc)
    tbx = sorted((map_dir / "buildings").glob("*.tbx"))
    mapstate.stamp(str(map_dir), "build")
    log.info("buildings %s: %d files in %.1fs\n%s", map_dir.name, len(tbx),
             time.time() - t0, out.getvalue()[-3000:])
    return jsonify({"count": len(tbx),
                    "pzw": f"{map_dir.name}.pzw",
                    "settings": settings.to_dict(),
                    "population": _population(map_dir)})


def _population(map_dir: Path) -> dict | None:
    try:
        with open(map_dir / f"{map_dir.name}_population.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@app.route("/api/layout-preview")
def api_layout_preview():
    """A picture of what the building step decided - buildings by use, or the
    zombie heat map - to judge the settings before a long compile."""
    from knoxbuild import layout_preview

    map_dir = _map_dir(request.args.get("map", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    layer = request.args.get("layer", "kinds")
    if layer not in layout_preview.LAYERS:
        return jsonify({"error": f"Unknown layer {layer!r}."}), 400
    if not (map_dir / f"{map_dir.name}_placements.csv").exists():
        return jsonify({"error": "Build the buildings first - there is nothing "
                                 "to show until then."}), 400
    path = layout_preview.render_to(str(map_dir), layer)
    return send_file(path, mimetype="image/png", max_age=0)


@app.route("/api/zombies", methods=["POST"])
def api_zombies():
    """Recount a built map's zombies with new settings, without rebuilding.

    The spawn map only depends on the buildings' footprints, heights and
    kinds, which the build saved. Redrawing it takes a second or two against
    the minutes a full building pass takes, so the zombie dials can be tried
    freely. The map still needs compiling again for the game to see it.
    """
    from knoxbuild.population import recount

    data = _json_body()
    map_dir = _map_dir(data.get("mapName", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    # Same rule as Generate buildings: settings sent with the request stand on
    # their own. Layering them over the saved ones let a saved value outrank
    # the preset being asked for, so "town" after a recount at 1.5 stayed 1.5.
    settings = Settings.from_dict(data.get("settings"))         if data.get("settings") else _load_settings(map_dir)
    try:
        summary = recount(str(map_dir), settings)
    except FileNotFoundError as exc:
        return failed(f"Cannot recount zombies: {exc}.", 400, exc)
    _save_settings(map_dir, settings)
    return jsonify({"population": summary})


@app.route("/api/worlded", methods=["POST"])
def api_worlded():
    """Open the generated project in PZWorldEd."""
    import subprocess

    data = _json_body()
    map_dir = _map_dir(data.get("mapName", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    import knoxpaths

    exe = _worlded_exe()
    if exe is None:
        return jsonify({"error": "PZWorldEd not found. Set the PZWORLDED "
                                 "environment variable to its full path."}), 400
    pzw = map_dir / f"{map_dir.name}.pzw"
    if not pzw.exists():
        return jsonify({"error": "No .pzw yet — generate the buildings first."}), 400
    subprocess.Popen(knoxpaths.command_for(exe) + [str(pzw)])
    return jsonify({"launched": str(pzw)})


# How many features one look at the map may bring back. A city has a hundred
# thousand; a page that is handed them all stops responding, and nobody edits
# a hundred thousand things by hand anyway. The biggest are sent first, so
# what arrives is what is worth editing.
EDIT_FEATURE_LIMIT = 1200


@app.route("/api/features/<name>")
def api_features(name: str):
    """This map's own features, as GeoJSON, for editing.

    They come from the download that made the map, not from a fresh one, so
    what is edited is exactly what was built - and editing costs no requests
    to Overpass at all.
    """
    map_dir = _map_dir(name)
    if map_dir is None:
        return jsonify({"error": "Bad map name."}), 400
    info = _map_summary(map_dir)
    box = info.get("bbox") or {}
    if not box:
        return jsonify({"error": "That map has no area recorded, so there is "
                                 "nothing to read its features from."}), 400
    settings = _load_settings(map_dir)
    cache, fetch_box = _osm_cache_for(
        map_dir, (box["south"], box["west"], box["north"], box["east"]),
        float(info.get("metersPerTile") or 1.0), settings)
    features = osm.load_cache(cache, fetch_box)
    if features is None:
        return jsonify({"error": "This map's download is not beside it any "
                                 "more, or it was made by a version whose "
                                 "filters have changed. Generate it again and "
                                 "its features can be edited."}), 404
    # Only what is being looked at, which is what keeps a town editable.
    view = None
    try:
        view = (float(request.args["south"]), float(request.args["west"]),
                float(request.args["north"]), float(request.args["east"]))
    except (KeyError, TypeError, ValueError):
        view = None
    out = mapedits.as_geojson(features, view, EDIT_FEATURE_LIMIT)
    out["edits"] = _load_edits(map_dir)
    return jsonify(out)


@app.route("/api/edits/<name>", methods=["GET", "POST"])
def api_edits(name: str):
    map_dir = _map_dir(name)
    if map_dir is None:
        return jsonify({"error": "Bad map name."}), 400
    if request.method == "GET":
        return jsonify(_load_edits(map_dir))
    changes = mapedits.clean(_json_body())
    _save_edits(map_dir, changes)
    return jsonify({"changes": mapedits.count(changes), **changes})


@app.route("/api/sketch")
def api_sketch_palette():
    """What can be drawn, and the OpenStreetMap tags each one carries.

    The page builds its palette from this rather than holding a second copy of
    the tags: a tag that is wrong here draws a shape that is missing from the
    finished map, and one list cannot disagree with itself."""
    return jsonify({"palette": sketch.palette(),
                    "categories": {e["id"]: sketch.category_of(e["id"])
                                   for e in sketch.palette()}})


@app.route("/api/design/<name>", methods=["GET", "POST"])
def api_design(name: str):
    """The editor's own sheet: what was drawn, in metres, and how big it is.

    Kept as drawn rather than as the GeoJSON it becomes, because metres from
    a corner are what the editor works in and degrees are not - a sheet that
    came back as longitude could not be drawn on again without guessing at
    the conversion backwards.
    """
    map_dir = _map_dir(name)
    if map_dir is None:
        return jsonify({"error": "Bad map name."}), 400
    if request.method == "GET":
        design = _load_design(map_dir)
        if design is None:
            return jsonify({"error": "That map was not drawn on a sheet."}), 404
        return jsonify(design)
    design = _json_body()
    if not isinstance(design, dict):
        return jsonify({"error": "Expected a sheet."}), 400
    _save_design(map_dir, design)
    south, west, north, east = sketch.design_bbox(design)
    made = sketch.features(sketch.design_to_drawn(design))
    return jsonify({"saved": len(design.get("features") or []),
                    "usable": len(made),
                    "bbox": {"south": south, "west": west,
                             "north": north, "east": east}})


# ---- drawing a map that joins one already built ----------------------------
#
# The drawing board has no world under it, which is the point of it and also
# means a street drawn on it stops dead at the edge. These two give it the map
# next door: where that map's roads leave the side they share, so the drawing
# can be made to meet them, and the ground it stands on afterwards, so the two
# really are neighbours in the game.

@app.route("/api/board/join/<name>")
def api_board_join(name: str):
    """A built map, as the drawing board needs to see it.

    `side` is where the new sheet goes - "E" means east of this map - and
    `w`/`h` are the sheet in metres, so the roads come back as points on it.
    """
    from knoxbuild import joins
    from knoxbuild import placement as P

    map_dir = _map_dir(name)
    if map_dir is None:
        return jsonify({"error": "Bad map name."}), 400
    side = (request.args.get("side") or "E").upper()
    try:
        sheet_w = float(request.args.get("w") or 900)
        sheet_h = float(request.args.get("h") or 900)
        out = joins.sheet_joins(str(map_dir), map_dir.name, side, sheet_w, sheet_h)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    where = P.saved(str(map_dir)) or P.project_origin(str(map_dir), map_dir.name)
    cells_w, cells_h = P.project_size(str(map_dir), map_dir.name)
    out["cells"] = ({"x": where[0], "y": where[1], "w": cells_w, "h": cells_h}
                    if where else None)
    out["cellTiles"] = P.CELL_SIZE
    preview = map_dir / f"{map_dir.name}_preview.png"
    out["preview"] = (f"/output/{map_dir.name}/{preview.name}"
                      if preview.exists() else None)
    return jsonify(out)


@app.route("/api/board/place-beside", methods=["POST"])
def api_board_place_beside():
    """Put a map that has just been built next to the one it was drawn to
    join, so the street that meets its border meets it in the game too."""
    from knoxbuild import joins
    from knoxbuild import placement as P

    body = _json_body()
    mine = _map_dir(str(body.get("map") or ""))
    theirs = _map_dir(str(body.get("neighbour") or ""))
    side = str(body.get("side") or "E").upper()
    if mine is None or theirs is None:
        return jsonify({"error": "Bad map name."}), 400
    if side not in joins.SIDES:
        return jsonify({"error": f"A map has four sides, not {side}."}), 400
    where = P.saved(str(theirs)) or P.project_origin(str(theirs), theirs.name)
    if not where:
        return jsonify({"error": f"{theirs.name} has no place in the world "
                                 f"yet, so there is nothing to sit beside."}), 400
    their_w, their_h = P.project_size(str(theirs), theirs.name)
    my_w, my_h = P.project_size(str(mine), mine.name)
    # Corners line up on the side they share, which is what makes the streets
    # across the border line up too.
    cell = {"E": (where[0] + their_w, where[1]),
            "W": (where[0] - my_w, where[1]),
            "S": (where[0], where[1] + their_h),
            "N": (where[0], where[1] - my_h)}[side]
    try:
        done = P.move(str(mine), mine.name, cell)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OSError as exc:
        return failed(f"The map could not be placed: {exc}", 500, exc)
    out = P.layout(str(mine), mine.name)
    out["moved"] = done
    out["beside"] = theirs.name
    return jsonify(out)


@app.route("/api/zones")
def api_zone_settings():
    """What a zone may say about itself, and the wall styles it may pin to.

    Served rather than written into the page, for the same reason the settings
    are: a second copy drifts, and a zone setting the window offers that the
    generator does not read is a dial that does nothing.
    """
    from knoxbuild import zones as Z
    from knoxbuild.build import style_names

    return jsonify({"settings": Z.overridable(), "styles": style_names(),
                    "styleKey": Z.STYLE_KEY})


@app.route("/api/zones/<name>", methods=["GET", "POST"])
def api_zones(name: str):
    from knoxbuild import zones as Z

    map_dir = _map_dir(name)
    if map_dir is None:
        return jsonify({"error": "Bad map name."}), 400
    if request.method == "GET":
        return jsonify({"zones": Z.load(str(map_dir), map_dir.name)})
    body = _json_body()
    drawn = body.get("zones") if isinstance(body, dict) else body
    if not isinstance(drawn, list):
        return jsonify({"error": "Expected a list of zones."}), 400
    try:
        Z.save(str(map_dir), map_dir.name, drawn)
    except OSError as exc:
        return failed(f"The zones could not be saved: {exc}", 500, exc)
    kept = Z.load(str(map_dir), map_dir.name)
    return jsonify({"zones": kept, "saved": len(kept)})


@app.route("/api/sketch/<name>", methods=["GET", "POST"])
def api_sketch(name: str):
    map_dir = _map_dir(name)
    if map_dir is None:
        return jsonify({"error": "Bad map name."}), 400
    if request.method == "GET":
        return jsonify(_load_sketch(map_dir))
    drawn = _json_body()
    if not isinstance(drawn, dict):
        return jsonify({"error": "Expected a GeoJSON FeatureCollection."}), 400
    _save_sketch(map_dir, drawn)
    made = sketch.features(drawn)
    return jsonify({"saved": len(drawn.get("features") or []),
                    "usable": len(made)})


def _expected_cells(map_dir: Path) -> int:
    """How many 256-tile cells the compile should produce, for a progress bar."""
    try:
        with open(map_dir / f"{map_dir.name}_info.json", encoding="utf-8") as f:
            info = json.load(f)
    except Exception:
        return 0
    from knoxbuild.world import CELL_SIZE, WORLD_ORIGIN_CELLS, _project_box

    # Where this map was actually built, which is not 70,0 once a PC holds
    # more than one of them (knoxbuild/world.py choose_origin).
    box = _project_box(str(map_dir / f"{map_dir.name}.pzw"))
    ox = (box[0] if box else WORLD_ORIGIN_CELLS[0]) * CELL_SIZE
    oy = (box[1] if box else WORLD_ORIGIN_CELLS[1]) * CELL_SIZE
    w = info.get("cells_x", 0) * CELL_SIZE
    h = info.get("cells_y", 0) * CELL_SIZE
    x0, x1 = ox // 256, (ox + w + 255) // 256
    y0, y1 = oy // 256, (oy + h + 255) // 256
    return max(0, (x1 - x0) * (y1 - y0))


@app.route("/api/compile-status")
def api_compile_status():
    """Progress of a running compile, so the page never has to block on one."""
    map_dir = _map_dir(request.args.get("map", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    with _PROGRESS_LOCK:
        state = dict(_COMPILE.get(map_dir.name, {"state": "idle"}))
    lots = map_dir / "lots"
    state["cells"] = len(list(lots.glob("*.lotheader"))) if lots.is_dir() else 0
    state["expected"] = _expected_cells(map_dir)
    state["tmx"] = len(list((map_dir / "tmx").glob("*.tmx"))) \
        if (map_dir / "tmx").is_dir() else 0
    # Cells the last compile tried three times and could not do. It steps over
    # them rather than throwing away the hours already spent, so this is how
    # the window knows to offer them again instead of saying "done".
    from tools.compile_map import failed_cells
    state["failed"] = failed_cells(map_dir)
    return jsonify(state)


@app.route("/api/compile", methods=["POST"])
def api_compile():
    """Convert and compile the whole map without touching WorldEd's menus.

    Uses the patched PZWorldEd_cli.exe, which adds a --generate-map switch that
    runs BMP to TMX and Generate Lots in order. Stock WorldEd has no such
    switch, so without the patched build this falls back to /api/worlded.
    """
    import subprocess

    data = _json_body()
    map_dir = _map_dir(data.get("mapName", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    from tools.compile_map import default_backend
    rust = (data.get("backend") or default_backend()) == "rust"
    exe = _worlded_exe(cli=True)
    if exe is None and rust:
        exe = Path("PZWorldEd_cli.exe")        # not used by the rust backend
    if exe is None:
        return jsonify({"error": "Patched PZWorldEd_cli.exe not found — use "
                                 "Open in WorldEd and run the two menu "
                                 "commands instead."}), 400
    pzw = map_dir / f"{map_dir.name}.pzw"
    if not pzw.exists():
        return jsonify({"error": "No .pzw yet — generate the buildings first."}), 400

    # "Compile the cells that failed" rather than the whole town again. A full
    # re-run would skip the batches already on disk anyway, but it walks every
    # one of them to find that out, and on a big map that is minutes before it
    # reaches the handful that matter.
    from tools.compile_map import failed_cells
    only = [f["cells"] for f in failed_cells(map_dir)] if data.get("onlyFailed") else None
    if data.get("onlyFailed") and not only:
        return jsonify({"error": "Nothing is recorded as failed."}), 400

    (map_dir / "tmx").mkdir(exist_ok=True)
    (map_dir / "lots").mkdir(exist_ok=True)

    name = map_dir.name
    with _PROGRESS_LOCK:
        if _COMPILE.get(name, {}).get("state") == "running":
            return jsonify({"started": False, "state": "running"})
        _COMPILE[name] = {"state": "running", "error": None}

    def worker() -> None:
        """Compiling a town takes many minutes.

        Running it inside the request blocked the whole UI - the window simply
        froze until it finished. It runs on its own thread now and the page
        polls /api/compile-status, so the app stays usable and shows progress.

        The work goes out in batches of cells, one short-lived WorldEd
        process each: a single process compiling a whole town never gives
        its memory back and took the machine down at 13.7 GB. See
        tools/compile_map.py.
        """
        from tools import compile_map as compiler

        def note(done: int, total: int, cells: int) -> None:
            with _PROGRESS_LOCK:
                now = _COMPILE.get(name, {})
                # Keep what the detail callback added, and a stop in progress.
                state = "stopping" if now.get("state") == "stopping" else "running"
                _COMPILE[name] = {**now, "state": state, "error": None,
                                  "batch": done, "batches": total}

        def phase(text: str, fraction: float) -> None:
            # The Rust compile has no batches: it says where it is and how far along.
            with _PROGRESS_LOCK:
                now = _COMPILE.get(name, {})
                state = "stopping" if now.get("state") == "stopping" else "running"
                _COMPILE[name] = {**now, "state": state, "error": None,
                                  "phase": text, "fraction": round(fraction, 3)}

        def detail(info: dict) -> None:
            with _PROGRESS_LOCK:
                now = {**_COMPILE.get(name, {"state": "running"}),
                       "workers": info["workers"],
                       "workersStart": info["started_with"],
                       "etaSeconds": info["eta_s"]}
                # Said once, before the first batch, and kept for the run.
                if info.get("incremental"):
                    now["incremental"] = info["incremental"]
                _COMPILE[name] = now

        log.info("compile %s: started", name)
        t0 = time.time()
        try:
            produced = compiler.compile_map(str(map_dir), batch=COMPILE_BATCH,
                                            exe=str(exe), on_progress=note,
                                            on_detail=detail, on_phase=phase,
                                            should_stop=_stopper(name),
                                            only_cells=only,
                                            fresh=bool(data.get("fresh")),
                                            backend="rust" if rust else "worlded")
            if not produced:
                eid = knoxlog.record(None, f"compile {name}: produced no cells")
                with _PROGRESS_LOCK:
                    _COMPILE[name] = {"state": "error", "errorId": eid,
                                      "error": "Compile produced no cells."}
                return
            log.info("compile %s: %d cells in %.0fs", name, produced, time.time() - t0)
            # A compile that stepped over a batch is finished but not whole:
            # installing it gives a map with a hole in it, so the window is
            # told what is missing rather than a plain "done".
            left = compiler.failed_cells(str(map_dir))
            with _PROGRESS_LOCK:
                mapstate.stamp(str(map_dir), "compile")
                _COMPILE[name] = {"state": "done", "error": None, "failed": left}
        except knoxstop.Stopped:
            log.info("compile %s: stopped after %.0fs", name, time.time() - t0)
            _done_stopping(name)
            with _PROGRESS_LOCK:
                _COMPILE[name] = {"state": "stopped", "error": None}
            return
        except subprocess.TimeoutExpired as exc:
            eid = knoxlog.record(exc, f"compile {name}: timed out")
            with _PROGRESS_LOCK:
                _COMPILE[name] = {"state": "error", "errorId": eid,
                                  "error": "Compile timed out."}
        except Exception as exc:
            eid = knoxlog.record(exc, f"compile {name}: failed")
            with _PROGRESS_LOCK:
                _COMPILE[name] = {"state": "error", "errorId": eid, "error": str(exc)}

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True, "expected": _expected_cells(map_dir)})


@app.route("/api/lots")
def api_lots():
    """Has WorldEd's Generate Lots produced anything yet?"""
    map_dir = _map_dir(request.args.get("map", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    lots = map_dir / "lots"
    cells = sorted(lots.glob("*.lotheader")) if lots.is_dir() else []
    return jsonify({"compiled": bool(cells), "cells": len(cells)})


@app.route("/api/placement/<name>", methods=["GET", "POST", "DELETE"])
def api_placement(name: str):
    """Where this map stands in the game's world, and where to put it.

    GET is the world as the window draws it: Knox County, the maps already
    installed, and this one. POST moves this one, which rewrites the few files
    that carry the world origin and says whether the map has to be compiled
    again (knoxbuild/placement.py). DELETE goes back to having a place picked
    automatically.
    """
    from knoxbuild import placement as P

    map_dir = _map_dir(name)
    if map_dir is None:
        return jsonify({"error": "Bad map name."}), 400
    if request.method == "GET":
        return jsonify(P.layout(str(map_dir), map_dir.name, str(CACHE_DIR)))
    if request.method == "DELETE":
        done = P.unplace(str(map_dir), map_dir.name)
        out = P.layout(str(map_dir), map_dir.name)
        out["moved"] = done
        return jsonify(out)
    body = _json_body()
    try:
        done = P.move(str(map_dir), map_dir.name,
                      (body.get("cell_x"), body.get("cell_y")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OSError as exc:
        return failed(f"The map could not be moved: {exc}", 500, exc)
    out = P.layout(str(map_dir), map_dir.name)
    out["moved"] = done
    return jsonify(out)


@app.route("/api/placement/<name>/link", methods=["POST"])
def api_placement_link(name: str):
    """The sheet for a road drawn between this map and another.

    The window sends where the road was drawn, in world tiles; what comes back
    is a map to build (generator/sketch.py): the strip of country it runs
    through, at the same scale as the map it joins so the road is the same
    width in tiles as the streets it meets.
    """
    from knoxbuild import placement as P

    map_dir = _map_dir(name)
    if map_dir is None:
        return jsonify({"error": "Bad map name."}), 400
    body = _json_body()
    try:
        out = P.link_design(str(map_dir), map_dir.name, body.get("points"),
                            str(body.get("kind") or "primary"),
                            scenery=body.get("scenery", True) is not False)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:                  # noqa: BLE001 - reported
        return failed(f"That road could not be laid out: {exc}", 500, exc)
    out["name"] = f"{map_dir.name}_link"
    return jsonify(out)


@app.route("/api/world-picture/<name>")
def api_world_picture(name: str):
    """The picture of a map already in the game, for the world view.

    Assembled from that map's own tile pyramid in the player's install and
    kept in the cache. It is their copy of the game, read on their own PC and
    shown in their own window; nothing is copied into a map or shipped.
    """
    from knoxbuild import placement as P

    where = P.picture_for(name, str(CACHE_DIR))
    if not where:
        return jsonify({"error": "No picture of that map."}), 404
    return send_file(where["file"], mimetype="image/png",
                     max_age=86400, conditional=True)


def _write_readme(map_dir: Path, map_name: str, result: renderer.RenderResult) -> None:
    text = f"""Project Zomboid map: {map_name}
Generated by KnoxMap.

Bitmap dimensions: {result.width}x{result.height} tiles
Cell grid:         {result.cells_x} x {result.cells_y} (cells are always 300 tiles)

Files
-----
{map_name}.bmp                  Landscape (base terrain — WorldEd's main input)
{map_name}_veg.bmp              Vegetation (trees, bushes, long grass)
{map_name}_ZombieSpawnMap.bmp   Zombie population (grayscale, 1/10 scale)
{map_name}_preview.png          Human-viewable preview of what you'll get
{map_name}_buildings.geojson    Building footprints from OSM (for reference)
{map_name}_info.json            Meta: bbox, scale, cell count
{map_name}.zip                  Everything in this folder, from the web UI

How to import (per Thuztor's Mapping Guide v0.2, chapter 2)
-----------------------------------------------------------
1. Open WorldEd (part of the Zomboid Mapping Tools).
2. File -> New, and choose a {result.cells_x} x {result.cells_y} cell grid.
3. From your file browser, drag {map_name}.bmp onto the empty grid.
   (WorldEd reads the matching {map_name}_veg.bmp automatically if it sits
   next to the landscape bitmap with the same base filename.)
4. File -> BMP to TMX -> All cells. Set an export folder for the .tmx output.
5. Open the resulting project in WorldEd / TileZed to place buildings
   (.tbx files) on top of the landscape. This tool does NOT place PZ
   buildings — OSM building footprints are exported as GeoJSON for
   reference only.
6. File -> Generate Lots. This produces .lotheader + .lotpack files.
7. Copy those into your game's media/maps folder (see chapter 9 of the
   guide for offset / world-origin details).

Notes
-----
* Roads render as asphalt (residential = light, secondary = medium,
  primary/motorway = dark). Paths and tracks render as dirt lines.
* Forests render as dense trees in the interior and a grass+tree blend
  at the edges so the transition isn't a hard rectangle.
* The spawn map is generated procedurally: higher density on asphalt,
  zero on water, slight randomness throughout.
* OSM building footprints CANNOT be converted directly to PZ buildings —
  PZ buildings are a separate thing you assemble in BuildingEd (.tbx).
  The GeoJSON file lets you see where buildings would sit in the real
  world and drop matching .tbx lots in the right tiles.
"""
    # UTF-8 whatever the PC's code page: the text has dashes that a Korean or
    # Japanese Windows cannot write in its own, and the whole generate request
    # failed on them after the map was already drawn.
    (map_dir / "README.txt").write_text(text, encoding="utf-8")


@app.route("/api/save", methods=["POST"])
def api_save():
    """Save a map's file where the player can find it and show it in Explorer.

    A download link does nothing in the app window: it is a WebView, not a
    browser, with nowhere to put a file. Everything is already on disk in the
    map's own folder, so "downloading" here means making the zip when that is
    what was asked for, and opening Explorer with the file selected.
    """
    data = _json_body()
    map_dir = _map_dir(data.get("mapName", ""))
    if map_dir is None:
        return failed("Unknown map.", 404)
    name = str(data.get("name", "")).strip()
    if name in ("", "zip"):
        target = map_dir / f"{map_dir.name}.zip"
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in sorted(map_dir.rglob("*")):
                    if not path.is_file() or path.suffix.lower() == ".zip":
                        continue
                    zf.write(path, arcname=str(Path(map_dir.name) / path.relative_to(map_dir)))
        except OSError as exc:
            return failed(f"Could not write the zip: {exc}", 500, exc)
    else:
        target = (map_dir / name).resolve()
        if not str(target).startswith(str(map_dir.resolve())) or not target.is_file():
            return failed("That file is not in this map's folder.", 400)
    knoxlog.open_folder(target)
    log.info("saved %s for %s", target.name, map_dir.name)
    return jsonify({"path": str(target), "name": target.name,
                    "folder": str(target.parent)})


@app.route("/download/<map_name>.zip")
def download_all(map_name: str):
    """Everything for one map, zipped on demand.

    Built when asked for rather than at generation time, so it also picks up
    anything produced later - the .tbx buildings, the .pzw project and the
    placement CSV that `python -m knoxbuild` writes into the same folder.
    """
    safe = SAFE_NAME.sub("_", map_name)
    map_dir = (common.OUTPUT_DIR / safe).resolve()
    if not str(map_dir).startswith(str(common.OUTPUT_DIR.resolve())):
        return jsonify({"error": "Bad map name."}), 400
    if not map_dir.is_dir():
        return jsonify({"error": f"No output for {safe!r}."}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(map_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() == ".zip":
                continue
            zf.write(path, arcname=str(Path(safe) / path.relative_to(map_dir)))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{safe}.zip")


# Names the tests and tools/selftest.py reach through this module. The code
# moved to web/; the old names still work for anything that reads them.
from generator import osm  # noqa: E402,F401
from web.checks import (BIG_AREA_KM2, BIG_LANDMARK_KM2, BIG_TILES_PER_SIDE,  # noqa: E402,F401
                        MAX_METERS_PER_TILE, MIN_METERS_PER_TILE, RENDER_SETTINGS,
                        _clean_shape, _too_big_for_memory, tidy_scale)
from web.common import BASE_DIR  # noqa: E402,F401
from web.presets import MAX_PRESET_POINTS  # noqa: E402,F401


# ---- the routes: one blueprint per area, in web/ ----------------------------------
app.register_blueprint(jobs.bp)
app.register_blueprint(updates.bp)
app.register_blueprint(geo.bp)
app.register_blueprint(system.bp)
app.register_blueprint(presets.bp)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)

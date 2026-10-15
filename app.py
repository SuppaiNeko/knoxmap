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
import sys
import urllib.parse
import zipfile
from pathlib import Path

from flask import (Flask, jsonify, render_template, request, send_file,
                   send_from_directory)

import knoxlog

from web import common, compiling, generation, geo, jobs, presets, system, updates, zones
from web.common import (SAFE_NAME, _json_body, _map_dir, _request_context,
                        failed, log)
from web.generation import _population
from web.geo import CACHE_DIR
from web.mapfiles import _load_settings, _map_summary
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


@app.route("/")
def index():
    # The app window cannot download a file the way a browser does - clicking
    # a download link there did nothing at all - so the page asks the server
    # to save it and show it in Explorer instead. See /api/save.
    return render_template("index.html", version=knoxlog.version(),
                           in_window=os.environ.get("KNOXMAP_WINDOW") == "1")


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
from web.generation import generate  # noqa: E402,F401
from web.presets import MAX_PRESET_POINTS  # noqa: E402,F401


# ---- the routes: one blueprint per area, in web/ ----------------------------------
app.register_blueprint(jobs.bp)
app.register_blueprint(updates.bp)
app.register_blueprint(geo.bp)
app.register_blueprint(system.bp)
app.register_blueprint(presets.bp)
app.register_blueprint(zones.bp)
app.register_blueprint(compiling.bp)
app.register_blueprint(generation.bp)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)

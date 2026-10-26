"""A download is kept for any map inside it, and a bigger map fetches only
its edges.

The file beside a map is only found for that map's exact box, which moves
with the scale, the turn and the name - so trying a setting meant fetching
the town again. The shared store (generator/areacache.py) answers any box
inside a download, and for a box that reaches past one, names the strips
still to fetch. Offline mode makes maps from the store and nothing else.

No network: every request is replaced.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from generator import areacache, osm

STORED = (40.0, 20.0, 40.01, 20.01)


def way(i, *points, tags=None):
    return osm.OSMFeature(i, "way", tags or {"highway": "residential"}, list(points))


INSIDE = way(1, (40.004, 20.004), (40.006, 20.006))
FAR = way(2, (40.0005, 20.0005), (40.001, 20.001))
# Crosses the edge of SMALL with no point inside it.
CROSSING = way(3, (40.005, 20.001), (40.005, 20.009))
SMALL = (40.003, 20.003, 40.007, 20.007)


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.object(osm, "AREA_CACHE_DIR", self.dir),
                  mock.patch.object(osm, "offline", lambda: False)):
            p.start()
            self.addCleanup(p.stop)

    def files(self):
        return sorted(os.listdir(self.dir))

    def test_a_box_inside_a_download_is_cut_out_of_it(self):
        osm.area_cache_put(STORED, [INSIDE, FAR, CROSSING])
        feats, missing = osm.area_cache_get(SMALL)
        self.assertEqual(missing, [])
        self.assertEqual({f.osm_id for f in feats}, {1, 3})

    def test_a_way_across_the_edge_comes_whole(self):
        osm.area_cache_put(STORED, [CROSSING])
        feats, _ = osm.area_cache_get(SMALL)
        self.assertEqual(feats[0].geometry, CROSSING.geometry)

    def test_a_bigger_box_is_told_only_the_strips_it_lacks(self):
        osm.area_cache_put(STORED, [INSIDE])
        grown = (40.0, 20.0, 40.012, 20.013)
        feats, missing = osm.area_cache_get(grown)
        self.assertEqual([f.osm_id for f in feats], [1])
        self.assertEqual(len(missing), 2)
        # The strips and what is stored make up the box, with nothing twice.
        self.assertAlmostEqual(sum(areacache.area(b) for b in missing)
                               + areacache.area(STORED), areacache.area(grown))
        for strip in missing:
            self.assertIsNone(areacache.intersection(strip, STORED))
            self.assertTrue(areacache.contains(grown, strip))

    def test_a_box_nothing_touches_is_all_missing(self):
        osm.area_cache_put(STORED, [INSIDE])
        far = (41.0, 21.0, 41.01, 21.01)
        self.assertEqual(osm.area_cache_get(far), ([], [far]))

    def test_the_smallest_download_that_holds_it_is_used(self):
        osm.area_cache_put((39.0, 19.0, 41.0, 21.0), [FAR])
        osm.area_cache_put(STORED, [INSIDE])
        feats, missing = osm.area_cache_get(SMALL)
        self.assertEqual((missing, [f.osm_id for f in feats]), ([], [1]))

    def test_a_merged_download_replaces_the_pieces_it_covers(self):
        osm.area_cache_put(STORED, [INSIDE])
        osm.area_cache_put((40.0, 20.0, 40.012, 20.013), [INSIDE, FAR])
        self.assertEqual(len(self.files()), 1)

    def test_an_older_filter_list_is_fetched_again_online_but_used_offline(self):
        osm.area_cache_put(STORED, [INSIDE])
        with mock.patch.object(osm, "FILTERS_VERSION", osm.FILTERS_VERSION + 1):
            self.assertEqual(osm.area_cache_get(SMALL), ([], [SMALL]))
            with mock.patch.object(osm, "offline", lambda: True):
                feats, missing = osm.area_cache_get(SMALL)
        self.assertEqual((missing, [f.osm_id for f in feats]), ([], [1]))

    def test_an_old_download_is_fetched_again_online_but_used_offline(self):
        osm.area_cache_put(STORED, [INSIDE])
        old = osm.time.time() - osm.AREA_CACHE_MAX_AGE_S - 60
        path = os.path.join(self.dir, self.files()[0])
        os.utime(path, (old, old))
        self.assertEqual(osm.area_cache_get(SMALL), ([], [SMALL]))
        with mock.patch.object(osm, "offline", lambda: True):
            self.assertEqual(osm.area_cache_get(SMALL)[1], [])

    def test_only_the_strips_are_downloaded_and_each_feature_kept_once(self):
        asked = []

        def fetch(s, w, n, e, **kw):
            asked.append(((s, w, n, e), kw["probe"]))
            return [INSIDE, way(100 + len(asked), (s, w), (n, e))]

        strips = [(40.01, 20.0, 40.012, 20.013), (40.0, 20.01, 40.01, 20.013)]
        with mock.patch.object(osm, "fetch_features_tiled", fetch):
            got = osm.fetch_missing([INSIDE, FAR], strips)
        self.assertEqual(asked, [(strips[0], True), (strips[1], False)])
        self.assertEqual(sorted(f.osm_id for f in got), [1, 2, 101, 102])


class Offline(unittest.TestCase):
    def test_nothing_is_sent(self):
        def post(*_a, **_k):
            self.fail("offline mode sent a request")

        with mock.patch.dict(os.environ, {"KNOXMAP_OFFLINE": "1"}), \
                mock.patch.object(osm.requests, "post", post):
            with self.assertRaises(osm.Offline) as caught:
                osm.fetch_features_tiled(*SMALL)
            with self.assertRaises(osm.Offline):
                osm.fetch_features(*SMALL)
        self.assertIn("Offline mode", osm.explain(caught.exception))

    def test_the_config_turns_it_on_and_the_environment_wins(self):
        with mock.patch.object(osm.knoxpaths, "load_config",
                               lambda: {"osm_offline": True}):
            with mock.patch.dict(os.environ):
                os.environ.pop("KNOXMAP_OFFLINE", None)
                self.assertTrue(osm.offline())
            with mock.patch.dict(os.environ, {"KNOXMAP_OFFLINE": "0"}):
                self.assertFalse(osm.offline())


class Generate(unittest.TestCase):
    """Through the window's own endpoint: a map in a new folder at a new
    scale is made from an earlier map's download, and a wider one fetches
    only its edges."""

    BOX = {"south": 40.001, "west": 20.001, "north": 40.004, "east": 20.004}

    def setUp(self):
        import app as knoxapp
        from web import common as webcommon

        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        self.client = knoxapp.app.test_client()
        self.out = work / "output"
        self.out.mkdir()
        self.asked = []

        def fetch(s, w, n, e, **_kw):
            self.asked.append((s, w, n, e))
            return [way(len(self.asked) * 10 + i, (s + (n - s) * i / 4, w),
                        (s + (n - s) * i / 4, e)) for i in (1, 2, 3)]

        for p in (mock.patch.object(osm, "AREA_CACHE_DIR", str(work / "areas")),
                  mock.patch.object(osm, "offline", lambda: False),
                  mock.patch.object(osm, "fetch_features_tiled", fetch),
                  mock.patch.object(webcommon, "OUTPUT_DIR", self.out)):
            p.start()
            self.addCleanup(p.stop)

    def generate(self, name, scale, **box):
        r = self.client.post("/api/generate", json={
            **self.BOX, **box, "metersPerTile": scale, "mapName": name})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_reuse_then_grow(self):
        # The cell grid is coarser at 2 m a tile, so its box holds the 1 m one.
        self.generate("first", 2)
        self.assertEqual(len(self.asked), 1)
        self.generate("second", 1)
        self.assertEqual(len(self.asked), 1, "the second map downloaded again")
        self.assertTrue((self.out / "second" / "second_turned_osm.json.gz").exists())

        first_box = self.asked[0]
        self.generate("third", 2, north=40.009)
        strips = self.asked[1:]
        self.assertTrue(strips, "the wider map fetched nothing")
        for strip in strips:
            self.assertIsNone(areacache.intersection(strip, first_box))
        # The edges and the old download were kept as one, for the next map.
        self.generate("fourth", 2, north=40.009)
        self.assertEqual(self.asked[1:], strips, "the same map downloaded again")

    def test_offline_and_not_stored_is_refused_without_asking(self):
        # Against the real fetch, not this class's stand-in: it is the one
        # that has to refuse, before the probe or any request.
        def post(*_a, **_k):
            self.fail("offline mode sent a request")

        with mock.patch.object(osm, "fetch_features_tiled", REAL_FETCH), \
                mock.patch.object(osm, "offline", lambda: True), \
                mock.patch.object(osm.requests, "post", post):
            r = self.client.post("/api/generate", json={
                **self.BOX, "metersPerTile": 1, "mapName": "nowhere"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("Offline mode", r.get_json()["error"])
        self.assertFalse((self.out / "nowhere").exists())


REAL_FETCH = osm.fetch_features_tiled


if __name__ == "__main__":
    unittest.main()

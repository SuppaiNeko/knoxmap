"""An older download is still the map's roads.

The download kept beside a map is stamped with the filter list it was fetched
with, and a map drawn before that list grew has an older stamp. Fetching is
right to refuse it - the new filters are there to bring back things the old
ones never asked for - but the steps after the map is drawn only ever read
this file. Turning it away there did not get the newer tags: the paper map
came out with the buildings on it and not one road, river or wood, and every
shop and surgery mapped as a point was lost with it.
"""
import gzip
import json
import os
import tempfile
import unittest

from generator import osm


def write_cache(path, bbox, filters, features=()):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump({"filters": filters, "bbox": list(bbox),
                   "features": list(features)}, f)


ROAD = {"osm_id": 1, "kind": "way", "tags": {"highway": "residential"},
        "geometry": [[1.0, 1.0], [1.0, 2.0]], "role_geoms": []}
BOX = (1.0, 2.0, 3.0, 4.0)


class AnOlderDownload(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "m_osm.json.gz")

    def test_fetching_still_refuses_one_made_with_an_older_filter_list(self):
        write_cache(self.path, BOX, osm.FILTERS_VERSION - 1, [ROAD])
        self.assertIsNone(osm.load_cache(self.path, BOX))

    def test_reading_takes_whatever_is_there(self):
        write_cache(self.path, BOX, osm.FILTERS_VERSION - 4, [ROAD])
        got = osm.load_cache(self.path, BOX, filters=None)
        self.assertEqual(len(got or []), 1)
        self.assertEqual(got[0].tags, {"highway": "residential"})

    def test_a_download_of_the_current_list_is_taken_either_way(self):
        write_cache(self.path, BOX, osm.FILTERS_VERSION, [ROAD])
        self.assertEqual(len(osm.load_cache(self.path, BOX) or []), 1)
        self.assertEqual(len(osm.load_cache(self.path, BOX, filters=None) or []), 1)

    def test_a_download_of_somewhere_else_is_refused_however_it_is_read(self):
        write_cache(self.path, (9.0, 9.0, 9.5, 9.5), osm.FILTERS_VERSION, [ROAD])
        self.assertIsNone(osm.load_cache(self.path, BOX))
        self.assertIsNone(osm.load_cache(self.path, BOX, filters=None))

    def test_the_steps_that_only_read_ask_for_whatever_is_there(self):
        # The rule itself, in the two places that read the download: a build
        # and a paper map never fetch, so neither may insist on a version.
        import inspect

        from knoxbuild import build, worldmap
        for module in (build, worldmap):
            source = inspect.getsource(module)
            for line in source.splitlines():
                if "load_cache(" in line and "def " not in line:
                    self.assertIn("filters=None", line, f"{module.__name__}: {line}")


if __name__ == "__main__":
    unittest.main()

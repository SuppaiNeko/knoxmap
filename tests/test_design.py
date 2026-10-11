"""The drawing board's sheet: metres on a blank page, into a real map.

The sheet is the one place in KnoxMap where somebody says how big a map is in
metres and expects that many metres. Everything else rounds up to whole
300-tile cells, which turns a small error into a large one: a 900 m sheet
measured as 900.04 m is four cells across instead of three, a third more map
from four centimetres. So the size is checked against the same question the
renderer asks, not against the arithmetic that produced it.
"""
import math
import unittest

from generator import sketch


def cells(metres: float) -> int:
    return max(300, math.ceil(metres / 300) * 300)


def sheet(width=900, height=900, lat=0.0, lon=0.0, features=None):
    return {"width_m": width, "height_m": height,
            "anchor": {"lat": lat, "lon": lon},
            "features": features or []}


class SheetSize(unittest.TestCase):
    def test_a_sheet_is_the_size_it_says(self):
        for width, height, lat in ((900, 900, 0.0), (900, 700, 42.0),
                                   (1500, 1200, 60.0), (300, 300, -35.0),
                                   (2400, 600, 12.0)):
            with self.subTest(width=width, height=height, lat=lat):
                box = sketch.design_bbox(sheet(width, height, lat))
                got_w, got_h = sketch._utm_extent(*box)
                self.assertAlmostEqual(got_w, width, delta=0.3)
                self.assertAlmostEqual(got_h, height, delta=0.3)

    def test_it_never_measures_over_and_so_never_costs_a_whole_cell(self):
        # The one that matters: at or just over a 300 m boundary.
        for size in (300, 600, 900, 1200, 3000):
            for lat in (0.0, 42.0, -35.0, 60.0):
                with self.subTest(size=size, lat=lat):
                    box = sketch.design_bbox(sheet(size, size, lat))
                    got_w, got_h = sketch._utm_extent(*box)
                    self.assertLessEqual(got_w, size)
                    self.assertLessEqual(got_h, size)
                    self.assertEqual(cells(got_w), size)
                    self.assertEqual(cells(got_h), size)

    def test_a_silly_size_is_brought_into_range(self):
        for asked in (0, -5, 10, 1e9, None, "big"):
            with self.subTest(asked=asked):
                w, h = sketch._sheet_size({"width_m": asked, "height_m": asked})
                self.assertGreaterEqual(w, sketch.MIN_SHEET_M)
                self.assertLessEqual(w, sketch.MAX_SHEET_M)
                self.assertGreaterEqual(h, sketch.MIN_SHEET_M)

    def test_a_sheet_with_no_corner_still_lands_somewhere(self):
        south, west, north, east = sketch.design_bbox({"width_m": 500,
                                                       "height_m": 500})
        self.assertEqual((south, west), (0.0, 0.0))
        self.assertGreater(north, south)
        self.assertGreater(east, west)

    def test_a_corner_off_the_world_is_not_used(self):
        for bad in ({"lat": 95.0, "lon": 0.0}, {"lat": 0.0, "lon": 400.0},
                    {"lat": "x", "lon": 0.0}, {}):
            with self.subTest(bad=bad):
                self.assertEqual(sketch._anchor({"anchor": bad}), (0.0, 0.0))


class WhatIsOnIt(unittest.TestCase):
    def test_metres_from_the_corner_land_in_the_right_corner(self):
        # (0, 0) is the bottom left of the sheet and (w, h) the top right.
        s, w, n, e = sketch.design_bbox(sheet(1000, 800))
        drawn = sketch.design_to_drawn(sheet(1000, 800, features=[
            {"kind": "residential", "points": [[0, 0], [1000, 800]]}]))
        (lon0, lat0), (lon1, lat1) = drawn["features"][0]["geometry"]["coordinates"]
        self.assertAlmostEqual(lat0, s, places=9)
        self.assertAlmostEqual(lon0, w, places=9)
        self.assertAlmostEqual(lat1, n, places=9)
        self.assertAlmostEqual(lon1, e, places=9)

    def test_an_area_comes_out_closed(self):
        drawn = sketch.design_to_drawn(sheet(features=[
            {"kind": "house", "points": [[10, 10], [20, 10], [20, 20], [10, 20]]}]))
        ring = drawn["features"][0]["geometry"]["coordinates"][0]
        self.assertEqual(ring[0], ring[-1])

    def test_the_palette_decides_the_shape(self):
        drawn = sketch.design_to_drawn(sheet(features=[
            {"kind": "residential", "points": [[0, 0], [10, 10]]},
            {"kind": "house", "points": [[0, 0], [10, 0], [10, 10]]},
            {"kind": "tree", "points": [[5, 5]]}]))
        self.assertEqual([f["geometry"]["type"] for f in drawn["features"]],
                         ["LineString", "Polygon", "Point"])

    def test_what_cannot_be_drawn_is_left_out(self):
        drawn = sketch.design_to_drawn(sheet(features=[
            {"kind": "not_a_tool", "points": [[0, 0], [10, 10]]},
            {"kind": "residential", "points": [[0, 0]]},
            {"kind": "house", "points": [[0, 0], [10, 10]]},
            {"points": [[0, 0], [1, 1]]},
            "rubbish"]))
        self.assertEqual(drawn["features"], [])

    def test_a_sheet_becomes_features_the_generator_knows(self):
        from generator.osm import classify
        drawn = sketch.design_to_drawn(sheet(features=[
            {"kind": "primary", "points": [[0, 450], [900, 450]]},
            {"kind": "house", "points": [[10, 10], [40, 10], [40, 40], [10, 40]]}]))
        feats = sketch.features(drawn)
        self.assertEqual([classify(f.tags, area=f.geometry[0] == f.geometry[-1])
                          for f in feats], ["road_major", "building"])

    def test_nothing_drawn_is_nothing_built(self):
        self.assertEqual(sketch.design_to_drawn(None)["features"], [])
        self.assertEqual(sketch.design_to_drawn({})["features"], [])


if __name__ == "__main__":
    unittest.main()

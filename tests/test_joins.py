"""Where a built map's roads reach its edge (knoxbuild/joins.py).

The drawing board has no world under it, so a street drawn on a sheet stopped
dead at the edge and a map drawn to carry on from another never lined up with
it. What is worth checking is that the places its roads leave it are read off
the map itself, and that they land on the right side of the new sheet however
the two are put together - a sheet east of a map meets its eastern edge on
the sheet's own western one, and the distance along the border is measured
from the same corner on both.
"""
import os
import tempfile
import unittest

import numpy as np
from PIL import Image

from knoxbuild import joins


def a_map(width=120, height=80, metres=2.0, roads=()):
    """A map folder with nothing in it but its size and its roads."""
    directory = tempfile.mkdtemp()
    with open(os.path.join(directory, "m_info.json"), "w", encoding="utf-8") as f:
        f.write('{"width_tiles": %d, "height_tiles": %d, "meters_per_tile": %s}'
                % (width, height, metres))
    picture = np.zeros((height, width), dtype=np.uint8)
    for where, which, rank in roads:
        if which == "row":
            picture[where[0]:where[1], :] = rank
        else:
            picture[:, where[0]:where[1]] = rank
    Image.fromarray(picture).save(os.path.join(directory, "m_road_hierarchy.bmp"),
                                  format="BMP")
    return directory


class WhereTheRoadsComeOut(unittest.TestCase):
    def test_a_road_across_the_map_leaves_both_sides(self):
        # Four tiles of road at 2 m a tile, across the middle: 8 m wide, and
        # the middle of it is 80 m down a 160 m map.
        where = a_map(roads=[((38, 42), "row", 3)])
        found = joins.edges(where, "m")
        self.assertEqual(found["widthM"], 240.0)
        self.assertEqual(found["heightM"], 160.0)
        for side in ("W", "E"):
            self.assertEqual(found["roads"][side],
                             [{"at": 80.0, "width": 8.0, "rank": 3}])
        self.assertEqual(found["roads"]["N"], [])
        self.assertEqual(found["roads"]["S"], [])

    def test_a_kerb_is_not_a_street_and_neither_is_a_whole_edge(self):
        thin = a_map(roads=[((0, 1), "row", 2)])          # one tile
        self.assertEqual(joins.edges(thin, "m")["roads"]["W"], [])
        broad = a_map(height=400, roads=[((0, 300), "row", 2)])
        self.assertEqual(joins.edges(broad, "m")["roads"]["W"], [])

    def test_a_map_with_no_road_picture_still_gives_its_size(self):
        bare = tempfile.mkdtemp()
        with open(os.path.join(bare, "m_info.json"), "w", encoding="utf-8") as f:
            f.write('{"width_tiles": 50, "height_tiles": 40, "meters_per_tile": 1}')
        found = joins.edges(bare, "m")
        self.assertEqual((found["widthM"], found["heightM"]), (50.0, 40.0))
        self.assertEqual(found["roads"], {s: [] for s in joins.SIDES})

    def test_a_map_folder_with_nothing_in_it_is_not_an_error(self):
        found = joins.edges(tempfile.mkdtemp(), "m")
        self.assertEqual((found["widthM"], found["heightM"]), (0.0, 0.0))


class OnTheNewSheet(unittest.TestCase):
    """The sheet is lined up with the map's corner on the side they share."""

    def setUp(self):
        # A road across the middle (east-west) and one down it (north-south).
        self.dir = a_map(roads=[((38, 42), "row", 3), ((58, 62), "col", 4)])

    def test_a_sheet_east_of_it_meets_its_eastern_edge_on_the_sheets_west(self):
        out = joins.sheet_joins(self.dir, "m", "E", 500.0, 160.0)
        self.assertEqual(out["meets"], "W")
        self.assertEqual(out["joins"], [{"x": 0.0, "y": 80.0, "width": 8.0,
                                         "rank": 3, "side": "W"}])

    def test_a_sheet_west_of_it_meets_it_on_the_sheets_east(self):
        out = joins.sheet_joins(self.dir, "m", "W", 500.0, 160.0)
        self.assertEqual(out["meets"], "E")
        self.assertEqual([j["x"] for j in out["joins"]], [500.0])

    def test_north_and_south_are_measured_across_from_the_same_corner(self):
        # A sheet north of the map has the map along its bottom edge, which
        # on a sheet measured upwards from its own corner is y = 0.
        north = joins.sheet_joins(self.dir, "m", "N", 240.0, 300.0)
        self.assertEqual(north["meets"], "S")
        self.assertEqual(north["joins"],
                         [{"x": 120.0, "y": 0.0, "width": 8.0, "rank": 4,
                           "side": "S"}])
        south = joins.sheet_joins(self.dir, "m", "S", 240.0, 300.0)
        self.assertEqual(south["meets"], "N")
        self.assertEqual([(j["x"], j["y"]) for j in south["joins"]], [(120.0, 300.0)])

    def test_a_road_past_the_end_of_a_short_sheet_is_left_off(self):
        out = joins.sheet_joins(self.dir, "m", "E", 500.0, 40.0)
        self.assertEqual(out["joins"], [])

    def test_a_side_that_is_not_a_side(self):
        with self.assertRaises(ValueError):
            joins.sheet_joins(self.dir, "m", "up", 100.0, 100.0)


if __name__ == "__main__":
    unittest.main()

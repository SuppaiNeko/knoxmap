import unittest

from shapely.geometry import MultiPolygon, Polygon

from knoxbuild.areas import AreaIndex


class Mask(unittest.TestCase):
    def test_a_multipolygon_area_is_drawn_not_a_crash(self):
        # issue #32: buffer(0) on a self-touching outline gives a MultiPolygon.
        two = MultiPolygon([Polygon([(0, 0), (4, 0), (4, 4), (0, 4)]),
                            Polygon([(10, 10), (14, 10), (14, 14), (10, 14)])])
        one = Polygon([(0, 16), (4, 16), (4, 19), (0, 19)])
        idx = AreaIndex([(two, {"category": "yard"}), (one, {"category": "yard"})])
        mask = idx.mask({"yard"}, 20, 20)
        self.assertTrue(mask[2, 2])
        self.assertTrue(mask[12, 12])
        self.assertTrue(mask[17, 2])
        self.assertFalse(mask[7, 7])


if __name__ == "__main__":
    unittest.main()

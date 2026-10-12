import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import numpy as np

from knoxbuild import build
from knoxbuild import layout
from knoxbuild.footprint import Footprint, place
from knoxbuild.profile import BuildingProfile


class EntrancePointMapping(unittest.TestCase):
    def test_entrance_nodes_are_not_consumed_as_points_of_use(self):
        grid = {(0, 0): [(10.5, 10.0, {"entrance": "main"}),
                         (10.5, 10.0, {"shop": "bakery"})]}
        outline = [(10, 10), (20, 10), (20, 20), (10, 20)]
        taken = set()
        self.assertEqual(build._points_inside(grid, outline, taken), [{"shop": "bakery"}])
        self.assertEqual(build._entrances_inside(grid, outline),
                         [(10.5, 10.0, {"entrance": "main"})])

    def test_split_footprint_receives_nearby_entrance_locally(self):
        units = [Footprint(10, 20, np.ones((4, 4), dtype=bool), 0, 4, 4),
                 Footprint(14, 20, np.ones((4, 4), dtype=bool), 0, 4, 4)]
        entrances = build._entrances_by_unit(
            [(17.5, 21.5, {"entrance": "main"})], units)
        self.assertEqual(entrances[0], [])
        self.assertEqual(entrances[1], [(3.5, 1.5, {"entrance": "main"})])

    def test_worker_forwards_entrances_after_party_walls(self):
        entrances = [(2.0, 0.0, {"entrance": "main"})]
        profile = BuildingProfile.infer({}, "house", 0.15, 90, 1, 4)
        party = {(0, 0, "N"): 1}
        plan = SimpleNamespace(storeys=[], rooms=[], furniture=[], escalators=[])
        with TemporaryDirectory() as directory:
            path = f"{directory}/building.tbx"
            job = (4, 4, 1, False, 1, "hospital", None, None, None, "hospital",
                     path, None, False, [], False, entrances, profile, [],
                     False, None, party)
            with mock.patch.object(build, "build_building", return_value=plan) as make, \
                    mock.patch.object(build, "render_tbx", return_value="tbx"):
                result = build._make_one(job)
            self.assertIsNone(result[3])
            make.assert_called_once()
            self.assertEqual(make.call_args.kwargs["entrances"], entrances)
            self.assertIs(make.call_args.kwargs["profile"], profile)
            self.assertEqual(make.call_args.kwargs["party"], party)

    def test_overlapping_footprints_become_adjoining_party_walls(self):
        occupied = np.zeros((40, 40), dtype=bool)
        lots = np.zeros_like(occupied)
        first, reason = place([(10, 10), (20, 10), (20, 20), (10, 20)],
                              occupied, lots=lots)
        self.assertEqual(reason, "ok")
        second, reason = place([(18, 10), (28, 10), (28, 20), (18, 20)],
                               occupied, lots=lots)
        self.assertEqual(reason, "ok")
        self.assertTrue(first.x0 + first.width <= second.x0
                        or second.x0 + second.width <= first.x0
                        or first.y0 + first.height <= second.y0
                        or second.y0 + second.height <= first.y0)

        owner = np.full(occupied.shape, -1, dtype=np.int32)
        owner[first.y0:first.y0 + first.height,
              first.x0:first.x0 + first.width][first.mask] = 0
        owner[second.y0:second.y0 + second.height,
              second.x0:second.x0 + second.width][second.mask] = 1
        party = build._party_walls(owner, 1, second.x0, second.y0,
                                   second.mask, [1, 1])

        self.assertTrue(party)
        plan = layout.build_building(second.width, second.height, kind="shed",
                                     party=party, seed=4).storeys[0]
        self.assertTrue(plan.doors)
        self.assertTrue(set(party).isdisjoint(plan.doors))


class TheSideAFrontDoorFaces(unittest.TestCase):
    """A building with no street near it put its door on the south wall, which
    for a hut on a pier is the lake. The way in is the side that is not
    water."""

    def ground(self, paint):
        import os

        from PIL import Image

        from generator import pz_colors as C

        directory = self.enterContext(TemporaryDirectory())
        tiles = np.zeros((40, 40, 3), dtype=np.uint8)
        tiles[:, :] = C.MEDIUM_GRASS
        paint(tiles, C)
        Image.fromarray(tiles).save(os.path.join(directory, "m.bmp"), format="BMP")
        return build._street_finder(directory, "m")

    def test_a_hut_at_the_water_has_its_door_on_the_landward_side(self):
        side = self.ground(lambda tiles, C: tiles.__setitem__(
            (slice(20, None), slice(None)), C.WATER))
        self.assertEqual(side(10, 16, 6, 4), "N")

    def test_a_hut_out_on_a_pier_faces_the_shore(self):
        def paint(tiles, C):
            tiles[:, :] = C.WATER
            tiles[:, :8] = C.MEDIUM_GRASS
        self.assertEqual(self.ground(paint)(12, 18, 4, 4), "W")

    def test_inland_with_no_street_is_still_no_answer(self):
        side = self.ground(lambda tiles, C: None)
        self.assertIsNone(side(10, 2, 6, 4))

    def test_a_street_is_still_what_decides_where_there_is_one(self):
        def paint(tiles, C):
            tiles[20:, :] = C.WATER
            tiles[10:12, :] = C.MEDIUM_ASPHALT      # the road along the shore
        self.assertEqual(self.ground(paint)(10, 16, 6, 4), "N")


if __name__ == "__main__":
    unittest.main()
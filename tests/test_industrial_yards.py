"""An industrial estate is ground a building may stand on.

The renderer paints a works' yard in the game's beige gravel, which is the
same colour it paints a track bed and a service lane. The building placer read
that colour as road and refused every footprint inside the estate, so a town's
industrial end came out as empty gravel in the exact shape of the landuse
polygon - with the Knox County roads setting on or off, which is what made it
clear the roads were not to blame.
"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np
from PIL import Image

from generator import pz_colors as C
from knoxbuild.areas import AreaIndex
from knoxbuild.build import YARD_AREAS, _road_weight

PROJ = SimpleNamespace(width=40, height=40, to_px=lambda lat, lon: (lon, lat))


def ring(x0, y0, x1, y1):
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]


class YardsAreNotRoads(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        ground = np.zeros((40, 40, 3), dtype=np.uint8)
        ground[:, :] = C.MEDIUM_GRASS
        ground[10:30, 10:30] = C.LIGHT_ASPHALT      # the works' yard
        ground[:, 34:38] = C.MEDIUM_ASPHALT         # a street past it
        Image.fromarray(ground).save(os.path.join(self.dir, "m.bmp"), format="BMP")

    def areas(self, category="industrial"):
        path = os.path.join(self.dir, "m_areas.geojson")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": [{
                "type": "Feature", "properties": {"category": category},
                "geometry": {"type": "Polygon",
                             "coordinates": ring(10, 10, 30, 30)}}]}, f)
        return AreaIndex.load(self.dir, "m", PROJ)

    def test_the_yard_of_an_industrial_estate_is_free_ground(self):
        weight = _road_weight(self.dir, "m", PROJ, None, self.areas())
        self.assertEqual(int(weight[20, 20]), 0, "the yard still reads as road")
        self.assertEqual(int(weight[20, 35]), 2, "the street stopped being one")

    def test_without_the_land_use_it_is_the_colour_that_decides(self):
        # Nothing says the gravel is a yard, so it is treated as it always was.
        weight = _road_weight(self.dir, "m", PROJ, None, None)
        self.assertEqual(int(weight[20, 20]), 2)

    def test_a_road_across_the_yard_is_still_a_road(self):
        hierarchy = np.zeros((40, 40), dtype=np.uint8)
        hierarchy[19:22, :] = 3                     # the works' access road
        weight = _road_weight(self.dir, "m", PROJ, hierarchy, self.areas())
        self.assertEqual(int(weight[20, 20]), 3)
        self.assertEqual(int(weight[25, 20]), 0)

    def test_a_car_park_is_left_alone(self):
        # A building in the middle of a car park is a kiosk nobody mapped;
        # keeping footprints off them is right.
        self.assertNotIn("parking", YARD_AREAS)
        weight = _road_weight(self.dir, "m", PROJ, None, self.areas("parking"))
        self.assertEqual(int(weight[20, 20]), 2)


if __name__ == "__main__":
    unittest.main()

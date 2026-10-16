import unittest
from unittest import mock

from PIL import Image

from generator import pz_colors as C
from generator import renderer
from generator.rendering import furniture


class RoadFurniturePlacement(unittest.TestCase):
    def make_road(self, roadside):
        ground = Image.new("RGB", (40, 40), C.DARK_GRASS)
        pixels = ground.load()
        for y in range(40):
            for x in range(8, 13):
                pixels[x, y] = C.MEDIUM_ASPHALT
            pixels[13, y] = C.PALE_CONCRETE
            pixels[14, y] = roadside
        vegetation = Image.new("RGB", ground.size, C.VEG_NOTHING)
        vegetation.putpixel((12, 20), C.EDGE_LINE_E)
        return vegetation, ground

    def test_signs_and_lamps_skip_asphalt_destination(self):
        for mod_tiles, prop in ((True, "speed"), (False, "lamp")):
            with self.subTest(prop=prop):
                vegetation, ground = self.make_road(C.DARK_ASPHALT)
                with mock.patch.object(furniture, "_mod_tiles_ready", return_value=mod_tiles):
                    renderer._paint_street_furniture(vegetation, ground)
                blocked_color = (C.SPEED_SIGNS[(25, "S")] if prop == "speed"
                                 else C.LAMP_W)
                self.assertNotEqual(vegetation.getpixel((14, 20)), blocked_color)

    def test_lamp_can_use_a_grass_verge(self):
        vegetation, ground = self.make_road(C.DARK_GRASS)
        with mock.patch.object(furniture, "_mod_tiles_ready", return_value=False):
            counts = renderer._paint_street_furniture(vegetation, ground)
        self.assertGreater(counts.get("lamp", 0), 0)


if __name__ == "__main__":
    unittest.main()

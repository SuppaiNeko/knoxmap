"""Zones the mapper draws, each with settings of its own.

A zone is the one place where two parts of one map are built to different
numbers, so what is worth checking is that the numbers really change inside it
and really do not outside it, that a zone cannot set what it has no business
setting, and that nesting resolves the way a mapper would expect.
"""
import unittest

from knoxbuild import zones
from knoxbuild.settings import LIMITS, Settings


class Projector:
    """Stands in for the renderer's, so a ring of degrees becomes tiles."""

    @staticmethod
    def to_px(lat, lon):
        return lon * 1000.0, lat * 1000.0


def square(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def zoning(*zone_dicts, settings=None):
    return zones.Zoning.build(list(zone_dicts), Projector(),
                              settings or Settings())


class Cleaning(unittest.TestCase):
    def test_a_zone_cannot_set_what_decides_the_whole_map(self):
        # The seed, the scale and the rotation are what the map is; a zone
        # with its own would be a different map in the middle of this one.
        kept = zones.clean([{"ring": square(0, 0, 1, 1),
                             "settings": {"seed": 7, "align_streets": 0,
                                          "rotate_degrees": 45,
                                          "max_levels": 9}}])
        self.assertEqual(kept[0]["settings"], {"max_levels": 9.0})

    def test_values_are_clamped_to_what_the_generator_accepts(self):
        lo, hi = LIMITS["max_levels"]
        kept = zones.clean([{"ring": square(0, 0, 1, 1),
                             "settings": {"max_levels": hi + 500}},
                            {"ring": square(0, 0, 1, 1),
                             "settings": {"max_levels": lo - 500}}])
        self.assertEqual(kept[0]["settings"]["max_levels"], float(hi))
        self.assertEqual(kept[1]["settings"]["max_levels"], float(lo))

    def test_a_shape_that_is_not_a_shape_is_dropped(self):
        self.assertEqual(zones.clean([{"ring": [[0, 0], [1, 1]]}]), [])
        self.assertEqual(zones.clean([{"ring": "nonsense"}]), [])
        self.assertEqual(zones.clean(["rubbish", 7, None]), [])
        self.assertEqual(zones.clean(None), [])

    def test_unreadable_numbers_are_left_out_rather_than_guessed_at(self):
        kept = zones.clean([{"ring": square(0, 0, 1, 1),
                             "settings": {"max_levels": "tall",
                                          "spawn_density": float("nan"),
                                          "room_size": 20}}])
        self.assertEqual(kept[0]["settings"], {"room_size": 20.0})

    def test_a_pinned_wall_style_is_kept_as_a_name(self):
        kept = zones.clean([{"ring": square(0, 0, 1, 1), "wall_style": " brick "}])
        self.assertEqual(kept[0][zones.STYLE_KEY], "brick")
        bare = zones.clean([{"ring": square(0, 0, 1, 1), "wall_style": 5}])
        self.assertNotIn(zones.STYLE_KEY, bare[0])


class Asking(unittest.TestCase):
    def test_inside_the_zone_its_settings_answer(self):
        z = zoning({"ring": square(0, 0, 1, 1),
                    "settings": {"max_levels": 12}})
        self.assertEqual(z.settings_at(500, 500).max_levels, 12)

    def test_outside_it_the_map_answers_with_its_own_object(self):
        base = Settings()
        z = zoning({"ring": square(0, 0, 1, 1),
                    "settings": {"max_levels": 12}}, settings=base)
        # The same object, not a copy: a map with no zones must cost nothing.
        self.assertIs(z.settings_at(5000, 5000), base)

    def test_a_zone_leaves_the_settings_it_says_nothing_about_alone(self):
        base = Settings()
        z = zoning({"ring": square(0, 0, 1, 1), "settings": {"max_levels": 12}})
        inside = z.settings_at(500, 500)
        self.assertEqual(inside.max_levels, 12)
        self.assertEqual(inside.room_size, base.room_size)
        self.assertEqual(inside.seed, base.seed)

    def test_the_smallest_zone_wins_so_they_can_nest(self):
        z = zoning({"ring": square(0, 0, 10, 10), "settings": {"max_levels": 3}},
                   {"ring": square(1, 1, 2, 2), "settings": {"max_levels": 9}})
        self.assertEqual(z.settings_at(1500, 1500).max_levels, 9)   # inner
        self.assertEqual(z.settings_at(5000, 5000).max_levels, 3)   # outer

    def test_an_int_setting_stays_an_int(self):
        # It is read straight into the generator, which indexes and ranges on
        # it; a float max_levels raises there rather than here.
        z = zoning({"ring": square(0, 0, 1, 1), "settings": {"max_levels": 7}})
        self.assertIsInstance(z.settings_at(500, 500).max_levels, int)

    def test_a_pinned_style_is_only_pinned_inside(self):
        z = zoning({"ring": square(0, 0, 1, 1), "wall_style": "brick"})
        self.assertEqual(z.style_at(500, 500), "brick")
        self.assertIsNone(z.style_at(5000, 5000))

    def test_no_zones_is_the_map_everywhere(self):
        base = Settings()
        z = zoning(settings=base)
        self.assertEqual(len(z), 0)
        self.assertIs(z.settings_at(1, 1), base)
        self.assertIsNone(z.style_at(1, 1))


class AcrossTheWholeMap(unittest.TestCase):
    def test_the_chunk_grid_says_what_each_part_of_the_map_is_set_to(self):
        # The spawn map is drawn a chunk at a time, so the zones are painted
        # into that grid once rather than asked about chunk by chunk.
        # The projector above makes a degree a thousand tiles, so this zone
        # is tiles 0-100 of a grid 20 chunks of 10 tiles across: 200 tiles.
        base = Settings()
        z = zoning({"ring": square(0, 0, 0.1, 0.1),
                    "settings": {"spawn_density": 60}}, settings=base)
        grid = z.chunk_values(("spawn_density",), gw=20, gh=20,
                              tiles_per_chunk=10)["spawn_density"]
        self.assertEqual(grid.shape, (20, 20))
        self.assertEqual(grid[5, 5], 60.0)                          # inside
        self.assertEqual(grid[19, 19], float(base.spawn_density))   # outside

    def test_a_zone_inside_another_wins_on_the_grid_too(self):
        z = zoning({"ring": square(0, 0, 0.2, 0.2), "settings": {"spawn_density": 5}},
                   {"ring": square(0, 0, 0.05, 0.05), "settings": {"spawn_density": 60}})
        grid = z.chunk_values(("spawn_density",), gw=30, gh=30,
                              tiles_per_chunk=10)["spawn_density"]
        self.assertEqual(grid[2, 2], 60.0)      # tiles 20-30: the small one
        self.assertEqual(grid[15, 15], 5.0)     # tiles 150-160: the big one

    def test_with_no_zones_every_chunk_is_the_map(self):
        base = Settings()
        grid = zoning(settings=base).chunk_values(
            ("spawn_density",), gw=4, gh=4, tiles_per_chunk=10)["spawn_density"]
        self.assertTrue((grid == float(base.spawn_density)).all())


class AZoneDrawnOverItself(unittest.TestCase):
    """An outline that crosses itself is still the ground it covers.

    Mending such an outline gives several polygons back instead of one, and
    one of those has no ring to paint, so painting the zones into the chunk
    grid threw 'MultiPolygon object has no attribute exterior'.
    """

    # Two squares joined by the line between them: tiles 0-40 and 60-100.
    PINCHED = [[0, 0], [0.04, 0], [0.04, 0.04], [0, 0.04], [0, 0],
               [0.06, 0.06], [0.1, 0.06], [0.1, 0.1], [0.06, 0.1], [0.06, 0.06]]

    def zoning(self):
        return zoning({"ring": self.PINCHED, "settings": {"spawn_density": 60}})

    def test_each_part_of_it_is_a_zone_of_its_own(self):
        z = self.zoning()
        self.assertEqual(len(z), 2)
        self.assertEqual(z.settings_at(20, 20).spawn_density, 60)
        self.assertEqual(z.settings_at(80, 80).spawn_density, 60)
        self.assertEqual(z.settings_at(50, 50).spawn_density,
                         Settings().spawn_density)

    def test_a_zone_inside_one_of_its_halves_still_wins(self):
        # Each half is smaller than the zone nested in it, and "the smallest
        # zone here wins" would hand the ground to the half that contains it.
        # What decides is the whole of a zone, however many pieces it is in.
        z = zoning({"ring": self.PINCHED, "settings": {"spawn_density": 5}},
                   {"ring": square(0.01, 0.01, 0.035, 0.035),
                    "settings": {"spawn_density": 60}})
        self.assertEqual(z.settings_at(20, 20).spawn_density, 60)
        self.assertEqual(z.settings_at(5, 5).spawn_density, 5)
        grid = z.chunk_values(("spawn_density",), gw=20, gh=20,
                              tiles_per_chunk=10)["spawn_density"]
        self.assertEqual(grid[2, 2], 60.0)      # tiles 20-30: the nested one
        self.assertEqual(grid[0, 0], 5.0)       # tiles 0-10: the big one

    def test_and_both_are_painted_into_the_chunk_grid(self):
        grid = self.zoning().chunk_values(("spawn_density",), gw=20, gh=20,
                                          tiles_per_chunk=10)["spawn_density"]
        self.assertEqual(grid[1, 1], 60.0)                          # tiles 10-20
        self.assertEqual(grid[8, 8], 60.0)                          # tiles 80-90
        self.assertEqual(grid[5, 5], float(Settings().spawn_density))


class ForTheWindow(unittest.TestCase):
    def test_every_offered_setting_is_one_a_zone_really_reads(self):
        offered = {s["id"] for s in zones.overridable()}
        self.assertEqual(offered, set(zones.OVERRIDABLE))

    def test_every_offered_setting_is_a_real_one_with_limits(self):
        known = Settings().to_dict()
        for entry in zones.overridable():
            with self.subTest(entry["id"]):
                self.assertIn(entry["id"], known)
                self.assertIn(entry["id"], LIMITS)
                self.assertLessEqual(entry["min"], entry["max"])


if __name__ == "__main__":
    unittest.main()

"""The dials and limits people asked for, and what they are worth.

Each of these came from somebody building maps, so what is worth checking is
the thing they asked for rather than the mechanism: that a scale between the
whole metres is really kept, that a skyline asked for is the skyline built,
that the woods can differ from the trees in the gardens, and that one kind of
place cannot take a whole list of landmarks.
"""
import collections
import random
import unittest

import app as knoxapp
from generator import places
from knoxbuild.build import building_levels
from knoxbuild.settings import HEIGHT_MIX_OPTIONS, LIMITS, Settings


class ScalesBetweenWholeMetres(unittest.TestCase):
    """1 m a tile is a town at full size and 2 m is everything in it half
    that; the steps in between are where a map is actually tuned."""

    def test_a_quarter_metre_is_kept_as_it_was_typed(self):
        for asked in (1.25, 1.5, 1.75, 2.25, 3.5):
            with self.subTest(asked=asked):
                self.assertEqual(knoxapp.tidy_scale(asked), asked)

    def test_anything_finer_is_rounded_to_the_nearest_quarter(self):
        self.assertEqual(knoxapp.tidy_scale(1.37), 1.25)
        self.assertEqual(knoxapp.tidy_scale(1.4), 1.5)
        self.assertEqual(knoxapp.tidy_scale(1.0137), 1.0)

    def test_the_half_metre_is_still_the_finest_there_is(self):
        for asked in (0.5, 0.4, 0.1, 0.0, -3):
            with self.subTest(asked=asked):
                self.assertEqual(knoxapp.tidy_scale(asked),
                                 knoxapp.MIN_METERS_PER_TILE)

    def test_a_scale_past_the_end_is_the_end(self):
        self.assertEqual(knoxapp.tidy_scale(900), knoxapp.MAX_METERS_PER_TILE)


class TheSkylineAsked(unittest.TestCase):
    """A height mix shapes the buildings OpenStreetMap says nothing about."""

    def heights(self, mix, kind=None, area=400, n=2000, **settings):
        base = {"preset": "city", "height_mix": mix, **settings}
        rng = random.Random(7)
        return [building_levels({}, kind, area, rng, Settings.from_dict(base),
                                1.0)[0] for _ in range(n)]

    def test_what_the_mapper_counted_is_never_overruled(self):
        # The whole point of the setting is that it fills in a blank. A tower
        # somebody counted the floors of stays the height they counted.
        for mix in HEIGHT_MIX_OPTIONS:
            with self.subTest(mix=mix):
                rng = random.Random(1)
                levels, from_osm = building_levels(
                    {"building:levels": "9"}, "apartment", 400, rng,
                    Settings.from_dict({"preset": "city", "height_mix": mix,
                                        "max_levels": 30}), 1.0)
                self.assertEqual((levels, from_osm), (9, True))

    def test_low_builds_lower_than_the_place_is_and_tall_builds_higher(self):
        low = self.heights("low")
        auto = self.heights("auto")
        tall = self.heights("tall")
        self.assertLess(sum(low) / len(low), sum(auto) / len(auto))
        self.assertGreater(sum(tall) / len(tall), sum(auto) / len(auto))

    def test_middle_gathers_in_the_middle_rather_than_spreading(self):
        # It used to average two draws, which rounds the halves of a pool of
        # 2, 3 and 4 outwards and came out wider than the guess it gathers.
        middle = collections.Counter(self.heights("middle"))
        auto = collections.Counter(self.heights("auto"))
        self.assertGreater(middle.most_common(1)[0][1],
                           auto.most_common(1)[0][1])

    def test_even_really_is_every_height_up_to_the_tallest(self):
        seen = set(self.heights("even", max_levels=8))
        self.assertEqual(seen, set(range(1, 9)))

    def test_as_the_place_is_leaves_the_generator_alone(self):
        # The default has to be what the generator did before there was a
        # setting, or every map ever made changes under its mapper.
        self.assertEqual(Settings().height_mix, "auto")
        rng = random.Random(3)
        settings = Settings()
        was = [building_levels({}, "civic", 900, rng, settings, 1.0)[0]
               for _ in range(50)]
        rng = random.Random(3)
        now = [building_levels({}, "civic", 900, rng,
                               Settings.from_dict({"height_mix": "auto"}), 1.0)[0]
               for _ in range(50)]
        self.assertEqual(was, now)

    def test_a_mix_the_window_does_not_offer_is_refused(self):
        self.assertEqual(Settings.from_dict({"height_mix": "enormous"}).height_mix,
                         "auto")

    def test_the_window_is_offered_every_mix_the_generator_knows(self):
        offered = {value for value, _label in
                   knoxapp.app.test_client().get("/api/settings")
                   .get_json()["options"]["height_mix"]}
        self.assertEqual(offered, set(HEIGHT_MIX_OPTIONS))


class WoodsApartFromTrees(unittest.TestCase):
    def test_the_woods_have_a_dial_of_their_own(self):
        self.assertEqual(Settings().forest_density, 1.0)
        self.assertIn("forest_density", LIMITS)
        self.assertEqual(Settings.from_dict({"forest_density": 99}).forest_density,
                         LIMITS["forest_density"][1])

    def test_it_is_a_setting_the_ground_has_to_be_drawn_again_for(self):
        # It paints the vegetation bitmap, so a map that already exists has to
        # be redrawn rather than only have its buildings laid out again.
        self.assertIn("forest_density", knoxapp.RENDER_SETTINGS)

    def test_the_renderer_takes_it(self):
        import inspect

        from generator import renderer

        got = inspect.signature(renderer.render).parameters["forest_density"]
        self.assertEqual(got.default, 1.0)


class LandmarksNobodyKindTakesOver(unittest.TestCase):
    """A town of forty churches used to spend a quarter of its list on them."""

    def town(self, **counts):
        rows = []
        for value, (key, n) in counts.items():
            for i in range(n):
                rows.append({"name": f"{value} {i}", "kind": key, "value": value,
                             "lat": 0.0, "lon": 0.0,
                             "_rank": places._notability(key, value, {})})
        rows.sort(key=lambda d: (-d["_rank"], d["value"], d["name"].lower()))
        return rows

    def setUp(self):
        self.rows = self.town(place_of_worship=("amenity", 400),
                              supermarket=("shop", 40),
                              theatre=("amenity", 5),
                              museum=("tourism", 3),
                              graveyard=("amenity", 1))

    def test_the_one_theatre_and_the_one_graveyard_are_in_the_list(self):
        kept = {d["value"] for d in places._share_out(self.rows, 100)}
        self.assertEqual(kept, {"place_of_worship", "supermarket", "theatre",
                                "museum", "graveyard"})

    def test_no_kind_takes_more_than_its_share_while_others_wait(self):
        kept = collections.Counter(d["value"]
                                   for d in places._share_out(self.rows, 100))
        share = max(places.VALUE_MIN, int(100 * places.VALUE_SHARE))
        # The churches only pass their share once everything else is in.
        self.assertGreater(kept["place_of_worship"], share)
        self.assertEqual(kept["supermarket"], share)

    def test_the_list_is_still_filled_to_the_limit(self):
        for limit in (10, 50, 100, 300):
            with self.subTest(limit=limit):
                self.assertEqual(len(places._share_out(self.rows, limit)), limit)

    def test_a_place_that_really_is_all_churches_still_gets_a_full_list(self):
        only = self.town(place_of_worship=("amenity", 200))
        self.assertEqual(len(places._share_out(only, 100)), 100)

    def test_a_short_list_is_left_exactly_as_it_was_ranked(self):
        self.assertEqual(places._share_out(self.rows[:20], 100), self.rows[:20])

    def test_more_landmarks_are_listed_than_the_old_three_hundred(self):
        self.assertGreater(places.LANDMARK_LIMIT, 300)


if __name__ == "__main__":
    unittest.main()

"""Hand-built buildings, kept and stamped onto plots.

The promise a preset makes is narrow and absolute: the building somebody built
is the building that appears. So what is worth checking is that nothing is
done to the file, that one is only chosen where it genuinely fits, and that
the same plot gets the same building every time the map is built - a preset
that moved around on a rebuild would be worse than no preset at all.
"""
import os
import tempfile
import unittest

from knoxbuild import presets


def tbx(width, height, floors=3, rooms=4):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<building version="4" width="{width}" height="{height}" '
            'ExteriorWall="19">\n'
            + "".join(' <room Name="livingroom"/>\n' for _ in range(rooms))
            + "".join(" <floor>\n </floor>\n" for _ in range(floors))
            + "</building>\n")


class Reading(unittest.TestCase):
    def test_a_building_file_is_measured_off_its_header(self):
        about = presets.measure(tbx(12, 10, floors=4, rooms=13))
        self.assertEqual((about["width"], about["height"]), (12, 10))
        self.assertEqual(about["rooms"], 13)
        # The generator writes a floor per storey and one more for the roof.
        self.assertEqual(about["storeys"], 3)

    def test_a_single_storey_building_is_one_storey(self):
        self.assertEqual(presets.measure(tbx(8, 8, floors=2))["storeys"], 1)

    def test_anything_that_is_not_a_building_file_is_not_one(self):
        for bad in ("", "hello", "<building>", '<building width="0" height="5">',
                    '<building height="5">'):
            with self.subTest(bad=bad):
                self.assertIsNone(presets.measure(bad))

    def test_a_name_that_could_escape_the_folder_cannot(self):
        for bad in ("../../etc/passwd", "a/b", "a\\b", "..", "  "):
            with self.subTest(bad=bad):
                clean = presets.safe_name(bad)
                self.assertNotIn("/", clean)
                self.assertNotIn("\\", clean)
                self.assertNotIn("..", clean)


class TheLibrary(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_what_goes_in_is_what_comes_out(self):
        # The whole point of a hand-built building: nothing happens to it.
        text = tbx(12, 10)
        presets.save(self.dir, "Brick Cottage", text)
        self.assertEqual(presets.read(self.dir, "Brick Cottage"), text)

    def test_saving_something_that_is_not_a_building_is_refused(self):
        with self.assertRaises(ValueError):
            presets.save(self.dir, "Nope", "hello")

    def test_a_preset_needs_a_name(self):
        with self.assertRaises(ValueError):
            presets.save(self.dir, "   ", tbx(4, 4))

    def test_the_library_lists_what_is_there_smallest_first(self):
        presets.save(self.dir, "Big", tbx(30, 30))
        presets.save(self.dir, "Small", tbx(6, 6))
        self.assertEqual([p["name"] for p in presets.library(self.dir)],
                         ["Small", "Big"])

    def test_a_stray_file_in_the_folder_is_not_offered(self):
        presets.save(self.dir, "Real", tbx(6, 6))
        with open(os.path.join(presets.folder(self.dir), "notes.txt"), "w") as f:
            f.write("shopping list")
        with open(os.path.join(presets.folder(self.dir), "broken.tbx"), "w") as f:
            f.write("not a building")
        self.assertEqual([p["name"] for p in presets.library(self.dir)], ["Real"])

    def test_a_library_that_is_not_there_is_empty_rather_than_an_error(self):
        self.assertEqual(presets.library(os.path.join(self.dir, "nowhere")), [])

    def test_one_can_be_thrown_away(self):
        presets.save(self.dir, "Gone", tbx(6, 6))
        self.assertTrue(presets.remove(self.dir, "Gone"))
        self.assertEqual(presets.library(self.dir), [])
        self.assertFalse(presets.remove(self.dir, "Gone"))


class Choosing(unittest.TestCase):
    def setUp(self):
        self.lib = [{"name": "Shed", "width": 4, "height": 4},
                    {"name": "Cottage", "width": 12, "height": 10},
                    {"name": "Villa", "width": 12, "height": 10},
                    {"name": "Hall", "width": 40, "height": 30}]

    def test_only_what_actually_fits_is_offered(self):
        fits = presets.fitting(self.lib, 12, 10)
        self.assertEqual({p["name"] for p in fits}, {"Shed", "Cottage", "Villa"})

    def test_a_preset_is_never_stretched_to_fit(self):
        # It is used at the size it was built, so a plot too small gets none.
        self.assertEqual(presets.fitting(self.lib, 3, 3), [])
        self.assertIsNone(presets.choose(self.lib, 3, 3, 0))

    def test_the_footprint_has_to_fit_the_way_round_it_is(self):
        # The .tbx is not rotated, so a 12x10 cottage does not go on a 10x12
        # plot however near the two are in area. Only the shed fits there.
        self.assertEqual([p["name"] for p in presets.fitting(self.lib, 10, 12)],
                         ["Shed"])

    def test_the_biggest_that_fits_wins(self):
        # A plot meant for a cottage should not get a shed on it.
        self.assertIn(presets.choose(self.lib, 12, 10, 0)["name"],
                      ("Cottage", "Villa"))
        self.assertEqual(presets.choose(self.lib, 40, 30, 0)["name"], "Hall")

    def test_the_same_plot_gets_the_same_building_every_time(self):
        # A rebuild that moved the hand-built houses around would be worse
        # than not having them.
        for seed in (0, 1, 7, 99, 1234):
            with self.subTest(seed=seed):
                first = presets.choose(self.lib, 12, 10, seed)["name"]
                again = presets.choose(self.lib, 12, 10, seed)["name"]
                self.assertEqual(first, again)

    def test_different_plots_do_not_all_get_the_same_one(self):
        # A street of identical plots should not be a street of one house.
        picked = {presets.choose(self.lib, 12, 10, s)["name"] for s in range(8)}
        self.assertEqual(picked, {"Cottage", "Villa"})

    def test_an_empty_library_chooses_nothing(self):
        self.assertIsNone(presets.choose([], 50, 50, 0))


class ForAKindOfBuilding(unittest.TestCase):
    """A preset that stands in for a sort of building, wherever one is built.

    The other half of the promise: a hand-built police station should be the
    police station on every map, without the mapper drawing a zone round each
    one. So what matters is that the kind is honoured - a shed is not stamped
    as a police station - and that the switch really is a switch.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.lib = [{"name": "Shed", "width": 4, "height": 4, "kind": "shed"},
                    {"name": "Station", "width": 12, "height": 10, "kind": "police"},
                    {"name": "Big Station", "width": 30, "height": 20, "kind": "police"},
                    {"name": "Anything", "width": 10, "height": 8, "kind": ""}]

    def test_every_kind_offered_is_one_the_generator_can_recognise(self):
        from knoxbuild.build import SPECIAL_BY_VALUE

        offered = {entry["id"] for entry in presets.kinds()}
        self.assertEqual(offered, {"house", "shed"} | set(SPECIAL_BY_VALUE.values()))
        self.assertTrue(all(entry["label"] for entry in presets.kinds()))

    def test_only_presets_built_for_the_kind_are_used_for_it(self):
        self.assertEqual([p["name"] for p in presets.for_kind(self.lib, "police")],
                         ["Station", "Big Station"])
        self.assertEqual(presets.for_kind(self.lib, "church"), [])

    def test_the_kind_is_preferred_and_any_building_is_the_fallback(self):
        # A plot big enough for the station gets the station...
        self.assertEqual(
            presets.choose_for_kind(self.lib, "police", 12, 10, 0)["name"],
            "Station")
        # ...and one too small for any police station falls back to the
        # preset built for no kind in particular, rather than to nothing.
        self.assertEqual(
            presets.choose_for_kind(self.lib, "police", 10, 8, 0)["name"],
            "Anything")

    def test_a_preset_for_another_kind_is_never_stamped_as_this_one(self):
        # The shed fits every plot here and is still never a police station.
        for width, height in ((4, 4), (6, 5), (9, 7)):
            with self.subTest(plot=(width, height)):
                self.assertIsNone(
                    presets.choose_for_kind(self.lib, "police", width, height, 0))

    def test_a_building_with_no_special_kind_is_a_house(self):
        houses = [{"name": "Cottage", "width": 10, "height": 8, "kind": "house"}]
        self.assertEqual(presets.choose_for_kind(houses, None, 12, 10, 0)["name"],
                         "Cottage")

    def test_nothing_is_switched_on_to_begin_with(self):
        self.assertEqual(presets.using(self.dir), set())

    def test_a_kind_can_be_switched_on_and_off_again(self):
        presets.set_use(self.dir, "police", True)
        presets.set_use(self.dir, "church", True)
        self.assertEqual(presets.using(self.dir), {"police", "church"})
        presets.set_use(self.dir, "church", False)
        self.assertEqual(presets.using(self.dir), {"police"})

    def test_a_kind_the_generator_does_not_know_cannot_be_switched_on(self):
        for bad in ("spaceport", "", None, "POLICE STATION"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    presets.set_use(self.dir, bad, True)

    def test_what_a_preset_is_for_is_kept_beside_it_not_in_it(self):
        text = tbx(12, 10)
        presets.save(self.dir, "Station", text, kind="police")
        # The .tbx is untouched: it is used exactly as it was saved.
        self.assertEqual(presets.read(self.dir, "Station"), text)
        self.assertEqual(presets.library(self.dir)[0]["kind"], "police")

    def test_a_preset_saved_without_a_kind_stands_in_for_any_building(self):
        presets.save(self.dir, "Whatever", tbx(8, 8))
        self.assertEqual(presets.library(self.dir)[0]["kind"], presets.ANY)

    def test_what_a_preset_is_for_can_be_changed(self):
        presets.save(self.dir, "Station", tbx(12, 10), kind="police")
        presets.set_kind(self.dir, "Station", "fire")
        self.assertEqual(presets.library(self.dir)[0]["kind"], "fire")
        presets.set_kind(self.dir, "Station", "")
        self.assertEqual(presets.library(self.dir)[0]["kind"], presets.ANY)

    def test_a_kind_the_generator_does_not_know_cannot_be_recorded(self):
        with self.assertRaises(ValueError):
            presets.save(self.dir, "Station", tbx(12, 10), kind="spaceport")

    def test_throwing_a_preset_away_forgets_what_it_was_for(self):
        presets.save(self.dir, "Station", tbx(12, 10), kind="police")
        presets.remove(self.dir, "Station")
        self.assertEqual(presets.manifest(self.dir)["kinds"], {})

    def test_a_folder_with_no_manifest_is_empty_rather_than_an_error(self):
        self.assertEqual(presets.manifest(os.path.join(self.dir, "nowhere")),
                         {"kinds": {}, "use": {}})

    def test_a_manifest_full_of_nonsense_is_ignored_rather_than_believed(self):
        os.makedirs(presets.folder(self.dir), exist_ok=True)
        path = os.path.join(presets.folder(self.dir), presets.MANIFEST)
        for junk in ("", "[1,2,3]", '{"kinds": 7, "use": "yes"}', "not json"):
            with self.subTest(junk=junk):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(junk)
                self.assertEqual(presets.manifest(self.dir),
                                 {"kinds": {}, "use": {}})
                self.assertEqual(presets.using(self.dir), set())


if __name__ == "__main__":
    unittest.main()

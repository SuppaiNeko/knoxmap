"""Buildings drawn by hand in the window (knoxbuild/designer.py).

A design is rectangles somebody dragged on a sheet, which is not what the rest
of the pipeline can build from: rooms there are rectangles that do not overlap.
So what is worth checking is that whatever is drawn comes out as rooms that
really do not overlap, that the building made from it is a building the game
can load - walls, doors, a way in, furniture - and that saving one keeps both
the .tbx and the drawing it came from.
"""
import os
import tempfile
import unittest

from knoxbuild import designer as D
from knoxbuild import presets as P


def room(x, y, w, h, kind="office", level=0):
    return {"level": level, "x": x, "y": y, "w": w, "h": h, "kind": kind}


def covered(design, level=0):
    """Every tile a room of this storey covers, with how many rooms cover it."""
    seen = {}
    for r in design["rooms"]:
        if r["level"] != level:
            continue
        for y in range(r["y"], r["y"] + r["h"]):
            for x in range(r["x"], r["x"] + r["w"]):
                seen[(x, y)] = seen.get((x, y), 0) + 1
    return seen


class TheSheet(unittest.TestCase):
    def test_a_blank_sheet_has_nothing_on_it(self):
        design = D.blank("police", 24, 18)
        self.assertEqual((design["width"], design["height"]), (24, 18))
        self.assertEqual(design["rooms"], [])
        self.assertEqual(design["kind"], "police")

    def test_a_sheet_is_kept_to_a_size_a_building_can_be(self):
        self.assertEqual(D.clean({"width": 2, "height": 9999})["width"],
                         D.MIN_SIDE)
        self.assertEqual(D.clean({"width": 2, "height": 9999})["height"],
                         D.MAX_SIDE)
        self.assertEqual(D.clean({"storeys": 99})["storeys"], D.MAX_STOREYS)

    def test_nonsense_is_a_blank_sheet_rather_than_an_error(self):
        for bad in (None, "hello", 7, [], {"rooms": "nope"},
                    {"rooms": [None, 3, {"x": "left"}]}):
            with self.subTest(bad=bad):
                design = D.clean(bad)
                self.assertGreaterEqual(design["width"], D.MIN_SIDE)
                self.assertIsInstance(design["rooms"], list)

    def test_a_room_that_is_not_a_room_the_game_knows_is_not_drawn(self):
        design = D.clean({"width": 20, "height": 20,
                          "rooms": [room(0, 0, 5, 5, "sauna")]})
        self.assertEqual(design["rooms"], [])

    def test_a_kind_of_building_the_generator_does_not_know_is_any_building(self):
        self.assertEqual(D.clean({"kind": "spaceport"})["kind"], "")

    def test_a_room_is_kept_inside_the_sheet(self):
        design = D.clean({"width": 10, "height": 10,
                          "rooms": [room(8, 8, 40, 40, "office")]})
        self.assertEqual(design["rooms"][0], room(8, 8, 2, 2, "office"))

    def test_a_design_rebuilt_from_its_own_output_is_the_same_design(self):
        # The window sends back what it was given, so cleaning has to settle.
        once = D.clean({"width": 20, "height": 16, "kind": "police", "seed": 5,
                        "rooms": [room(0, 0, 10, 8, "policeoffice"),
                                  room(5, 4, 10, 8, "policelocker")]})
        self.assertEqual(D.clean(once), once)


class DrawingOverWhatIsThere(unittest.TestCase):
    def test_the_newest_room_wins_and_the_old_one_keeps_the_rest(self):
        design = D.clean({"width": 20, "height": 20,
                          "rooms": [room(0, 0, 10, 10, "office"),
                                    room(5, 5, 10, 10, "storage")]})
        # Nothing is covered twice, and everything drawn is still covered.
        self.assertTrue(all(n == 1 for n in covered(design).values()))
        self.assertEqual(len(covered(design)), 10 * 10 + 10 * 10 - 5 * 5)
        at = {(x, y): r["kind"] for r in design["rooms"]
              for y in range(r["y"], r["y"] + r["h"])
              for x in range(r["x"], r["x"] + r["w"])}
        self.assertEqual(at[(7, 7)], "storage")     # the newer one
        self.assertEqual(at[(1, 1)], "office")      # what is left of the older

    def test_a_room_drawn_right_over_another_replaces_it(self):
        design = D.clean({"width": 20, "height": 20,
                          "rooms": [room(2, 2, 6, 6, "office"),
                                    room(2, 2, 6, 6, "kitchen")]})
        self.assertEqual([r["kind"] for r in design["rooms"]], ["kitchen"])

    def test_a_room_with_no_kind_is_the_eraser(self):
        design = D.clean({"width": 20, "height": 20,
                          "rooms": [room(0, 0, 10, 10, "office"),
                                    room(0, 0, 4, 10, "")]})
        self.assertEqual(len(covered(design)), 6 * 10)
        self.assertNotIn((1, 1), covered(design))

    def test_erasing_the_middle_leaves_the_ring_round_it(self):
        design = D.clean({"width": 20, "height": 20,
                          "rooms": [room(0, 0, 9, 9, "office"),
                                    room(3, 3, 3, 3, "")]})
        self.assertEqual(len(covered(design)), 9 * 9 - 9)
        self.assertTrue(all(n == 1 for n in covered(design).values()))

    def test_a_storey_is_drawn_without_cutting_the_one_below(self):
        design = D.clean({"width": 20, "height": 20, "storeys": 2,
                          "rooms": [room(0, 0, 10, 10, "office", level=0),
                                    room(0, 0, 10, 10, "bedroom", level=1)]})
        self.assertEqual(len(covered(design, 0)), 100)
        self.assertEqual(len(covered(design, 1)), 100)

    def test_a_room_drawn_on_a_storey_that_is_not_there_lands_on_the_ground(self):
        design = D.clean({"width": 20, "height": 20, "storeys": 1,
                          "rooms": [room(0, 0, 4, 4, "office", level=5)]})
        self.assertEqual(design["rooms"][0]["level"], 0)


class WhatGetsBuilt(unittest.TestCase):
    def setUp(self):
        self.design = D.clean(
            {"kind": "police", "width": 20, "height": 14, "seed": 11,
             "rooms": [room(0, 0, 10, 7, "policeoffice"),
                       room(10, 0, 10, 7, "policelocker"),
                       room(0, 7, 20, 7, "policehall")]})

    def test_the_rooms_drawn_are_the_rooms_built(self):
        built = D.stack(self.design)
        self.assertEqual(len(built.storeys), 1)
        kinds = {r.kind for r in built.storeys[0].rooms}
        self.assertEqual(kinds, {"policeoffice", "policelocker", "policehall"})

    def test_the_generator_puts_in_the_walls_doors_windows_and_furniture(self):
        storey = D.stack(self.design).storeys[0]
        self.assertTrue(storey.doors, "no doorways between the rooms")
        self.assertTrue(storey.windows, "no windows in the facade")
        self.assertTrue(storey.furniture, "nothing in the rooms")

    def test_there_is_a_way_in_from_outside(self):
        # A hand-built building with no exterior door is a sealed box.
        storey = D.stack(self.design).storeys[0]
        grid = storey.grid
        outside = []
        for x, y, d in storey.doors:
            near = grid[y][x] if 0 <= y < len(grid) and 0 <= x < len(grid[0]) else 0
            far = (grid[y][x - 1] if d == "W" and x else
                   grid[y - 1][x] if d == "N" and y else 0)
            if not near or not far:
                outside.append((x, y, d))
        self.assertTrue(outside, "nothing opens onto the street")

    def test_what_was_not_drawn_is_not_part_of_the_building(self):
        # The footprint is what was painted, so an L-shaped building is drawn
        # by leaving the corner empty rather than by masking it off.
        design = D.clean({"kind": "house", "width": 20, "height": 20,
                          "rooms": [room(0, 0, 10, 20, "livingroom"),
                                    room(10, 0, 10, 10, "kitchen")]})
        grid = D.stack(design).storeys[0].grid
        self.assertEqual(grid[15][15], 0)       # the empty corner
        self.assertNotEqual(grid[5][5], 0)

    def test_an_empty_sheet_builds_nothing_rather_than_raising(self):
        built = D.stack(D.blank("house", 10, 10))
        self.assertEqual(built.rooms, [])

    def test_a_single_tile_room_does_not_stop_the_build(self):
        design = D.clean({"kind": "house", "width": 12, "height": 12,
                          "rooms": [room(0, 0, 12, 12, "livingroom"),
                                    room(5, 5, 1, 1, "closet")]})
        self.assertTrue(D.stack(design).storeys[0].furniture)

    def test_two_storeys_get_one_staircase_in_the_same_place(self):
        design = dict(self.design, storeys=2)
        design["rooms"] = design["rooms"] + [
            dict(r, level=1) for r in self.design["rooms"]]
        built = D.stack(D.clean(design))
        self.assertEqual(len(built.storeys), 2)
        self.assertEqual(len(built.stairs), 1)
        cores = [[r for r in s.rooms if r.is_core] for s in built.storeys]
        self.assertTrue(all(len(c) == 1 for c in cores))
        self.assertEqual({(c[0].x0, c[0].y0, c[0].x1, c[0].y1) for c in cores}
                         .__len__(), 1)

    def test_the_same_design_builds_the_same_building_every_time(self):
        first = D.tbx_text(self.design, "Station")
        again = D.tbx_text(self.design, "Station")
        self.assertEqual(first, again)

    def test_a_building_of_a_drawn_design_is_a_building_file(self):
        about = P.measure(D.tbx_text(self.design, "Station"))
        self.assertEqual((about["width"], about["height"]), (20, 14))
        self.assertEqual(about["storeys"], 1)
        self.assertGreaterEqual(about["rooms"], 3)

    def test_a_design_names_the_walls_it_is_built_in(self):
        name = D.style_choices("police")[0]
        style = D.style_for(dict(self.design, style=name))
        self.assertTrue(style.get("exterior"))
        # A style the catalogue has nothing for is not an error: the building
        # keeps whatever its kind would have been built in.
        self.assertTrue(D.style_for(dict(self.design, style="marzipan")))


class StartingFromAGeneratedOne(unittest.TestCase):
    def test_the_generator_fills_the_sheet_with_the_rooms_it_would_cut(self):
        design = D.autofill(D.blank("police", 26, 20))
        self.assertTrue(design["rooms"])
        self.assertTrue(all(r["level"] == 0 for r in design["rooms"]))
        # Police rooms, not an office block: this is the point of the button.
        self.assertTrue({"policeoffice", "policelocker", "policestorage",
                         "policegunstorage", "evidenceroom", "prisoncells",
                         "interrogationroom", "armory", "lockerroom"}
                        & {r["kind"] for r in design["rooms"]})

    def test_what_it_fills_in_does_not_overlap(self):
        design = D.autofill(dict(D.blank("school", 40, 30), storeys=2))
        for level in range(design["storeys"]):
            self.assertTrue(all(n == 1 for n in covered(design, level).values()))

    def test_filling_it_in_again_gives_the_same_rooms(self):
        blank = D.blank("civic", 24, 18)
        self.assertEqual(D.autofill(blank), D.autofill(blank))

    def test_every_kind_of_building_can_be_filled_in_and_built(self):
        for entry in P.kinds():
            with self.subTest(entry["id"]):
                design = D.autofill(D.blank(entry["id"], 28, 22))
                self.assertTrue(design["rooms"], "nothing laid out")
                self.assertTrue(P.measure(D.tbx_text(design, "x")))


class ThePicture(unittest.TestCase):
    def test_a_storey_is_drawn_at_a_size_the_window_can_show(self):
        design = D.autofill(D.blank("house", 20, 16))
        img = D.preview(design)
        px = D.scale_for(design)
        self.assertEqual(img.size, (20 * px + 1, 16 * px + 1))

    def test_a_big_building_is_drawn_smaller_rather_than_enormous(self):
        self.assertLessEqual(D.scale_for(D.blank("civic", 150, 150)) * 150, 820)

    def test_a_room_is_the_same_colour_in_every_picture(self):
        self.assertEqual(D.room_colour("kitchen"), D.room_colour("kitchen"))
        self.assertNotEqual(D.room_colour("kitchen"), D.room_colour("bedroom"))

    def test_an_empty_sheet_is_still_a_picture(self):
        self.assertTrue(D.preview(D.blank("house", 10, 10)).size)

    def test_asking_for_a_storey_that_is_not_there_gives_the_top_one(self):
        design = D.autofill(D.blank("house", 16, 12))
        self.assertEqual(D.preview(design, 9).size, D.preview(design, 0).size)

    def test_an_ordinary_building_is_drawn_whole(self):
        # Waiting on the other floors costs nothing at this size, and the
        # windows are placed for the whole facade at once.
        design = D.clean(dict(D.blank("house", 20, 16), storeys=2))
        self.assertFalse(D.one_storey_at_a_time(design))

    def test_a_building_too_big_to_lay_out_after_every_drag_is_not(self):
        design = D.clean(dict(D.blank("civic", 150, 150), storeys=8))
        self.assertTrue(D.one_storey_at_a_time(design))

    def test_one_storey_on_its_own_is_the_storey_that_was_asked_for(self):
        design = D.autofill(dict(D.blank("civic", 30, 24), storeys=3))
        for level in range(3):
            with self.subTest(level=level):
                built = D.stack(design, only=level)
                self.assertEqual(len(built.storeys), 1)
                whole = D.stack(design).storeys[level]
                self.assertEqual({(r.x0, r.y0, r.kind) for r in built.storeys[0].rooms},
                                 {(r.x0, r.y0, r.kind) for r in whole.rooms})

    def test_what_is_saved_is_the_whole_building_however_it_was_drawn(self):
        design = D.clean(dict(D.blank("civic", 30, 24), storeys=3))
        design["rooms"] = [room(0, 0, 30, 24, "office", level=i) for i in range(3)]
        self.assertEqual(P.measure(D.tbx_text(design, "x"))["storeys"], 3)


class WhatItIsMadeOf(unittest.TestCase):
    def test_the_walls_and_each_room_are_listed_with_their_tiles(self):
        design = D.clean({"kind": "police", "width": 16, "height": 12,
                          "rooms": [room(0, 0, 16, 12, "policeoffice")]})
        listed = D.tiles_used(design)
        self.assertEqual([e["what"] for e in listed][:2],
                         ["Outside walls", "Inside walls"])
        self.assertTrue(listed[0]["tiles"], "no wall tiles named")
        room_entry = [e for e in listed if e["name"] == "policeoffice"][0]
        self.assertTrue(room_entry["fittings"], "no fittings listed")

    def test_the_rooms_are_tallied_biggest_first(self):
        design = D.clean({"width": 20, "height": 20,
                          "rooms": [room(0, 0, 4, 4, "closet"),
                                    room(4, 0, 16, 20, "livingroom")]})
        self.assertEqual([k for k, _n in D.tally(design)],
                         ["livingroom", "closet"])


class Keeping(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.design = D.clean(
            {"kind": "police", "width": 18, "height": 12,
             "rooms": [room(0, 0, 18, 12, "policeoffice")]})

    def test_saving_one_keeps_the_building_and_the_drawing(self):
        about = D.keep(self.dir, "Oak Street Station", self.design)
        self.assertEqual(about["kind"], "police")
        self.assertEqual(P.read(self.dir, "Oak Street Station"),
                         D.tbx_text(self.design, "Oak Street Station"))
        again = D.load_design(self.dir, "Oak Street Station")
        self.assertEqual(again["rooms"], self.design["rooms"])

    def test_a_saved_one_is_offered_as_a_preset_of_its_kind(self):
        D.keep(self.dir, "Oak Street Station", self.design)
        entry = P.library(self.dir)[0]
        self.assertEqual((entry["name"], entry["kind"]),
                         ("Oak Street Station", "police"))
        self.assertTrue(entry["editable"], "the drawing cannot be opened again")

    def test_an_empty_sheet_cannot_be_saved(self):
        with self.assertRaises(ValueError):
            D.keep(self.dir, "Nothing", D.blank("house", 10, 10))

    def test_a_preset_needs_a_name(self):
        with self.assertRaises(ValueError):
            D.keep(self.dir, "  ", self.design)

    def test_throwing_the_preset_away_throws_the_drawing_away_too(self):
        D.keep(self.dir, "Gone", self.design)
        self.assertTrue(P.remove(self.dir, "Gone"))
        self.assertIsNone(D.load_design(self.dir, "Gone"))
        self.assertEqual(P.library(self.dir), [])

    def test_a_drawing_that_is_not_there_is_nothing_rather_than_an_error(self):
        self.assertIsNone(D.load_design(self.dir, "never existed"))

    def test_a_name_that_could_escape_the_folder_cannot(self):
        path = D.design_path(self.dir, "../../etc/passwd")
        self.assertEqual(os.path.dirname(path), P.folder(self.dir))


if __name__ == "__main__":
    unittest.main()

"""What the window posts, and what is on disk beside a map, is untrusted.

Five places read it: the zones drawn on a map, the sheet a map was drawn on,
the drawn features themselves, the changes made to the real ones, and a
hand-built building. Each is a cleaner whose whole job is to leave out what it
cannot use - and each threw a Python error instead when the shape of what it
was given was wrong rather than its contents: a number where a list was
expected came back as "'int' object is not iterable", and the window showed a
500 with a traceback behind it.

A file edited by hand, a save from an older KnoxMap and a page that posts
badly are all this - and so is "NaN", which float() takes and nothing after it
does.
"""
import unittest

from generator import edits, sketch
from knoxbuild import designer, zones

# The shapes that broke them: the wrong type at every level, and the numbers
# that are not numbers.
JUNK = [None, "", "x", [], {}, True, False, 0, -1, 1.5, float("nan"),
        float("inf"), "NaN", "inf", [[1, 2]], [[1]], [[None]], [[1, 2], [3]],
        {"a": 1}, [1, 2, 3], 10 ** 12]


def every(raw_for):
    """The same junk in every slot of whatever that cleaner takes."""
    out = []
    for i, value in enumerate(JUNK):
        out.extend(raw_for(value, JUNK[(i + 7) % len(JUNK)]))
    return out


class NothingThrows(unittest.TestCase):
    def check(self, call, raws):
        for raw in raws:
            with self.subTest(raw=repr(raw)[:60]):
                call(raw)          # the test is that this does not throw

    def test_the_zones_drawn_on_a_map(self):
        self.check(zones.clean, [None, "x", 5, {}, [1], [None]] + every(
            lambda a, b: [[{"ring": a, "settings": b}],
                          [{"ring": [[0, 0], [1, 0], [1, 1]], "settings": a,
                            "style": b, "presets": a}]]))

    def test_a_hand_built_building(self):
        self.check(designer.clean, [None, "x", 5, []] + every(
            lambda a, b: [{"width": a, "height": b, "storeys": a, "kind": b,
                           "rooms": a, "name": b, "seed": a, "style": b},
                          {"rooms": [{"kind": a, "level": b, "x": a, "y": b,
                                      "w": a, "h": b}]},
                          {"rooms": [a]}]))

    def test_the_things_drawn_on_a_map(self):
        self.check(sketch.features, [None, "x", 5, {}] + every(
            lambda a, b: [{"features": a},
                          {"features": [{"properties": a, "geometry": b}]},
                          {"features": [{"properties": {"kind": "residential"},
                                         "geometry": {"type": "LineString",
                                                      "coordinates": a}}]},
                          {"features": [{"properties": {"kind": "pond"},
                                         "geometry": {"type": "Polygon",
                                                      "coordinates": a}}]},
                          {"features": [{"properties": {"kind": "tree"},
                                         "geometry": {"type": "Point",
                                                      "coordinates": a}}]}]))

    def test_the_sheet_a_map_was_drawn_on(self):
        def both(raw):
            sketch.design_to_drawn(raw)
            sketch.design_bbox(raw if isinstance(raw, dict) else {})

        self.check(both, [None, "x", 5, []] + every(
            lambda a, b: [{"width_m": a, "height_m": b, "anchor": a,
                           "features": b},
                          {"anchor": {"lat": a, "lon": b},
                           "features": [{"kind": "residential",
                                         "points": [[1, 2], [3, 4]]}]},
                          {"features": [{"kind": a, "points": b}]}]))

    def test_the_changes_made_to_the_real_features(self):
        self.check(edits.clean, [None, "x", 5, []] + every(
            lambda a, b: [{"removed": a, "moved": b, "tags": a},
                          {"moved": {"1": a}}, {"tags": {"1": a}}]))


class AndNothingIsInventedEither(unittest.TestCase):
    """Dropping what cannot be used is right; reading it as something else
    is not."""

    def test_a_string_of_digits_is_not_a_list_of_ids(self):
        # "12" is a sequence, so it was read as the two features 1 and 2 and
        # both were taken off the map.
        self.assertEqual(edits.clean({"removed": "12"})["removed"], [])

    def test_a_sheet_measured_in_nan_is_not_a_sheet(self):
        south, west, north, east = sketch.design_bbox(
            {"width_m": "NaN", "height_m": float("nan")})
        for value in (south, west, north, east):
            self.assertEqual(value, value)      # not NaN
        self.assertGreater(north, south)
        self.assertGreater(east, west)

    def test_a_point_that_is_not_a_pair_of_numbers_is_left_out(self):
        drawn = {"features": [{"properties": {"kind": "residential"},
                               "geometry": {"type": "LineString",
                                            "coordinates": [[0.0, 0.0], [None],
                                                            [1.0, 1.0]]}}]}
        made = sketch.features(drawn)
        self.assertEqual([len(f.geometry) for f in made], [2])


if __name__ == "__main__":
    unittest.main()

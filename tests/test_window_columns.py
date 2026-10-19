"""Windows upstairs stand above the windows downstairs.

A block of flats repeats its floor plan, so its windows always lined up. A
house does not: every storey is laid out room by room, the rooms upstairs are
not the rooms downstairs, and the windows went wherever each floor's rooms
wanted them. Looking up at a generated house from the street, 27 of every 100
upstairs windows stood above a window - which is what gives a town away,
because a real facade is built in columns.

Reported in #bug-reports by Zombaxx: "window alignment with the windows above
not properly lining up".
"""
import unittest

from knoxbuild.layout import build_building

# Measured over the buildings below. Houses were at 0.27 before the floor
# below's columns were taken first; the rest were already repeating their
# plan and must not get worse.
WANTED = {"house": 0.60, "apartment": 0.85, "civic": 0.85, "shop": 0.85}


def lined_up(kind, levels=3, size=(24, 18), seeds=range(8)):
    """How much of each upper floor's glazing stands above the floor below."""
    above = same = 0
    for seed in seeds:
        built = build_building(size[0], size[1], levels=levels, seed=seed,
                               kind=None if kind == "house" else kind,
                               commercial=kind not in ("house", "apartment"))
        for level in range(1, len(built.storeys)):
            under = set(built.storeys[level - 1].windows)
            here = set(built.storeys[level].windows)
            above += len(here)
            same += len(here & under)
    return same / max(1, above), above


class WindowsInColumns(unittest.TestCase):
    def test_most_of_an_upper_floor_sits_above_the_one_below(self):
        for kind, wanted in WANTED.items():
            with self.subTest(kind=kind):
                share, counted = lined_up(kind)
                self.assertGreater(counted, 20, "nothing to measure")
                self.assertGreaterEqual(
                    share, wanted,
                    f"{kind}: only {share:.0%} of upstairs windows are above one")

    def test_a_tall_house_keeps_its_columns_all_the_way_up(self):
        share, _counted = lined_up("house", levels=5)
        self.assertGreaterEqual(share, 0.60)

    def test_the_windows_are_still_there_to_line_up(self):
        # Lining them up must not be done by having fewer of them: a house of
        # this size has windows on every storey.
        built = build_building(24, 18, levels=3, seed=3)
        for level, storey in enumerate(built.storeys):
            with self.subTest(level=level):
                self.assertGreater(len(storey.windows), 4)


if __name__ == "__main__":
    unittest.main()

"""Cheap checks for the committed fixture metadata and golden-hash comparison.

The actual render + Rust compile check is `python tools/regression_maps.py`.
"""
import json
import re
import unittest

from tools.regression_maps import GOLDEN, SCENARIOS, compare, scenario_features


class RegressionMapTests(unittest.TestCase):
    def test_fixed_scenarios_have_compiled_file_hashes(self):
        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(set(expected), set(SCENARIOS))
        for name, spec in SCENARIOS.items():
            case = expected[name]
            features = scenario_features(name)
            self.assertGreaterEqual(len(features), spec["min_buildings"])
            self.assertEqual(case["seed"], spec["seed"])
            self.assertEqual(case["preset"], spec["preset"])
            self.assertGreaterEqual(case["buildings"], spec["min_buildings"])
            self.assertGreaterEqual(case["cells"], 1)
            self.assertEqual(len(case["files"]), case["cells"] * 3)
            self.assertTrue(all(re.fullmatch("[0-9a-f]{64}", digest)
                                for digest in case["files"].values()))

    def test_compiled_hash_comparison_reports_every_difference(self):
        self.assertFalse(compare({"same.lotheader": "abc"}, {"same.lotheader": "abc"}))
        mismatches = compare({"changed.lotheader": "new", "extra.lotpack": "abc"},
                             {"changed.lotheader": "old", "missing.lotpack": "xyz"})
        self.assertEqual(len(mismatches), 3)
        self.assertTrue(any("changed changed.lotheader" in m for m in mismatches))
        self.assertTrue(any("missing missing.lotpack" in m for m in mismatches))
        self.assertTrue(any("unexpected extra.lotpack" in m for m in mismatches))


if __name__ == "__main__":
    unittest.main()

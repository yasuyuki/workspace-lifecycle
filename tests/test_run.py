import json
from pathlib import Path
import tempfile
import unittest

import run


class ShardTest(unittest.TestCase):
    ids = [f"test_m.T.test_{index:02}" for index in range(23)]

    def test_shards_run_the_selection_once(self):
        timings = {test_id: float(index % 7) for index, test_id in enumerate(self.ids[:15])}
        for weights in ({}, timings):
            for count in range(1, 10):
                assigned = run.shards(self.ids, count, weights)
                self.assertEqual(len(assigned), count)
                self.assertEqual(sorted(test_id for shard in assigned for test_id in shard), sorted(self.ids))

    def test_timings_balance_shards_and_start_the_longest_first(self):
        timings = {"a": 90.0, "b": 50.0, "c": 40.0, "d": 30.0, "e": 30.0, "f": 20.0}
        assigned = run.shards(list(timings), 2, timings)
        loads = [sum(timings[test_id] for test_id in shard) for shard in assigned]
        self.assertLessEqual(max(loads) - min(loads), min(timings.values()))
        for shard in assigned:
            self.assertEqual(shard, sorted(shard, key=lambda test_id: -timings[test_id]))

    def test_an_untimed_test_starts_before_shorter_timed_tests(self):
        assigned = run.shards(["a_slow", "b_short", "c_new"], 1, {"a_slow": 5.0, "b_short": 1.0})
        self.assertEqual(assigned, [["a_slow", "c_new", "b_short"]])

    def test_malformed_timings_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timings.json"
            for content in ("[]", '{"x": "1"}', '{"x": -1}', '{"x": true}', "{"):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    run.load_timings(path)
            path.write_text(json.dumps({"x": 1.5}), encoding="utf-8")
            self.assertEqual(run.load_timings(path), {"x": 1.5})


if __name__ == "__main__":
    unittest.main()

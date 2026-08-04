from __future__ import annotations

import unittest

from scripts.measure_routing_performance import _p95_ms, _validate_samples


class RoutingPerformanceMeasurementTests(unittest.TestCase):
    def test_nearest_rank_p95_and_sample_bounds_are_deterministic(self) -> None:
        values = [value * 1_000_000 for value in range(1, 101)]
        self.assertEqual(_p95_ms(values), 95.0)
        self.assertEqual(_validate_samples(100), 100)
        self.assertEqual(_validate_samples(2_000), 2_000)
        for invalid in (True, 99, 2_001):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _validate_samples(invalid)

    def test_p95_rejects_missing_or_negative_samples(self) -> None:
        for values in ([], [1, -1]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                _p95_ms(values)


if __name__ == "__main__":
    unittest.main()

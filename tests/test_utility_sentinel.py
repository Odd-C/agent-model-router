import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler.utility import (
    DEFAULT_CONSTRAINTS,
    HardConstraints,
    quota_pressure,
    route_with_utility,
)


def _free_candidate(**overrides):
    candidate = {
        "id": "free-a",
        "provider": "bench",
        "tier": "B+",
        "capability": 0.8,
        "cost": "free",
        "quota_per_window": 100,
        "role": "free-bulk",
        "scenarios": ["text"],
    }
    candidate.update(overrides)
    return candidate


class QuotaLeftSentinelTests(unittest.TestCase):
    def test_quota_pressure_minus_one_returns_zero(self):
        self.assertEqual(quota_pressure(_free_candidate(), quota_left=-1), 0.0)

    def test_quota_pressure_zero_still_full_pressure(self):
        self.assertEqual(quota_pressure(_free_candidate(), quota_left=0), 1.0)

    def test_default_constraints_do_not_exclude_free_candidate_with_quota_left_minus_one(self):
        task = {"task_type": "text", "priority": "normal", "deadline": None}
        self.assertTrue(
            DEFAULT_CONSTRAINTS.satisfies(
                task,
                _free_candidate(),
                now=1000.0,
                quota_left=-1,
            )
        )

    def test_hard_constraints_still_exclude_when_quota_left_zero(self):
        task = {"task_type": "text", "priority": "normal", "deadline": None}
        self.assertFalse(
            HardConstraints(min_quota_left=0).satisfies(
                task,
                _free_candidate(),
                now=1000.0,
                quota_left=0,
            )
        )

    def test_route_with_utility_keeps_quota_unknown_free_candidate(self):
        task = {"task_type": "text", "priority": "normal", "deadline": None}
        result = route_with_utility(
            task,
            [_free_candidate(quota_left=-1)],
            now=1000.0,
            constraints=HardConstraints(min_quota_left=0),
        )
        self.assertEqual(result["model"], "free-a")
        self.assertGreater(result["score"], -1.0)


if __name__ == "__main__":
    unittest.main()

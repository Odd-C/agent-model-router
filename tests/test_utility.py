import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler.preferences import DEFAULT_WEIGHTS
from model_scheduler.utility import (
    DEFAULT_FAILURE_RISK,
    DEFAULT_LATENCY_PENALTY,
    UtilityScore,
    cost_penalty,
    deadline_pressure,
    failure_risk,
    latency_penalty,
    normalize_breakdowns,
    quality_fit,
    quota_pressure,
    route_with_utility,
    task_type_tier_expectation,
    utility,
)


def make_candidate(
    model="gpt-4o",
    provider="openai",
    tier="S",
    cost="paid",
    quota_per_window=None,
    peak_safe=True,
    role="stable",
    scenarios=None,
    quota_left=None,
    peak_hours=None,
):
    candidate = {
        "id": model,
        "provider": provider,
        "tier": tier,
        "cost": cost,
        "quota_per_window": quota_per_window,
        "peak_safe": peak_safe,
        "role": role,
        "fallback_chain": [],
        "scenarios": list(scenarios or []),
        "label": f"{model} ({provider})",
    }
    if quota_left is not None:
        candidate["quota_left"] = quota_left
    if peak_hours is not None:
        candidate["peak_hours"] = peak_hours
    return candidate


class FakePreferences:
    def __init__(self, weights):
        self.weights = dict(weights)

    def get_effective_weights(self):
        return dict(self.weights)


class UtilityComponentTests(unittest.TestCase):
    def test_task_type_tier_expectation(self):
        self.assertEqual(task_type_tier_expectation("coding"), ["S+", "S", "A"])
        self.assertEqual(task_type_tier_expectation("complex"), ["S+", "S", "A"])
        self.assertEqual(task_type_tier_expectation("daily"), ["A", "A-", "B+"])
        # simple -> 任意 tier。
        self.assertEqual(task_type_tier_expectation("simple"), ["S+", "S", "A", "A-", "B+", "B", "C"])
        # 未知 task_type 视为任意。
        self.assertEqual(task_type_tier_expectation("unknown"), ["S+", "S", "A", "A-", "B+", "B", "C"])

    def test_quality_fit(self):
        self.assertAlmostEqual(quality_fit("coding", make_candidate(tier="S", scenarios=["coding"])), 1.0)
        self.assertAlmostEqual(quality_fit("coding", make_candidate(tier="S")), 0.9)
        self.assertAlmostEqual(quality_fit("coding", make_candidate(tier="B+")), 0.4)
        self.assertAlmostEqual(quality_fit("daily", make_candidate(tier="B+", scenarios=["daily"])), 1.0)
        # simple 任意 tier 都给匹配分。
        self.assertAlmostEqual(quality_fit("simple", make_candidate(tier="C")), 0.9)

    def test_cost_penalty_free_paid_peak_unsafe(self):
        peak = datetime(2026, 1, 1, 10, 0)  # 默认 Asia/Shanghai 高峰
        off_peak = datetime(2026, 1, 1, 22, 0)
        free = make_candidate(cost="free", peak_safe=True)
        paid = make_candidate(cost="paid", peak_safe=True)
        unsafe = make_candidate(cost="free", peak_safe=False, peak_hours=[[9, 12]])
        self.assertEqual(cost_penalty(free, now=peak), 0.0)
        self.assertGreater(cost_penalty(paid, now=peak), cost_penalty(free, now=peak))
        self.assertGreater(cost_penalty(unsafe, now=peak), cost_penalty(unsafe, now=off_peak))
        self.assertEqual(cost_penalty(unsafe, now=off_peak), 0.0)

    def test_latency_penalty_default_and_priority_high(self):
        self.assertAlmostEqual(latency_penalty(None), DEFAULT_LATENCY_PENALTY)
        self.assertAlmostEqual(latency_penalty(None, priority="high"), DEFAULT_LATENCY_PENALTY * 1.5)
        self.assertAlmostEqual(latency_penalty({"p95": 1000.0}), 1.0)
        self.assertAlmostEqual(latency_penalty({"p95": 100.0}, priority="high"), 0.15)
        self.assertAlmostEqual(latency_penalty({"p95": 0.0}), 0.0)

    def test_failure_risk_default_and_from_health(self):
        self.assertAlmostEqual(failure_risk(None), DEFAULT_FAILURE_RISK)
        self.assertAlmostEqual(failure_risk({"failure_risk": 0.6}), 0.6)
        self.assertAlmostEqual(failure_risk({"failure_risk": None}), DEFAULT_FAILURE_RISK)

    def test_quota_pressure(self):
        paid = make_candidate(cost="paid")
        free = make_candidate(cost="free", quota_per_window=100)
        self.assertEqual(quota_pressure(paid, quota_left=0), 0.0)
        self.assertEqual(quota_pressure(free, quota_left=100), 0.0)
        self.assertEqual(quota_pressure(free, quota_left=50), 0.5)
        self.assertEqual(quota_pressure(free, quota_left=0), 1.0)
        self.assertEqual(quota_pressure(free, quota_left=150), 0.0)

    def test_deadline_pressure(self):
        self.assertEqual(deadline_pressure(None, now=1000.0), 0.0)
        self.assertEqual(deadline_pressure(1000.0, now=1000.0), 1.0)
        self.assertEqual(deadline_pressure(900.0, now=1000.0), 1.0)
        self.assertEqual(deadline_pressure(2800.0, now=1000.0), 0.5)
        self.assertEqual(deadline_pressure(4600.0, now=1000.0), 0.0)


class UtilityScoreTests(unittest.TestCase):
    def test_utility_score_breakdown_and_formula(self):
        candidate = make_candidate(
            model="gemini-2.0-flash",
            provider="google",
            tier="B+",
            cost="free",
            quota_per_window=1500,
            peak_safe=True,
            scenarios=["simple"],
            quota_left=750,
        )
        weights = {
            "quality_fit": 1.0,
            "cost_penalty": 1.0,
            "latency_penalty": 1.0,
            "failure_risk": 1.0,
            "quota_pressure": 1.0,
            "deadline_pressure": 1.0,
        }
        score = utility(
            {"task_type": "simple", "priority": "normal", "deadline": None},
            candidate,
            1000.0,
            weights,
        )
        self.assertIsInstance(score, UtilityScore)
        self.assertIsInstance(score.score, float)
        self.assertIn("quality_fit", score.breakdown)
        self.assertIn("weighted", score.breakdown)
        self.assertTrue(score.why)
        expected = (
            score.breakdown["quality_fit"]
            - score.breakdown["cost_penalty"]
            - score.breakdown["latency_penalty"]
            - score.breakdown["failure_risk"]
            - score.breakdown["quota_pressure"]
            + score.breakdown["deadline_pressure"]
        )
        self.assertAlmostEqual(score.score, expected)

    def test_weight_influence_cost_first_cheap_quality_first_strong(self):
        cheap = make_candidate(
            model="gemini-2.0-flash",
            provider="google",
            tier="B+",
            cost="free",
            quota_per_window=1500,
            quota_left=1500,
            scenarios=["simple"],
        )
        strong = make_candidate(
            model="gpt-4o",
            provider="openai",
            tier="S",
            cost="paid",
            quota_per_window=None,
            scenarios=["complex"],
        )
        task = {"task_type": "coding", "priority": "normal", "deadline": None}

        cost_weights = {
            "quality_fit": 1.0,
            "cost_penalty": 3.0,
            "latency_penalty": 1.0,
            "failure_risk": 1.0,
            "quota_pressure": 1.0,
            "deadline_pressure": 1.0,
        }
        result = route_with_utility(task, [strong, cheap], preferences=FakePreferences(cost_weights))
        self.assertEqual(result["model"], "gemini-2.0-flash")

        quality_weights = {
            "quality_fit": 3.0,
            "cost_penalty": 1.0,
            "latency_penalty": 1.0,
            "failure_risk": 1.0,
            "quota_pressure": 1.0,
            "deadline_pressure": 1.0,
        }
        result = route_with_utility(task, [strong, cheap], preferences=FakePreferences(quality_weights))
        self.assertEqual(result["model"], "gpt-4o")

    def test_tie_break_preserves_candidate_order(self):
        first = make_candidate(
            model="gemini-2.0-flash",
            provider="google",
            tier="B+",
            cost="free",
            quota_per_window=100,
            quota_left=50,
            scenarios=["simple"],
        )
        second = make_candidate(
            model="deepseek-chat",
            provider="deepseek",
            tier="B+",
            cost="free",
            quota_per_window=100,
            quota_left=50,
            scenarios=["simple"],
        )
        task = {"task_type": "simple", "priority": "normal", "deadline": None}
        result = route_with_utility(task, [first, second], preferences=FakePreferences({
            "quality_fit": 1.0,
            "cost_penalty": 1.0,
            "latency_penalty": 1.0,
            "failure_risk": 1.0,
            "quota_pressure": 1.0,
            "deadline_pressure": 1.0,
        }))
        self.assertEqual(result["model"], "gemini-2.0-flash")
        self.assertEqual(result["provider"], "google")

    def test_route_with_utility_returns_explainable_result(self):
        candidate = make_candidate(
            model="claude-3-5-sonnet",
            provider="anthropic",
            tier="S+",
            cost="free",
            quota_per_window=500,
            quota_left=500,
            scenarios=["complex", "coding"],
        )
        result = route_with_utility(
            {"task_type": "coding", "priority": "normal", "deadline": None},
            [candidate],
            preferences=FakePreferences({
                "quality_fit": 1.0,
                "cost_penalty": 1.0,
                "latency_penalty": 1.0,
                "failure_risk": 1.0,
                "quota_pressure": 1.0,
                "deadline_pressure": 1.0,
            }),
        )
        self.assertEqual(result["model"], "claude-3-5-sonnet")
        self.assertIn("score", result)
        self.assertIn("breakdown", result)
        self.assertIn("why", result)
        self.assertIn("reason", result)
        self.assertGreater(result["score"], 0)

    def test_deadline_pressure_boosts_score(self):
        candidate = make_candidate(
            model="gpt-4o-mini",
            provider="openai",
            tier="A",
            cost="paid",
            quota_per_window=None,
            scenarios=["simple"],
        )
        weights = {
            "quality_fit": 1.0,
            "cost_penalty": 1.0,
            "latency_penalty": 1.0,
            "failure_risk": 1.0,
            "quota_pressure": 1.0,
            "deadline_pressure": 1.0,
        }
        no_deadline = utility({"task_type": "simple", "priority": "normal", "deadline": None}, candidate, 1000.0, weights)
        near_deadline = utility({"task_type": "simple", "priority": "normal", "deadline": 2800.0}, candidate, 1000.0, weights)
        self.assertGreater(near_deadline.score, no_deadline.score)
        self.assertAlmostEqual(near_deadline.score - no_deadline.score, 0.5)


class NormalizationTests(unittest.TestCase):
    def test_normalize_breakdowns_strong_weak_quality_and_cost(self):
        strong_raw = {
            "quality_fit": 0.9,
            "cost_penalty": 0.6,
            "latency_penalty": 0.3,
            "failure_risk": 0.2,
            "quota_pressure": 0.0,
            "deadline_pressure": 0.0,
        }
        weak_raw = {
            "quality_fit": 0.4,
            "cost_penalty": 0.0,
            "latency_penalty": 0.3,
            "failure_risk": 0.2,
            "quota_pressure": 0.0,
            "deadline_pressure": 0.0,
        }
        normalized = normalize_breakdowns([strong_raw, weak_raw])
        # 质量强者在该维得 1.0，弱者 0.0。
        self.assertAlmostEqual(normalized[0]["normalized"]["quality_fit"], 1.0)
        self.assertAlmostEqual(normalized[1]["normalized"]["quality_fit"], 0.0)
        # 成本便宜（free, raw=0.0）在该维得 1.0。
        self.assertAlmostEqual(normalized[1]["normalized"]["cost_penalty"], 1.0)
        self.assertAlmostEqual(normalized[0]["normalized"]["cost_penalty"], 0.0)
        # max==min 的维度归一化为 1.0。
        self.assertAlmostEqual(normalized[0]["normalized"]["latency_penalty"], 1.0)
        self.assertAlmostEqual(normalized[1]["normalized"]["latency_penalty"], 1.0)

    def test_normalize_breakdowns_max_equals_min_sets_one(self):
        raw_a = {
            "quality_fit": 0.5,
            "cost_penalty": 0.2,
            "latency_penalty": 0.3,
            "failure_risk": 0.2,
            "quota_pressure": 0.0,
            "deadline_pressure": 0.0,
        }
        raw_b = {
            "quality_fit": 0.5,
            "cost_penalty": 0.8,
            "latency_penalty": 0.3,
            "failure_risk": 0.2,
            "quota_pressure": 0.0,
            "deadline_pressure": 0.0,
        }
        normalized = normalize_breakdowns([raw_a, raw_b])
        self.assertAlmostEqual(normalized[0]["normalized"]["quality_fit"], 1.0)
        self.assertAlmostEqual(normalized[1]["normalized"]["quality_fit"], 1.0)
        self.assertAlmostEqual(normalized[0]["normalized"]["cost_penalty"], 1.0)
        self.assertAlmostEqual(normalized[1]["normalized"]["cost_penalty"], 0.0)

    def test_normalize_breakdowns_single_candidate_all_norm_one(self):
        raw = {
            "quality_fit": 0.4,
            "cost_penalty": 0.0,
            "latency_penalty": 0.3,
            "failure_risk": 0.2,
            "quota_pressure": 0.0,
            "deadline_pressure": 0.0,
        }
        normalized = normalize_breakdowns([raw])
        self.assertEqual(len(normalized), 1)
        for key in (
            "quality_fit",
            "cost_penalty",
            "latency_penalty",
            "failure_risk",
            "quota_pressure",
            "deadline_pressure",
        ):
            self.assertAlmostEqual(normalized[0]["normalized"][key], 1.0)

    def test_balanced_coding_route_selects_gpt4o(self):
        # 回归关键用例：归一化后质量相对优势 > 成本相对劣势，balanced 选 gpt-4o。
        gpt4o = make_candidate(
            model="gpt-4o",
            provider="openai",
            tier="S",
            cost="paid",
            scenarios=["complex"],
        )
        deepseek = make_candidate(
            model="deepseek-chat",
            provider="deepseek",
            tier="A-",
            cost="free",
            quota_per_window=500,
            quota_left=500,
            scenarios=["simple", "daily"],
        )
        task = {"task_type": "coding", "priority": "normal", "deadline": None}
        result = route_with_utility(
            task,
            [gpt4o, deepseek],
            preferences=FakePreferences(dict(DEFAULT_WEIGHTS["balanced"])),
        )
        self.assertEqual(result["model"], "gpt-4o")
        self.assertEqual(result["provider"], "openai")
        self.assertIn("normalized", result["breakdown"])
        self.assertAlmostEqual(result["breakdown"]["normalized"]["quality_fit"], 1.0)
        # 回归点：成本便宜（free）的候选在 cost_penalty 维归一化为 1.0。
        self.assertAlmostEqual(result["breakdown"]["normalized"]["cost_penalty"], 0.0)


if __name__ == "__main__":
    unittest.main()

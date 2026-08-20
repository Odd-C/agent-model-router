import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler import policy, quota
from model_scheduler.utility import (
    DEFAULT_CONSTRAINTS,
    HardConstraints,
    route_with_utility,
)


def make_candidate(
    model="gpt-4o",
    provider="openai",
    tier="S",
    cost="paid",
    quota_per_window=None,
    quota_left=None,
    peak_safe=True,
    role="stable",
    scenarios=None,
    capability=None,
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
    if capability is not None:
        candidate["capability"] = capability
    return candidate


class HardConstraintsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        policy.configure_state_dir(self.tmp.name)

    def tearDown(self):
        policy.configure_state_dir(None)
        self.tmp.cleanup()

    def test_cost_max_free_excludes_paid(self):
        paid = make_candidate(cost="paid")
        free = make_candidate(
            model="deepseek-chat",
            provider="deepseek",
            tier="A-",
            cost="free",
            quota_per_window=500,
            quota_left=500,
        )
        constraints = HardConstraints(cost_max="free")
        task = {"task_type": "daily", "priority": "normal", "deadline": None}
        self.assertFalse(constraints.satisfies(task, paid, now=1000.0))
        self.assertTrue(constraints.satisfies(task, free, now=1000.0))

    def test_min_quota_left_excludes_low_quota(self):
        free = make_candidate(
            model="deepseek-chat",
            provider="deepseek",
            tier="A-",
            cost="free",
            quota_per_window=100,
            quota_left=5,
        )
        task = {"task_type": "daily", "priority": "normal", "deadline": None}
        self.assertFalse(
            HardConstraints(min_quota_left=10).satisfies(task, free, now=1000.0)
        )
        self.assertTrue(
            HardConstraints(min_quota_left=0).satisfies(task, free, now=1000.0)
        )
        exhausted = make_candidate(
            model="deepseek-chat",
            provider="deepseek",
            tier="A-",
            cost="free",
            quota_per_window=100,
            quota_left=0,
        )
        self.assertFalse(
            DEFAULT_CONSTRAINTS.satisfies(task, exhausted, now=1000.0)
        )

    def test_cooldown_excludes(self):
        quota.record_failure(
            "coolmodel",
            "provider",
            ts=1000.0,
            reason="rate_limit",
            status=429,
        )
        cool = make_candidate(
            model="coolmodel",
            provider="provider",
            tier="A-",
            cost="free",
            quota_per_window=100,
            quota_left=50,
        )
        task = {"task_type": "daily", "priority": "normal", "deadline": None}
        self.assertFalse(DEFAULT_CONSTRAINTS.satisfies(task, cool, now=1000.0))
        self.assertTrue(
            HardConstraints(exclude_in_cooldown=False).satisfies(
                task, cool, now=1000.0
            )
        )

    def test_max_failure_risk_excludes_high_failure(self):
        candidate = make_candidate(cost="paid")
        task = {"task_type": "daily", "priority": "normal", "deadline": None}
        self.assertFalse(
            HardConstraints(max_failure_risk=0.5).satisfies(
                task,
                candidate,
                now=1000.0,
                health_score={"failure_risk": 0.8},
            )
        )
        self.assertTrue(
            HardConstraints(max_failure_risk=None).satisfies(
                task,
                candidate,
                now=1000.0,
                health_score={"failure_risk": 0.8},
            )
        )

    def test_deadline_infeasible_excluded(self):
        candidate = make_candidate(cost="paid")
        task = {"task_type": "daily", "priority": "normal", "deadline": 1050.0}
        self.assertFalse(
            HardConstraints(deadline_slack_seconds=60).satisfies(
                task, candidate, now=1000.0
            )
        )
        self.assertTrue(
            HardConstraints(deadline_slack_seconds=0).satisfies(
                task, candidate, now=1000.0
            )
        )
        self.assertFalse(
            HardConstraints(deadline_slack_seconds=0).satisfies(
                task, candidate, now=1051.0
            )
        )

    def test_default_constraints_all_pass(self):
        candidate = make_candidate(
            model="deepseek-chat",
            provider="deepseek",
            tier="A-",
            cost="free",
            quota_per_window=100,
            quota_left=50,
        )
        task = {"task_type": "daily", "priority": "normal", "deadline": 2000.0}
        self.assertTrue(DEFAULT_CONSTRAINTS.satisfies(task, candidate, now=1000.0))

    def test_route_with_utility_applies_constraints_before_scoring(self):
        paid = make_candidate(model="gpt-4o", provider="openai", tier="S", cost="paid")
        free = make_candidate(
            model="deepseek-chat",
            provider="deepseek",
            tier="A-",
            cost="free",
            quota_per_window=500,
            quota_left=500,
        )
        task = {"task_type": "coding", "priority": "normal", "deadline": None}
        result = route_with_utility(
            task,
            [paid, free],
            preferences={
                "quality_fit": 3.0,
                "cost_penalty": 1.0,
                "latency_penalty": 1.0,
                "failure_risk": 1.0,
                "quota_pressure": 1.0,
                "deadline_pressure": 1.0,
            },
            constraints=HardConstraints(cost_max="free"),
        )
        self.assertEqual(result["model"], "deepseek-chat")
        self.assertEqual(result["provider"], "deepseek")


    def test_max_latency_ms_excludes_high_p95(self):
        candidate = make_candidate(cost="paid")
        task = {"task_type": "daily", "priority": "normal", "deadline": None}
        constraints = HardConstraints(max_latency_ms=3000)
        self.assertFalse(
            constraints.satisfies(
                task, candidate, now=1000.0, health_score={"p95": 5000}
            )
        )
        self.assertTrue(
            constraints.satisfies(
                task, candidate, now=1000.0, health_score={"p95": 2500}
            )
        )
        # 无档案 p95=None 视为通过，不误杀。
        self.assertTrue(
            constraints.satisfies(
                task, candidate, now=1000.0, health_score={"p95": None}
            )
        )
        self.assertTrue(
            constraints.satisfies(task, candidate, now=1000.0, health_score=None)
        )

    def test_min_quality_tier_keeps_strong_and_excludes_weak(self):
        strong = make_candidate(model="gpt-4o", provider="openai", tier="S", cost="paid")
        weak = make_candidate(
            model="gemini-2.0-flash",
            provider="google",
            tier="B+",
            cost="free",
            quota_per_window=1500,
            quota_left=1500,
        )
        unknown = make_candidate(model="mystery", provider="acme", tier="", cost="paid")
        task = {"task_type": "daily", "priority": "normal", "deadline": None}
        constraints = HardConstraints(min_quality_tier="A")
        self.assertTrue(constraints.satisfies(task, strong, now=1000.0))
        self.assertFalse(constraints.satisfies(task, weak, now=1000.0))
        self.assertFalse(constraints.satisfies(task, unknown, now=1000.0))

    def test_min_capability_pct_with_reference(self):
        low = make_candidate(model="deepseek-chat", provider="deepseek", capability=0.6)
        high = make_candidate(model="gpt-4o", provider="openai", capability=0.9)
        task = {"task_type": "daily", "priority": "normal", "deadline": None}
        constraints = HardConstraints(
            min_capability_pct=80,
            capability_reference="gpt-4o@openai",
        )
        self.assertFalse(constraints.satisfies(task, low, now=1000.0))
        self.assertTrue(constraints.satisfies(task, high, now=1000.0))
        # 0-1 float 写法等价于 80%。
        constraints_float = HardConstraints(
            min_capability_pct=0.8,
            capability_reference="gpt-4o@openai",
        )
        self.assertFalse(constraints_float.satisfies(task, low, now=1000.0))
        self.assertTrue(constraints_float.satisfies(task, high, now=1000.0))

    def test_cost_max_and_max_latency_combined(self):
        paid = make_candidate(model="gpt-4o", provider="openai", tier="S", cost="paid")
        free_fast = make_candidate(
            model="deepseek-chat",
            provider="deepseek",
            tier="A-",
            cost="free",
            quota_per_window=500,
            quota_left=500,
        )
        free_slow = make_candidate(
            model="gemini-2.0-flash",
            provider="google",
            tier="B+",
            cost="free",
            quota_per_window=1500,
            quota_left=1500,
        )
        task = {"task_type": "daily", "priority": "normal", "deadline": None}
        constraints = HardConstraints(cost_max="free", max_latency_ms=3000)
        # paid 直接被成本上限排除；free 慢模型被延迟上限排除。
        self.assertFalse(
            constraints.satisfies(task, paid, now=1000.0, health_score={"p95": 1000})
        )
        self.assertFalse(
            constraints.satisfies(task, free_slow, now=1000.0, health_score={"p95": 5000})
        )
        self.assertTrue(
            constraints.satisfies(task, free_fast, now=1000.0, health_score={"p95": 1000})
        )


if __name__ == "__main__":
    unittest.main()

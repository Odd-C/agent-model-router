import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler.policy_compiler import (
    CAPABILITY_REFERENCES,
    DEFAULT_MAX_LATENCY_MS,
    CompiledPolicy,
    compile_intent,
    describe,
    merge_policies,
    route_with_intent,
)
from model_scheduler.preferences import DEFAULT_WEIGHTS
from model_scheduler.utility import DEFAULT_CONSTRAINTS, HardConstraints


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


class ChineseIntentTests(unittest.TestCase):
    def test_speed_intent(self):
        compiled = compile_intent("尽快出结果")
        self.assertEqual(compiled.mode, "latency-first")
        self.assertEqual(compiled.constraints.max_latency_ms, DEFAULT_MAX_LATENCY_MS)
        self.assertEqual(compiled.weights, DEFAULT_WEIGHTS["latency-first"])
        self.assertIn("latency-first", compiled.explanation)

    def test_speed_intent_with_numeric_latency(self):
        compiled = compile_intent("3 秒内出结果")
        self.assertEqual(compiled.mode, "latency-first")
        self.assertEqual(compiled.constraints.max_latency_ms, 3000.0)

    def test_cost_intent(self):
        compiled = compile_intent("要便宜的")
        self.assertEqual(compiled.mode, "cost-first")
        self.assertEqual(compiled.constraints.cost_max, "free")
        self.assertEqual(compiled.weights, DEFAULT_WEIGHTS["cost-first"])

    def test_quality_intent(self):
        compiled = compile_intent("要高质量的")
        self.assertEqual(compiled.mode, "quality-first")
        self.assertEqual(compiled.constraints.min_quality_tier, "A")
        self.assertEqual(compiled.weights, DEFAULT_WEIGHTS["quality-first"])

    def test_balanced_intent(self):
        compiled = compile_intent("都行")
        self.assertEqual(compiled.mode, "balanced")
        self.assertEqual(compiled.constraints, DEFAULT_CONSTRAINTS)
        self.assertEqual(compiled.weights, DEFAULT_WEIGHTS["balanced"])


class EnglishIntentTests(unittest.TestCase):
    def test_speed_intent(self):
        compiled = compile_intent("I need fast results")
        self.assertEqual(compiled.mode, "latency-first")
        self.assertEqual(compiled.constraints.max_latency_ms, DEFAULT_MAX_LATENCY_MS)

    def test_cost_intent(self):
        compiled = compile_intent("make it cheap")
        self.assertEqual(compiled.mode, "cost-first")
        self.assertEqual(compiled.constraints.cost_max, "free")

    def test_quality_intent(self):
        compiled = compile_intent("best quality please")
        self.assertEqual(compiled.mode, "quality-first")
        self.assertEqual(compiled.constraints.min_quality_tier, "A")

    def test_balanced_intent(self):
        compiled = compile_intent("balanced")
        self.assertEqual(compiled.mode, "balanced")
        self.assertEqual(compiled.constraints, DEFAULT_CONSTRAINTS)


class CompoundIntentTests(unittest.TestCase):
    def test_quality_plus_cost(self):
        compiled = compile_intent("高质量但不要太贵")
        self.assertEqual(compiled.constraints.cost_max, "free")
        self.assertEqual(compiled.constraints.min_quality_tier, "A")
        # 质量优先级高于成本。
        self.assertEqual(compiled.mode, "quality-first")
        self.assertEqual(compiled.weights, DEFAULT_WEIGHTS["quality-first"])

    def test_capability_percentage_chinese(self):
        compiled = compile_intent("达到 gpt-4o 的 80%")
        self.assertEqual(compiled.constraints.min_capability_pct, 80.0)
        self.assertEqual(compiled.constraints.capability_reference, "gpt-4o@openai")
        self.assertEqual(compiled.mode, "quality-first")

    def test_capability_percentage_english(self):
        compiled = compile_intent("80% of gpt-4o")
        self.assertEqual(compiled.constraints.min_capability_pct, 80.0)
        self.assertEqual(compiled.constraints.capability_reference, "gpt-4o@openai")

    def test_no_match_falls_back_to_balanced(self):
        compiled = compile_intent("随便来一个")
        self.assertEqual(compiled.mode, "balanced")
        self.assertEqual(compiled.constraints, DEFAULT_CONSTRAINTS)
        self.assertEqual(compiled.weights, DEFAULT_WEIGHTS["balanced"])
        self.assertIn("未识别到特定意图", compiled.explanation)


class MergePolicyTests(unittest.TestCase):
    def test_merge_quality_and_cost_constraints(self):
        quality = compile_intent("要高质量的")
        cost = compile_intent("要便宜的")
        merged = merge_policies(quality, cost)

        self.assertIsInstance(merged, CompiledPolicy)
        self.assertEqual(merged.mode, "quality-first")
        self.assertEqual(merged.constraints.min_quality_tier, "A")
        self.assertEqual(merged.constraints.cost_max, "free")
        self.assertEqual(merged.weights, DEFAULT_WEIGHTS["quality-first"])

    def test_merge_keeps_stricter_latency(self):
        fast = compile_intent("尽快")
        faster = compile_intent("500ms 内")
        merged = merge_policies(fast, faster)
        self.assertEqual(merged.mode, "latency-first")
        self.assertEqual(merged.constraints.max_latency_ms, 500.0)

    def test_merge_requires_compiled_policies(self):
        with self.assertRaises(TypeError):
            merge_policies("not-a-policy", compile_intent("都行"))


class DescribeTests(unittest.TestCase):
    def test_describe_contains_key_information(self):
        compiled = compile_intent("要高质量的")
        text = describe(compiled)
        self.assertIn("quality-first", text)
        self.assertIn("A", text)
        self.assertIn("至少 A 档", text)

    def test_describe_rejects_non_compiled_policy(self):
        with self.assertRaises(TypeError):
            describe({"mode": "balanced"})


class RouteWithIntentTests(unittest.TestCase):
    def test_route_with_intent_uses_compiled_constraints(self):
        paid = make_candidate(model="gpt-4o", provider="openai", tier="S", cost="paid")
        free = make_candidate(
            model="deepseek-chat",
            provider="deepseek",
            tier="A-",
            cost="free",
            quota_per_window=500,
            quota_left=500,
        )
        task = {"task_type": "daily", "priority": "normal", "deadline": None}
        result = route_with_intent(task, [paid, free], "要便宜的")
        self.assertEqual(result["model"], "deepseek-chat")
        self.assertEqual(result["provider"], "deepseek")


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_router.router import (
    assess_difficulty,
    assess_urgency,
    format_model_key,
    parse_model_key,
    recommend_for_session,
    route_model,
)


class DifficultyScoringTests(unittest.TestCase):
    def test_empty_text_scores_zero(self):
        self.assertEqual(assess_difficulty(""), 0)

    def test_code_block_scores_two(self):
        self.assertEqual(assess_difficulty("```\nprint(1)\n```"), 2)

    def test_error_word_scores_two(self):
        self.assertEqual(assess_difficulty("程序 error 了"), 2)

    def test_source_reference_scores_one(self):
        self.assertEqual(assess_difficulty("看这个 .py 文件的 import 语句"), 1)

    def test_strong_intent_scores_four(self):
        self.assertEqual(assess_difficulty("帮我写一个 Python 脚本"), 4)

    def test_weak_intent_only_scores_one(self):
        self.assertEqual(assess_difficulty("这个项目的进度如何"), 1)

    def test_long_text_scores_length_bonus(self):
        self.assertEqual(assess_difficulty("a" * 2001), 1)
        self.assertEqual(assess_difficulty("a" * 8001), 2)

    def test_score_clamps_to_five(self):
        text = "```\nerror 报错\n" + "源码 函数 class def import 接口 .py .js .ts\n" + "帮我写一个 Python 脚本\n" + "a" * 9000
        self.assertEqual(assess_difficulty(text), 5)


class UrgencyTests(unittest.TestCase):
    def test_urgent_words(self):
        self.assertTrue(assess_urgency("紧急！"))
        self.assertTrue(assess_urgency("ASAP"))
        self.assertTrue(assess_urgency("尽快处理"))

    def test_non_urgent_text(self):
        self.assertFalse(assess_urgency("普通问题"))


class KeyCodecTests(unittest.TestCase):
    def test_format_model_key(self):
        self.assertEqual(format_model_key("gpt-4o-mini", "openai"), "gpt-4o-mini@openai")
        self.assertEqual(format_model_key("my-model", ""), "my-model")
        self.assertEqual(format_model_key("", ""), "")

    def test_parse_model_key(self):
        self.assertEqual(parse_model_key("gpt-4o-mini@openai"), ("gpt-4o-mini", "openai"))
        self.assertEqual(parse_model_key("my-model"), ("my-model", ""))
        self.assertEqual(parse_model_key(""), ("", ""))


class RouteDecisionTests(unittest.TestCase):
    def test_urgent_branch(self):
        result = route_model(0, urgent=True, quota_snapshot={})
        self.assertEqual(result["model"], "gpt-4o")
        self.assertEqual(result["provider"], "openai")
        self.assertIn("紧急", result["reason"])

    def test_difficulty_ge4_prefers_free_flagship_when_available(self):
        result = route_model(4, urgent=False, quota_snapshot={"claude-3-5-sonnet@anthropic": 10})
        self.assertEqual(result["model"], "claude-3-5-sonnet")
        self.assertEqual(result["provider"], "anthropic")
        self.assertEqual(result["tier"], "S+")
        self.assertEqual(result["cost"], "free")

    def test_difficulty_ge4_falls_back_to_paid_when_flagship_exhausted(self):
        now = datetime(2026, 1, 1, 10, 0)  # 高峰
        result = route_model(
            4,
            urgent=False,
            now=now,
            quota_snapshot={"claude-3-5-sonnet@anthropic": 0},
        )
        self.assertEqual(result["model"], "gpt-4o-mini")
        self.assertEqual(result["provider"], "openai")
        self.assertIn("高峰翻倍", result["reason"])

    def test_difficulty_2_3_prefers_free_bulk_first(self):
        result = route_model(2, urgent=False, quota_snapshot={"gemini-2.0-flash@google": 100})
        self.assertEqual(result["model"], "gemini-2.0-flash")
        self.assertEqual(result["provider"], "google")
        self.assertEqual(result["cost"], "free")

    def test_difficulty_2_3_falls_back_to_paid_when_all_free_exhausted(self):
        now = datetime(2026, 1, 1, 1, 0)  # 谷值
        result = route_model(
            2,
            urgent=False,
            now=now,
            quota_snapshot={
                "gemini-2.0-flash@google": 0,
                "deepseek-chat@deepseek": 0,
                "claude-3-5-sonnet@anthropic": 0,
            },
        )
        self.assertEqual(result["model"], "gpt-4o-mini")
        self.assertEqual(result["provider"], "openai")
        self.assertIn("谷值", result["reason"])

    def test_difficulty_0_1_prefers_free_bulk(self):
        result = route_model(0, urgent=False, quota_snapshot={"gemini-2.0-flash@google": 100})
        self.assertEqual(result["model"], "gemini-2.0-flash")

    def test_difficulty_0_1_falls_back_to_paid(self):
        result = route_model(
            1,
            urgent=False,
            quota_snapshot={
                "gemini-2.0-flash@google": 0,
                "deepseek-chat@deepseek": 0,
            },
        )
        self.assertEqual(result["model"], "gpt-4o-mini")
        self.assertEqual(result["provider"], "openai")

    def test_quota_snapshot_bare_model_id_is_supported(self):
        # 只传裸 model id（不含 provider）也能命中额度。
        result = route_model(2, urgent=False, quota_snapshot={"gemini-2.0-flash": 0, "deepseek-chat": 0, "claude-3-5-sonnet": 0})
        self.assertEqual(result["model"], "gpt-4o-mini")

    def test_recommend_for_session(self):
        rec = recommend_for_session(
            "帮我写一个 Python 脚本",
            message_count=2,
            now=datetime(2026, 1, 1, 10, 0),
            quota_snapshot={"claude-3-5-sonnet@anthropic": 10},
        )
        self.assertEqual(rec["difficulty"], 4)
        self.assertFalse(rec["urgent"])
        self.assertEqual(rec["model"], "claude-3-5-sonnet")
        self.assertEqual(rec["key"], "claude-3-5-sonnet@anthropic")
        self.assertTrue(rec["peak"])

    def test_paid_fallback_uses_target_model_peak_hours(self):
        # 全局默认 9-12 高峰，但 gpt-4o-mini 配了 [[22, 23]] 峰谷：
        # 上午 10 点回退到 gpt-4o-mini 时应判为「谷值」（目标模型自己不是高峰）
        import json
        import tempfile
        from pathlib import Path

        from llm_router.policy import ModelPolicy

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "model-policy.json").write_text(
                json.dumps({
                    "models": {
                        "gpt-4o-mini@openai": {"peak_hours": [[22, 23]]},
                    }
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            store = ModelPolicy(state)
            result = route_model(
                2,
                urgent=False,
                now=datetime(2026, 1, 1, 10, 0),
                quota_snapshot={
                    "gemini-2.0-flash@google": 0,
                    "deepseek-chat@deepseek": 0,
                    "claude-3-5-sonnet@anthropic": 0,
                },
                policy_store=store,
            )
            self.assertEqual(result["model"], "gpt-4o-mini")
            self.assertIn("谷值", result["reason"])


if __name__ == "__main__":
    unittest.main()

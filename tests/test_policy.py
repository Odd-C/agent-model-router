import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_model_router.policy import (
    DEFAULT_MODEL_POLICIES,
    ModelPolicy,
    atomic_write_json,
    default_state_dir,
    is_peak_hour,
)


class ModelPolicyTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp_dir.name)
        self.store = ModelPolicy(self.state_dir)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_default_policy_has_five_models(self):
        models = self.store.get_policy()["models"]
        self.assertEqual(len(models), 5)
        self.assertIn("gpt-4o@openai", models)
        self.assertIn("gpt-4o-mini@openai", models)
        self.assertIn("deepseek-chat@deepseek", models)
        self.assertIn("gemini-2.0-flash@google", models)
        self.assertIn("claude-3-5-sonnet@anthropic", models)

    def test_json_override_merges_by_unique_key(self):
        override = {
            "models": {
                "deepseek-chat@deepseek": {"quota_per_window": 888},
                "my-model@acme": {
                    "id": "my-model",
                    "provider": "acme",
                    "tier": "A",
                    "cost": "free",
                    "quota_per_window": 100,
                    "peak_safe": True,
                    "fallback_chain": ["gpt-4o@openai"],
                    "scenarios": ["simple"],
                    "label": "My Model",
                },
            }
        }
        (self.state_dir / "model-policy.json").write_text(
            json.dumps(override, ensure_ascii=False),
            encoding="utf-8",
        )

        models = self.store.get_policy()["models"]

        self.assertEqual(models["deepseek-chat@deepseek"]["quota_per_window"], 888)
        self.assertEqual(models["deepseek-chat@deepseek"]["cost"], "free")
        self.assertIn("my-model@acme", models)
        self.assertEqual(models["my-model@acme"]["tier"], "A")
        self.assertEqual(models["my-model@acme"]["quota_per_window"], 100)

    def test_list_models_contains_unique_keys(self):
        keys = {item["key"] for item in self.store.list_models()}
        self.assertIn("gpt-4o@openai", keys)
        self.assertIn("gpt-4o-mini@openai", keys)
        self.assertIn("claude-3-5-sonnet@anthropic", keys)

    def test_resolve_model_by_id_and_provider(self):
        entry = self.store.resolve_model("deepseek-chat", "deepseek")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["cost"], "free")

        entry = self.store.resolve_model("gpt-4o@openai")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["cost"], "paid")

        self.assertIsNone(self.store.resolve_model("no-such-model", "no-such-provider"))

    def test_find_by_role(self):
        free_bulk = self.store.find_by_role("free-bulk", cost="free")
        self.assertEqual(len(free_bulk), 1)
        self.assertEqual(free_bulk[0]["id"], "gemini-2.0-flash")

        stable = self.store.find_by_role("stable")
        self.assertEqual(len(stable), 1)
        self.assertEqual(stable[0]["id"], "gpt-4o")
        self.assertEqual(stable[0]["cost"], "paid")

        # 无匹配 role 返回空
        self.assertEqual(self.store.find_by_role("no-such-role"), [])

    def test_get_quota_table_only_contains_free_models(self):
        table = self.store.get_quota_table()
        self.assertEqual(table["gemini-2.0-flash@google"], 1500)
        self.assertEqual(table["claude-3-5-sonnet@anthropic"], 500)
        self.assertNotIn("gpt-4o@openai", table)

    def test_peak_hour_boundaries(self):
        cases = [
            (datetime(2026, 1, 1, 9, 0), True),
            (datetime(2026, 1, 1, 12, 0), True),
            (datetime(2026, 1, 1, 14, 0), True),
            (datetime(2026, 1, 1, 18, 0), True),
            (datetime(2026, 1, 1, 8, 59), False),
            (datetime(2026, 1, 1, 12, 1), False),
            (datetime(2026, 1, 1, 13, 59), False),
            (datetime(2026, 1, 1, 18, 1), False),
        ]
        for dt, expected in cases:
            with self.subTest(dt=dt):
                self.assertEqual(is_peak_hour(dt), expected)

    def test_peak_hour_aware_utc_is_converted_to_shanghai(self):
        self.assertTrue(is_peak_hour(datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)))   # 09:00
        self.assertTrue(is_peak_hour(datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)))   # 12:00
        self.assertFalse(is_peak_hour(datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc)))  # 13:00

    def test_peak_hours_custom_periods(self):
        # 自定义时段：8-10 高峰
        custom = [[8, 10]]
        self.assertTrue(is_peak_hour(datetime(2026, 1, 1, 8, 0), peak_hours=custom))
        self.assertTrue(is_peak_hour(datetime(2026, 1, 1, 9, 30), peak_hours=custom))
        self.assertTrue(is_peak_hour(datetime(2026, 1, 1, 10, 0), peak_hours=custom))
        self.assertFalse(is_peak_hour(datetime(2026, 1, 1, 11, 0), peak_hours=custom))
        # 多段自定义
        multi = [[8, 10], [20, 22]]
        self.assertTrue(is_peak_hour(datetime(2026, 1, 1, 21, 0), peak_hours=multi))
        self.assertFalse(is_peak_hour(datetime(2026, 1, 1, 15, 0), peak_hours=multi))

    def test_peak_hours_empty_means_no_peak(self):
        self.assertFalse(is_peak_hour(datetime(2026, 1, 1, 10, 0), peak_hours=[]))
        self.assertFalse(is_peak_hour(datetime(2026, 1, 1, 15, 0), peak_hours=[]))

    def test_peak_hours_invalid_falls_back_to_default(self):
        # 非法输入回退默认 9-12/14-18
        self.assertTrue(is_peak_hour(datetime(2026, 1, 1, 10, 0), peak_hours="bad"))
        self.assertFalse(is_peak_hour(datetime(2026, 1, 1, 13, 0), peak_hours=[[24, 25]]))

    def test_peak_hours_json_override(self):
        # model-policy.json 里配 peak_hours 覆盖默认
        override = {"peak_hours": [[8, 10], [20, 22]]}
        (self.state_dir / "model-policy.json").write_text(
            json.dumps(override, ensure_ascii=False),
            encoding="utf-8",
        )
        self.assertEqual(self.store.get_policy()["peak_hours"], [[8, 10], [20, 22]])
        # 实例方法按配置判断
        self.assertTrue(self.store.is_peak_hour(datetime(2026, 1, 1, 21, 0)))
        self.assertFalse(self.store.is_peak_hour(datetime(2026, 1, 1, 15, 0)))

    def test_peak_hours_per_model_override(self):
        # 模型级 peak_hours 覆盖：gemini 配了 [[22, 23]]，全局默认 9-12/14-18
        override = {
            "models": {
                "gemini-2.0-flash@google": {"peak_hours": [[22, 23]]},
            }
        }
        (self.state_dir / "model-policy.json").write_text(
            json.dumps(override, ensure_ascii=False),
            encoding="utf-8",
        )
        # gemini 模型级：22-23 高峰，10 点不是（尽管全局 10 点是高峰）
        self.assertTrue(self.store.is_peak_hour_for(datetime(2026, 1, 1, 22, 30), model_id="gemini-2.0-flash", provider="google"))
        self.assertFalse(self.store.is_peak_hour_for(datetime(2026, 1, 1, 10, 0), model_id="gemini-2.0-flash", provider="google"))
        # 未配置的模型（gpt-4o）走全局：10 点是高峰
        self.assertTrue(self.store.is_peak_hour_for(datetime(2026, 1, 1, 10, 0), model_id="gpt-4o", provider="openai"))

    def test_peak_hours_per_model_falls_back_to_global(self):
        # 模型级配空数组 = 无峰谷；未配置模型走全局
        override = {
            "peak_hours": [[9, 12]],
            "models": {
                "gemini-2.0-flash@google": {"peak_hours": []},
            }
        }
        (self.state_dir / "model-policy.json").write_text(
            json.dumps(override, ensure_ascii=False),
            encoding="utf-8",
        )
        self.assertFalse(self.store.is_peak_hour_for(datetime(2026, 1, 1, 10, 0), model_id="gemini-2.0-flash", provider="google"))
        self.assertTrue(self.store.is_peak_hour_for(datetime(2026, 1, 1, 10, 0), model_id="gpt-4o", provider="openai"))
        # peak_hours_for 返回值
        self.assertEqual(self.store.peak_hours_for("gemini-2.0-flash", "google"), [])
        self.assertEqual(self.store.peak_hours_for("gpt-4o", "openai"), [[9, 12]])

    def test_default_state_dir_uses_env_var(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LLM_ROUTER_STATE_DIR": tmp}):
                self.assertEqual(default_state_dir(), Path(tmp))

    def test_atomic_write_json(self):
        path = self.state_dir / "sub" / "test.json"
        atomic_write_json(path, {"a": 1})
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1})
        self.assertEqual(list((self.state_dir / "sub").glob(".test.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

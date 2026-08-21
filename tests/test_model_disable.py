import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_model_router.policy import ModelPolicy
from agent_model_router.router import recommend_for_session


class ModelDisableTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp_dir.name)
        self.store = ModelPolicy(self.state_dir)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def _write_policy(self, config):
        (self.state_dir / "model-policy.json").write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )

    def _model(self, model_id, provider="acme", **overrides):
        entry = {
            "id": model_id,
            "provider": provider,
            "tier": "A",
            "cost": "free",
            "quota_per_window": 100,
            "peak_safe": True,
            "role": "free-bulk",
            "fallback_chain": [],
            "scenarios": ["simple"],
            "label": model_id,
        }
        entry.update(overrides)
        return entry

    def test_disabled_json_model_excluded_from_policy_list_and_find_by_role(self):
        self._write_policy({
            "models": {
                "my-model@acme": self._model("my-model"),
                "my-disabled@acme": self._model("my-disabled", enabled=False),
            }
        })

        models = self.store.get_policy()["models"]

        self.assertIn("my-model@acme", models)
        self.assertNotIn("my-disabled@acme", models)

        list_keys = {item["key"] for item in self.store.list_models()}
        self.assertIn("my-model@acme", list_keys)
        self.assertNotIn("my-disabled@acme", list_keys)

        role_ids = {entry["id"] for entry in self.store.find_by_role("free-bulk")}
        self.assertIn("my-model", role_ids)
        self.assertNotIn("my-disabled", role_ids)

    def test_disabled_default_model_not_recommended(self):
        self._write_policy({
            "models": {
                "gpt-4o@openai": {"enabled": False},
                "my-real@acme": self._model(
                    "my-real",
                    tier="S",
                    cost="paid",
                    quota_per_window=None,
                    role="stable",
                    scenarios=["complex"],
                    label="My Real Model",
                ),
            }
        })

        result = recommend_for_session(
            "紧急！",
            quota_snapshot={},
            policy_store=self.store,
        )

        self.assertTrue(result["urgent"])
        self.assertEqual(result["model"], "my-real")
        self.assertEqual(result["provider"], "acme")
        self.assertEqual(result["key"], "my-real@acme")
        self.assertNotEqual(result["model"], "gpt-4o")

    def test_missing_enabled_means_true_and_defaults_unchanged(self):
        # 默认画像：所有条目 enabled 默认 True，数量仍为 5。
        default_models = self.store.get_policy()["models"]
        self.assertEqual(len(default_models), 5)
        self.assertTrue(all(entry.get("enabled") is not False for entry in default_models.values()))

        # JSON 覆盖不写 enabled：保持默认 True，行为不变。
        self._write_policy({
            "models": {
                "my-model@acme": self._model("my-model"),
            }
        })

        models = self.store.get_policy()["models"]
        self.assertIn("my-model@acme", models)
        self.assertIs(models["my-model@acme"].get("enabled"), True)

        list_keys = {item["key"] for item in self.store.list_models()}
        self.assertIn("my-model@acme", list_keys)

    def test_enabled_false_string_is_disabled(self):
        self._write_policy({
            "models": {
                "my-str-disabled@acme": self._model("my-str-disabled", enabled="false"),
            }
        })

        models = self.store.get_policy()["models"]
        self.assertNotIn("my-str-disabled@acme", models)

        list_keys = {item["key"] for item in self.store.list_models()}
        self.assertNotIn("my-str-disabled@acme", list_keys)

        role_ids = {entry["id"] for entry in self.store.find_by_role("free-bulk")}
        self.assertNotIn("my-str-disabled", role_ids)


if __name__ == "__main__":
    unittest.main()

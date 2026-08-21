import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agent_model_router.health as health_module
from agent_model_router.health import HEALTH_WINDOW_SECONDS, ProviderHealth


class ProviderHealthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.health = ProviderHealth(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_health_score_shape_and_no_archive_defaults(self):
        score = self.health.health_score("gpt-4o", "openai", now=1000.0)
        self.assertEqual(set(score), {"success_rate", "p50", "p95", "recent_failures", "failure_risk"})
        self.assertEqual(score["success_rate"], 1.0)
        self.assertIsNone(score["p50"])
        self.assertIsNone(score["p95"])
        self.assertEqual(score["recent_failures"], 0)
        self.assertEqual(score["failure_risk"], 0.2)

    def test_record_result_accumulates(self):
        self.health.record_result("gpt-4o", "openai", status=200, latency_ms=100.0, ts=1000.0)
        self.health.record_result("gpt-4o", "openai", status=200, latency_ms=300.0, ts=1000.0)
        self.health.record_result("gpt-4o", "openai", status=500, latency_ms=500.0, ts=1000.0)

        score = self.health.health_score("gpt-4o", "openai", now=1000.0)
        self.assertEqual(score["recent_failures"], 1)
        self.assertAlmostEqual(score["success_rate"], 2 / 3)
        self.assertAlmostEqual(score["failure_risk"], 1 / 3)
        self.assertEqual(score["p50"], 300.0)
        self.assertEqual(score["p95"], 500.0)

        raw = json.loads((self.state_dir / "model-health.json").read_text(encoding="utf-8"))
        entry = raw["gpt-4o@openai"]
        self.assertEqual(entry["calls"], 3)
        self.assertEqual(entry["failures"], 1)
        self.assertEqual(entry["status_counts"], {"200": 2, "500": 1})
        self.assertEqual(len(entry["latency_samples"]), 3)

    def test_sliding_window_expires_old_samples(self):
        self.health.record_result("gpt-4o", "openai", status=200, latency_ms=100.0, ts=1000.0)
        # 恰好 1 小时仍在窗口内（含边界）。
        score = self.health.health_score("gpt-4o", "openai", now=1000.0 + HEALTH_WINDOW_SECONDS)
        self.assertEqual(score["p50"], 100.0)
        self.assertEqual(score["recent_failures"], 0)
        # 超过 1 小时后过期。
        score = self.health.health_score("gpt-4o", "openai", now=1000.0 + HEALTH_WINDOW_SECONDS + 1)
        self.assertIsNone(score["p50"])
        self.assertEqual(score["recent_failures"], 0)
        self.assertEqual(score["failure_risk"], 0.2)

    def test_record_result_prunes_expired_samples(self):
        self.health.record_result("gpt-4o", "openai", status=200, latency_ms=100.0, ts=1000.0)
        self.health.record_result("gpt-4o", "openai", status=200, latency_ms=400.0, ts=1000.0 + HEALTH_WINDOW_SECONDS + 1)
        raw = json.loads((self.state_dir / "model-health.json").read_text(encoding="utf-8"))
        entry = raw["gpt-4o@openai"]
        self.assertEqual(entry["calls"], 2)  # 累计归档不清零
        self.assertEqual(len(entry["latency_samples"]), 1)
        self.assertEqual(entry["latency_samples"][0]["latency_ms"], 400.0)

    def test_corrupt_file_falls_back_to_empty_archive(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "model-health.json").write_text("not-json{{{", encoding="utf-8")
        score = self.health.health_score("gpt-4o", "openai", now=1000.0)
        self.assertEqual(score["success_rate"], 1.0)
        self.assertIsNone(score["p50"])
        self.assertEqual(score["failure_risk"], 0.2)

        # 损坏文件不应阻止后续 record_result 写入新档案。
        self.health.record_result("gpt-4o", "openai", status=200, latency_ms=120.0, ts=1000.0)
        score = self.health.health_score("gpt-4o", "openai", now=1000.0)
        self.assertEqual(score["p50"], 120.0)

    def test_module_level_convenience_functions(self):
        old_default = health_module._default_health
        old_state_dir = health_module.default_state_dir
        try:
            health_module.default_state_dir = lambda: self.state_dir
            health_module._default_health = None
            health_module.record_result("gpt-4o", "openai", status=200, latency_ms=150.0, ts=1000.0)
            score = health_module.health_score("gpt-4o", "openai", now=1000.0)
            self.assertEqual(score["p50"], 150.0)
        finally:
            health_module._default_health = old_default
            health_module.default_state_dir = old_state_dir


if __name__ == "__main__":
    unittest.main()

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler.quota import WINDOW_SECONDS, QuotaTracker


class QuotaTrackerTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp_dir.name)
        self.tracker = QuotaTracker(self.tmp_path)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_quota_left_returns_full_free_quota_initially(self):
        self.assertEqual(
            self.tracker.quota_left("gemini-2.0-flash", "google", now=1000.0),
            1500,
        )

    def test_record_call_reduces_quota(self):
        model = "gemini-2.0-flash"
        provider = "google"
        self.tracker.record_call(model, provider, ts=1000.0)
        self.assertEqual(self.tracker.quota_left(model, provider, now=1000.0), 1499)

    def test_paid_model_returns_unlimited(self):
        self.assertEqual(self.tracker.quota_left("gpt-4o", "openai", now=1000.0), -1)

    def test_unknown_model_returns_minus_one(self):
        self.assertEqual(self.tracker.quota_left("no-such-model", "provider", now=1000.0), -1)

    def test_sliding_window_five_hours(self):
        model = "gemini-2.0-flash"
        provider = "google"
        now = 1_000_000.0
        fresh_ts = now - 10
        expired_ts = now - WINDOW_SECONDS - 10
        self.tracker.record_call(model, provider, ts=expired_ts)
        self.tracker.record_call(model, provider, ts=fresh_ts)
        self.assertEqual(self.tracker.quota_left(model, provider, now=now), 1499)

    def test_sliding_window_boundary_inclusive(self):
        model = "gemini-2.0-flash"
        provider = "google"
        now = 1_000_000.0
        self.tracker.record_call(model, provider, ts=now - WINDOW_SECONDS)
        self.assertEqual(self.tracker.quota_left(model, provider, now=now), 1499)

    def test_reset_if_needed_removes_expired(self):
        model = "gemini-2.0-flash"
        provider = "google"
        now = 1_000_000.0
        self.tracker.record_call(model, provider, ts=now - WINDOW_SECONDS - 5)
        self.tracker.record_call(model, provider, ts=now - 10)
        self.assertEqual(self.tracker.reset_if_needed(now=now), 1)
        self.assertEqual(self.tracker.quota_left(model, provider, now=now), 1499)

    def test_record_call_thread_safety(self):
        model = "gemini-2.0-flash"
        provider = "google"
        n_threads = 8
        calls_per_thread = 5

        def worker(worker_id):
            for i in range(calls_per_thread):
                self.tracker.record_call(model, provider, ts=1000.0 + worker_id * calls_per_thread + i)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(
            self.tracker.quota_left(model, provider, now=2000.0),
            1500 - n_threads * calls_per_thread,
        )

    def test_atomic_write_leaves_no_tmp_files(self):
        self.tracker.record_call("gemini-2.0-flash", "google", ts=1000.0)
        self.assertEqual(list(self.tmp_path.glob(".model-quota.json.*.tmp")), [])
        data = json.loads((self.tmp_path / "model-quota.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["calls"]), 1)

    def test_failure_cooldown(self):
        self.tracker.record_failure("claude-3-5-sonnet", "anthropic", ts=1000.0)
        self.assertGreater(self.tracker.cooldown_seconds_left("claude-3-5-sonnet", "anthropic", now=1000.0), 0)
        self.assertEqual(self.tracker.cooldown_seconds_left("claude-3-5-sonnet", "anthropic", now=1300.0), 0)
        self.assertEqual(self.tracker.cooldown_seconds_left("claude-3-5-sonnet", "anthropic", now=1400.0), 0)

    def test_quota_table_left(self):
        table = self.tracker.quota_table_left(now=1000.0)
        self.assertEqual(table["gemini-2.0-flash@google"], 1500)
        self.assertEqual(table["claude-3-5-sonnet@anthropic"], 500)
        self.assertNotIn("gpt-4o@openai", table)


if __name__ == "__main__":
    unittest.main()

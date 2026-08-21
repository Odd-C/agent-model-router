import json
import math
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agent_model_router.benchmark as benchmark_module
import agent_model_router.quota as quota_module
import agent_model_router.taskserver as taskserver_module
from agent_model_router.executor import MockExecutor
from agent_model_router.health import ProviderHealth
from agent_model_router.quota import QuotaTracker
from agent_model_router.scheduler import TaskScheduler
from agent_model_router.task import Task, TaskStore
from agent_model_router.taskserver import create_server
from agent_model_router.utility import _effective_weights


def make_task(task_id="t1", status="queued", priority="normal", defer_until=None, deadline=None, attempts=0):
    return Task(
        task_id=task_id,
        task_type="text",
        priority=priority,
        deadline=deadline,
        defer_until=defer_until,
        status=status,
        payload={"prompt": "hello"},
        attempts=attempts,
        last_error=None,
        created_at=1000.0,
        updated_at=1000.0,
    )


class TaskValidateP2Tests(unittest.TestCase):
    def test_validate_rejects_nan_deadline_and_defer_until(self):
        with self.assertRaises(ValueError):
            make_task(deadline=float("nan")).validate()
        with self.assertRaises(ValueError):
            make_task(defer_until=float("nan")).validate()

    def test_validate_rejects_empty_task_type(self):
        task = make_task()
        task.task_type = ""
        with self.assertRaises(ValueError):
            task.validate()

    def test_validate_rejects_non_dict_payload(self):
        task = make_task()
        task.payload = ["not", "a", "dict"]
        with self.assertRaises(ValueError):
            task.validate()


class SchedulerNaNGuardP2Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.store = TaskStore(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_scheduler_rejects_nan_base_delay(self):
        with self.assertRaises(ValueError):
            TaskScheduler(self.store, MockExecutor(), base_delay=float("nan"))

    def test_scheduler_rejects_nan_deadline_horizon(self):
        with self.assertRaises(ValueError):
            TaskScheduler(self.store, MockExecutor(), deadline_horizon=float("nan"))

    def test_submit_rejects_nan_deadline(self):
        scheduler = TaskScheduler(self.store, MockExecutor())
        with self.assertRaises(ValueError):
            scheduler.submit("text", {}, deadline=float("nan"))


class EffectiveWeightsP2Tests(unittest.TestCase):
    def test_effective_weights_raises_value_error_not_type_error(self):
        with self.assertRaises(ValueError):
            _effective_weights({"quality_fit": None})
        with self.assertRaises(ValueError):
            _effective_weights({"latency_penalty": "not-a-number"})
        with self.assertRaises(ValueError):
            _effective_weights({"failure_risk": float("inf")})


class HealthMissingLatencyP2Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.health = ProviderHealth(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_latency_sample_excluded_from_percentiles_but_counts_failures(self):
        self.health.record_result("gpt-4o", "openai", status=200, latency_ms=100.0, ts=1000.0)
        self.health.record_result("gpt-4o", "openai", status=500, latency_ms=None, ts=1000.0)

        score = self.health.health_score("gpt-4o", "openai", now=1000.0)
        self.assertEqual(score["p50"], 100.0)
        self.assertEqual(score["p95"], 100.0)
        self.assertEqual(score["recent_failures"], 1)
        self.assertAlmostEqual(score["failure_risk"], 0.5)

        raw = json.loads((self.state_dir / "model-health.json").read_text(encoding="utf-8"))
        entry = raw["gpt-4o@openai"]
        self.assertEqual(entry["calls"], 2)
        self.assertEqual(entry["failures"], 1)
        self.assertIsNone(entry["latency_samples"][1]["latency_ms"])


class QuotaNormaliseP2Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.tracker = QuotaTracker(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_record_call_normalises_at_provider_model_format(self):
        model = "gemini-2.0-flash"
        provider = "google"
        self.tracker.record_call("@google:gemini-2.0-flash", provider=None, ts=1000.0)
        self.assertEqual(self.tracker.quota_left(model, provider, now=1000.0), 1499)

    def test_quota_module_has_all(self):
        for name in ("record_call", "quota_left", "QuotaTracker", "WINDOW_SECONDS", "COOLDOWN_SECONDS"):
            self.assertIn(name, quota_module.__all__)


class BenchmarkAllP2Tests(unittest.TestCase):
    def test_benchmark_module_has_all(self):
        for name in ("BenchmarkConfig", "BenchmarkResult", "run_benchmark", "format_report"):
            self.assertIn(name, benchmark_module.__all__)


class TaskServerP2Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.httpd = create_server(
            "127.0.0.1",
            0,
            state_dir=self.state_dir,
            executor=MockExecutor(result={"ok": True}),
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=5)
        finally:
            self._tmp.cleanup()

    def _post_raw(self, path, raw_bytes, content_type="application/json"):
        req = urllib.request.Request(
            self.base + path,
            data=raw_bytes,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
                return resp.status, json.loads(body.decode("utf-8")) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read()
            return exc.code, json.loads(body.decode("utf-8")) if body else None

    def test_read_json_object_rejects_body_over_size_limit_with_413(self):
        # 把上限调小，避免在单测里传输 1MB 请求体。
        old_limit = taskserver_module.MAX_BODY_BYTES
        taskserver_module.MAX_BODY_BYTES = 1024
        try:
            status, data = self._post_raw("/api/tasks", b"x" * 1025)
        finally:
            taskserver_module.MAX_BODY_BYTES = old_limit

        self.assertEqual(status, 413)
        self.assertEqual(data["error"]["type"], "agent_model_router.payload_too_large")
        self.assertNotIn("detail", data["error"])

    def test_internal_server_error_does_not_leak_exception_detail(self):
        def boom():
            raise RuntimeError("secret-internal-detail")

        self.httpd.app.stats = boom
        req = urllib.request.Request(self.base + "/api/stats", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
        data = json.loads(body.decode("utf-8")) if body else None

        self.assertEqual(status, 500)
        self.assertEqual(data["error"]["message"], "internal server error")
        self.assertEqual(data["error"]["type"], "agent_model_router.internal_error")
        self.assertNotIn("detail", data["error"])
        self.assertNotIn("secret-internal-detail", json.dumps(data))


if __name__ == "__main__":
    unittest.main()

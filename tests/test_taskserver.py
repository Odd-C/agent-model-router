import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler import taskserver
from model_scheduler.executor import MockExecutor
from model_scheduler.taskserver import create_server


class TaskServerTests(unittest.TestCase):
    """通过真实 HTTP 请求测试 Opportunistic Scheduling 看板。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.httpd = create_server(
            "127.0.0.1",
            0,
            state_dir=self.state_dir,
            executor=MockExecutor(result={"ok": True}, cost=0.5),
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

    def _request(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw.decode("utf-8")) if raw else None

    def _request_text(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers)

    def _submit(self, task_type="text", payload=None, priority="normal", deadline=None):
        body = {"task_type": task_type, "payload": payload if payload is not None else {"x": 1}}
        if priority is not None:
            body["priority"] = priority
        if deadline is not None:
            body["deadline"] = deadline
        return self._request("POST", "/api/tasks", body)

    def test_health(self):
        status, data = self._request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["version"], "0.6.0")

    def test_index_page_contains_title(self):
        status, text, headers = self._request_text("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("Opportunistic Scheduling", text)

    def test_page_js_uses_data_action_delegation_not_inline_onclick(self):
        self.assertNotIn('onclick="window.', taskserver._PAGE_HTML)
        self.assertNotIn("window.cancelTask", taskserver._PAGE_HTML)
        self.assertNotIn("window.deleteTask", taskserver._PAGE_HTML)
        self.assertIn("data-action", taskserver._PAGE_HTML)
        self.assertIn("closest('button[data-action]')", taskserver._PAGE_HTML)
        # 偏好区自然语言输入框 + 翻译并应用按钮。
        self.assertIn('id="pref-intent"', taskserver._PAGE_HTML)
        self.assertIn('id="pref-compile"', taskserver._PAGE_HTML)
        self.assertIn("翻译并应用", taskserver._PAGE_HTML)
        self.assertIn("/api/preferences/compile", taskserver._PAGE_HTML)
        self.assertIn('id="pref-compile-msg"', taskserver._PAGE_HTML)

    def test_submit_high_queued_and_low_deferred(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            status, high = self._submit(priority="high")
            self.assertEqual(status, 200)
            self.assertEqual(high["status"], "queued")
            self.assertIsNone(high["defer_until"])
            self.assertTrue(high["task_id"])

            status, low = self._submit(task_type="coding", priority="low")
            self.assertEqual(status, 200)
            self.assertEqual(low["status"], "deferred")
            self.assertEqual(low["defer_until"], 1600.0)  # 1000 + 300 * 2

    def test_submit_priority_defaults_to_normal(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            status, task = self._submit(priority=None)
            self.assertEqual(status, 200)
            # 默认 normal，base_delay=300，normal 权重 1.0 -> 1300.0
            self.assertEqual(task["status"], "deferred")
            self.assertEqual(task["defer_until"], 1300.0)

    def test_submit_accepts_iso_deadline_and_epoch_deadline(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            status, task = self._submit(priority="normal", deadline="1970-01-01T00:20:00Z")
            self.assertEqual(status, 200)
            self.assertEqual(task["status"], "queued")
            self.assertIsNone(task["defer_until"])

            status, task = self._submit(priority="normal", deadline=1400.0)
            self.assertEqual(status, 200)
            self.assertEqual(task["status"], "queued")

    def test_submit_invalid_json_and_invalid_priority(self):
        status, data = self._request("POST", "/api/tasks", body=None)
        self.assertEqual(status, 400)

        # 无效 priority
        status, data = self._request(
            "POST",
            "/api/tasks",
            {"task_type": "text", "payload": {}, "priority": "urgent"},
        )
        self.assertEqual(status, 400)

    def test_list_filters_detail(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            _, high = self._submit(task_type="text", priority="high")
            _, low = self._submit(task_type="coding", priority="low")

        status, data = self._request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["tasks"]), 2)

        status, data = self._request("GET", "/api/tasks?status=deferred")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["tasks"]), 1)
        self.assertEqual(data["tasks"][0]["task_id"], low["task_id"])
        self.assertEqual(data["tasks"][0]["status"], "deferred")

        status, data = self._request("GET", "/api/tasks?status=deferred&task_type=coding")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["tasks"]), 1)
        self.assertEqual(data["tasks"][0]["task_type"], "coding")

        status, data = self._request("GET", "/api/tasks?status=queued&task_type=text")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["tasks"]), 1)
        self.assertEqual(data["tasks"][0]["task_id"], high["task_id"])

        # 分页 offset/limit
        status, data = self._request("GET", "/api/tasks?limit=1&offset=0")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["tasks"]), 1)

        status, data = self._request("GET", "/api/tasks?limit=1&offset=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["tasks"]), 1)
        self.assertNotEqual(data["tasks"][0]["task_id"], self._request("GET", "/api/tasks?limit=1&offset=0")[1]["tasks"][0]["task_id"])

        status, data = self._request("GET", f"/api/tasks/{low['task_id']}")
        self.assertEqual(status, 200)
        for field in (
            "task_id",
            "task_type",
            "priority",
            "deadline",
            "defer_until",
            "status",
            "attempts",
            "last_error",
            "created_at",
            "updated_at",
            "result",
            "cost",
        ):
            self.assertIn(field, data)

        status, _ = self._request("GET", "/api/tasks/not-found")
        self.assertEqual(status, 404)

    def test_cancel_and_delete(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            _, task = self._submit(priority="high")

        status, data = self._request("POST", f"/api/tasks/{task['task_id']}/cancel")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        status, data = self._request("GET", f"/api/tasks/{task['task_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "cancelled")

        status, data = self._request("DELETE", f"/api/tasks/{task['task_id']}")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        status, _ = self._request("GET", f"/api/tasks/{task['task_id']}")
        self.assertEqual(status, 404)

        status, data = self._request("DELETE", f"/api/tasks/{task['task_id']}")
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])

    def test_tick_deferred_to_queued_to_done_and_stats(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            _, task = self._submit(task_type="batch", priority="low")
            self.assertEqual(task["status"], "deferred")
            self.assertEqual(task["defer_until"], 1600.0)

        with mock.patch("model_scheduler.scheduler.time.time", return_value=2000.0):
            status, data = self._request("POST", "/api/tick")
            self.assertEqual(status, 200)
            self.assertIn(task["task_id"], data["processed"])
            self.assertEqual(data["now"], 2000.0)

        status, data = self._request("GET", f"/api/tasks/{task['task_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["attempts"], 0)
        self.assertEqual(data["cost"], 0.5)
        self.assertEqual(data["result"], {"ok": True})

        status, stats = self._request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["by_status"]["done"], 1)
        self.assertEqual(stats["by_status"]["deferred"], 0)
        self.assertEqual(stats["total_cost"], 0.5)

    def test_stats_counts(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            self._submit(task_type="text", priority="high")
            self._submit(task_type="image", priority="normal")

        status, stats = self._request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["by_status"]["queued"], 1)
        self.assertEqual(stats["by_status"]["deferred"], 1)
        self.assertEqual(stats["by_type"]["text"], 1)
        self.assertEqual(stats["by_type"]["image"], 1)
        self.assertEqual(stats["total_cost"], 0.0)

    def test_preferences_get_and_put(self):
        status, data = self._request("GET", "/api/preferences")
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "balanced")
        self.assertEqual(data["weights"]["quality_fit"], 1.0)
        self.assertEqual(data["weights"]["latency_penalty"], 1.0)

        status, data = self._request("PUT", "/api/preferences", {"mode": "latency-first"})
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "latency-first")
        self.assertEqual(data["weights"]["latency_penalty"], 3.0)
        self.assertEqual(data["weights"]["quality_fit"], 1.0)

        # 文件持久化后 GET 应返回同一 mode
        status, data = self._request("GET", "/api/preferences")
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "latency-first")

    def test_preferences_invalid_mode(self):
        status, data = self._request("PUT", "/api/preferences", {"mode": "turbo"})
        self.assertEqual(status, 400)

    def test_get_task_result_done(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            _, task = self._submit(priority="high")

        with mock.patch("model_scheduler.scheduler.time.time", return_value=2000.0):
            status, _ = self._request("POST", "/api/tick")
            self.assertEqual(status, 200)

        status, data = self._request("GET", f"/api/tasks/{task['task_id']}/result")
        self.assertEqual(status, 200)
        self.assertEqual(data["task_id"], task["task_id"])
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["result"], {"ok": True})
        self.assertEqual(data["cost"], 0.5)
        self.assertIsNone(data["error"])
        self.assertNotIn("pending", data)

    def test_get_task_result_pending(self):
        with mock.patch("model_scheduler.scheduler.time.time", return_value=1000.0):
            _, task = self._submit(priority="low")

        status, data = self._request("GET", f"/api/tasks/{task['task_id']}/result")
        self.assertEqual(status, 200)
        self.assertEqual(data["task_id"], task["task_id"])
        self.assertEqual(data["status"], "deferred")
        self.assertIsNone(data["result"])
        self.assertIsNone(data["error"])
        self.assertTrue(data["pending"])
        self.assertEqual(data["cost"], 0.0)

    def test_get_task_result_not_found(self):
        status, data = self._request("GET", "/api/tasks/not-found/result")
        self.assertEqual(status, 404)

    def test_preferences_compile_chinese_intent(self):
        status, data = self._request("POST", "/api/preferences/compile", {"text": "要便宜一点的"})
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "cost-first")
        self.assertEqual(data["weights"]["cost_penalty"], 3.0)
        self.assertEqual(data["weights"]["quality_fit"], 1.0)
        self.assertIn("constraints", data)
        self.assertEqual(data["constraints"]["cost_max"], "free")
        self.assertIn("explanation", data)
        self.assertIn("只用免费模型", data["explanation"])

    def test_preferences_compile_empty_text(self):
        for body in ({"text": ""}, {"text": "   "}, {}):
            with self.subTest(body=body):
                status, data = self._request("POST", "/api/preferences/compile", body)
                self.assertEqual(status, 400)

    def test_put_preferences_with_weights_override_persists(self):
        status, data = self._request(
            "PUT",
            "/api/preferences",
            {"mode": "latency-first", "weights": {"quality_fit": 0.5, "cost_penalty": 2.5}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "latency-first")
        self.assertEqual(data["weights"]["latency_penalty"], 3.0)
        self.assertEqual(data["weights"]["quality_fit"], 0.5)
        self.assertEqual(data["weights"]["cost_penalty"], 2.5)

        status, data = self._request("GET", "/api/preferences")
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "latency-first")
        self.assertEqual(data["weights"]["quality_fit"], 0.5)
        self.assertEqual(data["weights"]["cost_penalty"], 2.5)

        # 无 weights 的 PUT 保持已有覆盖（向后兼容）。
        status, data = self._request("PUT", "/api/preferences", {"mode": "cost-first"})
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "cost-first")
        self.assertEqual(data["weights"]["quality_fit"], 0.5)
        self.assertEqual(data["weights"]["cost_penalty"], 2.5)


if __name__ == "__main__":
    unittest.main()

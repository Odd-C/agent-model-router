import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_model_router.policy import ModelPolicy
from agent_model_router.quota import COOLDOWN_SECONDS, QuotaTracker
from agent_model_router.server import ProxyApp, create_server

BASE_CONFIG = {
    "language": "en",
    "providers": {
        "openai": {"base_url": "https://api.openai.com/v1", "api_key": "sk-openai"},
        "deepseek": {"base_url": "https://api.deepseek.com", "api_key": "sk-deepseek"},
        "google": {
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "api_key": "sk-google",
        },
        "anthropic": {"base_url": "https://api.anthropic.com/v1", "api_key": "sk-anthropic"},
    },
}


class FakeRouter:
    def __init__(self, decision):
        self.decision = dict(decision)

    def recommend_for_session(self, text, **kwargs):
        return dict(self.decision)


class FakeTransport:
    def __init__(self, status, body=b"{}", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {"Content-Type": "application/json"}

    def __call__(self, url, headers, body, timeout):
        return self.status, dict(self.headers), self.body


class FakeStreamOpener:
    def __init__(self, status, chunks=None, headers=None):
        self.status = status
        self.chunks = chunks or []
        self.headers = headers or {"Content-Type": "text/event-stream"}

    def __call__(self, url, headers, body, timeout):
        return self.status, dict(self.headers), list(self.chunks)


class MidStreamErrorOpener:
    """流式 opener：先返回一个 chunk，迭代到第二个 chunk 时抛异常。"""

    def __init__(self, headers=None):
        self.headers = headers or {"Content-Type": "text/event-stream"}

    def __call__(self, url, headers, body, timeout):
        def chunks():
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            raise OSError("mid-stream upstream read failed")

        return 200, dict(self.headers), chunks()


class ExplodingTransport:
    def __call__(self, url, headers, body, timeout):
        raise OSError("connection refused")


class ErrorClassificationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self._servers = []

    def tearDown(self):
        for httpd, thread in self._servers:
            try:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
            except Exception:
                pass
        self._tmp.cleanup()

    def _write_policy(self):
        (self.state_dir / "model-policy.json").write_text(
            json.dumps(BASE_CONFIG, ensure_ascii=False),
            encoding="utf-8",
        )

    def _make_app(self, router=None, transport=None, stream_opener=None):
        self._write_policy()
        policy = ModelPolicy(self.state_dir)
        quota_tracker = QuotaTracker(self.state_dir, policy_store=policy)
        if router is None:
            router = FakeRouter(
                {
                    "model": "gpt-4o",
                    "provider": "openai",
                    "reason": "test",
                    "tier": "S",
                    "cost": "paid",
                    "difficulty": 0,
                    "urgent": False,
                    "peak": False,
                    "key": "gpt-4o@openai",
                }
            )
        app = ProxyApp(
            state_dir=self.state_dir,
            policy_store=policy,
            quota_tracker=quota_tracker,
            router=router,
            transport=transport,
            stream_opener=stream_opener,
        )
        return app, quota_tracker

    def _serve_app(self, app):
        httpd = create_server("127.0.0.1", 0, app)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self._servers.append((httpd, thread))
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def _post_json(self, base, payload, timeout=10):
        raw = json.dumps(payload).encode("utf-8")
        req = request.Request(
            base + "/v1/chat/completions",
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except error.HTTPError as exc:
            return exc.code, exc.read()

    def test_record_failure_writes_new_dict_shape(self):
        tracker = QuotaTracker(self.state_dir)
        tracker.record_failure("gpt-4o", "openai", ts=1000.0, reason="rate_limit", status=429)

        data = json.loads((self.state_dir / "model-cooldown.json").read_text(encoding="utf-8"))
        self.assertEqual(
            data["gpt-4o@openai"],
            {"ts": 1000.0, "reason": "rate_limit", "status": 429, "provider": "openai"},
        )
        self.assertGreater(tracker.cooldown_seconds_left("gpt-4o", "openai", now=1000.0), 0)

    def test_old_cooldown_file_format_still_readable(self):
        (self.state_dir / "model-cooldown.json").write_text(
            json.dumps({"gpt-4o@openai": 1000.0}),
            encoding="utf-8",
        )
        tracker = QuotaTracker(self.state_dir)
        # 300 - (1100 - 1000) = 200
        self.assertEqual(tracker.cooldown_seconds_left("gpt-4o", "openai", now=1100.0), 200.0)
        self.assertEqual(tracker.cooldown_seconds_left("gpt-4o", "openai", now=1400.0), 0.0)

    def test_400_401_403_do_not_enter_cooldown(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                sub = Path(self._tmp.name) / str(status)
                sub.mkdir()
                self.state_dir = sub
                app, tracker = self._make_app(transport=FakeTransport(status=status, body=b'{"error":"bad"}'))
                base = self._serve_app(app)
                resp_status, _ = self._post_json(
                    base,
                    {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
                )
                self.assertEqual(resp_status, status)
                self.assertFalse((self.state_dir / "model-cooldown.json").exists())
                self.assertEqual(tracker.cooldown_seconds_left("gpt-4o", "openai"), 0.0)

    def test_429_and_5xx_enter_cooldown_with_reason_and_status(self):
        cases = [(429, "rate_limit"), (500, "server_error"), (503, "server_error")]
        for status, reason in cases:
            with self.subTest(status=status):
                sub = Path(self._tmp.name) / str(status)
                sub.mkdir()
                self.state_dir = sub
                app, tracker = self._make_app(transport=FakeTransport(status=status, body=b'{"error":"upstream"}'))
                base = self._serve_app(app)
                resp_status, _ = self._post_json(
                    base,
                    {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
                )
                self.assertEqual(resp_status, status)
                self.assertGreater(tracker.cooldown_seconds_left("gpt-4o", "openai"), 0.0)
                data = json.loads((self.state_dir / "model-cooldown.json").read_text(encoding="utf-8"))
                entry = data["gpt-4o@openai"]
                self.assertEqual(entry["reason"], reason)
                self.assertEqual(entry["status"], status)
                self.assertEqual(entry["provider"], "openai")

    def test_transport_exception_enters_cooldown_with_transport_error(self):
        app, tracker = self._make_app(transport=ExplodingTransport())
        base = self._serve_app(app)
        resp_status, body = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(resp_status, 502)
        self.assertIn(b"upstream unreachable", body)
        self.assertGreater(tracker.cooldown_seconds_left("gpt-4o", "openai"), 0.0)
        data = json.loads((self.state_dir / "model-cooldown.json").read_text(encoding="utf-8"))
        entry = data["gpt-4o@openai"]
        self.assertEqual(entry["reason"], "transport_error")
        self.assertIsNone(entry["status"])
        self.assertEqual(entry["provider"], "openai")

    def test_stream_429_enters_cooldown_with_rate_limit(self):
        app, tracker = self._make_app(
            stream_opener=FakeStreamOpener(status=429, chunks=[b'{"error":"quota"}'])
        )
        base = self._serve_app(app)
        resp_status, body = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True},
        )
        self.assertEqual(resp_status, 429)
        self.assertIn(b"quota", body)
        self.assertGreater(tracker.cooldown_seconds_left("gpt-4o", "openai"), 0.0)
        data = json.loads((self.state_dir / "model-cooldown.json").read_text(encoding="utf-8"))
        self.assertEqual(data["gpt-4o@openai"]["reason"], "rate_limit")
        self.assertEqual(data["gpt-4o@openai"]["status"], 429)

    def test_stream_400_does_not_enter_cooldown(self):
        app, tracker = self._make_app(
            stream_opener=FakeStreamOpener(status=400, chunks=[b'{"error":"bad"}'])
        )
        base = self._serve_app(app)
        resp_status, _ = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True},
        )
        self.assertEqual(resp_status, 400)
        self.assertFalse((self.state_dir / "model-cooldown.json").exists())
        self.assertEqual(tracker.cooldown_seconds_left("gpt-4o", "openai"), 0.0)

    def test_stream_open_exception_enters_cooldown_with_transport_error(self):
        app, tracker = self._make_app(stream_opener=ExplodingTransport())
        base = self._serve_app(app)
        resp_status, body = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True},
        )
        self.assertEqual(resp_status, 502)
        self.assertIn(b"upstream unreachable", body)
        self.assertGreater(tracker.cooldown_seconds_left("gpt-4o", "openai"), 0.0)
        data = json.loads((self.state_dir / "model-cooldown.json").read_text(encoding="utf-8"))
        self.assertEqual(data["gpt-4o@openai"]["reason"], "transport_error")
        self.assertIsNone(data["gpt-4o@openai"]["status"])

    def test_default_transport_urlerror_enters_cooldown_with_transport_error(self):
        app, tracker = self._make_app()
        base = self._serve_app(app)
        real_urlopen = request.urlopen

        def fake_urlopen(req, timeout=None):
            # 仅让上游调用失败；客户端请求（127.0.0.1）仍走真实 urlopen。
            if "127.0.0.1" in str(getattr(req, "full_url", req)):
                return real_urlopen(req, timeout=timeout)
            raise error.URLError("connection refused")

        with mock.patch(
            "agent_model_router.server.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            resp_status, body = self._post_json(
                base,
                {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
            )
        self.assertEqual(resp_status, 502)
        self.assertIn(b"upstream unreachable", body)
        self.assertGreater(tracker.cooldown_seconds_left("gpt-4o", "openai"), 0.0)
        data = json.loads((self.state_dir / "model-cooldown.json").read_text(encoding="utf-8"))
        entry = data["gpt-4o@openai"]
        self.assertEqual(entry["reason"], "transport_error")
        self.assertIsNone(entry["status"])
        self.assertEqual(entry["provider"], "openai")

    def test_default_stream_opener_urlerror_enters_cooldown_with_transport_error(self):
        app, tracker = self._make_app()
        base = self._serve_app(app)
        real_urlopen = request.urlopen

        def fake_urlopen(req, timeout=None):
            if "127.0.0.1" in str(getattr(req, "full_url", req)):
                return real_urlopen(req, timeout=timeout)
            raise error.URLError("connection refused")

        with mock.patch(
            "agent_model_router.server.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            resp_status, body = self._post_json(
                base,
                {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True},
            )
        self.assertEqual(resp_status, 502)
        self.assertIn(b"upstream unreachable", body)
        self.assertGreater(tracker.cooldown_seconds_left("gpt-4o", "openai"), 0.0)
        data = json.loads((self.state_dir / "model-cooldown.json").read_text(encoding="utf-8"))
        entry = data["gpt-4o@openai"]
        self.assertEqual(entry["reason"], "transport_error")
        self.assertIsNone(entry["status"])

    def test_stream_mid_read_exception_enters_cooldown_with_transport_error(self):
        app, tracker = self._make_app(stream_opener=MidStreamErrorOpener())
        base = self._serve_app(app)

        resp_status, body = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True},
        )
        self.assertEqual(resp_status, 200)
        self.assertIn(b"data: ", body)
        self.assertIn(b"stream forwarding interrupted", body)
        self.assertGreater(tracker.cooldown_seconds_left("gpt-4o", "openai"), 0.0)
        data = json.loads((self.state_dir / "model-cooldown.json").read_text(encoding="utf-8"))
        entry = data["gpt-4o@openai"]
        self.assertEqual(entry["reason"], "transport_error")
        self.assertIsNone(entry["status"])
        self.assertEqual(entry["provider"], "openai")


if __name__ == "__main__":
    unittest.main()

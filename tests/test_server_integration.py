import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_model_router.policy import ModelPolicy
from agent_model_router.quota import QuotaTracker
from agent_model_router.router import ModelRouter
from agent_model_router.server import ProxyApp, create_server


class _MockProviderHandler(BaseHTTPRequestHandler):
    """本地 mock provider：返回固定 OpenAI 格式 JSON，或按 stream 分块返回 SSE。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        self.server.requests.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": payload,
        })

        if self.server.mode == "error":
            out = b'{"error":{"message":"mock quota exceeded","type":"mock_error"}}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            chunks = [
                'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'.encode("utf-8"),
                'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ]
            for chunk in chunks:
                self.wfile.write(chunk)
                self.wfile.flush()
            return

        out = b'{"id":"chatcmpl-mock","object":"chat.completion","choices":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class _MockProviderServer(ThreadingHTTPServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []
        self.mode = "success"


class ServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self._my_cleanups = []

    def tearDown(self):
        for cleanup in reversed(self._my_cleanups):
            try:
                cleanup()
            except Exception:
                pass
        self._tmp.cleanup()

    def _stop_server(self, httpd, thread):
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    def _start_mock(self, mode="success"):
        httpd = _MockProviderServer(("127.0.0.1", 0), _MockProviderHandler)
        httpd.mode = mode
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self._my_cleanups.append(lambda: self._stop_server(httpd, thread))
        return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"

    def _write_policy(self, config):
        (self.state_dir / "model-policy.json").write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )

    def _one_provider_config(self, base_url, provider="mock", model_id="mock-model", quota=1000):
        return {
            "language": "en",
            "providers": {
                provider: {"base_url": base_url, "api_key": "sk-mock"},
            },
            "models": {
                f"{model_id}@{provider}": {
                    "id": model_id,
                    "provider": provider,
                    "tier": "S",
                    "cost": "free",
                    "quota_per_window": quota,
                    "peak_safe": True,
                    "role": "free-bulk",
                    "fallback_chain": [],
                    "scenarios": ["simple"],
                    "label": "Mock Model",
                }
            },
        }

    def _start_proxy(self, config):
        self._write_policy(config)
        policy = ModelPolicy(self.state_dir)
        quota_tracker = QuotaTracker(self.state_dir, policy_store=policy)
        router = ModelRouter(policy_store=policy, quota_tracker=quota_tracker)
        app = ProxyApp(
            state_dir=self.state_dir,
            policy_store=policy,
            quota_tracker=quota_tracker,
            router=router,
            timeout=10,
        )
        httpd = create_server("127.0.0.1", 0, app)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self._my_cleanups.append(lambda: self._stop_server(httpd, thread))
        return app, f"http://127.0.0.1:{httpd.server_address[1]}"

    def _post(self, base, payload, timeout=10):
        raw = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            base + "/v1/chat/completions",
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()

    def test_non_stream_full_chain(self):
        mock_httpd, mock_base = self._start_mock("success")
        config = self._one_provider_config(mock_base)
        _, proxy_base = self._start_proxy(config)

        status, headers, body = self._post(
            proxy_base,
            {
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0.7,
                "max_tokens": 123,
            },
        )
        self.assertEqual(status, 200)
        self.assertIn(b"chatcmpl-mock", body)
        self.assertEqual(headers.get("x-model-scheduler"), "mock-model@mock")

        self.assertEqual(len(mock_httpd.requests), 1)
        req = mock_httpd.requests[0]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["headers"].get("authorization"), "Bearer sk-mock")
        self.assertEqual(req["body"]["model"], "mock-model")
        self.assertEqual(req["body"]["temperature"], 0.7)
        self.assertEqual(req["body"]["max_tokens"], 123)

    def test_stream_full_chain(self):
        mock_httpd, mock_base = self._start_mock("success")
        config = self._one_provider_config(mock_base)
        _, proxy_base = self._start_proxy(config)

        status, headers, body = self._post(
            proxy_base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "text/event-stream; charset=utf-8")
        text = body.decode("utf-8")
        self.assertIn('data: {"choices":[{"delta":{"content":"你"}}]}', text)
        self.assertIn('data: {"choices":[{"delta":{"content":"好"}}]}', text)
        self.assertIn("data: [DONE]", text)
        self.assertEqual(len(mock_httpd.requests), 1)
        self.assertTrue(mock_httpd.requests[0]["body"].get("stream"))

    def test_failure_cools_down_and_reroutes_to_other_provider(self):
        mock_a, base_a = self._start_mock("error")
        mock_b, base_b = self._start_mock("success")
        config = {
            "language": "en",
            "providers": {
                "mock-a": {"base_url": base_a, "api_key": "sk-a"},
                "mock-b": {"base_url": base_b, "api_key": "sk-b"},
            },
            "models": {
                "mock-a-model@mock-a": {
                    "id": "mock-a-model",
                    "provider": "mock-a",
                    "tier": "S",
                    "cost": "free",
                    "quota_per_window": 100,
                    "peak_safe": True,
                    "role": "free-bulk",
                    "fallback_chain": [],
                    "scenarios": ["simple"],
                    "label": "Mock A",
                },
                "mock-b-model@mock-b": {
                    "id": "mock-b-model",
                    "provider": "mock-b",
                    "tier": "S",
                    "cost": "free",
                    "quota_per_window": 100,
                    "peak_safe": True,
                    "role": "free-bulk",
                    "fallback_chain": [],
                    "scenarios": ["simple"],
                    "label": "Mock B",
                },
            },
        }
        app, proxy_base = self._start_proxy(config)
        payload = {"model": "auto", "messages": [{"role": "user", "content": "hello"}]}

        status, _, body = self._post(proxy_base, payload)
        self.assertEqual(status, 429)
        self.assertIn(b"mock quota exceeded", body)
        self.assertEqual(len(mock_a.requests), 1)
        self.assertEqual(len(mock_b.requests), 0)
        self.assertGreater(app.quota.cooldown_seconds_left("mock-a-model", "mock-a"), 0)

        # 第二次请求应绕过冷却中的 mock-a，路由到 mock-b。
        status2, _, body2 = self._post(proxy_base, payload)
        self.assertEqual(status2, 200)
        self.assertIn(b"chatcmpl-mock", body2)
        self.assertEqual(len(mock_a.requests), 1)
        self.assertEqual(len(mock_b.requests), 1)
        self.assertEqual(mock_b.requests[0]["body"]["model"], "mock-b-model")


if __name__ == "__main__":
    unittest.main()

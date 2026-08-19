import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_scheduler.policy import ModelPolicy
from model_scheduler.quota import QuotaTracker
from model_scheduler.router import ModelRouter
from model_scheduler.server import (
    ProxyApp,
    build_chat_completions_url,
    create_server,
    extract_messages_text,
    resolve_api_key,
)

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


def _decision(model, provider, reason="test reason", cost="paid"):
    return {
        "model": model,
        "provider": provider,
        "reason": reason,
        "tier": "",
        "cost": cost,
        "difficulty": 0,
        "urgent": False,
        "peak": False,
        "key": f"{model}@{provider}",
    }


class FakeRouter:
    """注入的假路由器，记录收到的会话文本并返回固定决策。"""

    def __init__(self, decision):
        self.decision = dict(decision)
        self.calls = []

    def recommend_for_session(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return dict(self.decision)


class FakeTransport:
    """注入的假非流式转发函数。"""

    def __init__(self, status=200, body=b'{"ok": true}', headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {"Content-Type": "application/json"}
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        return (self.status, dict(self.headers), self.body)


class FakeStreamOpener:
    """注入的假流式转发函数。"""

    def __init__(self, status=200, headers=None, chunks=None):
        self.status = status
        self.headers = headers or {"Content-Type": "text/event-stream"}
        self.chunks = chunks or []
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        return (self.status, dict(self.headers), list(self.chunks))


class ServerUnitTests(unittest.TestCase):
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

    def _write_policy(self, config):
        (self.state_dir / "model-policy.json").write_text(
            json.dumps(config, ensure_ascii=False),
            encoding="utf-8",
        )

    def _make_app(self, config=None, router=None, transport=None, stream_opener=None):
        if config is None:
            config = BASE_CONFIG
        self._write_policy(config)
        policy = ModelPolicy(self.state_dir)
        quota_tracker = QuotaTracker(self.state_dir, policy_store=policy)
        if router is None:
            router = ModelRouter(policy_store=policy, quota_tracker=quota_tracker)
        app = ProxyApp(
            state_dir=self.state_dir,
            policy_store=policy,
            quota_tracker=quota_tracker,
            router=router,
            transport=transport,
            stream_opener=stream_opener,
        )
        return app

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
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()

    def _post_raw(self, base, raw, timeout=10):
        req = request.Request(base + "/v1/chat/completions", data=raw, method="POST")
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                return resp.status, {}, resp.read()
        except error.HTTPError as exc:
            return exc.code, {}, exc.read()

    def _get_json(self, base, path, timeout=10):
        with request.urlopen(base + path, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()

    def test_extract_messages_text_concatenates_string_and_parts(self):
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": [{"type": "text", "text": "世界"}, {"type": "text", "text": "！"}]},
        ]
        self.assertEqual(extract_messages_text(messages), "你好\n世界\n！")

    def test_chat_uses_difficulty_route_and_rewrites_model(self):
        app = self._make_app(BASE_CONFIG)
        transport = FakeTransport()
        app.transport = transport
        base = self._serve_app(app)

        status, headers, _ = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "帮我写一个 Python 脚本"}], "temperature": 0.7},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(transport.calls), 1)
        sent = transport.calls[0]
        self.assertEqual(sent["url"], "https://api.anthropic.com/v1/chat/completions")
        sent_body = json.loads(sent["body"].decode("utf-8"))
        self.assertEqual(sent_body["model"], "claude-3-5-sonnet")
        self.assertEqual(sent_body["temperature"], 0.7)
        self.assertEqual(sent["headers"].get("Authorization"), "Bearer sk-anthropic")
        self.assertEqual(headers.get("x-model-scheduler"), "claude-3-5-sonnet@anthropic")

    def test_route_rewrite_and_base_url_without_v1(self):
        router = FakeRouter(_decision("deepseek-chat", "deepseek"))
        app = self._make_app(BASE_CONFIG, router=router)
        transport = FakeTransport()
        app.transport = transport
        base = self._serve_app(app)

        status, _, _ = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "temperature": 0.2, "max_tokens": 99},
        )
        self.assertEqual(status, 200)
        sent = transport.calls[0]
        self.assertEqual(sent["url"], "https://api.deepseek.com/v1/chat/completions")
        sent_body = json.loads(sent["body"].decode("utf-8"))
        self.assertEqual(sent_body["model"], "deepseek-chat")
        self.assertEqual(sent_body["temperature"], 0.2)
        self.assertEqual(sent_body["max_tokens"], 99)
        self.assertEqual(sent["headers"].get("Authorization"), "Bearer sk-deepseek")

    def test_provider_not_configured_returns_503_without_forwarding(self):
        router = FakeRouter(_decision("unknown-model", "unknown"))
        app = self._make_app(BASE_CONFIG, router=router)
        transport = FakeTransport()
        app.transport = transport
        base = self._serve_app(app)

        status, _, body = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(status, 503)
        self.assertEqual(len(transport.calls), 0)
        err = json.loads(body.decode("utf-8"))
        self.assertIn("provider not configured", err["error"]["message"])
        self.assertEqual(err["error"]["reason"], "test reason")

    def test_success_records_call_and_response_header(self):
        router = FakeRouter(_decision("gemini-2.0-flash", "google", cost="free"))
        app = self._make_app(BASE_CONFIG, router=router)
        transport = FakeTransport(status=200, body=b'{"ok": true}')
        app.transport = transport
        base = self._serve_app(app)

        status, headers, _ = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-model-scheduler"), "gemini-2.0-flash@google")
        self.assertEqual(app.quota.quota_left("gemini-2.0-flash", "google"), 1499)

    def test_http_4xx_records_failure_and_preserves_upstream_body(self):
        router = FakeRouter(_decision("gemini-2.0-flash", "google"))
        app = self._make_app(BASE_CONFIG, router=router)
        transport = FakeTransport(status=429, body=b'{"error":"quota exceeded"}')
        app.transport = transport
        base = self._serve_app(app)

        status, _, body = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(status, 429)
        self.assertIn(b"quota exceeded", body)
        self.assertGreater(app.quota.cooldown_seconds_left("gemini-2.0-flash", "google"), 0)
        # 失败不应扣减免费额度。
        self.assertEqual(app.quota.quota_left("gemini-2.0-flash", "google"), 1500)

    def test_connection_error_returns_502_and_records_failure(self):
        class ExplodingTransport:
            def __call__(self, url, headers, body, timeout):
                raise OSError("connection refused")

        router = FakeRouter(_decision("gemini-2.0-flash", "google"))
        app = self._make_app(BASE_CONFIG, router=router)
        app.transport = ExplodingTransport()
        base = self._serve_app(app)

        status, _, body = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(status, 502)
        self.assertIn(b"upstream unreachable", body)
        self.assertGreater(app.quota.cooldown_seconds_left("gemini-2.0-flash", "google"), 0)

    def test_models_endpoint_excludes_cooldown_and_exhausted(self):
        config = copy.deepcopy(BASE_CONFIG)
        config["models"] = {"gemini-2.0-flash@google": {"quota_per_window": 1}}
        self._write_policy(config)
        policy = ModelPolicy(self.state_dir)
        quota_tracker = QuotaTracker(self.state_dir, policy_store=policy)
        router = ModelRouter(policy_store=policy, quota_tracker=quota_tracker)
        app = ProxyApp(
            state_dir=self.state_dir,
            policy_store=policy,
            quota_tracker=quota_tracker,
            router=router,
        )

        # 耗尽 gemini 免费额度，并把 deepseek 放入冷却期。
        quota_tracker.record_call("gemini-2.0-flash", "google")
        quota_tracker.record_failure("deepseek-chat", "deepseek")

        base = self._serve_app(app)
        status, _, body = self._get_json(base, "/v1/models")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))["data"]
        ids = [item["id"] for item in data]
        self.assertNotIn("gemini-2.0-flash@google", ids)
        self.assertNotIn("deepseek-chat@deepseek", ids)
        self.assertIn("gpt-4o@openai", ids)
        self.assertIn("claude-3-5-sonnet@anthropic", ids)
        for item in data:
            self.assertEqual(item["object"], "model")
            self.assertTrue(item["owned_by"])

    def test_health_returns_ok(self):
        app = self._make_app(BASE_CONFIG)
        base = self._serve_app(app)
        status, _, body = self._get_json(base, "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8")), {"status": "ok", "version": "0.2.4"})

    def test_api_key_env_reference_resolved(self):
        config = copy.deepcopy(BASE_CONFIG)
        config["providers"]["openai"]["api_key"] = "env:MS_SERVER_TEST_KEY"
        router = FakeRouter(_decision("gpt-4o", "openai"))
        app = self._make_app(config, router=router)
        transport = FakeTransport()
        app.transport = transport
        base = self._serve_app(app)

        with mock.patch.dict(os.environ, {"MS_SERVER_TEST_KEY": "secret-env"}):
            status, _, _ = self._post_json(
                base,
                {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
            )
        self.assertEqual(status, 200)
        self.assertEqual(transport.calls[0]["headers"].get("Authorization"), "Bearer secret-env")

    def test_api_key_direct_string_used_as_is(self):
        config = copy.deepcopy(BASE_CONFIG)
        config["providers"]["openai"]["api_key"] = "sk-direct"
        router = FakeRouter(_decision("gpt-4o", "openai"))
        app = self._make_app(config, router=router)
        transport = FakeTransport()
        app.transport = transport
        base = self._serve_app(app)

        status, _, _ = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(transport.calls[0]["headers"].get("Authorization"), "Bearer sk-direct")

    def test_stream_mode_uses_stream_opener_and_transparent_sse(self):
        router = FakeRouter(_decision("gpt-4o", "openai"))
        app = self._make_app(BASE_CONFIG, router=router)
        opener = FakeStreamOpener(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            chunks=[
                b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                b"data: [DONE]\n\n",
            ],
        )
        app.stream_opener = opener
        base = self._serve_app(app)

        status, headers, body = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-model-scheduler"), "gpt-4o@openai")
        self.assertIn(b"data: [DONE]", body)
        sent = opener.calls[0]
        self.assertEqual(json.loads(sent["body"].decode("utf-8"))["model"], "gpt-4o")

    def test_stream_http_error_before_sse_preserves_status_and_records_failure(self):
        router = FakeRouter(_decision("gpt-4o", "openai"))
        app = self._make_app(BASE_CONFIG, router=router)
        opener = FakeStreamOpener(
            status=429,
            headers={"Content-Type": "application/json"},
            chunks=[b'{"error":"quota exceeded"}'],
        )
        app.stream_opener = opener
        base = self._serve_app(app)

        status, _, body = self._post_json(
            base,
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True},
        )
        self.assertEqual(status, 429)
        self.assertIn(b"quota exceeded", body)
        self.assertGreater(app.quota.cooldown_seconds_left("gpt-4o", "openai"), 0)

    def test_malformed_json_returns_400(self):
        app = self._make_app(BASE_CONFIG)
        base = self._serve_app(app)
        status, _, body = self._post_raw(base, b"not-json")
        self.assertEqual(status, 400)
        self.assertIn(b"invalid JSON body", body)

    def test_build_chat_completions_url_variants(self):
        self.assertEqual(
            build_chat_completions_url("https://api.openai.com/v1"),
            "https://api.openai.com/v1/chat/completions",
        )
        self.assertEqual(
            build_chat_completions_url("https://api.openai.com/v1/"),
            "https://api.openai.com/v1/chat/completions",
        )
        self.assertEqual(
            build_chat_completions_url("https://api.deepseek.com"),
            "https://api.deepseek.com/v1/chat/completions",
        )
        self.assertEqual(
            build_chat_completions_url("https://generativelanguage.googleapis.com/v1beta/openai"),
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        )

    def test_policy_provider_methods(self):
        self._write_policy(BASE_CONFIG)
        policy = ModelPolicy(self.state_dir)
        providers = policy.get_providers()
        self.assertIn("openai", providers)
        self.assertEqual(providers["openai"]["api_key"], "sk-openai")
        self.assertEqual(policy.provider_config("openai")["base_url"], "https://api.openai.com/v1")
        self.assertIsNone(policy.provider_config("nope"))
        self.assertTrue(policy.has_provider("openai"))
        self.assertFalse(policy.has_provider("nope"))

    def test_missing_messages_passes_empty_text_to_router(self):
        router = FakeRouter(_decision("gpt-4o", "openai"))
        app = self._make_app(BASE_CONFIG, router=router)
        transport = FakeTransport()
        app.transport = transport
        base = self._serve_app(app)

        status, _, _ = self._post_json(base, {"model": "auto"})
        self.assertEqual(status, 200)
        self.assertEqual(router.calls[0][0], "")

    def test_body_fields_are_preserved_after_rewrite(self):
        router = FakeRouter(_decision("gpt-4o", "openai"))
        app = self._make_app(BASE_CONFIG, router=router)
        transport = FakeTransport()
        app.transport = transport
        base = self._serve_app(app)

        payload = {
            "model": "auto",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.3,
            "max_tokens": 123,
            "top_p": 0.9,
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
            "response_format": {"type": "json_object"},
        }
        status, _, _ = self._post_json(base, payload)
        self.assertEqual(status, 200)
        sent_body = json.loads(transport.calls[0]["body"].decode("utf-8"))
        self.assertEqual(sent_body["model"], "gpt-4o")
        self.assertEqual(sent_body["temperature"], 0.3)
        self.assertEqual(sent_body["max_tokens"], 123)
        self.assertEqual(sent_body["top_p"], 0.9)
        self.assertEqual(sent_body["tools"], payload["tools"])
        self.assertEqual(sent_body["response_format"], payload["response_format"])


if __name__ == "__main__":
    unittest.main()

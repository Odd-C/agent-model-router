"""model_scheduler.server — OpenAI 兼容代理层（零依赖，纯标准库）。

用法：
    python -m model_scheduler.server --config model-policy.json --host 127.0.0.1 --port 8765

技术栈仅使用标准库：http.server.ThreadingHTTPServer + urllib.request。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .policy import ModelPolicy
from .quota import QuotaTracker
from .router import ModelRouter, format_model_key

__version__ = "0.2.0"

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 120.0
DEFAULT_STATE_DIR_NAME = ".model-scheduler"

# 转发给上游时需要丢弃/重写的逐跳头或由代理负责生成的头。
_HOP_BY_HOP_HEADERS = {
    "host",
    "content-length",
    "authorization",
    "connection",
    "accept-encoding",
    "transfer-encoding",
    "content-type",
}


class _ConfigModelPolicy(ModelPolicy):
    """支持从任意路径读取 model-policy.json 的 ModelPolicy。

    state_dir 仍用于额度/冷却状态文件；policy_path 可单独指定，
    便于 --config 指向 state_dir 之外的策略文件。
    """

    def __init__(self, state_dir: str | Path, config_path: str | Path) -> None:
        super().__init__(state_dir)
        self._config_path = Path(config_path).expanduser()

    @property
    def policy_path(self) -> Path:
        return self._config_path


def _headers_dict(resp) -> dict:
    """把 urllib 响应头转成普通 dict（保留原始大小写 key）。"""
    out = {}
    for key, value in resp.headers.items():
        out[key] = value
    return out


def _first_header(headers: dict | None, name: str) -> str | None:
    """不区分大小写读取响应头。"""
    if not headers:
        return None
    for key, value in headers.items():
        if str(key).lower() == str(name).lower():
            return str(value)
    return None


def _as_bytes(data) -> bytes:
    """把传输层返回的 chunk 统一为 bytes。"""
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    if data is None:
        return b""
    return str(data).encode("utf-8")


def _chunk_iter(chunks):
    """兼容 bytes / str / 可迭代对象 三种流式返回形态。"""
    if chunks is None:
        return ()
    if isinstance(chunks, (bytes, str)):
        return (chunks,)
    return chunks


def _join_chunks(chunks) -> bytes:
    """把流式 opener 返回的 chunks 全部读成 bytes（仅用于 4xx/5xx 错误场景）。"""
    parts = []
    for chunk in _chunk_iter(chunks):
        data = _as_bytes(chunk)
        if data:
            parts.append(data)
    return b"".join(parts)


def _default_transport(url, headers, body, timeout):
    """默认非流式转发实现。返回 (status, headers, body_bytes)。

    连接错误/超时统一返回 502，由上层记录失败冷却。
    """
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _headers_dict(resp), resp.read()
    except urllib.error.HTTPError as exc:
        # 4xx/5xx：保留上游状态码与错误 body。
        return exc.code, _headers_dict(exc), exc.read()
    except Exception as exc:
        logger.warning("upstream unreachable (%s): %s", url, exc)
        payload = json.dumps(
            {
                "error": {
                    "message": "upstream unreachable",
                    "type": "model_scheduler.upstream_unreachable",
                    "detail": str(exc),
                }
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return 502, {"Content-Type": "application/json"}, payload


def _default_stream_opener(url, headers, body, timeout):
    """默认流式转发实现。返回 (status, headers, chunks_iterable)。

    4xx/5xx 读完整错误 body 后返回；连接错误直接抛出，由上层统一处理。
    """
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return exc.code, _headers_dict(exc), (exc.read(),)

    # 逐行读取上游 SSE，不解析、不缓存流内容。
    def _iter_lines():
        with resp:
            for line in resp:
                yield line

    return resp.status, _headers_dict(resp), _iter_lines()


def extract_messages_text(messages) -> str:
    """把 messages 中所有 content 拼接成一个字符串，作为难度评估输入。

    content 支持 str 与 OpenAI content parts list 两种形态。
    """
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    # 常见 part 字段：text / content / input_text。
                    for field in ("text", "content", "input_text"):
                        value = item.get(field)
                        if isinstance(value, str) and value:
                            parts.append(value)
                            break
    return "\n".join(part for part in parts if part)


def build_chat_completions_url(base_url: str) -> str:
    """根据 provider base_url 构造 /chat/completions 转发地址。

    规则：
    - base_url 已包含 OpenAI 兼容版本路径（路径中含 /v1，如 /v1、/v1beta/openai）：
      直接拼接 /chat/completions
    - 否则自动补 /v1/chat/completions
    """
    base = str(base_url or "").rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    path = urllib.parse.urlparse(base).path
    if "/v1" in path:
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def resolve_api_key(api_key) -> str:
    """解析 provider.api_key。

    支持 env:VAR_NAME 引用环境变量（server 侧读取），也支持直接字符串。
    """
    if not isinstance(api_key, str):
        return ""
    key = api_key.strip()
    if key.startswith("env:"):
        env_name = key[4:].strip()
        if not env_name:
            return ""
        return os.environ.get(env_name, "")
    return key


class ProxyApp:
    """代理层应用对象：持有策略/额度/路由实例与可注入的转发函数。"""

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        config_path: str | Path | None = None,
        policy_store: Any | None = None,
        quota_tracker: Any | None = None,
        router: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Any | None = None,
        stream_opener: Any | None = None,
    ) -> None:
        if state_dir is not None:
            self.state_dir = Path(state_dir).expanduser()
        elif policy_store is not None:
            self.state_dir = Path(getattr(policy_store, "state_dir", Path.home() / DEFAULT_STATE_DIR_NAME))
        else:
            self.state_dir = Path.home() / DEFAULT_STATE_DIR_NAME
        self.state_dir = Path(self.state_dir)

        if policy_store is not None:
            self.policy = policy_store
        else:
            self.policy = _ConfigModelPolicy(
                self.state_dir,
                config_path or (self.state_dir / "model-policy.json"),
            )

        self.quota = quota_tracker or QuotaTracker(state_dir=self.state_dir, policy_store=self.policy)
        self.router = router or ModelRouter(policy_store=self.policy, quota_tracker=self.quota)
        self.timeout = float(timeout)
        self.transport = transport or _default_transport
        self.stream_opener = stream_opener or _default_stream_opener

    def available_models(self) -> list[dict]:
        """返回当前可用模型列表（排除无 provider 配置、冷却中、免费额度耗尽）。"""
        out: list[dict] = []
        for entry in self.policy.list_models():
            model_id = str(entry.get("id") or "").strip()
            provider_name = str(entry.get("provider") or "").strip()
            if not model_id:
                continue
            # 只展示当前可实际转发的模型。
            if provider_name and not self.policy.has_provider(provider_name):
                continue
            try:
                cooldown = self.quota.cooldown_seconds_left(model_id, provider_name)
            except Exception:
                cooldown = 0.0
            if cooldown > 0:
                continue
            if str(entry.get("cost") or "").lower() == "free":
                try:
                    left = self.quota.quota_left(model_id, provider_name)
                except Exception:
                    left = -1
                if left <= 0:
                    continue
            key = entry.get("key") or format_model_key(model_id, provider_name)
            out.append({"id": key, "object": "model", "owned_by": provider_name})
        return out


def make_handler(app: ProxyApp):
    """构造绑定指定 ProxyApp 的 BaseHTTPRequestHandler。"""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "model-scheduler/" + __version__

        def log_message(self, fmt, *args):
            try:
                msg = fmt % args
            except Exception:
                msg = str(fmt)
            logger.debug("%s - %s", self.address_string(), msg)

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/v1/health":
                self._handle_health()
            elif path == "/v1/models":
                self._handle_models()
            else:
                self._send_json(
                    404,
                    {"error": {"message": "not found", "type": "model_scheduler.not_found"}},
                )

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/v1/chat/completions":
                self._handle_chat()
            else:
                self._send_json(
                    404,
                    {"error": {"message": "not found", "type": "model_scheduler.not_found"}},
                )

        def _handle_health(self):
            self._send_json(200, {"status": "ok", "version": __version__})

        def _handle_models(self):
            self._send_json(200, {"object": "list", "data": self.app.available_models()})

        def _send_json(self, status, obj, extra_headers=None):
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(str(key), str(value))
            self.end_headers()
            self.wfile.write(raw)

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length <= 0:
                return b""
            return self.rfile.read(length)

        def _forward_headers(self, api_key):
            """只重写 Authorization / Content-Type，其余请求头原样透传。"""
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": "Bearer " + str(api_key or ""),
            }
            for key, value in self.headers.items():
                if key.lower() in _HOP_BY_HOP_HEADERS:
                    continue
                headers[key] = value
            return headers

        def _handle_chat(self):
            raw = self._read_body()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_json(
                    400,
                    {"error": {"message": "invalid JSON body", "type": "model_scheduler.invalid_json"}},
                )
                return
            if not isinstance(payload, dict):
                self._send_json(
                    400,
                    {"error": {"message": "request body must be a JSON object", "type": "model_scheduler.invalid_request"}},
                )
                return

            # 1. 提取 messages 文本，交给调度器做难度/紧急度评估。
            text = extract_messages_text(payload.get("messages"))
            try:
                decision = self.app.router.recommend_for_session(text)
            except Exception:
                logger.exception("routing decision failed")
                self._send_json(
                    500,
                    {"error": {"message": "routing decision failed", "type": "model_scheduler.routing_failed"}},
                )
                return

            decision = dict(decision or {})
            model = str(decision.get("model") or "").strip()
            provider_name = str(decision.get("provider") or "").strip()
            if not model or not provider_name:
                self._send_json(
                    503,
                    {
                        "error": {
                            "message": "no model available",
                            "type": "model_scheduler.no_model",
                            "reason": decision.get("reason", ""),
                        }
                    },
                )
                return

            decision["key"] = decision.get("key") or format_model_key(model, provider_name)

            # 2. 读取 provider 配置；未配置则 503 且不转发。
            provider_cfg = self.app.policy.provider_config(provider_name)
            if not provider_cfg:
                self._send_json(
                    503,
                    {
                        "error": {
                            "message": "provider not configured: " + provider_name,
                            "type": "model_scheduler.provider_not_configured",
                            "reason": decision.get("reason", ""),
                        }
                    },
                )
                return
            base_url = str(provider_cfg.get("base_url") or "").strip()
            if not base_url:
                self._send_json(
                    503,
                    {
                        "error": {
                            "message": "provider base_url is empty: " + provider_name,
                            "type": "model_scheduler.provider_not_configured",
                            "reason": decision.get("reason", ""),
                        }
                    },
                )
                return

            api_key = resolve_api_key(provider_cfg.get("api_key"))
            url = build_chat_completions_url(base_url)

            # 3. 改写 body 的 model 字段，其余字段原样透传。
            payload["model"] = model
            forward_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            forward_headers = self._forward_headers(api_key)

            # 4. 转发到目标 provider。
            if payload.get("stream") is True:
                self._handle_chat_stream(url, forward_headers, forward_body, decision)
            else:
                self._handle_chat_non_stream(url, forward_headers, forward_body, decision)

        def _handle_chat_non_stream(self, url, headers, body, decision):
            try:
                status, upstream_headers, upstream_body = self.app.transport(
                    url, headers, body, self.app.timeout
                )
            except Exception as exc:
                logger.warning("upstream transport failed: %s", exc)
                self.app.quota.record_failure(decision.get("model"), decision.get("provider"))
                self._send_json(
                    502,
                    {
                        "error": {
                            "message": "upstream unreachable",
                            "type": "model_scheduler.upstream_unreachable",
                            "detail": str(exc),
                        }
                    },
                )
                return

            upstream_body = _as_bytes(upstream_body)

            # 5. 成功记账；失败冷却。
            if status >= 400:
                self.app.quota.record_failure(decision.get("model"), decision.get("provider"))
            else:
                self.app.quota.record_call(decision.get("model"), decision.get("provider"))

            content_type = _first_header(upstream_headers, "content-type") or "application/json"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(upstream_body)))
            self.send_header("X-Model-Scheduler", decision.get("key", ""))
            self.end_headers()
            if upstream_body:
                self.wfile.write(upstream_body)

        def _handle_chat_stream(self, url, headers, body, decision):
            try:
                status, upstream_headers, chunks = self.app.stream_opener(
                    url, headers, body, self.app.timeout
                )
            except Exception as exc:
                logger.warning("upstream stream open failed: %s", exc)
                self.app.quota.record_failure(decision.get("model"), decision.get("provider"))
                self._send_json(
                    502,
                    {
                        "error": {
                            "message": "upstream unreachable",
                            "type": "model_scheduler.upstream_unreachable",
                            "detail": str(exc),
                        }
                    },
                )
                return

            if status >= 400:
                # 上游在 SSE 开始前返回 4xx/5xx：保留状态码与错误 body。
                self.app.quota.record_failure(decision.get("model"), decision.get("provider"))
                error_body = _join_chunks(chunks)
                content_type = _first_header(upstream_headers, "content-type") or "application/json"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(error_body)))
                self.send_header("X-Model-Scheduler", decision.get("key", ""))
                self.end_headers()
                if error_body:
                    self.wfile.write(error_body)
                return

            # 2xx：先记账，再开始 SSE 透传。
            self.app.quota.record_call(decision.get("model"), decision.get("provider"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Model-Scheduler", decision.get("key", ""))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            try:
                # 逐块透传 SSE，不解析、不缓存流内容。
                for chunk in _chunk_iter(chunks):
                    data = _as_bytes(chunk)
                    if data:
                        self.wfile.write(data)
                        self.wfile.flush()
            except Exception as exc:
                logger.warning("stream forwarding interrupted: %s", exc)
                self._write_sse_error("stream forwarding interrupted")

        def _write_sse_error(self, message):
            """流已开始后出错：向客户端发送一条错误 SSE 事件后关闭。"""
            try:
                data = json.dumps(
                    {"error": {"message": message, "type": "model_scheduler.stream_error"}},
                    ensure_ascii=False,
                )
                self.wfile.write(("data: " + data + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

    Handler.app = app
    return Handler


def create_server(host: str, port: int, app: ProxyApp) -> ThreadingHTTPServer:
    """创建并返回 ThreadingHTTPServer（未启动 serve_forever）。"""
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd


def _resolve_state_config(args) -> tuple[Path, Path]:
    """解析 CLI 参数，确定 state_dir 与 model-policy.json 路径。"""
    if args.state_dir:
        state_dir = Path(args.state_dir).expanduser()
        config_path = Path(args.config).expanduser() if args.config else state_dir / "model-policy.json"
    elif args.config:
        config_path = Path(args.config).expanduser()
        state_dir = config_path.parent
    else:
        state_dir = Path.home() / DEFAULT_STATE_DIR_NAME
        config_path = state_dir / "model-policy.json"
    return state_dir, config_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m model_scheduler.server",
        description="model-scheduler OpenAI compatible proxy",
    )
    parser.add_argument("--config", default=None, help="model-policy.json 路径（默认：state_dir/model-policy.json）")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口（默认 8765）")
    parser.add_argument("--state-dir", default=None, help="状态目录（默认：config 同目录或 ~/.model-scheduler）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="上游转发超时秒数（默认 120）")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    state_dir, config_path = _resolve_state_config(args)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"model-scheduler: cannot create state dir {state_dir}: {exc}", file=sys.stderr)
        return 2

    policy_store = _ConfigModelPolicy(state_dir, config_path)
    providers = policy_store.get_providers()
    model_count = len(policy_store.get_policy()["models"])

    if not providers:
        print(
            f"model-scheduler: no providers configured in {config_path}; "
            "add a 'providers' section to model-policy.json",
            file=sys.stderr,
        )
        return 2

    app = ProxyApp(state_dir=state_dir, policy_store=policy_store, timeout=args.timeout)
    httpd = create_server(args.host, args.port, app)
    host, port = httpd.server_address[:2]

    print(f"model-scheduler v{__version__} listening on http://{host}:{port}")
    print(f"state_dir={state_dir} config={config_path} providers={len(providers)} models={model_count}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

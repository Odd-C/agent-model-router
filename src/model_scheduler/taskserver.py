"""model_scheduler.taskserver — Opportunistic Scheduling 看板 HTTP 服务（零依赖）。

单文件看板后端 + 内嵌 HTML 前端。仅使用标准库：http.server、json、urllib。
后端复用 TaskStore / TaskScheduler / PreferencesStore / MockExecutor。

用法：
    python -m model_scheduler.taskserver --host 127.0.0.1 --port 8080 [--state-dir PATH]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .executor import MockExecutor
from .policy import configure_state_dir, default_state_dir
from .preferences import PreferencesStore, VALID_MODES
from .scheduler import (
    DEFAULT_BASE_DELAY,
    DEFAULT_DEADLINE_HORIZON,
    DEFAULT_MAX_RETRIES,
    TaskScheduler,
)
from .task import VALID_PRIORITIES, VALID_STATUSES, Task, TaskStore

__version__ = "0.3.1"

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_STATE_DIR = Path.home() / ".hermes" / "webui"
PAGE_SIZE = 20

# 对外 API 返回的 task 字段契约（不包含 payload，避免把大字段塞进列表）。
TASK_FIELDS = (
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
)


def _parse_int(value: str | None, name: str) -> int:
    """把 query 参数解析为非负整数。"""
    text = str(value or "").strip()
    if text == "":
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        parsed = int(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def _parse_deadline(value: Any) -> float | None:
    """解析 deadline：None / ISO 8601 字符串 / epoch 秒。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("deadline must be an ISO 8601 string or epoch seconds")
    if isinstance(value, (int, float)):
        deadline = float(value)
        if not math.isfinite(deadline) or deadline <= 0:
            raise ValueError("deadline must be a positive number or None")
        return deadline
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        # 纯数字字符串按 epoch 秒解析（如 "1700000000"）。
        try:
            return _parse_deadline(float(text))
        except ValueError:
            pass
        try:
            normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                # 无时区 ISO 字符串按本机时区解释，保持 datetime.timestamp() 语义。
                dt = dt.astimezone()
            deadline = dt.timestamp()
        except ValueError as exc:
            raise ValueError(f"invalid deadline: {value!r}") from exc
        if deadline <= 0:
            raise ValueError("deadline must be a positive number or None")
        return deadline
    raise ValueError("deadline must be an ISO 8601 string or epoch seconds")


def _task_to_public(task: Task) -> dict[str, Any]:
    """把后端 Task 转成 API 契约字段。"""
    raw = task.to_dict()
    return {key: raw.get(key) for key in TASK_FIELDS}


class TaskDashboardApp:
    """看板应用：组合 TaskStore / TaskScheduler / PreferencesStore。"""

    def __init__(
        self,
        state_dir: str | Path | None = None,
        executor: Any | None = None,
        *,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_retries: int = DEFAULT_MAX_RETRIES,
        deadline_horizon: float = DEFAULT_DEADLINE_HORIZON,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else default_state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = TaskStore(self.state_dir)
        self.preferences_store = PreferencesStore(self.state_dir)
        self.executor = executor if executor is not None else MockExecutor()
        self.scheduler = TaskScheduler(
            self.store,
            self.executor,
            base_delay=base_delay,
            max_retries=max_retries,
            deadline_horizon=deadline_horizon,
        )

    def list_tasks(
        self,
        status: str | None = None,
        task_type: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[Task]:
        return self.store.list(status=status, task_type=task_type, offset=offset, limit=limit)

    def get_task(self, task_id: str) -> Task | None:
        return self.store.get(task_id)

    def cancel_task(self, task_id: str) -> bool:
        return self.scheduler.cancel(task_id)

    def delete_task(self, task_id: str) -> bool:
        return self.store.remove(task_id) is not None

    def tick(self) -> tuple[list[str], float]:
        now = time.time()
        processed = self.scheduler.tick(now=now)
        return processed, now

    def stats(self) -> dict[str, Any]:
        tasks = self.store.list()
        by_status: dict[str, int] = {status: 0 for status in VALID_STATUSES}
        by_type: dict[str, int] = {}
        total_cost = 0.0
        for task in tasks:
            by_status[task.status] = by_status.get(task.status, 0) + 1
            by_type[task.task_type] = by_type.get(task.task_type, 0) + 1
            total_cost += float(task.cost or 0.0)
        return {
            "total": len(tasks),
            "by_status": by_status,
            "by_type": by_type,
            "total_cost": total_cost,
        }

    def get_preferences(self) -> dict[str, Any]:
        prefs = self.preferences_store.load()
        return {
            "mode": prefs.mode,
            "weights": self.preferences_store.get_effective_weights(),
        }

    def set_preferences_mode(self, mode: str) -> dict[str, Any]:
        self.preferences_store.set_mode(mode)
        return self.get_preferences()


def _first_query(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    return values[0]


def make_handler(app: TaskDashboardApp) -> type[BaseHTTPRequestHandler]:
    """创建绑定 app 的请求处理器。"""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "model-scheduler-taskserver/0.3.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            try:
                self._route(method)
            except Exception as exc:
                logger.exception("request failed: %s %s", method, self.path)
                try:
                    self._send_json(
                        500,
                        {
                            "error": {
                                "message": "internal server error",
                                "type": "model_scheduler.internal_error",
                                "detail": str(exc),
                            }
                        },
                    )
                except Exception:
                    pass

        def _route(self, method: str) -> None:
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            # 页面
            if path == "/" or path == "/index.html":
                if method == "GET":
                    self._handle_page()
                else:
                    self._send_error(405, "method not allowed", "model_scheduler.method_not_allowed")
                return

            parts = [unquote(seg) for seg in path.split("/") if seg]

            if len(parts) == 2 and parts[0] == "api":
                name = parts[1]
                if name == "health":
                    if method == "GET":
                        self._handle_health()
                    else:
                        self._send_error(405, "method not allowed", "model_scheduler.method_not_allowed")
                    return
                if name == "tasks":
                    if method == "GET":
                        self._handle_list_tasks(query)
                    elif method == "POST":
                        self._handle_create_task()
                    else:
                        self._send_error(405, "method not allowed", "model_scheduler.method_not_allowed")
                    return
                if name == "tick":
                    if method == "POST":
                        self._handle_tick()
                    else:
                        self._send_error(405, "method not allowed", "model_scheduler.method_not_allowed")
                    return
                if name == "stats":
                    if method == "GET":
                        self._handle_stats()
                    else:
                        self._send_error(405, "method not allowed", "model_scheduler.method_not_allowed")
                    return
                if name == "preferences":
                    if method == "GET":
                        self._handle_get_preferences()
                    elif method == "PUT":
                        self._handle_put_preferences()
                    else:
                        self._send_error(405, "method not allowed", "model_scheduler.method_not_allowed")
                    return

            if len(parts) == 3 and parts[0] == "api" and parts[1] == "tasks":
                task_id = parts[2]
                if method == "GET":
                    self._handle_get_task(task_id)
                elif method == "DELETE":
                    self._handle_delete_task(task_id)
                else:
                    self._send_error(405, "method not allowed", "model_scheduler.method_not_allowed")
                return

            if len(parts) == 4 and parts[0] == "api" and parts[1] == "tasks" and parts[3] == "cancel":
                if method == "POST":
                    self._handle_cancel_task(parts[2])
                else:
                    self._send_error(405, "method not allowed", "model_scheduler.method_not_allowed")
                return

            self._send_error(404, "not found", "model_scheduler.not_found")

        def _handle_page(self) -> None:
            raw = _PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(raw)

        def _handle_health(self) -> None:
            self._send_json(200, {"status": "ok", "version": __version__})

        def _handle_list_tasks(self, query: dict[str, list[str]]) -> None:
            status = _first_query(query, "status")
            task_type = _first_query(query, "task_type")

            if status is not None and status not in VALID_STATUSES:
                self._send_error(
                    400,
                    f"invalid status: {status!r} (must be one of {list(VALID_STATUSES)})",
                    "model_scheduler.invalid_status",
                )
                return

            try:
                offset = _parse_int(_first_query(query, "offset") or "0", "offset")
                raw_limit = _first_query(query, "limit")
                limit = None if raw_limit is None else _parse_int(raw_limit, "limit")
            except ValueError as exc:
                self._send_error(400, str(exc), "model_scheduler.invalid_query")
                return

            try:
                tasks = self.app.list_tasks(
                    status=status,
                    task_type=task_type,
                    offset=offset,
                    limit=limit,
                )
            except ValueError as exc:
                self._send_error(400, str(exc), "model_scheduler.invalid_query")
                return

            self._send_json(200, {"tasks": [_task_to_public(task) for task in tasks]})

        def _handle_create_task(self) -> None:
            data = self._read_json_object()
            if data is None:
                return

            task_type = data.get("task_type")
            if not isinstance(task_type, str) or not task_type.strip():
                self._send_error(400, "task_type must be a non-empty string", "model_scheduler.invalid_request")
                return

            payload = data.get("payload")
            if not isinstance(payload, dict):
                self._send_error(400, "payload must be a JSON object", "model_scheduler.invalid_request")
                return

            priority = data.get("priority", "normal")
            if priority is None:
                priority = "normal"
            if not isinstance(priority, str) or priority not in VALID_PRIORITIES:
                self._send_error(
                    400,
                    f"invalid priority: {priority!r} (must be one of {list(VALID_PRIORITIES)})",
                    "model_scheduler.invalid_priority",
                )
                return

            try:
                deadline = _parse_deadline(data.get("deadline"))
            except ValueError as exc:
                self._send_error(400, str(exc), "model_scheduler.invalid_deadline")
                return

            try:
                task = self.app.scheduler.submit(
                    task_type=task_type.strip(),
                    payload=payload,
                    priority=priority,
                    deadline=deadline,
                )
            except ValueError as exc:
                self._send_error(400, str(exc), "model_scheduler.invalid_request")
                return

            self._send_json(
                200,
                {
                    "task_id": task.task_id,
                    "status": task.status,
                    "defer_until": task.defer_until,
                },
            )

        def _handle_get_task(self, task_id: str) -> None:
            task = self.app.get_task(task_id)
            if task is None:
                self._send_error(404, "task not found", "model_scheduler.not_found")
                return
            self._send_json(200, _task_to_public(task))

        def _handle_cancel_task(self, task_id: str) -> None:
            ok = self.app.cancel_task(task_id)
            self._send_json(200, {"ok": ok})

        def _handle_delete_task(self, task_id: str) -> None:
            ok = self.app.delete_task(task_id)
            self._send_json(200, {"ok": ok})

        def _handle_tick(self) -> None:
            processed, now = self.app.tick()
            self._send_json(200, {"processed": processed, "now": now})

        def _handle_stats(self) -> None:
            self._send_json(200, self.app.stats())

        def _handle_get_preferences(self) -> None:
            self._send_json(200, self.app.get_preferences())

        def _handle_put_preferences(self) -> None:
            data = self._read_json_object()
            if data is None:
                return
            mode = data.get("mode")
            if not isinstance(mode, str) or mode not in VALID_MODES:
                self._send_error(
                    400,
                    f"invalid mode: {mode!r} (must be one of {list(VALID_MODES)})",
                    "model_scheduler.invalid_mode",
                )
                return
            self._send_json(200, self.app.set_preferences_mode(mode))

        def _read_json_object(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                self._send_error(400, "request body must be a JSON object", "model_scheduler.invalid_json")
                return None
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_error(400, "invalid JSON body", "model_scheduler.invalid_json")
                return None
            if not isinstance(data, dict):
                self._send_error(400, "request body must be a JSON object", "model_scheduler.invalid_request")
                return None
            return data

        def _send_json(self, status: int, obj: dict[str, Any]) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _send_error(self, status: int, message: str, error_type: str) -> None:
            self._send_json(
                status,
                {"error": {"message": message, "type": error_type}},
            )

    Handler.app = app
    return Handler


def create_server(
    host: str,
    port: int,
    state_dir: str | Path | None = None,
    executor: Any | None = None,
    *,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_retries: int = DEFAULT_MAX_RETRIES,
    deadline_horizon: float = DEFAULT_DEADLINE_HORIZON,
) -> ThreadingHTTPServer:
    """创建并返回 ThreadingHTTPServer（未启动 serve_forever）。"""
    if state_dir is None:
        state_dir = default_state_dir()
    app = TaskDashboardApp(
        state_dir=state_dir,
        executor=executor,
        base_delay=base_delay,
        max_retries=max_retries,
        deadline_horizon=deadline_horizon,
    )
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    httpd.app = app
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m model_scheduler.taskserver",
        description="Opportunistic Scheduling 看板 HTTP 服务",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址（默认 {DEFAULT_HOST}）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--state-dir", default=None, help=f"状态目录（默认 {DEFAULT_STATE_DIR}）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    if args.state_dir:
        state_dir = Path(args.state_dir).expanduser()
    else:
        state_dir = DEFAULT_STATE_DIR

    # 通过 configure_state_dir 参数化，让库内 TaskStore()/PreferencesStore()
    # 不传目录时也落到同一 state 目录。
    configure_state_dir(state_dir)

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"model-scheduler: cannot create state dir {state_dir}: {exc}", file=sys.stderr)
        return 2

    httpd = create_server(args.host, args.port, state_dir=state_dir, executor=MockExecutor())
    host, port = httpd.server_address[:2]

    print(f"model-scheduler v{__version__} Opportunistic Scheduling dashboard listening on http://{host}:{port}")
    print(f"state_dir={state_dir}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        httpd.server_close()
    return 0


_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Opportunistic Scheduling</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #f4f6fa;
  --fg: #1e2430;
  --muted: #6b7280;
  --card: #ffffff;
  --border: #dde3ec;
  --accent: #2563eb;
  --accent-contrast: #ffffff;
  --danger: #dc2626;
  --shadow: 0 1px 3px rgba(0, 0, 0, .06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1523;
    --fg: #e5eaf3;
    --muted: #8b93a7;
    --card: #171e2e;
    --border: #26304a;
    --accent: #3b82f6;
    --accent-contrast: #ffffff;
    --danger: #ef4444;
    --shadow: 0 1px 3px rgba(0, 0, 0, .35);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.5;
}
.container { max-width: 1200px; margin: 0 auto; padding: 24px; }
.page-header h1 { margin: 0 0 6px; font-size: 1.9rem; }
.page-header p { margin: 0; color: var(--muted); }
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px;
  margin-top: 18px;
  box-shadow: var(--shadow);
}
.card h2 { margin: 0 0 14px; font-size: 1.05rem; }
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(126px, 1fr));
  gap: 12px;
  margin-top: 18px;
}
.stat-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px 14px;
  box-shadow: var(--shadow);
}
.stat-card .label { font-size: .78rem; color: var(--muted); }
.stat-card .value { font-size: 1.35rem; font-weight: 650; margin-top: 2px; }
.badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: .74rem;
  font-weight: 650;
  white-space: nowrap;
}
.badge.queued { background: #6b7280; color: #fff; }
.badge.deferred { background: #3b82f6; color: #fff; }
.badge.running { background: #10b981; color: #fff; }
.badge.done { background: #047857; color: #fff; }
.badge.failed { background: #ef4444; color: #fff; }
.badge.cancelled { background: #f97316; color: #fff; }
.badge.expired { background: #8b5cf6; color: #fff; }
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-bottom: 14px;
}
.toolbar label { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: .88rem; }
select, input, textarea {
  background: var(--card);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font: inherit;
}
select:focus, input:focus, textarea:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
textarea {
  width: 100%;
  min-height: 110px;
  resize: vertical;
  font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
  font-size: .85rem;
}
button {
  background: var(--accent);
  color: var(--accent-contrast);
  border: none;
  border-radius: 8px;
  padding: 8px 14px;
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
button:hover { filter: brightness(1.05); }
button.secondary { background: var(--border); color: var(--fg); }
button.danger { background: var(--danger); color: #fff; }
button:disabled { opacity: .5; cursor: not-allowed; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: .87rem; }
th, td { text-align: left; padding: 9px 8px; border-bottom: 1px solid var(--border); vertical-align: middle; }
th { color: var(--muted); font-weight: 650; font-size: .76rem; text-transform: uppercase; letter-spacing: .04em; }
tr:hover td { background: rgba(128, 128, 128, .06); }
.task-id {
  display: inline-block;
  max-width: 128px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
  font-size: .82rem;
  color: var(--muted);
  vertical-align: bottom;
}
.err {
  display: inline-block;
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--muted);
  cursor: help;
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.form-grid .full { grid-column: 1 / -1; }
.form-grid label { display: block; margin-bottom: 6px; color: var(--muted); font-size: .88rem; }
.form-grid select, .form-grid input { width: 100%; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
@media (max-width: 860px) {
  .two-col { grid-template-columns: 1fr; }
  .form-grid { grid-template-columns: 1fr; }
}
.message { margin: 14px 0 0; min-height: 1.4em; color: var(--muted); white-space: pre-wrap; }
.message.ok { color: #059669; }
.message.err { color: var(--danger); }
.weights {
  margin-top: 10px;
  font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
  font-size: .78rem;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px;
  white-space: pre-wrap;
}
.footer { margin-top: 24px; color: var(--muted); font-size: .8rem; text-align: center; }
</style>
</head>
<body>
<div class="container">
  <header class="page-header">
    <h1>Opportunistic Scheduling</h1>
    <p>机会型调度：利用空闲资源窗口执行非紧急任务。版本 0.3.1</p>
  </header>

  <section>
    <div class="stats-grid" id="stats-grid">
      <div class="stat-card"><div class="label">加载中</div><div class="value">--</div></div>
    </div>
  </section>

  <section class="card">
    <h2>任务列表</h2>
    <div class="toolbar">
      <label>状态
        <select id="filter-status">
          <option value="">全部</option>
          <option value="queued">queued</option>
          <option value="deferred">deferred</option>
          <option value="running">running</option>
          <option value="done">done</option>
          <option value="failed">failed</option>
          <option value="cancelled">cancelled</option>
          <option value="expired">expired</option>
        </select>
      </label>
      <label>类型
        <select id="filter-type">
          <option value="">全部</option>
          <option value="text">text</option>
          <option value="image">image</option>
          <option value="coding">coding</option>
          <option value="batch">batch</option>
          <option value="maintenance">maintenance</option>
        </select>
      </label>
      <button class="secondary" id="prev-btn" type="button">上一页</button>
      <button class="secondary" id="next-btn" type="button">下一页</button>
      <span id="page-info" style="color:var(--muted);font-size:.85rem;"></span>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>任务 ID</th>
            <th>类型</th>
            <th>优先级</th>
            <th>状态</th>
            <th>attempts</th>
            <th>成本</th>
            <th>最后错误（悬停）</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody id="task-tbody">
          <tr><td colspan="9">加载中...</td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <section class="card">
    <h2>提交任务</h2>
    <form id="submit-form" class="form-grid">
      <div>
        <label for="task-type">任务类型</label>
        <select id="task-type">
          <option value="text">text</option>
          <option value="image">image</option>
          <option value="coding">coding</option>
          <option value="batch">batch</option>
          <option value="maintenance">maintenance</option>
        </select>
      </div>
      <div>
        <label for="task-priority">优先级</label>
        <select id="task-priority">
          <option value="high">high</option>
          <option value="normal" selected>normal</option>
          <option value="low">low</option>
        </select>
      </div>
      <div class="full">
        <label for="task-deadline">deadline（可选，ISO 8601 字符串或 epoch 秒）</label>
        <input id="task-deadline" placeholder="例如 2025-12-31T23:59:59Z 或 1767225599">
      </div>
      <div class="full">
        <label for="task-payload">payload JSON</label>
        <textarea id="task-payload">{
  "prompt": "hello from opportunistic scheduling"
}</textarea>
      </div>
      <div class="full">
        <button type="submit">提交任务</button>
      </div>
    </form>
  </section>

  <div class="two-col">
    <section class="card">
      <h2>调度控制</h2>
      <button id="tick-btn" type="button">手动 tick</button>
    </section>
    <section class="card">
      <h2>偏好设置</h2>
      <div class="toolbar" style="margin-bottom:0;">
        <label>模式
          <select id="pref-mode">
            <option value="quality-first">quality-first</option>
            <option value="cost-first">cost-first</option>
            <option value="latency-first">latency-first</option>
            <option value="balanced" selected>balanced</option>
          </select>
        </label>
        <button id="pref-save" type="button">保存</button>
      </div>
      <div class="weights" id="pref-weights">加载中...</div>
    </section>
  </div>

  <div class="message" id="message"></div>
  <div class="footer">model-scheduler v0.3.1 · Opportunistic Scheduling dashboard · no external resources</div>
</div>

<script>
(function () {
  'use strict';

  var STATUS_LABELS = {
    queued: 'queued',
    deferred: 'deferred',
    running: 'running',
    done: 'done',
    failed: 'failed',
    cancelled: 'cancelled',
    expired: 'expired'
  };

  var state = { status: '', type: '', offset: 0, limit: 20 };

  function $(sel) { return document.querySelector(sel); }

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function setMessage(text, ok) {
    var el = $('#message');
    el.textContent = text || '';
    el.className = 'message ' + (ok === undefined ? '' : (ok ? 'ok' : 'err'));
  }

  function formatTime(epoch) {
    if (!epoch) return '--';
    var date = new Date(Number(epoch) * 1000);
    if (isNaN(date.getTime())) return '--';
    return date.toLocaleString();
  }

  async function api(path, opts) {
    opts = opts || {};
    var init = {};
    for (var key in opts) {
      if (Object.prototype.hasOwnProperty.call(opts, key)) init[key] = opts[key];
    }
    init.headers = init.headers || {};
    if (init.body && !init.headers['Content-Type']) {
      init.headers['Content-Type'] = 'application/json';
    }
    var resp = await fetch(path, init);
    var text = await resp.text();
    var data = {};
    try { data = JSON.parse(text); } catch (_) { data = {}; }
    if (!resp.ok) {
      var msg = 'HTTP ' + resp.status;
      if (data && data.error) {
        msg = typeof data.error === 'string' ? data.error : (data.error.message || JSON.stringify(data.error));
      }
      throw new Error(msg);
    }
    return data;
  }

  async function loadStats() {
    var data = await api('/api/stats');
    var html = '';
    html += '<div class="stat-card"><div class="label">总数</div><div class="value">' + esc(data.total) + '</div></div>';
    html += '<div class="stat-card"><div class="label">总成本</div><div class="value">' + esc(Number(data.total_cost || 0).toFixed(4)) + '</div></div>';
    Object.keys(STATUS_LABELS).forEach(function (status) {
      var count = (data.by_status && data.by_status[status]) || 0;
      html += '<div class="stat-card"><div class="label"><span class="badge ' + esc(status) + '">' + esc(STATUS_LABELS[status]) + '</span></div><div class="value">' + esc(count) + '</div></div>';
    });
    $('#stats-grid').innerHTML = html;
  }

  async function loadTasks() {
    var params = new URLSearchParams();
    if (state.status) params.set('status', state.status);
    if (state.type) params.set('task_type', state.type);
    params.set('offset', String(state.offset));
    params.set('limit', String(state.limit));
    var data = await api('/api/tasks?' + params.toString());
    var tasks = data.tasks || [];
    renderTasks(tasks);
    $('#page-info').textContent = 'offset ' + state.offset + '，每页 ' + state.limit + '，本页 ' + tasks.length + ' 条';
    $('#prev-btn').disabled = state.offset === 0;
    $('#next-btn').disabled = tasks.length < state.limit;
  }

  function renderTasks(tasks) {
    var tbody = $('#task-tbody');
    if (!tasks.length) {
      tbody.innerHTML = '<tr><td colspan="9">暂无任务</td></tr>';
      return;
    }
    tbody.innerHTML = tasks.map(function (task) {
      var lastErrorText = '';
      var lastErrorTitle = '';
      if (task.last_error) {
        lastErrorText = task.last_error.message || JSON.stringify(task.last_error);
        lastErrorTitle = JSON.stringify(task.last_error);
      }
      return '<tr>' +
        '<td><span class="task-id" title="' + esc(task.task_id) + '">' + esc(task.task_id) + '</span></td>' +
        '<td>' + esc(task.task_type) + '</td>' +
        '<td>' + esc(task.priority) + '</td>' +
        '<td><span class="badge ' + esc(task.status) + '">' + esc(STATUS_LABELS[task.status] || task.status) + '</span></td>' +
        '<td>' + esc(task.attempts) + '</td>' +
        '<td>' + esc(Number(task.cost || 0).toFixed(4)) + '</td>' +
        '<td>' + (task.last_error ? '<span class="err" title="' + esc(lastErrorTitle) + '">' + esc(lastErrorText) + '</span>' : '--') + '</td>' +
        '<td>' + esc(formatTime(task.created_at)) + '</td>' +
        '<td><button class="secondary" type="button" data-action="cancel" data-id="' + esc(task.task_id) + '">取消</button> ' +
        '<button class="danger" type="button" data-action="delete" data-id="' + esc(task.task_id) + '">删除</button></td>' +
        '</tr>';
    }).join('');
  }

  async function doCancelTask(taskId) {
    if (!window.confirm('确认取消任务 ' + taskId + ' ?')) return;
    try {
      var result = await api('/api/tasks/' + encodeURIComponent(taskId) + '/cancel', { method: 'POST' });
      setMessage(result.ok ? '任务已取消：' + taskId : '该任务当前状态不可取消', result.ok);
      await Promise.all([loadStats(), loadTasks()]);
    } catch (err) {
      setMessage(err.message, false);
    }
  }

  async function doDeleteTask(taskId) {
    if (!window.confirm('确认删除任务 ' + taskId + ' ?')) return;
    try {
      var result = await api('/api/tasks/' + encodeURIComponent(taskId), { method: 'DELETE' });
      setMessage(result.ok ? '任务已删除：' + taskId : '任务不存在：' + taskId, result.ok);
      await Promise.all([loadStats(), loadTasks()]);
    } catch (err) {
      setMessage(err.message, false);
    }
  }

  async function loadPreferences() {
    var prefs = await api('/api/preferences');
    $('#pref-mode').value = prefs.mode;
    $('#pref-weights').textContent = JSON.stringify(prefs.weights, null, 2);
  }

  async function refreshAll() {
    await Promise.all([loadStats(), loadTasks(), loadPreferences()]);
  }

  function bindEvents() {
    $('#filter-status').addEventListener('change', function () {
      state.status = this.value;
      state.offset = 0;
      loadTasks().catch(function (err) { setMessage(err.message, false); });
    });
    $('#filter-type').addEventListener('change', function () {
      state.type = this.value;
      state.offset = 0;
      loadTasks().catch(function (err) { setMessage(err.message, false); });
    });
    $('#prev-btn').addEventListener('click', function () {
      state.offset = Math.max(0, state.offset - state.limit);
      loadTasks().catch(function (err) { setMessage(err.message, false); });
    });
    $('#next-btn').addEventListener('click', function () {
      state.offset += state.limit;
      loadTasks().catch(function (err) { setMessage(err.message, false); });
    });

    $('#task-tbody').addEventListener('click', function (ev) {
      var button = ev.target.closest('button[data-action]');
      if (!button) return;
      var taskId = button.getAttribute('data-id') || '';
      var action = button.getAttribute('data-action');
      if (action === 'cancel') {
        doCancelTask(taskId).catch(function (err) { setMessage(err.message, false); });
      } else if (action === 'delete') {
        doDeleteTask(taskId).catch(function (err) { setMessage(err.message, false); });
      }
    });

    $('#submit-form').addEventListener('submit', async function (ev) {
      ev.preventDefault();
      var taskType = $('#task-type').value;
      var priority = $('#task-priority').value;
      var deadline = $('#task-deadline').value.trim();
      var payloadText = $('#task-payload').value.trim();
      var payload;
      try {
        payload = JSON.parse(payloadText);
      } catch (_) {
        setMessage('payload 不是合法 JSON', false);
        return;
      }
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        setMessage('payload 必须是 JSON 对象', false);
        return;
      }
      var body = { task_type: taskType, payload: payload, priority: priority };
      if (deadline) body.deadline = deadline;
      try {
        var task = await api('/api/tasks', { method: 'POST', body: JSON.stringify(body) });
        setMessage('任务已创建：' + task.task_id + '（' + (STATUS_LABELS[task.status] || task.status) + '）', true);
        await Promise.all([loadStats(), loadTasks()]);
      } catch (err) {
        setMessage(err.message, false);
      }
    });

    $('#tick-btn').addEventListener('click', async function () {
      try {
        var result = await api('/api/tick', { method: 'POST' });
        setMessage('tick 完成，处理 ' + (result.processed || []).length + ' 个任务', true);
        await Promise.all([loadStats(), loadTasks()]);
      } catch (err) {
        setMessage(err.message, false);
      }
    });

    $('#pref-save').addEventListener('click', async function () {
      var mode = $('#pref-mode').value;
      try {
        var prefs = await api('/api/preferences', { method: 'PUT', body: JSON.stringify({ mode: mode }) });
        $('#pref-weights').textContent = JSON.stringify(prefs.weights, null, 2);
        setMessage('偏好已保存：' + prefs.mode, true);
      } catch (err) {
        setMessage(err.message, false);
      }
    });
  }

  bindEvents();
  refreshAll().catch(function (err) { setMessage(err.message, false); });
})();
</script>
</body>
</html>
"""


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_STATE_DIR",
    "PAGE_SIZE",
    "TaskDashboardApp",
    "create_server",
    "make_handler",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())

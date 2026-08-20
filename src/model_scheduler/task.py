"""model_scheduler.task — 任务模型与 StateStore 持久化。

任务模型保持「干净」：只描述「做什么 + 何时能做 + 优先级」，不描述
「怎么做」。`payload` 是不透明 dict，scheduler 永不解析，只有 Executor 解释。
"""
from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .policy import default_state_dir
from .store import StateStore, create_store

logger = logging.getLogger(__name__)

VALID_PRIORITIES = ("high", "normal", "low")
VALID_STATUSES = (
    "queued",
    "deferred",
    "running",
    "done",
    "failed",
    "cancelled",
    "expired",
)

# 合法状态迁移表（v0.3 定稿）。非法迁移一律 ValueError。
_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "deferred", "cancelled", "expired"}),
    "deferred": frozenset({"queued", "cancelled", "expired"}),
    "running": frozenset({"done", "failed"}),
    "failed": frozenset({"queued", "cancelled"}),
    "done": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
}


def valid_transition(from_status: str, to_status: str) -> bool:
    """返回 from_status -> to_status 是否为合法状态迁移。"""
    from_status = str(from_status or "").strip()
    to_status = str(to_status or "").strip()
    if not from_status or not to_status:
        return False
    return to_status in _TRANSITIONS.get(from_status, frozenset())


@dataclass
class Task:
    """统一任务模型。

    `payload` 不透明：scheduler 只做状态机/时间窗口决策，不解析内容。
    `result` 与 `cost` 由 scheduler 在 Executor 执行成功后回填。
    """

    task_id: str
    task_type: str
    priority: str
    deadline: float | None
    defer_until: float | None
    status: str
    payload: dict
    attempts: int
    last_error: dict | None
    created_at: float
    updated_at: float
    result: dict | None = None
    cost: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 持久化的 dict。"""
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "priority": self.priority,
            "deadline": self.deadline,
            "defer_until": self.defer_until,
            "status": self.status,
            "payload": dict(self.payload or {}),
            "attempts": self.attempts,
            "last_error": dict(self.last_error) if self.last_error is not None else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": dict(self.result) if self.result is not None else None,
            "cost": self.cost,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        """从持久化 dict 还原 Task；结构缺失/非法时抛出 ValueError。"""
        if not isinstance(data, dict):
            raise ValueError("task data must be a dict")
        try:
            task = cls(
                task_id=str(data.get("task_id") or ""),
                task_type=str(data.get("task_type") or ""),
                priority=str(data.get("priority") or ""),
                deadline=_optional_float(data.get("deadline")),
                defer_until=_optional_float(data.get("defer_until")),
                status=str(data.get("status") or ""),
                payload=data.get("payload") if isinstance(data.get("payload"), dict) else {},
                attempts=int(data.get("attempts") or 0),
                last_error=data.get("last_error") if isinstance(data.get("last_error"), dict) else None,
                created_at=float(data.get("created_at") or 0.0),
                updated_at=float(data.get("updated_at") or 0.0),
                result=data.get("result") if isinstance(data.get("result"), dict) else None,
                cost=float(data.get("cost") or 0.0),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid task data: {exc}") from exc
        task.validate()
        return task

    def validate(self) -> None:
        """校验必填字段与字段约束；非法时抛出 ValueError。"""
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if self.priority not in VALID_PRIORITIES:
            raise ValueError(f"invalid priority: {self.priority!r}")
        if self.status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {self.status!r}")
        if not isinstance(self.task_type, str) or not self.task_type.strip():
            raise ValueError("task_type must be a non-empty string")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be a dict")
        for field_name, value in (("deadline", self.deadline), ("defer_until", self.defer_until)):
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{field_name} must be a positive number or None")
                numeric = float(value)
                if not math.isfinite(numeric) or numeric <= 0:
                    raise ValueError(f"{field_name} must be a positive finite number or None")
        if self.last_error is not None:
            if not isinstance(self.last_error, dict):
                raise ValueError("last_error must be a dict or None")
            if "error_type" not in self.last_error or "action_taken" not in self.last_error:
                raise ValueError("last_error must contain error_type and action_taken")


def _optional_float(value: Any) -> float | None:
    """把可空字段规范化为 float 或 None。"""
    if value is None or value == "":
        return None
    return float(value)


def _parse_tasks(raw: str | None) -> dict[str, dict[str, Any]]:
    """把 StateStore 中的任务表 JSON 字符串解析为 dict；异常/非法结构返回空表。"""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        logger.warning("Failed to load model-tasks; treating as empty", exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out[str(key)] = value
    return out


class TaskStore:
    """任务存储。

    存储层统一走 StateStore；默认 backend="json" 时行为与 v0.5 完全
    兼容（状态文件为 state 目录下的 ``model-tasks.json``）。backend="sqlite"
    时使用 ``model-scheduler.db`` 的 kv 表（全新存储，不迁移旧 JSON 文件）。

    并发语义：
      - add/update/remove 通过 ``StateStore.atomic_update`` 完成读改写，
        避免 ``_load()`` + ``_save()`` 两步操作之间的并发丢失。
      - JSON 后端：atomic_update 由 ``threading.Lock`` 保护，仅单进程内安全。
      - SQLite 后端：atomic_update 在 ``BEGIN IMMEDIATE`` 事务内完成，
        借助 sqlite3 文件锁实现跨进程安全（多进程可安全使用）。
    """

    def __init__(self, state_dir: str | Path | None = None, *, backend: str = "json") -> None:
        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else default_state_dir()
        self.backend = str(backend or "json").strip().lower() or "json"
        self.store: StateStore = create_store(self.backend, self.state_dir)
        self.path = self.state_dir / "model-tasks.json"
        self._lock = threading.Lock()

    @property
    def backend_name(self) -> str:
        """当前存储后端名（"json" 或 "sqlite"）。"""
        return self.store.backend_name

    def _load(self) -> dict[str, dict[str, Any]]:
        """通过 StateStore 读取原始任务表；不存在返回空表。"""
        return _parse_tasks(self.store.get("model-tasks"))

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.store.set("model-tasks", json.dumps(data, ensure_ascii=False))

    def add(self, task: Task) -> Task:
        """新增任务；task_id 已存在或初始状态非法时抛出 ValueError。

        使用 ``store.atomic_update`` 完成「读取 -> 存在性检查 -> 写回」，
        与进程内其它写操作以及 SQLite 后端的跨进程写操作互斥。
        """
        task.validate()
        if task.status not in ("queued", "deferred"):
            raise ValueError(f"invalid initial status: {task.status!r} (must be queued or deferred)")

        def mutator(raw: str | None) -> str:
            data = _parse_tasks(raw)
            if task.task_id in data:
                raise ValueError(f"task already exists: {task.task_id}")
            data[task.task_id] = task.to_dict()
            return json.dumps(data, ensure_ascii=False)

        self.store.atomic_update("model-tasks", mutator)
        return task

    def get(self, task_id: str) -> Task | None:
        """按 task_id 读取；不存在返回 None。"""
        task_id = str(task_id or "").strip()
        if not task_id:
            return None
        with self._lock:
            data = self._load()
            raw = data.get(task_id)
            if raw is None:
                return None
            try:
                return Task.from_dict(raw)
            except ValueError:
                logger.warning("Invalid task data for %s; treating as missing", task_id, exc_info=True)
                return None

    def list(
        self,
        status: str | None = None,
        task_type: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[Task]:
        """按状态/类型过滤任务，支持 offset/limit 分页。

        limit=None 表示返回全部；offset/limit 必须非负。
        """
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError("limit must be a non-negative integer or None")
        with self._lock:
            raw = self._load()
        tasks: list[Task] = []
        for raw_task in raw.values():
            try:
                task = Task.from_dict(raw_task)
            except ValueError:
                logger.warning("Skipping invalid task entry in model-tasks.json", exc_info=True)
                continue
            if status is not None and task.status != status:
                continue
            if task_type is not None and task.task_type != task_type:
                continue
            tasks.append(task)
        tasks.sort(key=lambda t: (t.created_at, t.task_id))
        if limit is None:
            return tasks[offset:]
        return tasks[offset:offset + limit]

    def update(self, task: Task) -> Task:
        """更新任务；task_id 不存在时抛出 KeyError，非法状态迁移抛 ValueError。

        存在性检查与迁移校验在 atomic_update 的 mutator 内完成，避免
        并发 tick / cancel 之间出现 list -> update 的丢失更新或双执行。
        """
        task.validate()

        def mutator(raw: str | None) -> str:
            data = _parse_tasks(raw)
            if task.task_id not in data:
                raise KeyError(f"task not found: {task.task_id}")
            old_status = data[task.task_id].get("status")
            if not valid_transition(old_status, task.status):
                raise ValueError(f"invalid transition: {old_status} -> {task.status}")
            data[task.task_id] = task.to_dict()
            return json.dumps(data, ensure_ascii=False)

        self.store.atomic_update("model-tasks", mutator)
        return task

    def remove(self, task_id: str) -> Task | None:
        """删除任务并返回被删除的 Task；不存在返回 None。"""
        task_id = str(task_id or "").strip()
        if not task_id:
            return None

        removed: dict[str, Any] = {}

        def mutator(raw: str | None) -> str | None:
            data = _parse_tasks(raw)
            raw_task = data.pop(task_id, None)
            if raw_task is None:
                return None
            removed["task"] = raw_task
            return json.dumps(data, ensure_ascii=False)

        self.store.atomic_update("model-tasks", mutator)

        raw_task = removed.get("task")
        if raw_task is None:
            return None
        try:
            return Task.from_dict(raw_task)
        except ValueError:
            logger.warning("Removed invalid task data for %s", task_id, exc_info=True)
            return None


__all__ = [
    "VALID_PRIORITIES",
    "VALID_STATUSES",
    "Task",
    "TaskStore",
    "valid_transition",
]

"""model_scheduler.scheduler — 单进程任务调度器（v0.3 后端核心）。

职责：最小 defer 决策 + 轮询循环。不做并发 worker、不做分布式。
scheduler 只做「任务 + 模型 + 时间窗口」的状态机调度；模型选择/打分
在 v0.4 接入，本版本预留 policy/preferences 参数。
"""
from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from typing import Any

from .executor import Executor, ExecutorResult
from .task import Task, TaskStore, valid_transition

logger = logging.getLogger(__name__)

DEFAULT_BASE_DELAY = 300.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_DEADLINE_HORIZON = 3600.0  # 1 小时内到期视为紧急，立即排队
PRIORITY_WEIGHTS = {"high": 0.0, "normal": 1.0, "low": 2.0}

# v0.4 降级矩阵：error_type -> 失败路径 action_taken。
# 只改变 last_error.action_taken 的记录语义；状态机、defer 决策与
# max_retries 重试上限保持 v0.3 行为不变。
DEGRADATION_MATRIX = {
    "invalid_payload": "abort",
    "auth_error": "abort",
    "invalid_request": "abort",
    "rate_limit": "cooldown_retry",
    "server_error": "retry_then_fallback",
    "transport_error": "retry_then_fallback",
    "timeout": "retry_then_fallback",
    "model_not_found": "fallback",
}


def decide_action(error_type: str) -> str:
    """根据 error_type 返回降级矩阵规定的 action_taken。

    未识别的 error_type 保守返回 "abort"（不重试）。
    """
    key = str(error_type or "").strip().lower()
    return DEGRADATION_MATRIX.get(key, "abort")


def _normalise_now(now: float | None) -> float:
    """把 None / datetime / epoch 统一成 epoch 秒。"""
    if now is None:
        return time.time()
    if hasattr(now, "timestamp"):
        return float(now.timestamp())  # type: ignore[union-attr]
    return float(now)


class TaskScheduler:
    """最小调度器：defer 决策 + tick 轮询循环。

    - submit：priority=high 或 deadline 1 小时内 -> 立即 queued；
      否则 deferred，defer_until = now + base_delay × priority_weight。
    - tick：deferred 到点转 queued；queued 任务立即执行并写回结果。
    - loop：阻塞式轮询循环，供集成方起后台线程使用。
    """

    def __init__(
        self,
        store: TaskStore,
        executor: Executor,
        policy: Any | None = None,
        preferences: Any | None = None,
        *,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_retries: int = DEFAULT_MAX_RETRIES,
        deadline_horizon: float = DEFAULT_DEADLINE_HORIZON,
    ) -> None:
        if not isinstance(store, TaskStore):
            raise TypeError("store must be a TaskStore")
        if executor is None:
            raise TypeError("executor is required")
        self.store = store
        self.executor = executor
        self.policy = policy
        self.preferences = preferences
        self.base_delay = float(base_delay)
        self.max_retries = int(max_retries)
        self.deadline_horizon = float(deadline_horizon)
        if not math.isfinite(self.base_delay) or self.base_delay < 0:
            raise ValueError("base_delay must be a finite non-negative number")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if not math.isfinite(self.deadline_horizon) or self.deadline_horizon < 0:
            raise ValueError("deadline_horizon must be a finite non-negative number")
        self._tick_lock = threading.Lock()

    def submit(
        self,
        task_type: str,
        payload: dict,
        priority: str = "normal",
        deadline: float | None = None,
    ) -> Task:
        """创建任务并决定立即执行（queued）、defer（deferred）或 expired。"""
        now = time.time()
        priority = str(priority or "").strip()
        if priority not in ("high", "normal", "low"):
            raise ValueError(f"invalid priority: {priority!r}")

        if deadline is not None:
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
                raise ValueError("deadline must be a number or None")
            deadline = float(deadline)
            if not math.isfinite(deadline) or deadline <= 0:
                raise ValueError("deadline must be a positive finite number or None")

        defer_until: float | None
        expired = deadline is not None and deadline <= now
        if expired:
            # 已过期：不排队、不 defer，最终状态置 expired。
            # TaskStore.add 只接受 queued/deferred 初始状态，因此先以 queued
            # 落盘，再按合法状态迁移 queued -> expired 持久化。
            status = "queued"
            defer_until = None
        elif priority == "high":
            status = "queued"
            defer_until = None
        elif deadline is not None and 0 < deadline - now <= self.deadline_horizon:
            status = "queued"
            defer_until = None
        else:
            status = "deferred"
            defer_until = now + self.base_delay * PRIORITY_WEIGHTS[priority]

        task = Task(
            task_id=uuid.uuid4().hex,
            task_type=str(task_type or ""),
            priority=priority,
            deadline=deadline,
            defer_until=defer_until,
            status=status,
            payload=dict(payload or {}),
            attempts=0,
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        task.validate()
        self.store.add(task)
        if expired:
            task.status = "expired"
            task.updated_at = now
            self.store.update(task)
        return task

    def tick(self, now: float | None = None) -> list[str]:
        """单轮调度。

        1) deferred 到点 -> queued；
        2) 遍历 queued -> 执行 -> done/failed（按重试上限回 queued，超限 cancelled）。
        返回本轮处理过的任务 id 列表（含被转 queued 的 deferred 任务）。

        单进程内由 ``_tick_lock`` 串行化，避免两个线程同时 tick 时
        list -> update 序列交叉导致非法迁移或重复执行；跨进程并发写由
        TaskStore 的 SQLite atomic_update 兜底。
        """
        with self._tick_lock:
            return self._tick_locked(now)

    def _tick_locked(self, now: float | None = None) -> list[str]:
        """tick 的主体逻辑；调用方必须持有 ``_tick_lock``。"""
        now = _normalise_now(now)
        processed: list[str] = []

        # 0. 到期的 queued/deferred 任务置 expired。
        #    统一使用 deadline <= now：deadline 恰为 now 时视为已过期。
        for task in self.store.list(status="queued", limit=None):
            if task.deadline is not None and float(task.deadline) <= now:
                task.status = "expired"
                task.updated_at = now
                self.store.update(task)
                processed.append(task.task_id)
        for task in self.store.list(status="deferred", limit=None):
            if task.deadline is not None and float(task.deadline) <= now:
                task.status = "expired"
                task.updated_at = now
                self.store.update(task)
                processed.append(task.task_id)

        # 1. 到点的 deferred 任务转 queued。
        for task in self.store.list(status="deferred", limit=None):
            defer_until = float(task.defer_until or 0.0)
            if defer_until <= now:
                if not valid_transition(task.status, "queued"):
                    continue
                task.status = "queued"
                task.defer_until = None
                task.updated_at = now
                self.store.update(task)
                processed.append(task.task_id)

        # 2. 遍历 queued，立即执行。
        for task in self.store.list(status="queued", limit=None):
            if task.task_id not in processed:
                processed.append(task.task_id)

            # queued -> running
            if not valid_transition(task.status, "running"):
                continue
            task.status = "running"
            task.updated_at = now
            self.store.update(task)

            try:
                exec_result = self.executor.execute(task)
                if not isinstance(exec_result, ExecutorResult):
                    exec_result = ExecutorResult(result={"result": exec_result}, cost=0.0, error=None)
            except Exception as exc:
                logger.warning("executor raised for task %s: %s", task.task_id, exc)
                exec_result = ExecutorResult(
                    result={},
                    cost=0.0,
                    error={"error_type": "executor_exception", "status": None, "message": str(exc)},
                )

            task.updated_at = now

            if exec_result.error is None:
                # running -> done
                if valid_transition(task.status, "done"):
                    task.status = "done"
                    task.result = dict(exec_result.result or {})
                    task.cost = float(exec_result.cost or 0.0)
                    task.last_error = None
                self.store.update(task)
                continue

            # running -> failed（先持久化 failed，再做 failed 的后续迁移）
            if valid_transition(task.status, "failed"):
                task.status = "failed"
            task.attempts += 1
            error = exec_result.error if isinstance(exec_result.error, dict) else {"message": str(exec_result.error)}
            error_type = str(error.get("error_type") or "unknown_error")
            task.last_error = {
                "error_type": error_type,
                "status": error.get("status"),
                "action_taken": decide_action(error_type),
                "message": str(error.get("message") or ""),
            }
            task.result = None
            task.cost = 0.0
            self.store.update(task)

            # failed -> queued（重试上限内）/ cancelled（超限）
            if task.attempts <= self.max_retries:
                if valid_transition(task.status, "queued"):
                    task.status = "queued"
            else:
                if valid_transition(task.status, "cancelled"):
                    task.status = "cancelled"
                else:
                    task.status = "cancelled"
            task.updated_at = now
            self.store.update(task)

        return processed

    def run_once(self, now: float | None = None) -> list[str]:
        """tick 的别名。"""
        return self.tick(now=now)

    def loop(self, interval: float = 5, stop_event: threading.Event | None = None) -> None:
        """阻塞式轮询循环；interval 为轮询间隔（秒）。"""
        interval = float(interval)
        if interval < 0:
            raise ValueError("interval must be non-negative")
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                self.tick()
            except Exception:
                logger.exception("scheduler tick failed")
            if stop_event is not None:
                if stop_event.wait(interval):
                    return
            else:
                time.sleep(interval)

    def cancel(self, task_id: str) -> bool:
        """取消任务。仅 queued/deferred/failed 可取消；返回是否取消成功。"""
        task = self.store.get(task_id)
        if task is None:
            return False
        if not valid_transition(task.status, "cancelled"):
            return False
        task.status = "cancelled"
        task.updated_at = time.time()
        task.defer_until = None
        self.store.update(task)
        return True


__all__ = [
    "DEFAULT_BASE_DELAY",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_DEADLINE_HORIZON",
    "DEGRADATION_MATRIX",
    "PRIORITY_WEIGHTS",
    "TaskScheduler",
    "decide_action",
]

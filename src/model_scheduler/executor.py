"""model_scheduler.executor — Executor 执行器接口与通用示例实现。

scheduler 只依赖 Executor Protocol；Ruya 等私有 Executor 不进本仓库。
CommandExecutor 是零依赖通用示例：payload 形如 ``{"command": ["echo", "hi"]}``。
安全约束：永远 shell=False，command 必须为非空 list。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .task import Task


@dataclass
class ExecutorResult:
    """Executor 执行结果。

    ``error`` 为 None 表示成功；否则为失败，结构与 Task.last_error 对齐：
    ``{error_type, status, message}``。
    """

    result: dict
    cost: float = 0.0
    error: dict | None = None


@runtime_checkable
class Executor(Protocol):
    """Executor 协议：scheduler 只依赖 execute(task) -> ExecutorResult。"""

    def execute(self, task: Task) -> ExecutorResult:
        """执行任务并返回结果；永不抛异常（异常应转成 ExecutorResult.error）。"""
        ...


class MockExecutor:
    """测试/演示用执行器。

    默认返回固定成功结果；可通过构造参数让它按 payload 触发失败：
      MockExecutor(error={...})  # 固定失败
      payload={"fail": True}    # 按 payload 触发失败
    """

    def __init__(
        self,
        result: dict | None = None,
        cost: float = 0.0,
        error: dict | None = None,
    ) -> None:
        self.result = dict(result) if result is not None else {"mock": True}
        self.cost = float(cost)
        self.error = dict(error) if error is not None else None

    def execute(self, task: Task) -> ExecutorResult:
        if self.error is not None:
            return ExecutorResult(result=dict(self.result), cost=self.cost, error=dict(self.error))
        payload = task.payload or {}
        if isinstance(payload, dict) and payload.get("fail"):
            return ExecutorResult(
                result=dict(self.result),
                cost=self.cost,
                error={"error_type": "mock_error", "status": None, "message": "mock failure triggered by payload"},
            )
        return ExecutorResult(result=dict(self.result), cost=self.cost)


class CommandExecutor:
    """通用示例执行器：执行 payload["command"]。

    - 命令必须为 list，且非空；所有元素必须为 str。
    - 永远 shell=False。
    - 成功：result={"exit_code": 0, "stdout": ..., "stderr": ...}。
    - 非零退出：error={"error_type": "command_failed", "status": exit_code, "message": stderr/stdout 摘要}。
    - 异常：error={"error_type": "command_error", "status": None, "message": str(exc)}。
    """

    def __init__(self, timeout: float = 120.0) -> None:
        self.timeout = float(timeout)
        if self.timeout < 0:
            raise ValueError("timeout must be non-negative")

    def execute(self, task: Task) -> ExecutorResult:
        payload = task.payload if isinstance(task.payload, dict) else {}
        command = payload.get("command")
        if not isinstance(command, list) or not command:
            return ExecutorResult(
                result={},
                cost=0.0,
                error={
                    "error_type": "invalid_payload",
                    "status": None,
                    "message": "payload.command must be a non-empty list",
                },
            )
        if not all(isinstance(part, str) for part in command):
            return ExecutorResult(
                result={},
                cost=0.0,
                error={
                    "error_type": "invalid_payload",
                    "status": None,
                    "message": "payload.command must contain only strings",
                },
            )
        try:
            completed = subprocess.run(
                command,
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except Exception as exc:
            return ExecutorResult(
                result={},
                cost=0.0,
                error={"error_type": "command_error", "status": None, "message": str(exc)},
            )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "").strip()
            return ExecutorResult(
                result={},
                cost=0.0,
                error={
                    "error_type": "command_failed",
                    "status": completed.returncode,
                    "message": message[:2000] or f"command exited with code {completed.returncode}",
                },
            )
        return ExecutorResult(
            result={
                "exit_code": completed.returncode,
                "stdout": completed.stdout or "",
                "stderr": completed.stderr or "",
            },
            cost=0.0,
        )


__all__ = ["ExecutorResult", "Executor", "MockExecutor", "CommandExecutor"]

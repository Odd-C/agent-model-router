"""agent_model_router.preferences — v0.3 偏好分级权重配置。

默认权重表是契约：六个分项 + 四个 mode 档位，代码定死；只能通过
preferences.json 覆盖数值，不能发明新档位/新分项。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .policy import atomic_write_json, default_state_dir

logger = logging.getLogger(__name__)

VALID_MODES = ("quality-first", "cost-first", "latency-first", "balanced")
WEIGHT_KEYS = (
    "quality_fit",
    "cost_penalty",
    "latency_penalty",
    "failure_risk",
    "quota_pressure",
    "deadline_pressure",
)

DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    "quality-first": {
        "quality_fit": 3.0,
        "cost_penalty": 1.0,
        "latency_penalty": 1.0,
        "failure_risk": 1.0,
        "quota_pressure": 1.0,
        "deadline_pressure": 1.0,
    },
    "cost-first": {
        "quality_fit": 1.0,
        "cost_penalty": 3.0,
        "latency_penalty": 1.0,
        "failure_risk": 1.0,
        "quota_pressure": 1.0,
        "deadline_pressure": 1.0,
    },
    "latency-first": {
        "quality_fit": 1.0,
        "cost_penalty": 1.0,
        "latency_penalty": 3.0,
        "failure_risk": 1.0,
        "quota_pressure": 1.0,
        "deadline_pressure": 1.0,
    },
    "balanced": {
        "quality_fit": 1.0,
        "cost_penalty": 1.0,
        "latency_penalty": 1.0,
        "failure_risk": 1.0,
        "quota_pressure": 1.0,
        "deadline_pressure": 1.0,
    },
}


@dataclass
class Preferences:
    """偏好配置：mode + 覆盖权重表（覆盖 DEFAULT_WEIGHTS 中对应数值）。"""

    mode: str = "balanced"
    weights: dict[str, float] | None = None

    def __post_init__(self) -> None:
        if self.weights is None:
            self.weights = {}
        self.validate()

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "weights": dict(self.weights or {})}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Preferences":
        if not isinstance(data, dict):
            raise ValueError("preferences data must be a dict")
        mode = str(data.get("mode") or "balanced")
        raw_weights = data.get("weights")
        if raw_weights is None:
            raw_weights = {}
        if not isinstance(raw_weights, dict):
            raise ValueError("weights must be a dict")
        weights = {str(k): v for k, v in raw_weights.items()}
        return cls(mode=mode, weights=weights)

    def validate(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"invalid mode: {self.mode!r} (must be one of {VALID_MODES})")
        if not isinstance(self.weights, dict):
            raise ValueError("weights must be a dict")
        for key, value in self.weights.items():
            if key not in WEIGHT_KEYS:
                raise ValueError(f"invalid weight key: {key!r} (must be one of {WEIGHT_KEYS})")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"weight for {key!r} must be a positive number")


class PreferencesStore:
    """preferences.json 持久化。

    文件位于 state 目录下 ``preferences.json``；无文件时 load() 返回默认
    ``Preferences("balanced", {})``。模式/权重校验失败会抛 ValueError。
    """

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else default_state_dir()
        self.path = self.state_dir / "preferences.json"

    def load(self) -> Preferences:
        """读取偏好；文件不存在返回默认。非法内容抛 ValueError。"""
        if not self.path.exists():
            return Preferences(mode="balanced", weights={})
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid preferences.json: {exc}") from exc
        except OSError as exc:
            logger.warning("Failed to read preferences.json; falling back to defaults", exc_info=True)
            return Preferences(mode="balanced", weights={})
        if not isinstance(raw, dict):
            raise ValueError("preferences.json must contain a JSON object")
        return Preferences.from_dict(raw)

    def save(self, prefs: Preferences | None = None) -> Preferences:
        """保存偏好（原子写盘）。prefs 为 None 时保存当前加载的偏好。"""
        if prefs is None:
            prefs = self.load()
        prefs.validate()
        atomic_write_json(self.path, prefs.to_dict())
        return prefs

    def set_mode(self, mode: str) -> Preferences:
        """设置 mode 并持久化。"""
        prefs = self.load()
        prefs.mode = mode
        prefs.validate()
        self.save(prefs)
        return prefs

    def get_effective_weights(self) -> dict[str, float]:
        """返回 mode 对应默认权重 + 覆盖值合并后的有效权重。"""
        prefs = self.load()
        effective = dict(DEFAULT_WEIGHTS[prefs.mode])
        for key, value in (prefs.weights or {}).items():
            effective[key] = value
        return effective


__all__ = [
    "VALID_MODES",
    "WEIGHT_KEYS",
    "DEFAULT_WEIGHTS",
    "Preferences",
    "PreferencesStore",
]

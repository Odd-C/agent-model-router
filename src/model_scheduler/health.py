"""model_scheduler.health — ProviderHealth 长期健康档案。

与 quota.model-cooldown.json（短期路由冷却）不同，本模块把每次调用的
状态码与延迟写入独立状态文件 model-health.json，作为长期质量/延迟评估依据。

文件形状：
    {model@provider: {calls, failures, status_counts, latency_samples, updated_at}}
其中 ``latency_samples`` 为滑动窗口内的逐次调用采样
``[{"ts": epoch, "status": status, "latency_ms": ms | null}, ...]``；
``latency_ms`` 为 null 表示该次调用缺失延迟样本，只参与调用/失败统计，
不参与 p50/p95 延迟分位计算。``calls/failures/status_counts`` 为累计
统计（归档用途）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .policy import atomic_write_json, default_state_dir

logger = logging.getLogger(__name__)

HEALTH_WINDOW_SECONDS = 3600  # 滑动窗口：默认近 1 小时


def _normalise_now(now) -> float:
    """把 None / datetime / epoch 秒统一成 epoch 秒。"""
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    try:
        return float(now)
    except (TypeError, ValueError):
        return time.time()


def _normalise_model(model_id: str) -> tuple:
    """处理 ``@provider:model`` / ``id@provider`` 格式，返回 (model, provider)。"""
    mid = str(model_id or "").strip()
    prov = ""
    if mid.startswith("@"):
        rest = mid[1:]
        if ":" in rest:
            prov, mid = rest.split(":", 1)
            mid = mid.strip()
            prov = prov.strip()
        else:
            mid = rest
    elif "@" in mid:
        mid, prov = mid.split("@", 1)
        mid = mid.strip()
        prov = prov.strip()
    return mid, prov


def _latency_ms(value) -> float:
    """把 latency_ms 规范化为非负 float；缺省/非法返回 0.0。"""
    if value is None:
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _normalise_status(status) -> Any:
    """状态码规范化：int 保持 int，其它保持字符串原貌。"""
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    if status is None:
        return None
    text = str(status).strip()
    if text.isdigit():
        return int(text)
    return text


def _is_failure_status(status) -> bool:
    """判断一次调用是否计入健康失败（429/5xx/限流/超时/传输错误）。"""
    if status is None:
        return False
    if isinstance(status, int):
        return status == 429 or status >= 500
    text = str(status).strip().lower()
    if text.isdigit():
        return int(text) == 429 or int(text) >= 500
    if text in {
        "rate_limit",
        "timeout",
        "transport_error",
        "server_error",
        "5xx",
        "429",
    }:
        return True
    return text.startswith("5")


def _entry_defaults() -> dict[str, Any]:
    return {
        "calls": 0,
        "failures": 0,
        "status_counts": {},
        "latency_samples": [],
        "updated_at": None,
    }


class ProviderHealth:
    """线程安全的 ProviderHealth 长期健康档案。

    ``record_result`` 记录每次调用结果并累计；``health_score`` 在近
    ``window_seconds`` 滑动窗口内计算成功率、p50/p95 延迟、近期失败数
    与失败风险（近期 429/5xx 率）。
    """

    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        window_seconds: float = HEALTH_WINDOW_SECONDS,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else default_state_dir()
        self.path = self.state_dir / "model-health.json"
        self.window_seconds = float(window_seconds)
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._lock = threading.Lock()

    def _load(self) -> dict:
        """读取健康档案；文件不存在/损坏时返回空档案。"""
        path = self.path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to load model-health.json; treating as empty", exc_info=True)
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                out[str(key)] = value
        return out

    def _save(self, data: dict) -> None:
        atomic_write_json(self.path, data)

    def record_result(self, model_id, provider=None, status=None, latency_ms=None, *, ts=None) -> None:
        """记录一次调用结果（成功/失败均记录）。"""
        mid, prov_from_model = _normalise_model(model_id)
        prov = str(provider or prov_from_model or "").strip()
        if not mid:
            return
        now = _normalise_now(ts)
        key = f"{mid}@{prov}" if prov else mid
        status = _normalise_status(status)
        # 缺失 latency 样本时保留 None，后续只计调用/失败统计，不进入延迟分位。
        latency = None if latency_ms is None else _latency_ms(latency_ms)

        with self._lock:
            data = self._load()
            entry = data.get(key)
            if not isinstance(entry, dict):
                entry = _entry_defaults()
            entry.setdefault("calls", 0)
            entry.setdefault("failures", 0)
            entry.setdefault("status_counts", {})
            entry.setdefault("latency_samples", [])
            entry.setdefault("updated_at", None)

            # 维护滑动窗口采样（同时保留累计归档）。
            samples = [s for s in entry.get("latency_samples") or [] if isinstance(s, dict)]
            cutoff = now - self.window_seconds
            samples = [s for s in samples if _normalise_now(s.get("ts", 0)) >= cutoff]
            samples.append({"ts": now, "status": status, "latency_ms": latency})
            entry["latency_samples"] = samples

            entry["calls"] = int(entry.get("calls") or 0) + 1
            if _is_failure_status(status):
                entry["failures"] = int(entry.get("failures") or 0) + 1

            status_counts = entry.get("status_counts")
            if not isinstance(status_counts, dict):
                status_counts = {}
            status_key = "None" if status is None else str(status)
            status_counts[status_key] = int(status_counts.get(status_key) or 0) + 1
            entry["status_counts"] = status_counts
            entry["updated_at"] = now

            data[key] = entry
            self._save(data)

    def health_score(self, model_id, provider=None, *, now=None) -> dict:
        """计算近窗口内的健康评分。

        返回 ``{success_rate, p50, p95, recent_failures, failure_risk}``。
        无档案时 success_rate=1.0、p50/p95=None、recent_failures=0、
        failure_risk=0.2（默认先验，与 utility 的无档案默认一致）。
        """
        mid, prov_from_model = _normalise_model(model_id)
        prov = str(provider or prov_from_model or "").strip()
        now = _normalise_now(now)
        key = f"{mid}@{prov}" if prov else mid

        with self._lock:
            data = self._load()
        entry = data.get(key)
        if not isinstance(entry, dict):
            return {
                "success_rate": 1.0,
                "p50": None,
                "p95": None,
                "recent_failures": 0,
                "failure_risk": 0.2,
            }

        samples = [s for s in entry.get("latency_samples") or [] if isinstance(s, dict)]
        cutoff = now - self.window_seconds
        recent = [s for s in samples if _normalise_now(s.get("ts", 0)) >= cutoff]
        if not recent:
            return {
                "success_rate": 1.0,
                "p50": None,
                "p95": None,
                "recent_failures": 0,
                "failure_risk": 0.2,
            }

        latencies = sorted(
            _latency_ms(s.get("latency_ms"))
            for s in recent
            if s.get("latency_ms") is not None
        )
        failures = sum(1 for s in recent if _is_failure_status(s.get("status")))
        total = len(recent)
        success_rate = max(0.0, min(1.0, (total - failures) / total))
        failure_risk = max(0.0, min(1.0, failures / total))

        def _percentile(p: float) -> float:
            if not latencies:
                return None
            # 最近邻索引，与常见 percentile 定义保持简单可解释。
            idx = min(len(latencies) - 1, max(0, int(round(p * (len(latencies) - 1)))))
            return latencies[idx]

        return {
            "success_rate": success_rate,
            "p50": _percentile(0.50),
            "p95": _percentile(0.95),
            "recent_failures": failures,
            "failure_risk": failure_risk,
        }


_default_health: ProviderHealth | None = None


def _get_health() -> ProviderHealth:
    global _default_health
    expected = default_state_dir()
    if _default_health is None or _default_health.state_dir != expected:
        _default_health = ProviderHealth(state_dir=expected)
    return _default_health


def record_result(model_id, provider=None, status=None, latency_ms=None, *, ts=None) -> None:
    """模块级便捷函数：记录一次调用结果。"""
    _get_health().record_result(model_id, provider, status=status, latency_ms=latency_ms, ts=ts)


def health_score(model_id, provider=None, *, now=None) -> dict:
    """模块级便捷函数：查询健康评分。"""
    return _get_health().health_score(model_id, provider, now=now)


__all__ = [
    "HEALTH_WINDOW_SECONDS",
    "ProviderHealth",
    "record_result",
    "health_score",
]

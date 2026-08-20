"""model_scheduler.quota — 免费额度本地近似跟踪。

状态文件 model-quota.json：{"calls": [{"model", "provider", "ts"} ...]}
窗口：5 小时滑动窗口。本地计数只作路由参考，真实额度以提供方 API 为准。
失败冷却状态文件 model-cooldown.json：{model@provider: {ts, reason, status, provider}}。
旧格式（纯时间戳）仍可读取，按 {ts, reason:None, status:None, provider:None} 处理。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import policy

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 5 * 3600
COOLDOWN_SECONDS = 300  # 5 分钟冷却


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


def _ts(call: dict) -> float:
    try:
        return float(call.get("ts", 0))
    except (TypeError, ValueError):
        return 0.0


def _normalise_model(model_id: str) -> tuple:
    """处理调用方可能传来的 '@provider:model' 格式，返回 (model, provider)。"""
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
    return mid, prov


def _count_used(calls: list, model_id: str, provider: str, cutoff: float) -> int:
    mid = str(model_id or "")
    prov = str(provider or "")
    used = 0
    for c in calls:
        try:
            if str(c.get("model") or "") != mid:
                continue
            if prov and str(c.get("provider") or "") != prov:
                continue
            if _ts(c) >= cutoff:
                used += 1
        except Exception:
            continue
    return used


class QuotaTracker:
    """线程安全的免费额度跟踪器。"""

    def __init__(
        self,
        state_dir: str | Path | None = None,
        policy_store: Any | None = None,
    ) -> None:
        if state_dir is not None:
            self.state_dir = Path(state_dir).expanduser()
        elif policy_store is not None:
            self.state_dir = getattr(policy_store, "state_dir", policy.default_state_dir())
        else:
            self.state_dir = policy.default_state_dir()
        self.policy = policy_store if policy_store is not None else policy.ModelPolicy(self.state_dir)
        self._lock = threading.Lock()

    @property
    def quota_path(self) -> Path:
        return self.state_dir / "model-quota.json"

    @property
    def cooldown_path(self) -> Path:
        return self.state_dir / "model-cooldown.json"

    def _load_calls(self) -> list:
        path = self.quota_path
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            calls = data.get("calls") if isinstance(data, dict) else None
            if not isinstance(calls, list):
                return []
            return [c for c in calls if isinstance(c, dict) and c.get("model")]
        except Exception:
            logger.warning("Failed to load model-quota.json", exc_info=True)
            return []

    def _save_calls(self, calls: list) -> None:
        policy.atomic_write_json(self.quota_path, {"calls": calls})

    def record_failure(
        self,
        model_id,
        provider=None,
        ts=None,
        reason=None,
        status=None,
    ) -> None:
        """记录一次模型调用失败，触发路由冷却。

        reason/status 用于错误分类：transport_error / rate_limit / server_error 等。
        400/401/403 等不触发冷却的错误不应调用本方法。
        """
        mid, prov_from_model = _normalise_model(model_id)
        prov = str(provider or prov_from_model or "").strip()
        if not mid:
            return
        now = _normalise_now(ts)
        key = f"{mid}@{prov}" if prov else mid
        try:
            with self._lock:
                path = self.cooldown_path
                data = {}
                if path.exists():
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except Exception:
                        data = {}
                if not isinstance(data, dict):
                    data = {}
                data[key] = {
                    "ts": now,
                    "reason": reason,
                    "status": status,
                    "provider": prov or None,
                }
                policy.atomic_write_json(path, data)
        except Exception:
            logger.warning("Failed to record model failure cooldown", exc_info=True)

    def cooldown_seconds_left(self, model_id, provider=None, now=None) -> float:
        """返回该模型剩余冷却秒数（0 = 不在冷却中）。"""
        mid, prov_from_model = _normalise_model(model_id)
        prov = str(provider or prov_from_model or "").strip()
        if not mid:
            return 0.0
        now = _normalise_now(now)
        key = f"{mid}@{prov}" if prov else mid
        try:
            with self._lock:
                path = self.cooldown_path
                if not path.exists():
                    return 0.0
                data = json.loads(path.read_text(encoding="utf-8"))
                value = data.get(key, 0)
                if isinstance(value, dict):
                    ts = float(value.get("ts", 0) or 0)
                else:
                    # 兼容旧格式：{model@provider: failure_ts}
                    ts = float(value or 0)
                if ts <= 0:
                    return 0.0
                remaining = COOLDOWN_SECONDS - (now - ts)
                return max(0.0, remaining)
        except Exception:
            return 0.0

    def record_call(self, model_id, provider, ts=None) -> None:
        """记录一次模型调用。只对免费模型有实际影响。"""
        mid = str(model_id or "").strip()
        if not mid:
            return
        now = _normalise_now(ts)

        with self._lock:
            calls = self._load_calls()
            cutoff = now - WINDOW_SECONDS
            calls = [c for c in calls if _ts(c) >= cutoff]
            calls.append({"model": mid, "provider": str(provider or ""), "ts": now})
            self._save_calls(calls)

    def quota_left(self, model_id, provider, now=None) -> int:
        """返回 5h 滑动窗口内剩余次数。

        - 无画像记录 / 付费模型 / 无 quota_per_window：返回 -1 表示不受限。
        - 免费模型且 quota<=0：返回 0。
        """
        now = _normalise_now(now)

        entry = self.policy.resolve_model(model_id, provider)
        if not entry:
            return -1
        if str(entry.get("cost") or "").lower() != "free":
            return -1

        quota = entry.get("quota_per_window")
        if quota is None:
            return -1
        try:
            quota = int(quota)
        except (TypeError, ValueError):
            return -1
        if quota <= 0:
            return 0

        with self._lock:
            calls = self._load_calls()
        cutoff = now - WINDOW_SECONDS
        used = _count_used(calls, entry.get("id"), entry.get("provider"), cutoff)
        return max(0, quota - used)

    def reset_if_needed(self, now=None) -> int:
        """清理 5h 窗口外的过期记录；返回清理后保留的记录数。"""
        now = _normalise_now(now)

        with self._lock:
            calls = self._load_calls()
            cutoff = now - WINDOW_SECONDS
            kept = [c for c in calls if _ts(c) >= cutoff]
            if len(kept) != len(calls):
                self._save_calls(kept)
            return len(kept)

    def quota_table_left(self, now=None) -> dict:
        """返回所有免费模型当前剩余额度，key 为 id@provider。"""
        now = _normalise_now(now)

        out = {}
        for key, entry in self.policy.get_policy()["models"].items():
            if str(entry.get("cost") or "").lower() != "free":
                continue
            quota = entry.get("quota_per_window")
            if quota is None:
                continue
            try:
                quota = int(quota)
            except (TypeError, ValueError):
                continue
            if quota <= 0:
                out[key] = 0
            else:
                out[key] = self.quota_left(entry.get("id"), entry.get("provider"), now)
        return out


_default_tracker: QuotaTracker | None = None


def _get_tracker() -> QuotaTracker:
    global _default_tracker
    expected = policy.default_state_dir()
    if _default_tracker is None or _default_tracker.state_dir != expected:
        _default_tracker = QuotaTracker(state_dir=expected)
    return _default_tracker


def record_call(model_id, provider, ts=None) -> None:
    _get_tracker().record_call(model_id, provider, ts)


def quota_left(model_id, provider, now=None) -> int:
    return _get_tracker().quota_left(model_id, provider, now)


def reset_if_needed(now=None) -> int:
    return _get_tracker().reset_if_needed(now)


def quota_table_left(now=None) -> dict:
    return _get_tracker().quota_table_left(now)


def record_failure(model_id, provider=None, ts=None, reason=None, status=None) -> None:
    _get_tracker().record_failure(model_id, provider, ts=ts, reason=reason, status=status)


def cooldown_seconds_left(model_id, provider=None, now=None) -> float:
    return _get_tracker().cooldown_seconds_left(model_id, provider, now)

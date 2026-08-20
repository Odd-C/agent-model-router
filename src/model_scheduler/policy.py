"""model_scheduler.policy — 模型画像表。

职责：
  1. 内置通用示例模型画像（能力档、付费/免费、5h 窗口配额、
     峰谷可用性、场景标签、降级链、路由角色）。
  2. 支持从 state 目录下的 model-policy.json 覆盖默认画像。
  3. 提供峰谷判断 is_peak_hour()（Asia/Shanghai 本地时间 9:00-12:00、
     14:00-18:00，含边界）。

所有读写均使用可参数化的 state 目录；import 本模块不会创建任何目录或文件。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 5 小时滑动窗口（秒）。
QUOTA_WINDOW_SECONDS = 5 * 3600

try:
    from zoneinfo import ZoneInfo

    _SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 无 IANA tz 数据时的保守回退
    _SHANGHAI_TZ = timezone(timedelta(hours=8))

# 模型 key 一律用 id@provider，避免同名模型多 provider 冲突。
# role 字段：stable（付费最稳，紧急兜底）/ free-flagship（免费旗舰，复杂任务）/
#           free-bulk（免费量大，日常主力）/ free-preview（免费预览，日常兜底）/
#           paid-fallback（付费兜底）
# ⚠️ 以下默认画像仅为「通用示例」，演示机制用。请按你的真实模型/额度修改
#    model-policy.json 覆盖，或直接改这份默认值。
DEFAULT_MODEL_POLICIES: dict[str, dict[str, Any]] = {
    "gpt-4o@openai": {
        "id": "gpt-4o",
        "provider": "openai",
        "tier": "S",
        "capability": 1.0,
        "cost": "paid",
        "quota_per_window": None,
        "peak_safe": True,
        "role": "stable",
        "fallback_chain": [],
        "scenarios": ["complex", "dsh"],
        "label": "GPT-4o (paid flagship)",
    },
    "gpt-4o-mini@openai": {
        "id": "gpt-4o-mini",
        "provider": "openai",
        "tier": "A",
        "capability": 0.7,
        "cost": "paid",
        "quota_per_window": None,
        "peak_safe": True,
        "role": "paid-fallback",
        "fallback_chain": [],
        "scenarios": ["simple", "daily", "complex"],
        "label": "GPT-4o mini (paid economy)",
    },
    "deepseek-chat@deepseek": {
        "id": "deepseek-chat",
        "provider": "deepseek",
        "tier": "A-",
        "capability": 0.6,
        "cost": "free",
        "quota_per_window": 500,
        "peak_safe": True,
        "role": "free-preview",
        "fallback_chain": ["gpt-4o@openai"],
        "scenarios": ["simple", "daily"],
        "label": "DeepSeek Chat (free preview)",
    },
    "gemini-2.0-flash@google": {
        "id": "gemini-2.0-flash",
        "provider": "google",
        "tier": "B+",
        "capability": 0.8,
        "cost": "free",
        "quota_per_window": 1500,
        "peak_safe": True,
        "role": "free-bulk",
        "fallback_chain": [
            "deepseek-chat@deepseek",
            "gpt-4o@openai",
        ],
        "scenarios": ["simple", "daily"],
        "label": "Gemini 2.0 Flash (free high-volume)",
    },
    "claude-3-5-sonnet@anthropic": {
        "id": "claude-3-5-sonnet",
        "provider": "anthropic",
        "tier": "S+",
        "capability": 0.95,
        "cost": "free",
        "quota_per_window": 500,
        "peak_safe": True,
        # 限流/额度不足时降级付费经济型
        "role": "free-flagship",
        "fallback_chain": ["gpt-4o-mini@openai"],
        "scenarios": ["complex", "daily"],
        "label": "Claude 3.5 Sonnet (free flagship)",
    },
}

# 决策链（按 role 驱动，与具体模型名解耦）。每条链按优先级排列。
#   urgent_chain：紧急任务（能力优先，最稳付费模型）
#   complex_chain：难度 >=4（免费旗舰优先，额度不足降级付费）
#   daily_chain：难度 2-3（免费量大 → 免费预览 → 免费旗舰 → 付费兜底）
#   simple_chain：难度 0-1（免费量大 → 免费预览 → 付费兜底）
ROUTE_CHAINS: dict[str, list[str]] = {
    "urgent": ["stable", "paid-fallback"],
    "complex": ["free-flagship", "paid-fallback"],
    "daily": ["free-bulk", "free-preview", "free-flagship", "paid-fallback"],
    "simple": ["free-bulk", "free-preview", "paid-fallback"],
}

DEFAULT_ENABLED = True
DEFAULT_SCHEDULE: list[dict[str, Any]] = []
DEFAULT_LANGUAGE = "zh"

# 峰谷时段（Asia/Shanghai，含边界）。可被 model-policy.json 的 peak_hours
# 覆盖：如 [[8, 10], [20, 22]]；空数组 [] 表示无峰谷（全天平峰）。
DEFAULT_PEAK_HOURS: list[list[int]] = [[9, 12], [14, 18]]

_configured_state_dir: Path | None = None


def configure_state_dir(path: str | Path | None) -> Path:
    """配置模块级默认 state 目录；传 None 表示恢复环境变量/默认值。"""
    global _configured_state_dir
    if path is None:
        _configured_state_dir = None
    else:
        _configured_state_dir = Path(path).expanduser()
    return default_state_dir()


def default_state_dir() -> Path:
    """返回 state 目录（不创建）：环境变量 > configure_state_dir > ~/.llm-router。"""
    if _configured_state_dir is not None:
        return _configured_state_dir
    env = os.getenv("LLM_ROUTER_STATE_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".llm-router"


def get_state_dir() -> Path:
    """返回 state 目录并确保其存在（首次调用才会创建）。"""
    d = default_state_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.warning("llm-router state dir unavailable: %s", d)
    return d


def get_policy_path() -> Path:
    return default_state_dir() / "model-policy.json"


def atomic_write_json(path, data) -> None:
    """原子写 JSON：同目录 tmp + fsync + os.replace。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    f = None
    try:
        f = os.fdopen(fd, "w", encoding="utf-8")
        with f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        else:
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _split_key(key) -> tuple:
    key = str(key or "").strip()
    if not key:
        return "", ""
    if "@" in key:
        model, provider = key.split("@", 1)
        return model.strip(), provider.strip()
    return key, ""


def _normalise_entry(key, item) -> dict | None:
    """把一条画像记录规范化，补齐字段、归一化配额/降级链/场景。"""
    entry = dict(item)
    mid = str(entry.get("id") or "").strip()
    prov = str(entry.get("provider") or "").strip()
    if not mid:
        m, p = _split_key(key)
        mid, prov = m, p or prov
    if not mid:
        return None

    entry["id"] = mid
    entry["provider"] = prov
    entry.setdefault("tier", "")
    entry.setdefault("capability", 0.5)
    entry.setdefault("cost", "paid")
    entry.setdefault("quota_per_window", None)
    entry.setdefault("peak_safe", True)
    entry.setdefault("role", "")
    entry.setdefault("scenarios", [])
    entry.setdefault("fallback_chain", [])
    entry.setdefault("label", "")
    entry.setdefault("enabled", True)

    entry["enabled"] = False if str(entry.get("enabled") or "").lower() in ("false", "0", "no", "off") else bool(entry.get("enabled", True))
    entry["tier"] = str(entry.get("tier") or "").strip()
    try:
        entry["capability"] = max(0.0, min(1.0, float(entry.get("capability", 0.5))))
    except (TypeError, ValueError):
        entry["capability"] = 0.5
    entry["cost"] = "free" if str(entry.get("cost") or "").strip().lower() == "free" else "paid"
    entry["peak_safe"] = bool(entry.get("peak_safe", True))

    quota = entry.get("quota_per_window")
    if quota is not None and str(quota).strip() != "":
        try:
            entry["quota_per_window"] = int(quota)
        except (TypeError, ValueError):
            entry["quota_per_window"] = None
    else:
        entry["quota_per_window"] = None

    chain = []
    for c in entry.get("fallback_chain") or []:
        if isinstance(c, str):
            c = c.strip()
            if c:
                chain.append(c)
        elif isinstance(c, dict):
            cid = str(c.get("id") or c.get("model") or "").strip()
            cprov = str(c.get("provider") or "").strip()
            if cid:
                chain.append(f"{cid}@{cprov}" if cprov else cid)
    entry["fallback_chain"] = chain

    scenarios = entry.get("scenarios") or []
    if isinstance(scenarios, str):
        scenarios = [scenarios]
    entry["scenarios"] = [str(s).strip() for s in scenarios if str(s).strip()]

    # per-model 峰谷时段覆盖（优先级：模型级 > 全局 peak_hours > 默认）。
    # 存 None = 未配置（走全局/默认）；存 [] = 显式无峰谷（全天平峰）。
    raw_ph = entry.get("peak_hours")
    if raw_ph is None:
        entry["peak_hours"] = None
    else:
        entry["peak_hours"] = _normalise_peak_hours(raw_ph, [])
    return entry


def _normalise_peak_hours(raw, default) -> list:
    """规范化峰谷时段配置：[[start_hour, end_hour], ...]，含边界。

    非法输入回退默认；空列表 [] 表示无峰谷（全天平峰）。
    """
    if raw is None:
        return [list(x) for x in default]
    if not isinstance(raw, list):
        return [list(x) for x in default]
    out = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            start = int(item[0])
            end = int(item[1])
        except (TypeError, ValueError):
            continue
        if 0 <= start <= 23 and 0 <= end <= 23 and start <= end:
            out.append([start, end])
    return out


def _normalise_providers(raw) -> dict:
    """规范化 providers 段：{"<name>": {"base_url": "...", "api_key": "env:VAR" | "sk-..."}}。

    只做结构规范化与字符串清理，不读取环境变量（环境变量解析在 server 侧）。
    providers 段缺失/非法时返回空 dict，不抛错。
    """
    if not isinstance(raw, dict):
        return {}
    out = {}
    for raw_name, item in raw.items():
        name = str(raw_name or "").strip()
        if not name or not isinstance(item, dict):
            continue
        cfg = dict(item)
        cfg["base_url"] = str(cfg.get("base_url") or "").strip()

        api_key = cfg.get("api_key")
        if api_key is None:
            api_key = ""
        elif isinstance(api_key, str):
            api_key = api_key.strip()
        else:
            api_key = str(api_key).strip()
        cfg["api_key"] = api_key

        out[name] = cfg
    return out


def is_peak_hour(dt=None, peak_hours=None) -> bool:
    """峰谷判断（Asia/Shanghai 时间，含边界）。

    peak_hours 可传 [[start, end], ...] 自定义（如 [[8, 10], [20, 22]]），
    默认使用 DEFAULT_PEAK_HOURS（9:00-12:00、14:00-18:00）。
    传入 naive datetime 时按 Asia/Shanghai 解释；传入 aware datetime 时
    会转换到 Asia/Shanghai 后再判断。
    """
    if dt is None:
        dt = datetime.now(_SHANGHAI_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_SHANGHAI_TZ)
    else:
        try:
            dt = dt.astimezone(_SHANGHAI_TZ)
        except Exception:
            pass
    periods = _normalise_peak_hours(peak_hours, DEFAULT_PEAK_HOURS)
    if not periods:
        return False  # 空配置 = 无峰谷（全天平峰）
    hm = dt.hour * 60 + dt.minute
    return any(start * 60 <= hm <= end * 60 for start, end in periods)


class ModelPolicy:
    """模型画像表。"""

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else default_state_dir()

    @property
    def policy_path(self) -> Path:
        return self.state_dir / "model-policy.json"

    def is_peak_hour(self, dt=None) -> bool:
        """按本实例配置的全局 peak_hours 判断峰谷（默认 9-12/14-18）。"""
        try:
            periods = self.get_policy().get("peak_hours") or DEFAULT_PEAK_HOURS
        except Exception:
            periods = DEFAULT_PEAK_HOURS
        return is_peak_hour(dt, peak_hours=periods)

    def peak_hours_for(self, model_id, provider=None) -> list:
        """查询某模型的峰谷时段（per-model 覆盖 > 全局 > 默认）。

        返回 [[start, end], ...]；空列表 = 无峰谷（全天平峰）。
        """
        entry = self.resolve_model(model_id, provider)
        if entry:
            ph = entry.get("peak_hours")
            if ph is not None:
                return [list(x) for x in ph]
        try:
            global_ph = self.get_policy().get("peak_hours")
        except Exception:
            global_ph = None
        if global_ph:
            return [list(x) for x in global_ph]
        return [list(x) for x in DEFAULT_PEAK_HOURS]

    def is_peak_hour_for(self, dt=None, model_id=None, provider=None) -> bool:
        """按模型级/全局峰谷判断。model_id 给定时优先查该模型的 peak_hours。"""
        if model_id is not None:
            periods = self.peak_hours_for(model_id, provider)
            return is_peak_hour(dt, peak_hours=periods)
        return self.is_peak_hour(dt)

    def get_policy(self) -> dict:
        """返回合并后的策略：{"models": {key: entry}, "schedule": [...], "enabled": bool, ...}。

        `enabled` 是信息性字段：`get_policy()` 会返回它、`update_policy()` 会持久化它，
        但路由决策（router._route）从不读取它。启用/禁用调度由接入方自己的开关控制；
        只要调用本库 API，就会按画像/额度/冷却执行路由。
        """
        models = {k: dict(v) for k, v in DEFAULT_MODEL_POLICIES.items()}
        schedule = list(DEFAULT_SCHEDULE)
        enabled = DEFAULT_ENABLED

        raw_data = {}
        path = self.policy_path
        try:
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw_data = loaded
        except Exception:
            logger.warning("Failed to load model-policy.json; falling back to defaults", exc_info=True)

        raw_models = raw_data.get("models")
        iterable = []
        if isinstance(raw_models, dict):
            iterable = [(str(k), v) for k, v in raw_models.items()]
        elif isinstance(raw_models, list):
            for item in raw_models:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "").strip()
                if not key:
                    mid = str(item.get("id") or "").strip()
                    prov = str(item.get("provider") or "").strip()
                    if mid:
                        key = f"{mid}@{prov}" if prov else mid
                if key:
                    iterable.append((key, item))

        for key, item in iterable:
            if not isinstance(item, dict):
                continue
            hint = _normalise_entry(key, item)
            if not hint:
                continue
            actual_key = f"{hint['id']}@{hint['provider']}" if hint["provider"] else hint["id"]
            merged = dict(models.get(actual_key, {}))
            for k, v in item.items():
                if k == "key":
                    continue
                if v is not None:
                    merged[k] = v
            final = _normalise_entry(actual_key, merged)
            if final:
                if final.get("enabled") is False:
                    models.pop(actual_key, None)
                    continue
                models[actual_key] = final

        if isinstance(raw_data.get("schedule"), list):
            schedule = []
            for r in raw_data["schedule"]:
                if isinstance(r, dict):
                    schedule.append({
                        "time": str(r.get("time") or "").strip(),
                        "model": str(r.get("model") or "").strip(),
                        "provider": str(r.get("provider") or "").strip() or None,
                        "label": str(r.get("label") or "").strip(),
                    })

        if isinstance(raw_data.get("enabled"), bool):
            enabled = raw_data.get("enabled")

        peak_hours = _normalise_peak_hours(raw_data.get("peak_hours"), DEFAULT_PEAK_HOURS)

        language = str(raw_data.get("language") or DEFAULT_LANGUAGE).strip().lower()
        if language not in ("zh", "en"):
            language = DEFAULT_LANGUAGE

        providers = _normalise_providers(raw_data.get("providers"))

        return {
            "models": models,
            "schedule": schedule,
            "enabled": enabled,
            "peak_hours": peak_hours,
            "language": language,
            "providers": providers,
        }

    def list_models(self) -> list:
        """返回画像表全量列表（每个条目带 key）。"""
        out = []
        for key, entry in self.get_policy()["models"].items():
            if entry.get("enabled") is False:
                continue
            item = dict(entry)
            item["key"] = key
            out.append(item)
        out.sort(key=lambda x: str(x.get("key", "")))
        return out

    def resolve_model(self, model_id, provider=None) -> dict | None:
        """按 (model_id, provider) 或 id@provider 解析画像条目；未找到返回 None。"""
        mid = str(model_id or "").strip()
        if not mid:
            return None
        models = self.get_policy()["models"]
        prov = str(provider or "").strip()
        if prov:
            if "@" in mid:
                return models.get(mid)
            return models.get(f"{mid}@{prov}")
        if "@" in mid:
            return models.get(mid)
        for entry in models.values():
            if entry.get("id") == mid:
                return entry
        return None

    def find_by_role(self, role: str, cost: str | None = None) -> list:
        """按 role（可选 cost 过滤）返回候选画像条目列表。

        返回按 tier 降序（S+ > S > A > A- > B+）排列的条目，调用方按序
        检查额度即可实现「能力优先」的降级链。与具体模型名解耦。
        """
        wanted = str(role or "").strip()
        models = self.get_policy()["models"]
        tier_order = {"S+": 0, "S": 1, "A": 2, "A-": 3, "B+": 4, "B": 5, "C": 6}
        out = []
        for entry in models.values():
            if entry.get("enabled") is False:
                continue
            if wanted and str(entry.get("role") or "") != wanted:
                continue
            if cost is not None and str(entry.get("cost") or "").lower() != str(cost).lower():
                continue
            out.append(entry)
        out.sort(key=lambda e: tier_order.get(str(e.get("tier") or ""), 99))
        return out

    def get_providers(self) -> dict:
        """返回 providers 段完整配置（含 api_key 原文或 env:VAR 引用字符串）。

        providers 段缺失时返回空 dict，不抛错。
        """
        return self.get_policy().get("providers", {})

    def provider_config(self, name) -> dict | None:
        """返回单个 provider 配置；不存在时返回 None。"""
        providers = self.get_providers()
        name = str(name or "").strip()
        if not name:
            return None
        return providers.get(name)

    def has_provider(self, name) -> bool:
        """判断 provider 是否存在。"""
        return self.provider_config(name) is not None

    def get_quota_table(self) -> dict:
        """返回免费模型的 5h 窗口配额上限，key 为 id@provider。"""
        out = {}
        for key, entry in self.get_policy()["models"].items():
            if str(entry.get("cost") or "").lower() != "free":
                continue
            quota = entry.get("quota_per_window")
            if quota is None:
                continue
            try:
                out[key] = int(quota)
            except (TypeError, ValueError):
                out[key] = 0
        return out

    def update_policy(self, updates: dict) -> dict:
        """更新 model-policy.json（schedule/enabled/models/可调参数），原子写盘。

        `enabled` 仅作为信息性字段写入文件，供接入方自己的开关读取；
        本库路由决策（router._route）不读取它，传入 `enabled` 不会改变任何路由行为。
        """
        if not isinstance(updates, dict):
            raise ValueError("policy updates must be a dict")
        path = self.policy_path
        current = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current = loaded
            except Exception:
                current = {}

        for key in ("models", "schedule", "enabled"):
            if key in updates:
                current[key] = updates[key]
        for key, value in updates.items():
            if key not in ("models", "schedule", "enabled"):
                current[key] = value

        atomic_write_json(path, current)
        return self.get_policy()


_default_policy: ModelPolicy | None = None


def _get_default_policy() -> ModelPolicy:
    global _default_policy
    if _default_policy is None or _default_policy.state_dir != default_state_dir():
        _default_policy = ModelPolicy()
    return _default_policy


def get_policy() -> dict:
    return _get_default_policy().get_policy()


def get_language() -> str:
    """当前全局语言（zh/en），从 model-policy.json 的 language 字段读取。"""
    try:
        return str(get_policy().get("language") or DEFAULT_LANGUAGE)
    except Exception:
        return DEFAULT_LANGUAGE


def list_models() -> list:
    return _get_default_policy().list_models()


def resolve_model(model_id, provider=None) -> dict | None:
    return _get_default_policy().resolve_model(model_id, provider)


def find_by_role(role: str, cost: str | None = None) -> list:
    return _get_default_policy().find_by_role(role, cost)


def is_peak_hour_for(dt=None, model_id=None, provider=None) -> bool:
    """按模型级/全局峰谷判断（模块级便捷入口）。"""
    return _get_default_policy().is_peak_hour_for(dt, model_id=model_id, provider=provider)


def get_quota_table() -> dict:
    return _get_default_policy().get_quota_table()


def update_policy(updates: dict) -> dict:
    return _get_default_policy().update_policy(updates)


def get_providers() -> dict:
    """模块级便捷入口：返回默认 state 目录下的 providers 配置。"""
    return _get_default_policy().get_providers()


def provider_config(name) -> dict | None:
    """模块级便捷入口：返回单个 provider 配置。"""
    return _get_default_policy().provider_config(name)


def has_provider(name) -> bool:
    """模块级便捷入口：判断 provider 是否存在。"""
    return _get_default_policy().has_provider(name)

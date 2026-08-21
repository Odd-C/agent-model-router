"""agent_model_router.router — 智能模型调度器·决策引擎（核心）。

决策按 ROUTE_CHAINS（role 链）驱动，与具体模型名解耦：
  urgent   -> stable -> paid-fallback
  complex  -> free-flagship -> paid-fallback
  daily    -> free-bulk -> free-preview -> free-flagship -> paid-fallback
  simple   -> free-bulk -> free-preview -> paid-fallback

免费模型高峰/谷值均优先；回退付费模型时，高峰 reason 含「官方高峰翻倍」警告。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import policy, quota

logger = logging.getLogger(__name__)

# reason 文案 i18n 表（语言由 model-policy.json 的 language 字段控制，默认 zh）
_TXT = {
    "zh": {
        "urgent": "紧急任务，能力优先",
        "complex": "复杂任务",
        "daily": "日常任务",
        "simple": "简单任务",
        "stable": "{label} 最稳",
        "free_available": "{label} 可用",
        "no_model": "无可选模型",
        "fallback": "免费额度不足，回退 {label}",
        "peak_double": "该模型高峰翻倍",
        "global_peak_double": "官方高峰翻倍",
        "off_peak": "谷值正常价兜底",
    },
    "en": {
        "urgent": "Urgent, capability first",
        "complex": "Complex task",
        "daily": "Daily task",
        "simple": "Simple task",
        "stable": "{label} (most reliable)",
        "free_available": "{label} available",
        "no_model": "No model available",
        "fallback": "Free quota exhausted, fallback to {label}",
        "peak_double": "this model's peak price is doubled",
        "global_peak_double": "official peak price doubled",
        "off_peak": "off-peak normal price fallback",
    },
}


def _lang(store=None) -> str:
    """解析当前语言：优先 policy_store 的 language 配置，回退全局。"""
    if store is not None:
        try:
            lang = str(store.get_policy().get("language") or "zh").strip().lower()
            if lang in ("zh", "en"):
                return lang
        except Exception:
            pass
    try:
        lang = str(policy.get_language()).strip().lower()
        if lang in ("zh", "en"):
            return lang
    except Exception:
        pass
    return "zh"


def _t(key: str, lang: str, **fmt) -> str:
    """按语言取文案并格式化。"""
    template = _TXT.get(lang, _TXT["zh"]).get(key, _TXT["zh"].get(key, key))
    if fmt:
        try:
            return template.format(**fmt)
        except (KeyError, IndexError):
            return template
    return template

_CODE_BLOCK = "```"
_ERROR_RE = re.compile(r"报错|error|exception|traceback|failed|崩溃|fail", re.IGNORECASE)
_SOURCE_RE = re.compile(r"\.py|\.js|\.ts|源码|函数|class\b|def\b|import\b|接口")
_URGENT_RE = re.compile(r"紧急|马上|尽快|asap|urgent|立刻", re.IGNORECASE)
# 强意图 +3（命中词同时属于弱词表 → 自然叠加到 >=4 → 复杂任务档）；
# 弱意图 +1（日常闲聊含「代码/项目/bug」等词不误伤，只 +1）。
_TASK_STRONG_RE = re.compile(
    r"写(一?个|段|点|个)?[^，。！？\n]{0,20}?(python|py|js|javascript|java|go|rust|shell|bash|sql|html|css|vue|react|脚本|代码|程序|函数|爬虫|工具|demo)"
    r"|编[程码写]|开发|重构|改(一?下|一?个)?[^，。！？\n]{0,12}?(代码|脚本|程序|函数)"
    r"|做(一?个|个)?[^，。！？\n]{0,12}?项目|搞(一?个|个)?[^，。！？\n]{0,12}?(项目|系统|网站|应用|平台|工具)|搭(建|一?个)?(项目|系统|网站|应用|平台|框架)"
    r"|创建.*项目|实现.*(功能|模块|接口|算法)|优化.*(代码|性能|脚本|程序|系统)"
    r"|修(一?个|一下|一?下)?bug|修(一?下|一?个)?[^，。！？\n]{0,12}?(代码|脚本|程序|函数)|修复.*(bug|问题|错误)|debug|排错|排查.*(bug|问题)",
    re.IGNORECASE)
_TASK_HINT_RE = re.compile(
    r"代码|脚本|项目|bug|爬虫|算法|模块|接口|前端|后端|数据库|部署|优化|重构|功能|系统|网站|应用|框架|demo|函数",
    re.IGNORECASE)


def assess_difficulty(text: str) -> int:
    """规则式难度分 0-5（纯 CPU，不调 LLM）。"""
    text = str(text or "")
    score = 0
    if _CODE_BLOCK in text:
        score += 2
    if _ERROR_RE.search(text):
        score += 2
    if _SOURCE_RE.search(text):
        score += 1
    if _TASK_STRONG_RE.search(text):
        score += 3
    if _TASK_HINT_RE.search(text):
        score += 1
    length = len(text)
    if length > 2000:
        score += 1
    if length > 8000:
        score += 1
    return max(0, min(5, score))


def assess_urgency(text: str) -> bool:
    return bool(_URGENT_RE.search(str(text or "")))


def format_model_key(model, provider) -> str:
    model = str(model or "").strip()
    provider = str(provider or "").strip()
    if not model:
        return ""
    return f"{model}@{provider}" if provider else model


def parse_model_key(key) -> tuple:
    key = str(key or "").strip()
    if not key:
        return "", ""
    if "@" in key:
        model, provider = key.split("@", 1)
        return model.strip(), provider.strip()
    return key, ""


def format_selector_key(model, provider) -> str:
    """Format a model selector key as `provider/model`.

    This is the value shape commonly used by UI model pickers and external
    systems (for example `openai/gpt-4o`). It is intentionally separate from
    `format_model_key`, which returns the library's internal unique key
    `id@provider` used for policy/quota/cooldown state-file keys.

    Returns the bare model id when `provider` is empty, and `""` when
    `model` is empty. A model id containing `/` only round-trips when
    `provider` is non-empty (the usual `provider/model` convention).
    """
    model = str(model or "").strip()
    provider = str(provider or "").strip()
    if not model:
        return ""
    return f"{provider}/{model}" if provider else model


def parse_selector_key(value) -> tuple:
    """Parse a `provider/model` selector value into `(model, provider)`.

    Values without `/` are treated as bare model ids with an empty provider.
    Empty input returns `("", "")`. This helper does not process the
    library's internal `id@provider` format (use `format_model_key` /
    `parse_model_key`) or the legacy `@provider:model` form accepted by
    `quota._normalise_model`; callers must use the matching codec for each
    key shape.
    """
    value = str(value or "").strip()
    if not value:
        return "", ""
    if "/" in value:
        provider, model = value.split("/", 1)
        return model.strip(), provider.strip()
    return value, ""


def _policy_store(policy_store=None):
    return policy_store if policy_store is not None else policy


def _as_timestamp(now):
    if now is None:
        return None
    if isinstance(now, datetime):
        return now.timestamp()
    try:
        return float(now)
    except (TypeError, ValueError):
        return None


def _build_result(model: str, provider: str, reason: str, policy_store=None) -> dict:
    store = _policy_store(policy_store)
    entry = store.resolve_model(model, provider)
    return {
        "model": model,
        "provider": provider,
        "reason": reason,
        "tier": entry.get("tier", "") if entry else "",
        "cost": entry.get("cost", "paid") if entry else "paid",
    }


def _remaining(entry: dict, quota_snapshot, now_ts, quota_tracker=None) -> int:
    key = format_model_key(entry.get("id"), entry.get("provider"))
    if quota_snapshot is not None:
        # 传入 snapshot 时它是唯一依据，不再碰真实额度文件。
        if key in quota_snapshot:
            try:
                return int(quota_snapshot.get(key))
            except (TypeError, ValueError):
                return 0
        if entry.get("id") in quota_snapshot:
            try:
                return int(quota_snapshot.get(entry.get("id")))
            except (TypeError, ValueError):
                return 0
        return 0
    if quota_tracker is not None:
        return quota_tracker.quota_left(entry.get("id"), entry.get("provider"), now_ts)
    return quota.quota_left(entry.get("id"), entry.get("provider"), now_ts)


def _entry_available(entry: dict, quota_snapshot, now, quota_tracker=None) -> bool:
    if not entry:
        return False
    if str(entry.get("cost") or "").lower() != "free":
        return True
    if quota_snapshot is None:
        # 失败冷却中的免费模型视为不可用（免费 provider 限流 → 自动降级）。
        now_ts = _as_timestamp(now)
        if quota_tracker is not None:
            cd = quota_tracker.cooldown_seconds_left(entry.get("id"), entry.get("provider"), now_ts)
        else:
            cd = quota.cooldown_seconds_left(entry.get("id"), entry.get("provider"), now_ts)
        if cd > 0:
            return False
        return _remaining(entry, None, now_ts, quota_tracker) > 0
    return _remaining(entry, quota_snapshot, _as_timestamp(now), quota_tracker) > 0


def _paid_warning(now, base: str, store=None, model=None, provider=None) -> str:
    """付费回退警告。按回退目标模型的峰谷时段判断（per-model > 全局 > 默认）。

    base 是已按语言格式化的「免费额度不足，回退 {label}」前缀。
    """
    lang = _lang(store)
    if store is not None and model:
        try:
            if store.is_peak_hour_for(now, model_id=model, provider=provider):
                return f"{base}，{_t('peak_double', lang)}"
        except Exception:
            pass
    elif policy.is_peak_hour(now):
        return f"{base}，{_t('global_peak_double', lang)}"
    return f"{base}，{_t('off_peak', lang)}"


def _route(
    difficulty: int,
    *,
    urgent: bool,
    now,
    quota_snapshot,
    policy_store=None,
    quota_tracker=None,
) -> dict:
    store = _policy_store(policy_store)
    lang = _lang(store)
    if now is None:
        now = datetime.now()
    try:
        difficulty = max(0, min(5, int(difficulty or 0)))
    except (TypeError, ValueError):
        difficulty = 0
    urgent = bool(urgent)

    # 决策链按 role 驱动（见 policy.ROUTE_CHAINS），与具体模型名解耦。
    if urgent:
        chain = policy.ROUTE_CHAINS.get("urgent", ["stable"])
        chain_label = _t("urgent", lang)
    elif difficulty >= 4:
        chain = policy.ROUTE_CHAINS.get("complex", ["free-flagship"])
        chain_label = _t("complex", lang)
    elif 2 <= difficulty <= 3:
        chain = policy.ROUTE_CHAINS.get("daily", ["free-bulk"])
        chain_label = _t("daily", lang)
    else:
        chain = policy.ROUTE_CHAINS.get("simple", ["free-bulk"])
        chain_label = _t("simple", lang)

    sep = "，" if lang == "zh" else ", "

    for role in chain:
        # 免费 role 先查免费模型；stable/paid-fallback 是付费兜底，免费/付费都允许
        # （付费模型没有额度概念，cost=paid 恒可用）。
        if role in ("stable", "paid-fallback"):
            candidates = store.find_by_role(role)
        else:
            candidates = store.find_by_role(role, cost="free")
        for entry in candidates:
            if not _entry_available(entry, quota_snapshot, now, quota_tracker):
                continue
            model = str(entry.get("id") or "").strip()
            provider = str(entry.get("provider") or "").strip()
            label = str(entry.get("label") or f"{model}@{provider}")
            if role == "stable":
                reason = f"{chain_label}{sep}{_t('stable', lang, label=label)}"
            elif role == "paid-fallback":
                reason = _paid_warning(
                    now,
                    f"{chain_label}{sep}{_t('fallback', lang, label=label)}",
                    store, model, provider,
                )
            else:
                reason = f"{chain_label}{sep}{_t('free_available', lang, label=label)}"
            return _build_result(model, provider, reason, store)

    # 兜底：连付费候选都没有时返回空结果（理论上不会到，chain 末尾必有付费）。
    return {
        "model": "",
        "provider": "",
        "reason": f"{chain_label}{sep}{_t('no_model', lang)}",
        "tier": "",
        "cost": "paid",
    }


def route_model(difficulty: int, *, urgent: bool, now=None, quota_snapshot=None, policy_store=None) -> dict:
    """核心路由决策。quota_snapshot 可选：{id@provider: remaining}，缺省时实时查 quota。

    policy_store 可选：传入 ModelPolicy 实例（自定义 state 目录/画像），缺省用模块级默认。
    """
    if now is None:
        now = datetime.now()
    return _route(
        difficulty,
        urgent=urgent,
        now=now,
        quota_snapshot=quota_snapshot,
        policy_store=policy_store,
    )


def _normalise_session_id(session_id):
    """Return a non-empty session id for result passthrough, or None.

    session_id is an opaque caller-provided correlation value: when non-empty,
    it is appended to the recommendation result for log association / cache
    keys. It is never used by difficulty assessment, routing, or quota
    decisions.
    """
    if session_id is None:
        return None
    if isinstance(session_id, str):
        session_id = session_id.strip()
    if session_id == "":
        return None
    return session_id


def _recommend_core(route_fn, session_text: str, *, message_count: int = 0, session_id=None,
                    now=None, quota_snapshot=None, peak_policy_store=None) -> dict:
    """评估难度/紧急度 -> 路由 -> 组装推荐结果（两个入口共享）。"""
    if now is None:
        now = datetime.now()
    text = str(session_text or "")
    difficulty = assess_difficulty(text)
    urgent = assess_urgency(text)
    route = route_fn(difficulty, urgent=urgent, now=now, quota_snapshot=quota_snapshot)
    try:
        messages = max(0, int(message_count or 0))
    except (TypeError, ValueError):
        messages = 0
    result = {
        "difficulty": difficulty,
        "urgent": urgent,
        "message_count": messages,
        "peak": _peak_for_route(route, now, peak_policy_store),
        **route,
        "key": format_model_key(route.get("model"), route.get("provider")),
    }
    sid = _normalise_session_id(session_id)
    if sid is not None:
        result["session_id"] = sid
    return result


def recommend_for_session(session_text: str, *, message_count: int = 0, session_id=None,
                          now=None, quota_snapshot=None, policy_store=None) -> dict:
    """会话创建/新消息前的推荐入口。

    session_id 为非空时透传到结果末尾（None/空串/纯空白不追加），
    不参与难度评估、路由决策或配额判断。
    """
    def route(difficulty, *, urgent, now, quota_snapshot):
        return route_model(difficulty, urgent=urgent, now=now, quota_snapshot=quota_snapshot, policy_store=policy_store)

    return _recommend_core(
        route,
        session_text,
        message_count=message_count,
        session_id=session_id,
        now=now,
        quota_snapshot=quota_snapshot,
        peak_policy_store=None,
    )



def _peak_for_route(route: dict, now, policy_store=None) -> bool:
    """按推荐结果中模型的峰谷时段判断当前是否高峰（无模型时回退全局判断）。"""
    model = str(route.get("model") or "").strip()
    if not model:
        return policy.is_peak_hour(now)
    store = _policy_store(policy_store)
    try:
        return bool(store.is_peak_hour_for(now, model_id=model, provider=route.get("provider")))
    except Exception:
        return policy.is_peak_hour(now)


class ModelRouter:
    """可参数化 state 目录的路由器（供多实例/多租户场景使用）。"""

    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        policy_store=None,
        quota_tracker=None,
    ) -> None:
        if policy_store is not None:
            self.policy = policy_store
        else:
            self.policy = policy.ModelPolicy(state_dir)
        if quota_tracker is not None:
            self.quota = quota_tracker
        else:
            self.quota = quota.QuotaTracker(
                state_dir=getattr(self.policy, "state_dir", state_dir),
                policy_store=self.policy,
            )

    def route_model(self, difficulty: int, *, urgent: bool, now=None, quota_snapshot=None) -> dict:
        if now is None:
            now = datetime.now()
        return _route(
            difficulty,
            urgent=urgent,
            now=now,
            quota_snapshot=quota_snapshot,
            policy_store=self.policy,
            quota_tracker=self.quota,
        )

    def recommend_for_session(self, session_text: str, *, message_count: int = 0,
                              session_id=None, now=None, quota_snapshot=None) -> dict:
        return _recommend_core(
            self.route_model, session_text, message_count=message_count,
            session_id=session_id, now=now, quota_snapshot=quota_snapshot,
            peak_policy_store=self.policy,
        )

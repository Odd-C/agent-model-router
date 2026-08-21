"""model_scheduler.utility — v0.4 Utility 效用评分。

综合质量、成本、延迟、健康度、额度压力、截止紧迫，为每个候选模型打一个
可解释的效用分。所有分项均为 [0,1]；单候选评分按绝对分合成：

    Utility = quality_fit×wq − cost_penalty×wc − latency_penalty×wl
              − failure_risk×wf − quota_pressure×wqp + deadline_pressure×wdp

多候选评分先做候选集内相对归一化（min-max），把每个分项映射为「该维
最优=1.0、最差=0.0」的相对分后，再加权求和。负向分项在归一化时已经
翻转为「越大越好」，因此归一化后的加权求和全部为正向贡献：

    Utility_norm = Σ normalized(feature) × weight

``route_with_utility`` 对给定候选列表逐个打分，返回最高分者，并携带
raw / normalized / weighted 三层 breakdown，让「为什么选 A 不选 B」可解释。
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import policy, quota
from .preferences import DEFAULT_WEIGHTS, WEIGHT_KEYS, PreferencesStore

logger = logging.getLogger(__name__)

# 截止压力时间窗：deadline 在 1 小时内到期时，deadline_pressure 线性上升。
DEADLINE_PRESSURE_HORIZON = 3600.0

# 延迟惩罚归一化基准：p95 达到 1000ms 时 latency_penalty = 1.0。
LATENCY_P95_FULL_MS = 1000.0

# 无健康档案时的默认 p95 延迟惩罚 / 失败风险先验。
DEFAULT_LATENCY_PENALTY = 0.3
DEFAULT_FAILURE_RISK = 0.2

ALL_TIERS = ["S+", "S", "A", "A-", "B+", "B", "C"]

# task_type -> 期望 tier 列表（可配置字典）。空列表表示「任意 tier」。
# simple 使用 ALL_TIERS 表示任意档位均可。
DEFAULT_TASK_TYPE_TIER_EXPECTATION: dict[str, list[str]] = {
    "coding": ["S+", "S", "A"],
    "complex": ["S+", "S", "A"],
    "daily": ["A", "A-", "B+"],
    "simple": ALL_TIERS,
    "image": ["S+", "S", "A", "A-", "B+"],   # 图片/视觉类，tier 区间放宽但需配能力校验
    "vision": ["S+", "S", "A", "A-", "B+"],  # 视觉理解同 image
    "batch": ["S+", "S", "A", "A-", "B+"],   # 批量处理，tier 区间放宽
    "maintenance": ["S+", "S", "A", "A-", "B+"],  # 维护/部署
}

# 正向分项：原始值越大越好。
_POSITIVE_FEATURES = ("quality_fit", "deadline_pressure")
# 负向分项：原始值越大越差；归一化时翻转为「越大越好」。
_NEGATIVE_FEATURES = ("cost_penalty", "latency_penalty", "failure_risk", "quota_pressure")


@dataclass
class UtilityScore:
    """一次 Utility 评分的可解释结果。"""

    score: float
    breakdown: dict
    why: str


@dataclass
class HardConstraints:
    """硬约束：先砍后评，不可谈判。

    ``satisfies()`` 逐条检查候选是否满足全部硬约束。软约束（权重）只在
    通过硬约束的候选中进行。
    """

    cost_max: str | None = None      # "free" / "paid" / None（None=不限）
    min_quota_left: int = 0          # 免费模型剩余额度低于此值排除（默认0=耗尽才排除）
    exclude_in_cooldown: bool = True # 冷却中排除（默认开）
    max_failure_risk: float | None = None  # health failure_risk 超此值排除（None=不限，保守默认关）
    deadline_slack_seconds: float = 0.0    # deadline 前需留的余量（deadline 不可行判定）
    # --- 新增（能力上下限）---
    max_latency_ms: float | None = None          # 延迟硬上限：p95 超此值排除
    min_quality_tier: str | None = None          # 质量硬下限：tier 低于此值排除
    min_capability_pct: float | None = None      # 能力百分比下限：候选 capability < 参考×pct 排除
    capability_reference: str | None = None      # 参考模型 id@provider（与 pct 成对使用）

    def satisfies(
        self,
        task,
        candidate,
        *,
        now=None,
        quota_left=None,
        health_score=None,
    ) -> bool:
        """逐条检查硬约束；任意一条不满足即返回 False。"""
        candidate = dict(candidate or {})
        cost = str(candidate.get("cost") or "").strip().lower()
        now_ts = _as_epoch(now)

        # 1) 成本上限：cost_max 指定时只允许对应成本档。
        if self.cost_max is not None:
            want = str(self.cost_max).strip().lower()
            if want in ("free", "paid") and cost != want:
                return False

        # 2) 免费额度耗尽：free 且剩余额度 <= min_quota_left 时排除。
        if cost == "free":
            q = quota_left
            if q is None:
                q = candidate.get("quota_left")
            if q is None:
                try:
                    q = quota.quota_left(
                        candidate.get("id"),
                        candidate.get("provider"),
                        now_ts,
                    )
                except Exception:
                    q = None
            if q is not None:
                try:
                    q_value = float(q)
                except (TypeError, ValueError):
                    q_value = None
                if q_value is not None:
                    # quota_left 为负值（如 -1）表示“未知/不受限”，放行；
                    # 只有真实额度数字才参与耗尽判定。
                    if q_value < 0:
                        pass
                    elif q_value <= float(self.min_quota_left):
                        return False

        # 3) 冷却中排除。
        if self.exclude_in_cooldown:
            try:
                cd = quota.cooldown_seconds_left(
                    candidate.get("id"),
                    candidate.get("provider"),
                    now_ts,
                )
            except Exception:
                cd = 0.0
            if cd > 0:
                return False

        # 4) 健康红线：failure_risk 超过阈值排除。
        if self.max_failure_risk is not None:
            hs = health_score
            if not isinstance(hs, dict):
                hs = candidate.get("health") if isinstance(candidate.get("health"), dict) else None
            if isinstance(hs, dict):
                value = hs.get("failure_risk")
                if value is not None:
                    try:
                        if float(value) > float(self.max_failure_risk):
                            return False
                    except (TypeError, ValueError):
                        pass

        # 5) deadline 不可行：now + 余量 > deadline 时排除（任务已经来不及）。
        deadline = _task_value(task, "deadline", None)
        if deadline is not None:
            try:
                if now_ts + float(self.deadline_slack_seconds) > float(deadline):
                    return False
            except (TypeError, ValueError):
                pass

        # 6) 延迟硬上限：p95 超过阈值排除；无档案 p95=None 视为通过，不误杀。
        if self.max_latency_ms is not None:
            hs = health_score
            if not isinstance(hs, dict):
                hs = candidate.get("health") if isinstance(candidate.get("health"), dict) else None
            if isinstance(hs, dict):
                p95 = hs.get("p95")
                if p95 is not None:
                    try:
                        if float(p95) > float(self.max_latency_ms):
                            return False
                    except (TypeError, ValueError):
                        pass

        # 7) 质量硬下限：候选 tier 低于阈值（ALL_TIERS 中位置更靠后）排除；
        #    候选 tier 缺失/未知视为不满足质量下限。
        if self.min_quality_tier is not None:
            threshold_tier = str(self.min_quality_tier).strip()
            if threshold_tier:
                if threshold_tier not in ALL_TIERS:
                    return False
                threshold_pos = ALL_TIERS.index(threshold_tier)
                candidate_tier = str(candidate.get("tier") or "").strip()
                if candidate_tier not in ALL_TIERS or ALL_TIERS.index(candidate_tier) > threshold_pos:
                    return False

        # 8) 能力百分比下限：候选 capability < 参考模型 capability × pct 排除。
        #    pct 支持 0-100（如 80）或 0-1（如 0.8）两种写法。
        if self.min_capability_pct is not None:
            try:
                pct_value = float(self.min_capability_pct)
            except (TypeError, ValueError):
                return False
            if pct_value > 1.0:
                pct_value = pct_value / 100.0
            pct_value = max(0.0, min(1.0, pct_value))

            ref_key = str(self.capability_reference or "").strip()
            if not ref_key:
                return False
            ref_model, ref_provider = _split_model_key(ref_key)
            try:
                ref_entry = policy.resolve_model(ref_model, ref_provider)
            except Exception:
                ref_entry = None
            if not isinstance(ref_entry, dict):
                return False
            ref_cap = ref_entry.get("capability")
            if ref_cap is None:
                return False
            try:
                ref_cap = max(0.0, float(ref_cap))
            except (TypeError, ValueError):
                return False

            try:
                cand_cap = float(candidate.get("capability", 0.5))
            except (TypeError, ValueError):
                cand_cap = 0.5
            cand_cap = max(0.0, cand_cap)
            if cand_cap < ref_cap * pct_value:
                return False

        return True


# 默认硬约束：成本不限、免费额度耗尽才排除、冷却中排除、健康红线关、无 deadline 余量。
DEFAULT_CONSTRAINTS = HardConstraints()


def _as_epoch(now) -> float:
    """把 None / datetime / epoch 秒统一成 epoch 秒。"""
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    try:
        return float(now)
    except (TypeError, ValueError):
        return time.time()


def _as_datetime(now) -> datetime:
    """把 epoch 秒 / datetime 统一成 datetime（供峰谷判断）。"""
    if isinstance(now, datetime):
        return now
    try:
        return datetime.fromtimestamp(float(now)).astimezone()
    except (TypeError, ValueError, OSError):
        return datetime.now().astimezone()


def _task_value(task, key: str, default=None):
    """从 Task 对象或 dict 中读取字段。"""
    if isinstance(task, dict):
        return task.get(key, default)
    return getattr(task, key, default)


def _split_model_key(key: str) -> tuple[str, str]:
    """把 ``id@provider`` / 裸 id 解析为 (model, provider)；供能力参考解析。"""
    key = str(key or "").strip()
    if not key:
        return "", ""
    if "@" in key:
        model, provider = key.split("@", 1)
        return model.strip(), provider.strip()
    return key, ""


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def task_type_tier_expectation(task_type: str) -> list[str]:
    """task_type -> 期望 tier 列表（简单映射，可配置字典）。

    未配置的 task_type 返回全部 tier（视为任意档位可用）。
    """
    key = str(task_type or "").strip().lower()
    return list(DEFAULT_TASK_TYPE_TIER_EXPECTATION.get(key, ALL_TIERS))


def quality_fit(task_type: str, candidate: dict) -> float:
    """任务类型与模型能力匹配度。

    期望 tier 匹配得 0.9；不匹配得 0.4；``scenarios`` 含 task_type 时
    再 +0.1（上限 1.0）。范围 [0,1]。

    image/vision 任务先做能力校验：候选必须具备视觉能力（scenarios 或
    role 含 vision/image），否则直接返回 0.0（宁可不选也不选错）。
    """
    task_type = str(task_type or "").strip()

    # 能力校验：image/vision 任务要求候选具备视觉能力
    tt = task_type.lower()
    if tt in ("image", "vision"):
        raw_scenarios = candidate.get("scenarios") or []
        if isinstance(raw_scenarios, str):
            raw_scenarios = [raw_scenarios]
        scenarios = {str(s).strip().lower() for s in raw_scenarios if str(s).strip()}
        role = str(candidate.get("role") or "").lower()
        has_vision = (
            "vision" in scenarios
            or "image" in scenarios
            or "vision" in role
            or "image" in role
        )
        if not has_vision:
            return 0.0  # 无视觉能力 → 质量匹配为 0（宁可选不到也不选错）

    tier = str(candidate.get("tier") or "").strip()
    expected = task_type_tier_expectation(task_type)
    matched = (len(expected) == 0) or (tier in expected)
    score = 0.9 if matched else 0.4

    scenarios = candidate.get("scenarios") or []
    if isinstance(scenarios, str):
        scenarios = [scenarios]
    scenario_keys = {str(s).strip().lower() for s in scenarios if str(s).strip()}
    if task_type and task_type.lower() in scenario_keys:
        score += 0.1
    return _clamp(score)


def _is_peak_for_candidate(candidate: dict, now=None) -> bool:
    """候选模型当前是否处于高峰（per-model peak_hours > 全局默认）。"""
    dt = _as_datetime(now)
    peak_hours = candidate.get("peak_hours")
    if peak_hours is not None:
        return bool(policy.is_peak_hour(dt, peak_hours=peak_hours))
    return bool(policy.is_peak_hour(dt))


def cost_penalty(candidate: dict, *, now=None) -> float:
    """成本惩罚：paid > free；peak_safe=False 且高峰时段额外扣分。范围 [0,1]。"""
    cost = str(candidate.get("cost") or "paid").strip().lower()
    score = 0.0 if cost == "free" else 0.6

    peak_safe = bool(candidate.get("peak_safe", True))
    if not peak_safe and _is_peak_for_candidate(candidate, now):
        score += 0.4
    return _clamp(score)


def latency_penalty(health_score: dict | None = None, *, priority: str = "normal") -> float:
    """延迟惩罚：来自 ProviderHealth 的 p95 延迟（无档案默认 0.3）。

    p95 以毫秒计，1000ms 封顶 1.0；priority=high 时放大 1.5 倍（封顶 1.0）。
    范围 [0,1]。
    """
    priority = str(priority or "normal").strip().lower()
    p95 = None
    if isinstance(health_score, dict):
        p95 = health_score.get("p95")
    if p95 is None:
        raw = DEFAULT_LATENCY_PENALTY
    else:
        try:
            p95 = float(p95)
        except (TypeError, ValueError):
            raw = DEFAULT_LATENCY_PENALTY
        else:
            raw = _clamp(max(0.0, p95) / LATENCY_P95_FULL_MS)
    if priority == "high":
        raw = raw * 1.5
    return _clamp(raw)


def failure_risk(health_score: dict | None = None) -> float:
    """失败风险：来自 ProviderHealth 的近期 429/5xx 率；无档案默认 0.2。

    范围 [0,1]。
    """
    if isinstance(health_score, dict):
        value = health_score.get("failure_risk")
        if value is not None:
            try:
                return _clamp(float(value))
            except (TypeError, ValueError):
                pass
    return DEFAULT_FAILURE_RISK


def quota_pressure(candidate: dict, quota_left: int | float | None = None) -> float:
    """额度压力：免费模型 quota 剩余越少扣越多；paid 无额度概念 = 0。

    ``quota_left`` 未显式给定时会尝试读取 candidate["quota_left"]；
    再没有则返回 0（调用方负责注入实时剩余额度）。范围 [0,1]。
    """
    if str(candidate.get("cost") or "").strip().lower() != "free":
        return 0.0
    quota_per_window = candidate.get("quota_per_window")
    try:
        quota_per_window = int(quota_per_window)
    except (TypeError, ValueError):
        return 0.0
    if quota_per_window <= 0:
        return 0.0

    if quota_left is None:
        quota_left = candidate.get("quota_left")
    if quota_left is None:
        return 0.0
    try:
        quota_left = float(quota_left)
    except (TypeError, ValueError):
        return 0.0
    if quota_left < 0:
        # -1 等负值表示“未知/不受限”，不产生额度压力。
        return 0.0

    ratio = _clamp(max(0.0, quota_left) / float(quota_per_window))
    return 1.0 - ratio


def deadline_pressure(deadline: float | None, now=None) -> float:
    """截止压力：有 deadline 且临近时加分；无 deadline = 0。范围 [0,1]。"""
    if deadline is None:
        return 0.0
    now_ts = _as_epoch(now)
    try:
        deadline = float(deadline)
    except (TypeError, ValueError):
        return 0.0
    if deadline <= now_ts:
        return 1.0
    remaining = deadline - now_ts
    if remaining >= DEADLINE_PRESSURE_HORIZON:
        return 0.0
    return _clamp(1.0 - remaining / DEADLINE_PRESSURE_HORIZON)


def _effective_weights(weights: dict[str, float]) -> dict[str, float]:
    """把调用方权重补全为六个分项的有效权重。

    非法值（无法转换为 float 或非有限数）抛出 ValueError，避免 TypeError
    泄漏给调用方。
    """
    weights = dict(weights or {})
    effective: dict[str, float] = {}
    for key in WEIGHT_KEYS:
        raw = weights.get(key, DEFAULT_WEIGHTS["balanced"].get(key, 1.0))
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid weight for {key}: {raw!r}") from exc
        if not math.isfinite(value):
            raise ValueError(f"invalid weight for {key}: {raw!r}")
        effective[key] = value
    return effective


def _signed_weighted(raw: dict, safe_weights: dict[str, float]) -> dict[str, float]:
    """单候选绝对分合成：正项加、负项减。"""
    return {
        "quality_fit": raw["quality_fit"] * safe_weights["quality_fit"],
        "cost_penalty": -raw["cost_penalty"] * safe_weights["cost_penalty"],
        "latency_penalty": -raw["latency_penalty"] * safe_weights["latency_penalty"],
        "failure_risk": -raw["failure_risk"] * safe_weights["failure_risk"],
        "quota_pressure": -raw["quota_pressure"] * safe_weights["quota_pressure"],
        "deadline_pressure": raw["deadline_pressure"] * safe_weights["deadline_pressure"],
    }


def _normalized_weighted(normalized: dict, safe_weights: dict[str, float]) -> dict[str, float]:
    """归一化分合成：归一化后所有分项均为「越大越好」，全部正向加权。"""
    return {key: normalized[key] * safe_weights[key] for key in WEIGHT_KEYS}


def _raw_breakdown(task, candidate: dict, now_ts: float) -> dict:
    """计算单个候选的六个绝对分项。"""
    candidate = dict(candidate or {})
    task_type = str(_task_value(task, "task_type", "") or "")
    priority = str(_task_value(task, "priority", "normal") or "normal")
    deadline = _task_value(task, "deadline", None)

    health = candidate.get("health") if isinstance(candidate.get("health"), dict) else None

    qf = quality_fit(task_type, candidate)
    cp = cost_penalty(candidate, now=_as_datetime(now_ts))
    lp = latency_penalty(health, priority=priority)
    fr = failure_risk(health)

    quota_left = candidate.get("quota_left")
    if quota_left is None:
        try:
            quota_left = quota.quota_left(candidate.get("id"), candidate.get("provider"), now_ts)
        except Exception:
            quota_left = None
    qp = quota_pressure(candidate, quota_left)

    dp = deadline_pressure(deadline, now_ts)

    return {
        "quality_fit": qf,
        "cost_penalty": cp,
        "latency_penalty": lp,
        "failure_risk": fr,
        "quota_pressure": qp,
        "deadline_pressure": dp,
    }


def _format_why(raw: dict, weighted: dict[str, float]) -> str:
    return (
        f"quality_fit={raw['quality_fit']:.2f}(weighted {weighted['quality_fit']:+.2f}), "
        f"cost_penalty={raw['cost_penalty']:.2f}(weighted {weighted['cost_penalty']:+.2f}), "
        f"latency_penalty={raw['latency_penalty']:.2f}(weighted {weighted['latency_penalty']:+.2f}), "
        f"failure_risk={raw['failure_risk']:.2f}(weighted {weighted['failure_risk']:+.2f}), "
        f"quota_pressure={raw['quota_pressure']:.2f}(weighted {weighted['quota_pressure']:+.2f}), "
        f"deadline_pressure={raw['deadline_pressure']:.2f}(weighted {weighted['deadline_pressure']:+.2f})"
    )


def _format_normalized_why(raw: dict, normalized: dict, weighted: dict[str, float]) -> str:
    return (
        f"quality_fit raw={raw['quality_fit']:.2f} norm={normalized['quality_fit']:.2f} (weighted {weighted['quality_fit']:+.2f}), "
        f"cost_penalty raw={raw['cost_penalty']:.2f} norm={normalized['cost_penalty']:.2f} (weighted {weighted['cost_penalty']:+.2f}), "
        f"latency_penalty raw={raw['latency_penalty']:.2f} norm={normalized['latency_penalty']:.2f} (weighted {weighted['latency_penalty']:+.2f}), "
        f"failure_risk raw={raw['failure_risk']:.2f} norm={normalized['failure_risk']:.2f} (weighted {weighted['failure_risk']:+.2f}), "
        f"quota_pressure raw={raw['quota_pressure']:.2f} norm={normalized['quota_pressure']:.2f} (weighted {weighted['quota_pressure']:+.2f}), "
        f"deadline_pressure raw={raw['deadline_pressure']:.2f} norm={normalized['deadline_pressure']:.2f} (weighted {weighted['deadline_pressure']:+.2f})"
    )


def normalize_breakdowns(breakdowns: list[dict]) -> list[dict]:
    """候选集内相对归一化（min-max）。

    输入为各候选的 raw 分项 dict 列表，输出为带 normalized 的列表：:

        [{"raw": {...}, "normalized": {...}}, ...]

    归一化规则：
      - 正向分项（quality_fit、deadline_pressure）：norm = (v - min) / (max - min)
      - 负向分项（cost_penalty、latency_penalty、failure_risk、quota_pressure）：
        norm = (max - v) / (max - min)
      - max == min 时该分项 norm = 1.0（不放大也不惩罚）
    """
    if not breakdowns:
        return []

    keys = list(WEIGHT_KEYS)
    by_key: dict[str, list[float]] = {key: [] for key in keys}
    for bd in breakdowns:
        bd = dict(bd or {})
        for key in keys:
            try:
                by_key[key].append(float(bd.get(key, 0.0)))
            except (TypeError, ValueError):
                by_key[key].append(0.0)

    out: list[dict] = []
    for bd in breakdowns:
        bd = dict(bd or {})
        normalized: dict[str, float] = {}
        for key in keys:
            try:
                value = float(bd.get(key, 0.0))
            except (TypeError, ValueError):
                value = 0.0
            vmin = min(by_key[key])
            vmax = max(by_key[key])
            if vmax == vmin:
                normalized[key] = 1.0
            elif key in _POSITIVE_FEATURES:
                normalized[key] = _clamp((value - vmin) / (vmax - vmin))
            else:
                normalized[key] = _clamp((vmax - value) / (vmax - vmin))
        out.append({"raw": bd, "normalized": normalized})
    return out


def utility(task, candidate: dict, time, weights: dict[str, float]) -> UtilityScore:
    """核心评分函数（单候选绝对分，不归一化）。

    ``task`` 可为 Task 对象或 dict（读取 task_type/priority/deadline）。
    ``candidate`` 是 policy 画像 dict；可选注入 ``quota_left`` 与
    ``health`` 键（route_with_utility 会自动注入）。
    ``time`` 为评分时间（epoch 秒或 datetime）。
    ``weights`` 来自 ``preferences.get_effective_weights()``。
    """
    candidate = dict(candidate or {})
    now_ts = _as_epoch(time)
    raw = _raw_breakdown(task, candidate, now_ts)

    safe_weights = _effective_weights(weights)
    weighted = _signed_weighted(raw, safe_weights)
    score = sum(weighted.values())

    breakdown = {
        "quality_fit": raw["quality_fit"],
        "cost_penalty": raw["cost_penalty"],
        "latency_penalty": raw["latency_penalty"],
        "failure_risk": raw["failure_risk"],
        "quota_pressure": raw["quota_pressure"],
        "deadline_pressure": raw["deadline_pressure"],
        "weights": safe_weights,
        "weighted": weighted,
    }
    why = _format_why(raw, weighted)

    return UtilityScore(score=score, breakdown=breakdown, why=why)


def _route_result(candidate: dict, us: UtilityScore) -> dict:
    model = str(candidate.get("id") or "").strip()
    provider = str(candidate.get("provider") or "").strip()
    reason = f"{model}@{provider}: {us.why}" if provider else f"{model}: {us.why}"
    return {
        "model": model,
        "provider": provider,
        "reason": reason,
        "score": us.score,
        "breakdown": us.breakdown,
        "why": us.why,
    }


def _route_no_candidates() -> dict:
    return {
        "model": "",
        "provider": "",
        "reason": "no candidates",
        "score": 0.0,
        "breakdown": {},
        "why": "no candidates",
    }


def route_with_utility(
    task,
    candidates,
    *,
    now=None,
    preferences=None,
    health=None,
    constraints: HardConstraints | None = None,
) -> dict:
    """对给定候选列表算 UtilityScore，返回分最高者。

    ``candidates`` 应为 policy 画像 dict 列表。候选分数并列时按传入顺序
    优先（调用方按现有 role 链顺序传入即可保持 v0.3 的链序语义）。
    ``preferences`` 可为 PreferencesStore 或普通权重 dict；缺省读取默认
    PreferencesStore（无文件时即 balanced）。
    ``health`` 可为 ProviderHealth 实例；缺省不注入健康档案（使用默认先验）。
    ``constraints`` 非 None 时先按硬约束过滤候选，再对剩余候选评分。
    """
    now_ts = _as_epoch(now)

    if preferences is None:
        try:
            weights = PreferencesStore().get_effective_weights()
        except Exception:
            weights = dict(DEFAULT_WEIGHTS["balanced"])
    elif hasattr(preferences, "get_effective_weights"):
        weights = preferences.get_effective_weights()
    elif isinstance(preferences, dict):
        weights = dict(preferences)
    else:
        weights = dict(DEFAULT_WEIGHTS["balanced"])

    cands = list(candidates or [])
    if not cands:
        return _route_no_candidates()

    prepared: list[tuple[int, dict]] = []
    for idx, raw_candidate in enumerate(cands):
        if not isinstance(raw_candidate, dict):
            continue
        candidate = dict(raw_candidate)

        health_data = None
        if health is not None and hasattr(health, "health_score"):
            try:
                health_data = health.health_score(
                    candidate.get("id"),
                    candidate.get("provider"),
                    now=now_ts,
                )
            except Exception:
                health_data = None
        if isinstance(health_data, dict):
            candidate["health"] = health_data

        if "quota_left" not in candidate:
            try:
                candidate["quota_left"] = quota.quota_left(
                    candidate.get("id"),
                    candidate.get("provider"),
                    now_ts,
                )
            except Exception:
                candidate["quota_left"] = None

        if constraints is not None:
            health_score = candidate.get("health") if isinstance(candidate.get("health"), dict) else None
            if not constraints.satisfies(
                task,
                candidate,
                now=now_ts,
                quota_left=candidate.get("quota_left"),
                health_score=health_score,
            ):
                continue

        # 能力硬过滤：quality_fit == 0.0 表示该候选不具备任务所需能力
        # （例如 image/vision 任务遇到纯文本模型），直接剔除，避免后续
        # min-max 归一化把「全部 0 分」拉回 1.0 后被 cost/latency 选上。
        task_type = str(_task_value(task, "task_type", "") or "")
        if quality_fit(task_type, candidate) <= 0.0:
            continue

        prepared.append((idx, candidate))

    if not prepared:
        return _route_no_candidates()

    # 单候选退化为绝对分（无归一化参照）。
    if len(prepared) == 1:
        _, candidate = prepared[0]
        us = utility(task, candidate, now_ts, weights)
        # 标注单候选：归一化字段为 None + 说明，避免调用方把绝对分误当相对分。
        us.breakdown["normalized"] = None
        us.breakdown["note"] = "single candidate — no normalization reference"
        return _route_result(candidate, us)

    # 多候选：先算 raw → 候选集内 min-max 归一化 → 加权 → 选最高。
    raws = [_raw_breakdown(task, candidate, now_ts) for _, candidate in prepared]
    normalized_entries = normalize_breakdowns(raws)
    safe_weights = _effective_weights(weights)

    scored: list[tuple[int, dict, UtilityScore]] = []
    for (idx, candidate), norm_entry in zip(prepared, normalized_entries):
        raw = norm_entry["raw"]
        normalized = norm_entry["normalized"]
        weighted = _normalized_weighted(normalized, safe_weights)
        score = sum(weighted.values())

        breakdown = {
            "quality_fit": raw["quality_fit"],
            "cost_penalty": raw["cost_penalty"],
            "latency_penalty": raw["latency_penalty"],
            "failure_risk": raw["failure_risk"],
            "quota_pressure": raw["quota_pressure"],
            "deadline_pressure": raw["deadline_pressure"],
            "raw": raw,
            "normalized": normalized,
            "weights": safe_weights,
            "weighted": weighted,
        }
        why = _format_normalized_why(raw, normalized, weighted)
        scored.append((idx, candidate, UtilityScore(score=score, breakdown=breakdown, why=why)))

    best_idx, best_candidate, best_score = max(
        scored,
        key=lambda item: (item[2].score, -item[0]),
    )
    return _route_result(best_candidate, best_score)


__all__ = [
    "ALL_TIERS",
    "DEFAULT_CONSTRAINTS",
    "DEFAULT_FAILURE_RISK",
    "DEFAULT_LATENCY_PENALTY",
    "DEFAULT_TASK_TYPE_TIER_EXPECTATION",
    "DEADLINE_PRESSURE_HORIZON",
    "LATENCY_P95_FULL_MS",
    "HardConstraints",
    "UtilityScore",
    "cost_penalty",
    "deadline_pressure",
    "failure_risk",
    "latency_penalty",
    "normalize_breakdowns",
    "quality_fit",
    "quota_pressure",
    "route_with_utility",
    "task_type_tier_expectation",
    "utility",
]

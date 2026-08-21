"""agent_model_router.policy_compiler — v0.5 Policy Compiler 策略翻译器。

把用户的自然语言意图（中文/英文关键词）翻译成 v0.4 评分流水线可执行的
``HardConstraints`` + 六分项权重 + mode + explanation。第一版为规则模板
匹配，不依赖任何 NLP 库。

规则表 ``INTENT_RULES`` 与能力参考表 ``CAPABILITY_REFERENCES`` 均可按需
扩展；本模块 import 零 I/O 副作用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .preferences import DEFAULT_WEIGHTS
from .utility import ALL_TIERS, HardConstraints, route_with_utility

DEFAULT_MIN_QUALITY_TIER = "A"
DEFAULT_MAX_LATENCY_MS = 3000.0

# 能力参考模型映射表（公开示例，可配置）。用户说 "达到 gpt-4o 的 80%"
# 时，编译器将其翻译为 capability_reference="gpt-4o@openai"。
CAPABILITY_REFERENCES: dict[str, str] = {
    "gpt-4o": "gpt-4o@openai",
    "gemini": "gemini-2.0-flash@google",
    "deepseek": "deepseek-chat@deepseek",
}

# 成本类意图中，出现以下「放宽成本」关键词时，不设 cost_max="free"。
_COST_RELAX_KEYWORDS = ("付费", "收费", "paid", "贵一点也行", "贵点也行", "贵一点也可以")

# 速度类意图中的数字+时间单位解析（"3 秒内出结果" / "500ms" / "0.5s"）。
_LATENCY_MS_PATTERN = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>ms|毫秒|s|sec|second|seconds|秒)",
    re.IGNORECASE,
)

# 能力百分比意图：英文 "80% of gpt-4o"；中文 "达到 gpt-4o 的 80%"。
_CAPABILITY_PATTERNS = (
    re.compile(
        r"(?P<num>\d+(?:\.\d+)?)\s*%\s*of\s+(?P<ref>[A-Za-z0-9][A-Za-z0-9._-]*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<ref>[A-Za-z0-9][A-Za-z0-9._-]*)\s*的\s*(?P<num>\d+(?:\.\d+)?)\s*%",
    ),
)

# 规则表：priority 数值越小优先级越高。quality > cost > speed > balanced。
# capability 归属质量类意图（mode=quality-first），优先级介于 quality 与 cost 之间。
# ``constraints`` 为静态默认值；速度/能力两类会按文本中的数字动态解析覆盖。
INTENT_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "quality",
        "priority": 10,
        "mode": "quality-first",
        "keywords": ("高质量", "最好", "旗舰", "最强", "quality", "best", "flagship"),
        "constraints": {"min_quality_tier": DEFAULT_MIN_QUALITY_TIER},
    },
    {
        "id": "capability",
        "priority": 15,
        "mode": "quality-first",
        "keywords": (),
        "constraints": {},
    },
    {
        "id": "cost",
        "priority": 20,
        "mode": "cost-first",
        "keywords": ("便宜", "省钱", "免费", "额度", "预算", "太贵", "cheap", "free", "cost", "budget"),
        "constraints": {"cost_max": "free"},
    },
    {
        "id": "speed",
        "priority": 30,
        "mode": "latency-first",
        "keywords": ("快", "迅速", "尽快", "马上", "秒出", "秒", "instant", "fast", "speed", "latency"),
        "constraints": {"max_latency_ms": DEFAULT_MAX_LATENCY_MS},
    },
    {
        "id": "balanced",
        "priority": 40,
        "mode": "balanced",
        "keywords": ("均衡", "一般", "都行", "balanced"),
        "constraints": {},
    },
)

_MODE_PRIORITY = {
    "quality-first": 0,
    "cost-first": 1,
    "latency-first": 2,
    "balanced": 3,
}


@dataclass
class CompiledPolicy:
    """策略编译器产物：硬约束 + 权重 + mode + 可读说明。"""

    constraints: HardConstraints
    weights: dict[str, float]
    mode: str
    explanation: str


def _is_chinese(text: str) -> bool:
    """粗判输入是否含中文（用于 explanation / describe 跟随输入语言）。"""
    return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))


def _contains_any(lower_text: str, keywords: tuple[str, ...]) -> bool:
    return any(k and k in lower_text for k in keywords)


def _extract_latency_ms(text: str) -> float | None:
    """从文本解析延迟上限（毫秒）。支持 500ms / 0.5s / 3 秒 等写法。"""
    match = _LATENCY_MS_PATTERN.search(str(text or ""))
    if not match:
        return None
    try:
        num = float(match.group("num"))
    except (TypeError, ValueError):
        return None
    unit = str(match.group("unit") or "").strip().lower()
    if unit in ("ms", "毫秒"):
        return num
    if unit in ("s", "sec", "second", "seconds", "秒"):
        return num * 1000.0
    return num


def _extract_capability(text: str) -> tuple[float, str] | None:
    """从文本解析能力百分比与参考模型，返回 (pct, capability_reference)。"""
    raw = str(text or "")
    for pattern in _CAPABILITY_PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue
        try:
            pct = float(match.group("num"))
        except (TypeError, ValueError):
            continue
        ref = str(match.group("ref") or "").strip()
        if not ref:
            continue
        return pct, _map_capability_reference(ref)
    return None


def _map_capability_reference(name: str) -> str:
    """把用户口中的参考模型名映射为 id@provider；未收录时原样返回。"""
    raw = str(name or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        return raw
    key = raw.lower()
    for short, full in CAPABILITY_REFERENCES.items():
        if key == short:
            return full
    return raw


def _rule_matches(rule: dict[str, Any], text: str, lower_text: str) -> bool:
    """判断单条意图规则是否命中。"""
    rule_id = str(rule.get("id") or "")
    if rule_id == "speed":
        return _contains_any(lower_text, rule.get("keywords") or ()) or _extract_latency_ms(text) is not None
    if rule_id == "capability":
        return _extract_capability(text) is not None
    return _contains_any(lower_text, rule.get("keywords") or ())


def _apply_rule(
    rule: dict[str, Any],
    text: str,
    lower_text: str,
    constraint_kwargs: dict[str, Any],
    phrases: list[str],
    lang: str,
) -> None:
    """把命中的规则翻译为硬约束字段，并追加人类可读意图短语。"""
    rule_id = str(rule.get("id") or "")
    if rule_id == "quality":
        constraint_kwargs.setdefault("min_quality_tier", DEFAULT_MIN_QUALITY_TIER)
        phrases.append("高质量" if lang == "zh" else "high quality")
    elif rule_id == "cost":
        if _contains_any(lower_text, _COST_RELAX_KEYWORDS):
            # 用户明确说付费也行/贵一点也行：成本上限放开（None=不限）。
            constraint_kwargs["cost_max"] = None
            phrases.append("成本可放宽" if lang == "zh" else "cost-flexible")
        else:
            constraint_kwargs["cost_max"] = "free"
            phrases.append("省钱" if lang == "zh" else "cost-saving")
    elif rule_id == "speed":
        parsed = _extract_latency_ms(text)
        constraint_kwargs["max_latency_ms"] = parsed if parsed is not None else DEFAULT_MAX_LATENCY_MS
        phrases.append("速度优先" if lang == "zh" else "speed-first")
    elif rule_id == "capability":
        capability = _extract_capability(text)
        if capability is not None:
            pct, ref = capability
            constraint_kwargs["min_capability_pct"] = pct
            constraint_kwargs["capability_reference"] = ref
            phrases.append("能力达标" if lang == "zh" else "capability-fit")
    elif rule_id == "balanced":
        phrases.append("均衡" if lang == "zh" else "balanced")


def _constraints_summary(constraints: HardConstraints, lang: str = "zh") -> str:
    """把硬约束翻译成简短人类可读摘要（中文或英文）。"""
    zh = lang == "zh"
    parts: list[str] = []
    if constraints.cost_max is not None:
        want = str(constraints.cost_max).strip().lower()
        if want == "free":
            parts.append("只用免费模型" if zh else "free models only")
        elif want == "paid":
            parts.append("只用付费模型" if zh else "paid models only")
    if constraints.min_quality_tier:
        parts.append(
            f"至少 {constraints.min_quality_tier} 档" if zh else f"tier >= {constraints.min_quality_tier}"
        )
    if constraints.max_latency_ms is not None:
        parts.append(
            f"延迟 ≤ {constraints.max_latency_ms:g}ms" if zh else f"latency <= {constraints.max_latency_ms:g}ms"
        )
    if constraints.min_capability_pct is not None:
        ref = constraints.capability_reference or "reference"
        parts.append(
            f"能力 ≥ {ref} 的{constraints.min_capability_pct:g}%"
            if zh
            else f"capability >= {constraints.min_capability_pct:g}% of {ref}"
        )
    if constraints.min_quota_left:
        parts.append(
            f"免费额度至少余 {constraints.min_quota_left}" if zh else f"free quota left > {constraints.min_quota_left}"
        )
    if constraints.max_failure_risk is not None:
        parts.append(
            f"失败风险 ≤ {constraints.max_failure_risk:g}" if zh else f"failure risk <= {constraints.max_failure_risk:g}"
        )
    if constraints.deadline_slack_seconds:
        parts.append(
            f"截止前留 {constraints.deadline_slack_seconds:g}s" if zh else f"deadline slack >= {constraints.deadline_slack_seconds:g}s"
        )
    if not parts:
        return "默认约束" if zh else "default constraints"
    return " + ".join(parts)


def _build_explanation(
    phrases: list[str],
    constraints: HardConstraints,
    mode: str,
    lang: str,
) -> str:
    """生成跟随输入语言的人类可读说明。"""
    if lang == "zh":
        if not phrases:
            return f"未识别到特定意图 → 硬约束：默认约束；权重：{mode}"
        return f"已识别意图：{' + '.join(phrases)} → 硬约束：{_constraints_summary(constraints, 'zh')}；权重：{mode}"
    if not phrases:
        return f"No specific intent recognized -> hard constraints: default; weights: {mode}"
    return (
        f"Recognized intents: {' + '.join(phrases)} -> "
        f"hard constraints: {_constraints_summary(constraints, 'en')}; weights: {mode}"
    )


def compile_intent(text: str) -> CompiledPolicy:
    """把一句自然语言意图翻译为 CompiledPolicy。

    - 多意图并存时：硬约束取并集（AND），mode 取最高优先级意图。
    - 无匹配时：balanced + 默认硬约束，并说明未识别到特定意图。
    """
    raw = str(text or "")
    lower = raw.lower()
    lang = "zh" if _is_chinese(raw) else "en"

    matched = [
        rule
        for rule in sorted(INTENT_RULES, key=lambda r: int(r.get("priority", 100)))
        if _rule_matches(rule, raw, lower)
    ]

    if not matched:
        constraints = HardConstraints()
        return CompiledPolicy(
            constraints=constraints,
            weights=dict(DEFAULT_WEIGHTS["balanced"]),
            mode="balanced",
            explanation=_build_explanation([], constraints, "balanced", lang),
        )

    # 已按 priority 升序排列，首位即最高优先级 mode。
    mode = str(matched[0].get("mode") or "balanced")
    constraint_kwargs: dict[str, Any] = {}
    phrases: list[str] = []

    for rule in matched:
        _apply_rule(rule, raw, lower, constraint_kwargs, phrases, lang)

    constraints = HardConstraints(**constraint_kwargs)
    weights = dict(DEFAULT_WEIGHTS.get(mode, DEFAULT_WEIGHTS["balanced"]))
    explanation = _build_explanation(phrases, constraints, mode, lang)

    return CompiledPolicy(
        constraints=constraints,
        weights=weights,
        mode=mode,
        explanation=explanation,
    )


def _mode_rank(mode: str) -> int:
    return _MODE_PRIORITY.get(str(mode or "").strip(), 99)


def _cost_rank(cost_max: str | None) -> int:
    if cost_max is None:
        return 0
    want = str(cost_max).strip().lower()
    if want == "free":
        return 2
    if want == "paid":
        return 1
    return 0


def _stricter_cost_max(a: str | None, b: str | None) -> str | None:
    """硬约束合并：成本上限取更严格者（free > paid > None）。"""
    if _cost_rank(a) >= _cost_rank(b):
        return a
    return b


def _tier_rank(tier: str) -> int:
    try:
        return ALL_TIERS.index(str(tier).strip())
    except ValueError:
        return len(ALL_TIERS) + 1


def _normalise_pct(value: float | None) -> float:
    """把 0-100 或 0-1 的百分比统一为 0-1 小数，便于比较。"""
    if value is None:
        return 0.0
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return 0.0
    if pct > 1.0:
        pct = pct / 100.0
    return max(0.0, min(1.0, pct))


def _merge_constraint_kwargs(a: HardConstraints, b: HardConstraints) -> dict[str, Any]:
    """合并两个硬约束：并集 = 逐字段取更严格者。"""
    kwargs: dict[str, Any] = {}

    # 成本上限：free 比 paid 更严格，paid 比不限更严格。
    cost_max = _stricter_cost_max(a.cost_max, b.cost_max)
    if cost_max is not None:
        kwargs["cost_max"] = cost_max

    kwargs["min_quota_left"] = max(int(a.min_quota_left or 0), int(b.min_quota_left or 0))
    kwargs["exclude_in_cooldown"] = bool(a.exclude_in_cooldown or b.exclude_in_cooldown)

    risks = [x for x in (a.max_failure_risk, b.max_failure_risk) if x is not None]
    if risks:
        kwargs["max_failure_risk"] = min(float(x) for x in risks)

    kwargs["deadline_slack_seconds"] = max(
        float(a.deadline_slack_seconds or 0),
        float(b.deadline_slack_seconds or 0),
    )

    latencies = [x for x in (a.max_latency_ms, b.max_latency_ms) if x is not None]
    if latencies:
        kwargs["max_latency_ms"] = min(float(x) for x in latencies)

    tiers = [t for t in (a.min_quality_tier, b.min_quality_tier) if t]
    if tiers:
        kwargs["min_quality_tier"] = min(tiers, key=_tier_rank)

    # 能力百分比：HardConstraints 只支持单参考模型，合并时保留要求更高的
    # 那条（按归一化 pct 比较；相同则保留 a 的）。
    cap_a = (a.min_capability_pct is not None, _normalise_pct(a.min_capability_pct))
    cap_b = (b.min_capability_pct is not None, _normalise_pct(b.min_capability_pct))
    if cap_a[0] and cap_b[0]:
        chosen = a if cap_a[1] >= cap_b[1] else b
    elif cap_a[0]:
        chosen = a
    elif cap_b[0]:
        chosen = b
    else:
        chosen = None

    if chosen is not None:
        kwargs["min_capability_pct"] = chosen.min_capability_pct
        if chosen.capability_reference:
            kwargs["capability_reference"] = chosen.capability_reference

    return kwargs


def _fill_weights(weights: dict[str, float] | None, mode: str) -> dict[str, float]:
    """补全六分项权重（缺省回填 mode 默认值）。"""
    base = dict(DEFAULT_WEIGHTS.get(mode, DEFAULT_WEIGHTS["balanced"]))
    for key, value in (weights or {}).items():
        if key in base:
            base[key] = float(value)
    return base


def merge_policies(a: CompiledPolicy, b: CompiledPolicy) -> CompiledPolicy:
    """合并两个 CompiledPolicy：硬约束取并集，mode 取优先级更高者。

    权重保留 mode 获胜方的权重（compile_intent 产出的权重来自预设 mode，
    因此该策略等价于 mode 对应的默认权重）。
    """
    if not isinstance(a, CompiledPolicy) or not isinstance(b, CompiledPolicy):
        raise TypeError("merge_policies expects two CompiledPolicy instances")

    constraints = HardConstraints(**_merge_constraint_kwargs(a.constraints, b.constraints))
    mode = a.mode if _mode_rank(a.mode) <= _mode_rank(b.mode) else b.mode
    weights = _fill_weights(a.weights if a.mode == mode else b.weights, mode)

    zh = _is_chinese(a.explanation) or _is_chinese(b.explanation)
    explanation = (
        f"合并策略：{a.explanation}；{b.explanation}"
        if zh
        else f"Merged policy: {a.explanation}; {b.explanation}"
    )

    return CompiledPolicy(
        constraints=constraints,
        weights=weights,
        mode=mode,
        explanation=explanation,
    )


def describe(compiled: CompiledPolicy) -> str:
    """返回人类可读摘要（UI/调试用）。"""
    if not isinstance(compiled, CompiledPolicy):
        raise TypeError("describe expects a CompiledPolicy")
    zh = _is_chinese(compiled.explanation)
    summary = _constraints_summary(compiled.constraints, "zh" if zh else "en")
    return (
        f"mode={compiled.mode}, weights={compiled.weights}, "
        f"hard_constraints={summary}, explanation={compiled.explanation}"
    )


class _CompiledPreferences:
    """route_with_intent 专用：把 compiled mode+weights 适配为
    route_with_utility 所需的 ``get_effective_weights()`` 协议。"""

    def __init__(self, mode: str, weights: dict[str, float]) -> None:
        self.mode = mode
        self.weights = dict(weights or {})

    def get_effective_weights(self) -> dict[str, float]:
        return dict(self.weights)


def route_with_intent(
    task,
    candidates,
    text: str,
    *,
    now=None,
    preferences=None,
    health=None,
) -> dict:
    """便捷入口：compile_intent(text) → route_with_utility(..., constraints=...)。

    ``preferences`` 缺省时，用 compiled.mode + compiled.weights 构造一个仅
    内存的 preferences 适配器（零 I/O）。若调用方显式传入 preferences /
    health，则原样透传给 route_with_utility。
    """
    compiled = compile_intent(text)
    effective_preferences = preferences
    if effective_preferences is None:
        effective_preferences = _CompiledPreferences(compiled.mode, compiled.weights)

    return route_with_utility(
        task,
        candidates,
        now=now,
        preferences=effective_preferences,
        health=health,
        constraints=compiled.constraints,
    )


__all__ = [
    "CAPABILITY_REFERENCES",
    "DEFAULT_MAX_LATENCY_MS",
    "DEFAULT_MIN_QUALITY_TIER",
    "INTENT_RULES",
    "CompiledPolicy",
    "compile_intent",
    "describe",
    "merge_policies",
    "route_with_intent",
]

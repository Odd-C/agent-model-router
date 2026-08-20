"""model_scheduler.benchmark — 路由策略基准对比工具（零第三方依赖）。

用可复现的合成任务集对比三种策略：
  - utility      : v0.4/v0.5 评分制（route_with_utility + HardConstraints）
  - chain        : v0.3 角色链制（route_model + quota_snapshot）
  - round_robin  : 朴素轮询基线

模拟维度：成本（free=0 / paid=1）、延迟（按候选 health.p95 的正态分布）、
失败（fail_rate 概率 5xx/429，失败后尝试 fallback）与免费额度递减。

用法：
    PYTHONPATH=src python3 -m model_scheduler.benchmark --tasks 200 --seed 42 [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import policy
from .router import assess_difficulty, assess_urgency, route_model
from .utility import HardConstraints, route_with_utility

# 合成任务集与模拟时钟的统一基准 epoch（2023-11-14T22:13:20Z）。
# 固定基准时间保证同 seed 的 deadline 与路由决策完全可复现。
BENCHMARK_BASE_NOW = 1_700_000_000.0

# 生成任务 payload 的文本样本（覆盖简单 / 日常 / 复杂 / 紧急）。
_SAMPLE_TEXTS: tuple[str, ...] = (
    "hello",
    "今天天气不错",
    "处理一下数据",
    "帮我看看这个代码问题",
    "这段代码有报错 error",
    "日常文本处理",
    "debug 排查一下",
    "写一个 python 脚本",
    "请写一个爬虫工具",
    "紧急 马上处理这个请求",
    "实现一个接口",
    "分析代码",
    "搭建一个网站",
    "优化代码性能",
    "修复 bug 问题",
)

_TIER_ORDER = {"S+": 0, "S": 1, "A": 2, "A-": 3, "B+": 4, "B": 5, "C": 6}


@dataclass
class BenchmarkConfig:
    """基准配置。"""

    n_tasks: int = 200
    seed: int = 42
    task_types: tuple = ("text", "coding", "image", "batch", "maintenance")
    priority_weights: tuple = (0.2, 0.5, 0.3)  # high / normal / low
    deadline_prob: float = 0.3
    latency_base_ms: float = 300.0
    fail_rate: float = 0.05
    quota_cap: int = 1000
    candidates: list[dict] | None = None


@dataclass
class BenchmarkResult:
    """单个策略的基准结果。"""

    strategy: str
    success_rate: float
    total_cost: float
    p95_latency: float
    fallback_rate: float
    quota_exhausted: int
    decisions: list[dict[str, Any]] = field(default_factory=list)
    n_tasks: int = 0
    quota_degraded: int = 0


# ---------------------------------------------------------------------------
# 候选画像与模拟辅助
# ---------------------------------------------------------------------------

def _as_epoch(now) -> float:
    """把 None / datetime / epoch 秒统一成 epoch 秒。"""
    if now is None:
        return BENCHMARK_BASE_NOW
    if hasattr(now, "timestamp"):
        return float(now.timestamp())
    try:
        return float(now)
    except (TypeError, ValueError):
        return BENCHMARK_BASE_NOW


def _model_key(candidate: dict) -> str:
    """候选画像 -> id@provider 唯一 key。"""
    mid = str(candidate.get("id") or "").strip()
    prov = str(candidate.get("provider") or "").strip()
    if not mid:
        return ""
    return f"{mid}@{prov}" if prov else mid


def _parse_key(key: str) -> tuple[str, str]:
    """id@provider -> (id, provider)。"""
    key = str(key or "").strip()
    if not key:
        return "", ""
    if "@" in key:
        mid, prov = key.split("@", 1)
        return mid.strip(), prov.strip()
    return key, ""


def _default_health(candidate: dict) -> dict[str, float]:
    """按角色给候选注入模拟健康档案（仅 benchmark 模拟用）。"""
    role = str(candidate.get("role") or "")
    cost = str(candidate.get("cost") or "paid").strip().lower()
    profiles = {
        "stable": {"p50": 120.0, "p95": 220.0, "failure_risk": 0.01},
        "paid-fallback": {"p50": 180.0, "p95": 350.0, "failure_risk": 0.03},
        "free-flagship": {"p50": 150.0, "p95": 250.0, "failure_risk": 0.02},
        "free-preview": {"p50": 220.0, "p95": 450.0, "failure_risk": 0.05},
        "free-bulk": {"p50": 300.0, "p95": 800.0, "failure_risk": 0.09},
    }
    if role in profiles:
        return dict(profiles[role])
    if cost == "free":
        return {"p50": 250.0, "p95": 600.0, "failure_risk": 0.06}
    return {"p50": 200.0, "p95": 400.0, "failure_risk": 0.04}


def _prepare_candidates(cfg: BenchmarkConfig) -> list[dict]:
    """准备候选画像列表（缺省用默认模型集），并注入模拟健康档案。"""
    if cfg.candidates is None:
        raw = [dict(entry) for entry in policy.DEFAULT_MODEL_POLICIES.values()]
    else:
        raw = [dict(c) for c in cfg.candidates if isinstance(c, dict)]

    candidates: list[dict] = []
    for entry in raw:
        mid = str(entry.get("id") or entry.get("model") or "").strip()
        if not mid:
            continue
        cand = dict(entry)
        cand["id"] = mid
        cand["provider"] = str(cand.get("provider") or "").strip()
        cost = str(cand.get("cost") or "paid").strip().lower()
        cand["cost"] = "free" if cost == "free" else "paid"
        cand.setdefault("role", "")
        cand.setdefault("tier", "B")
        cand.setdefault("capability", 0.5)
        cand.setdefault("fallback_chain", [])

        # 免费模型统一使用 cfg.quota_cap 作为模拟窗口额度。
        if cand["cost"] == "free":
            cand["quota_per_window"] = int(cfg.quota_cap)
        else:
            cand.setdefault("quota_per_window", None)

        health = cand.get("health")
        if not isinstance(health, dict):
            cand["health"] = _default_health(cand)
        else:
            merged = _default_health(cand)
            merged.update({k: float(v) for k, v in health.items() if k in ("p50", "p95", "failure_risk")})
            cand["health"] = merged
        candidates.append(cand)
    return candidates


def _initial_quota(candidates: list[dict], cfg: BenchmarkConfig) -> dict[str, int]:
    """免费候选的初始剩余额度。"""
    quota: dict[str, int] = {}
    for cand in candidates:
        if cand.get("cost") == "free":
            quota[_model_key(cand)] = int(cfg.quota_cap)
    return quota


def _find_candidate(candidates: list[dict], model: str, provider: str = "") -> dict | None:
    """按 model/provider 查找候选；provider 为空时按 model id 匹配第一个。"""
    mid = str(model or "").strip()
    prov = str(provider or "").strip()
    if not mid:
        return None
    for cand in candidates:
        if str(cand.get("id") or "").strip() != mid:
            continue
        if prov and str(cand.get("provider") or "").strip() != prov:
            continue
        return cand
    return None


def _find_candidate_by_key(candidates: list[dict], key: str) -> dict | None:
    mid, prov = _parse_key(key)
    return _find_candidate(candidates, mid, prov)


def _task_value(task, key: str, default=None):
    if isinstance(task, dict):
        return task.get(key, default)
    return getattr(task, key, default)


def _task_text(task) -> str:
    payload = _task_value(task, "payload", {}) or {}
    if isinstance(payload, dict):
        return str(payload.get("text") or payload.get("prompt") or "")
    return str(payload or "")


def _candidate_p95(candidate: dict, cfg: BenchmarkConfig) -> float:
    health = candidate.get("health")
    if isinstance(health, dict):
        try:
            p95 = float(health.get("p95", cfg.latency_base_ms))
            if p95 > 0:
                return p95
        except (TypeError, ValueError):
            pass
    return float(cfg.latency_base_ms)


def _candidate_failure_risk(candidate: dict) -> float:
    health = candidate.get("health")
    if isinstance(health, dict):
        try:
            return max(0.0, min(1.0, float(health.get("failure_risk", 0.0))))
        except (TypeError, ValueError):
            pass
    return 0.0


def _simulate_cost(candidate: dict) -> float:
    return 0.0 if str(candidate.get("cost") or "").strip().lower() == "free" else 1.0


def _simulate_latency(candidate: dict, cfg: BenchmarkConfig, rng: random.Random) -> float:
    """按候选 p95 为均值、base_ms*0.3 为标准差生成一次模拟延迟。"""
    mean = _candidate_p95(candidate, cfg)
    std = max(1.0, float(cfg.latency_base_ms) * 0.3)
    return max(1.0, rng.gauss(mean, std))


def _decrement_quota(quota_left: dict[str, int], candidate: dict) -> None:
    if str(candidate.get("cost") or "").strip().lower() != "free":
        return
    key = _model_key(candidate)
    if key:
        quota_left[key] = max(0, int(quota_left.get(key, 0)) - 1)


def _choose_fallback(
    primary: dict | None,
    primary_key: str,
    candidates: list[dict],
    quota_left: dict[str, int],
) -> dict | None:
    """选择 fallback 候选：优先主选模型的 fallback_chain，再兜底其他可用候选。"""
    if primary is not None:
        chain = primary.get("fallback_chain") or []
        for item in chain:
            fb = _find_candidate_by_key(candidates, str(item)) if isinstance(item, str) else None
            if isinstance(item, dict):
                fb = _find_candidate(candidates, str(item.get("id") or item.get("model") or ""), str(item.get("provider") or ""))
            if fb is None:
                continue
            key = _model_key(fb)
            if key == primary_key:
                continue
            if fb.get("cost") == "free" and int(quota_left.get(key, 0)) <= 0:
                continue
            return fb

    for fb in candidates:
        key = _model_key(fb)
        if key == primary_key:
            continue
        if fb.get("cost") == "free" and int(quota_left.get(key, 0)) <= 0:
            continue
        return fb
    return None


def _p95(values: list[float]) -> float:
    """最近秩法 P95。"""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    idx = max(0, int(math.ceil(0.95 * len(ordered))) - 1)
    return ordered[idx]


# ---------------------------------------------------------------------------
# 任务集生成
# ---------------------------------------------------------------------------

def generate_tasks(cfg: BenchmarkConfig | None = None) -> list[dict]:
    """生成可复现的合成任务集。

    覆盖 task_type / priority / deadline / payload 难度四个维度；同 seed
    返回完全相同的任务列表（含 deadline 绝对值）。
    """
    cfg = cfg or BenchmarkConfig()
    rng = random.Random(cfg.seed)
    task_types = list(cfg.task_types or ("text",))
    weights = list(cfg.priority_weights or (0.2, 0.5, 0.3))
    if len(weights) != 3 or sum(float(w) for w in weights) <= 0:
        weights = [0.2, 0.5, 0.3]

    tasks: list[dict] = []
    for i in range(int(cfg.n_tasks)):
        task_type = rng.choice(task_types)
        priority = rng.choices(("high", "normal", "low"), weights=weights, k=1)[0]
        has_deadline = rng.random() < float(cfg.deadline_prob)
        deadline = BENCHMARK_BASE_NOW + rng.uniform(300.0, 7200.0) if has_deadline else None
        text = rng.choice(_SAMPLE_TEXTS)
        tasks.append({
            "task_id": f"bench-{i:04d}",
            "task_type": task_type,
            "priority": priority,
            "deadline": deadline,
            "payload": {"text": text},
        })
    return tasks


# ---------------------------------------------------------------------------
# 三种策略的 route_fn 包装
# ---------------------------------------------------------------------------

class _BenchmarkPolicyStore:
    """route_model 用的内存策略画像（与 utility 使用同一候选集，无磁盘 I/O）。"""

    def __init__(self, candidates: list[dict]) -> None:
        self._models: dict[str, dict] = {}
        for cand in candidates:
            key = _model_key(cand)
            if key:
                self._models[key] = dict(cand)

    def get_policy(self) -> dict:
        return {
            "models": {k: dict(v) for k, v in self._models.items()},
            "language": "zh",
            "peak_hours": policy.DEFAULT_PEAK_HOURS,
        }

    def find_by_role(self, role: str, cost: str | None = None) -> list[dict]:
        wanted = str(role or "").strip()
        out: list[dict] = []
        for entry in self._models.values():
            if wanted and str(entry.get("role") or "") != wanted:
                continue
            if cost is not None and str(entry.get("cost") or "").lower() != str(cost).lower():
                continue
            out.append(dict(entry))
        out.sort(key=lambda e: _TIER_ORDER.get(str(e.get("tier") or ""), 99))
        return out

    def resolve_model(self, model_id: str, provider: str | None = None) -> dict | None:
        return _find_candidate(list(self._models.values()), model_id, provider or "")

    def is_peak_hour_for(self, dt=None, model_id=None, provider=None) -> bool:
        return policy.is_peak_hour(dt)


def _make_utility_route(candidates: list[dict]):
    """v0.4/v0.5 评分制：免费额度未耗尽时在免费候选内评分，耗尽后转付费池。

    这样与 chain 的 free-first 结构保持一致：两边都优先使用免费模型，
    差异只在「免费池内按效用评分」还是「按固定 role 链顺序」。付费兜底
    同样由 HardConstraints 过滤掉已耗尽的免费候选。
    """

    def route(task, *, now, quota_left: dict[str, int]) -> dict:
        free_pool = [
            c for c in candidates
            if c.get("cost") == "free" and int(quota_left.get(_model_key(c), 0)) > 0
        ]
        if free_pool:
            pool: list[dict] = free_pool
        else:
            paid_pool = [c for c in candidates if c.get("cost") != "free"]
            pool = paid_pool or list(candidates)

        cands: list[dict] = []
        for cand in pool:
            c = dict(cand)
            if c.get("cost") == "free":
                key = _model_key(c)
                c["quota_left"] = int(quota_left.get(key, 0))
            cands.append(c)
        return route_with_utility(
            task,
            cands,
            now=now,
            constraints=HardConstraints(exclude_in_cooldown=False),
        )

    return route


def _make_chain_route(candidates: list[dict]):
    """v0.3 角色链制：route_model + quota_snapshot。"""
    store = _BenchmarkPolicyStore(candidates)

    def route(task, *, now, quota_left: dict[str, int]) -> dict:
        text = _task_text(task)
        difficulty = assess_difficulty(text)
        urgent = assess_urgency(text)
        return route_model(
            difficulty,
            urgent=urgent,
            now=now,
            quota_snapshot=dict(quota_left),
            policy_store=store,
        )

    return route


def _make_round_robin_route(candidates: list[dict]):
    """朴素轮询基线：不检查额度，按候选顺序轮转。"""
    state = {"idx": 0}

    def route(task, *, now, quota_left: dict[str, int]) -> dict:
        idx = state["idx"] % len(candidates)
        state["idx"] += 1
        cand = candidates[idx]
        return {"model": cand.get("id", ""), "provider": cand.get("provider", "")}

    return route


# ---------------------------------------------------------------------------
# 模拟执行
# ---------------------------------------------------------------------------

def _simulate_one(
    task: dict,
    task_index: int,
    route_fn,
    candidates: list[dict],
    quota_left: dict[str, int],
    now_ts: float,
    rng: random.Random,
    cfg: BenchmarkConfig,
) -> dict[str, Any]:
    free_keys = {_model_key(c) for c in candidates if c.get("cost") == "free"}
    exhausted_any = any(int(quota_left.get(k, 0)) <= 0 for k in free_keys)
    quota_before = dict(quota_left)

    route = route_fn(task, now=now_ts, quota_left=quota_left)
    route = route if isinstance(route, dict) else {}
    model = str(route.get("model") or "").strip()
    provider = str(route.get("provider") or "").strip()
    primary = _find_candidate(candidates, model, provider)
    primary_key = _model_key(primary) if primary else ""

    primary_cost = 0.0
    primary_latency = 0.0
    primary_success = False

    if primary is None:
        primary_success = False
    else:
        quota_blocked = (
            primary.get("cost") == "free" and int(quota_left.get(primary_key, 0)) <= 0
        )
        if quota_blocked:
            primary_success = False
        else:
            primary_latency = _simulate_latency(primary, cfg, rng)
            primary_success = rng.random() >= float(cfg.fail_rate)
            primary_cost = _simulate_cost(primary)
            _decrement_quota(quota_left, primary)

    fallback_cost = 0.0
    fallback_latency = 0.0
    fallback_success = False
    fallback_used = False

    if not primary_success:
        fallback = _choose_fallback(primary, primary_key, candidates, quota_left)
        if fallback is not None:
            fallback_used = True
            fallback_latency = _simulate_latency(fallback, cfg, rng)
            fallback_success = rng.random() >= float(cfg.fail_rate)
            fallback_cost = _simulate_cost(fallback)
            _decrement_quota(quota_left, fallback)
        else:
            fallback_success = False

    success = primary_success or fallback_success
    total_cost = primary_cost + fallback_cost
    total_latency = primary_latency + fallback_latency

    degraded = True
    if exhausted_any:
        if primary is None:
            degraded = False
        elif primary.get("cost") == "free" and int(quota_before.get(primary_key, 0)) <= 0:
            degraded = False

    return {
        "task_index": task_index,
        "task_id": _task_value(task, "task_id", f"bench-{task_index:04d}"),
        "task_type": _task_value(task, "task_type", ""),
        "priority": _task_value(task, "priority", "normal"),
        "deadline": _task_value(task, "deadline", None),
        "selected": primary_key,
        "model": model,
        "provider": provider,
        "primary_success": primary_success,
        "primary_cost": primary_cost,
        "primary_latency_ms": primary_latency,
        "fallback_used": fallback_used,
        "fallback_success": fallback_success,
        "fallback_cost": fallback_cost,
        "fallback_latency_ms": fallback_latency,
        "success": success,
        "total_cost": total_cost,
        "total_latency_ms": total_latency,
        "quota_exhausted": exhausted_any,
        "degraded": degraded,
    }


def _aggregate(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(decisions)
    if n == 0:
        return {
            "n_tasks": 0,
            "success_rate": 0.0,
            "total_cost": 0.0,
            "p95_latency": 0.0,
            "fallback_rate": 0.0,
            "quota_exhausted": 0,
            "quota_degraded": 0,
            "decisions": [],
        }
    success_count = sum(1 for d in decisions if d.get("success"))
    fallback_count = sum(1 for d in decisions if d.get("fallback_used"))
    quota_exhausted = sum(1 for d in decisions if d.get("quota_exhausted"))
    quota_degraded = sum(
        1 for d in decisions if d.get("quota_exhausted") and d.get("degraded")
    )
    latencies = [
        float(d["total_latency_ms"])
        for d in decisions
        if float(d.get("total_latency_ms") or 0.0) > 0
    ]
    return {
        "n_tasks": n,
        "success_rate": success_count / n,
        "total_cost": sum(float(d.get("total_cost") or 0.0) for d in decisions),
        "p95_latency": _p95(latencies),
        "fallback_rate": fallback_count / n,
        "quota_exhausted": quota_exhausted,
        "quota_degraded": quota_degraded,
        "decisions": decisions,
    }


def simulate_route(
    route_fn,
    tasks: list[dict],
    cfg: BenchmarkConfig | None = None,
    *,
    candidates: list[dict] | None = None,
    now=None,
) -> dict[str, Any]:
    """对每个任务调用 route_fn 并模拟成本/延迟/失败/额度消耗。

    ``route_fn(task, *, now, quota_left)`` 返回 ``{"model":..., "provider":...}``。
    随机数使用 ``cfg.seed`` 独立初始化，因此同 seed 同 route_fn 可复现。
    """
    cfg = cfg or BenchmarkConfig()
    task_list = list(tasks or [])
    cands = candidates if candidates is not None else _prepare_candidates(cfg)
    if not cands:
        return _aggregate([])

    now_ts = _as_epoch(now)
    rng = random.Random(cfg.seed)
    quota_left = _initial_quota(cands, cfg)

    decisions: list[dict[str, Any]] = []
    for index, task in enumerate(task_list):
        decisions.append(
            _simulate_one(task, index, route_fn, cands, quota_left, now_ts, rng, cfg)
        )
    return _aggregate(decisions)


# ---------------------------------------------------------------------------
# Benchmark 主流程与报告
# ---------------------------------------------------------------------------

def run_benchmark(cfg: BenchmarkConfig | None = None) -> list[BenchmarkResult]:
    """生成任务集并对比三种策略，返回结果列表。"""
    cfg = cfg or BenchmarkConfig()
    tasks = generate_tasks(cfg)
    candidates = _prepare_candidates(cfg)

    strategies: list[tuple[str, Any]] = [
        ("utility", _make_utility_route(candidates)),
        ("chain", _make_chain_route(candidates)),
        ("round_robin", _make_round_robin_route(candidates)),
    ]

    results: list[BenchmarkResult] = []
    for strategy, route_fn in strategies:
        sim = simulate_route(route_fn, tasks, cfg, candidates=candidates)
        results.append(BenchmarkResult(
            strategy=strategy,
            success_rate=float(sim["success_rate"]),
            total_cost=float(sim["total_cost"]),
            p95_latency=float(sim["p95_latency"]),
            fallback_rate=float(sim["fallback_rate"]),
            quota_exhausted=int(sim["quota_exhausted"]),
            decisions=list(sim["decisions"]),
            n_tasks=int(sim["n_tasks"]),
            quota_degraded=int(sim["quota_degraded"]),
        ))
    return results


def format_report(results: list[BenchmarkResult] | BenchmarkResult) -> str:
    """人类可读的 Markdown 对比报告。"""
    if isinstance(results, BenchmarkResult):
        rows = [results]
    else:
        rows = list(results or [])
    lines = [
        "# Benchmark Report",
        "",
        "| strategy | tasks | success_rate | total_cost | p95_latency_ms | fallback_rate | quota_exhausted | quota_degraded |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r.strategy} | {r.n_tasks} | {r.success_rate:.4f} | {r.total_cost:.4f} "
            f"| {r.p95_latency:.1f} | {r.fallback_rate:.4f} | {r.quota_exhausted} | {r.quota_degraded} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m model_scheduler.benchmark",
        description="model-scheduler 路由策略基准对比（utility vs chain vs round-robin）",
    )
    parser.add_argument("--tasks", type=int, default=200, help="任务数（默认 200）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    parser.add_argument("--json", default=None, metavar="PATH", help="额外输出 JSON 报告到指定路径")
    args = parser.parse_args(argv)

    if args.tasks <= 0:
        parser.error("--tasks must be a positive integer")

    cfg = BenchmarkConfig(n_tasks=args.tasks, seed=args.seed)
    results = run_benchmark(cfg)
    print(format_report(results))

    if args.json:
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "config": {
                "n_tasks": cfg.n_tasks,
                "seed": cfg.seed,
                "task_types": list(cfg.task_types),
                "priority_weights": list(cfg.priority_weights),
                "deadline_prob": cfg.deadline_prob,
                "latency_base_ms": cfg.latency_base_ms,
                "fail_rate": cfg.fail_rate,
                "quota_cap": cfg.quota_cap,
            },
            "results": [asdict(r) for r in results],
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON report written to {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

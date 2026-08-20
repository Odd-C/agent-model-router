"""Tests for model_scheduler.benchmark (v0.6 benchmark 工具)."""
from __future__ import annotations

import json

from model_scheduler.benchmark import (
    BenchmarkConfig,
    format_report,
    generate_tasks,
    main,
    run_benchmark,
    simulate_route,
)


def _free_candidate(mid="free-a", provider="bench", *, fallback_chain=()):
    return {
        "id": mid,
        "provider": provider,
        "tier": "B+",
        "capability": 0.8,
        "cost": "free",
        "role": "free-bulk",
        "scenarios": ["text"],
        "fallback_chain": list(fallback_chain),
        "health": {"p50": 100.0, "p95": 150.0, "failure_risk": 0.01},
    }


def _paid_candidate(mid="paid-a", provider="bench", *, fallback_chain=()):
    return {
        "id": mid,
        "provider": provider,
        "tier": "S",
        "capability": 0.95,
        "cost": "paid",
        "role": "stable",
        "scenarios": ["text"],
        "fallback_chain": list(fallback_chain),
        "health": {"p50": 80.0, "p95": 120.0, "failure_risk": 0.005},
    }


def _route_to(candidate):
    def route_fn(task, *, now, quota_left):
        return {"model": candidate["id"], "provider": candidate.get("provider", "")}

    return route_fn


def test_generate_tasks_reproducible():
    cfg = BenchmarkConfig(n_tasks=100, seed=42)
    tasks_a = generate_tasks(cfg)
    tasks_b = generate_tasks(cfg)
    assert tasks_a == tasks_b
    assert len(tasks_a) == 100

    tasks_c = generate_tasks(BenchmarkConfig(n_tasks=100, seed=43))
    assert tasks_a != tasks_c

    # 覆盖 task_type / priority / deadline / payload 四个维度
    assert {t["task_type"] for t in tasks_a} == set(cfg.task_types)
    assert {t["priority"] for t in tasks_a} == {"high", "normal", "low"}
    assert any(t["deadline"] is not None for t in tasks_a)
    assert all(t["payload"].get("text") for t in tasks_a)


def test_simulate_route_reproducible():
    free = _free_candidate()
    paid = _paid_candidate()
    cfg = BenchmarkConfig(n_tasks=20, seed=5, fail_rate=0.05, candidates=[free, paid])
    tasks = generate_tasks(cfg)
    sim_a = simulate_route(_route_to(free), tasks, cfg)
    sim_b = simulate_route(_route_to(free), tasks, cfg)
    assert sim_a == sim_b


def test_simulate_route_cost_and_success():
    free = _free_candidate()
    cfg = BenchmarkConfig(n_tasks=20, seed=5, fail_rate=0.0, candidates=[free])
    tasks = generate_tasks(cfg)
    sim = simulate_route(_route_to(free), tasks, cfg)

    assert sim["n_tasks"] == 20
    assert sim["success_rate"] == 1.0
    assert sim["total_cost"] == 0.0
    assert sim["fallback_rate"] == 0.0
    assert sim["quota_exhausted"] == 0
    assert len(sim["decisions"]) == 20


def test_simulate_route_failure_fallback_stats():
    free = _free_candidate(fallback_chain=["paid-a@bench"])
    paid = _paid_candidate()
    cfg = BenchmarkConfig(n_tasks=10, seed=7, fail_rate=1.0, candidates=[free, paid])
    tasks = generate_tasks(cfg)
    sim = simulate_route(_route_to(free), tasks, cfg)

    # 主选 free 必失败，fallback 到 paid 也必失败：成功率 0、fallback 率 1、
    # 每个任务产生一次 paid fallback 成本（free 成本为 0）。
    assert sim["success_rate"] == 0.0
    assert sim["fallback_rate"] == 1.0
    assert sim["total_cost"] == 10.0
    assert all(d["fallback_used"] for d in sim["decisions"])
    assert all(not d["success"] for d in sim["decisions"])


def test_run_benchmark_three_strategies():
    cfg = BenchmarkConfig(n_tasks=100, seed=42)
    results = run_benchmark(cfg)
    by_strategy = {r.strategy: r for r in results}

    assert set(by_strategy) == {"utility", "chain", "round_robin"}
    for r in results:
        assert 0.0 <= r.success_rate <= 1.0
        assert len(r.decisions) == 100
        assert r.n_tasks == 100
        assert r.total_cost >= 0.0
        assert r.p95_latency >= 0.0

    # 核心硬断言：评分制 utility 成功率不低于角色链 chain。
    assert by_strategy["utility"].success_rate >= by_strategy["chain"].success_rate


def test_quota_exhaustion_utility_degrades():
    cfg = BenchmarkConfig(n_tasks=30, seed=11, quota_cap=5)
    results = run_benchmark(cfg)
    utility = next(r for r in results if r.strategy == "utility")

    assert utility.quota_exhausted > 0
    exhausted = [d for d in utility.decisions if d["quota_exhausted"]]
    assert exhausted
    # 额度耗尽后的每个任务都必须成功降级到未耗尽模型。
    for d in exhausted:
        assert d["degraded"] is True
    assert utility.quota_degraded == utility.quota_exhausted


def test_format_report_contains_strategies():
    results = run_benchmark(BenchmarkConfig(n_tasks=30, seed=1))
    report = format_report(results)
    assert "utility" in report
    assert "chain" in report
    assert "round_robin" in report
    assert "success_rate" in report


def test_cli_json_output(tmp_path):
    out = tmp_path / "benchmark.json"
    rc = main(["--tasks", "20", "--seed", "42", "--json", str(out)])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["config"]["n_tasks"] == 20
    assert data["config"]["seed"] == 42
    assert {r["strategy"] for r in data["results"]} == {"utility", "chain", "round_robin"}

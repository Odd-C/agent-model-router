# Benchmark 工具（v0.6.2）

`src/model_scheduler/benchmark.py` 是零第三方依赖的本地基准工具，用可复现的合成任务集
对比三种路由策略，输出结构化报告（成功率 / 成本 / P95 延迟 / fallback rate / quota exhaustion）。

## 用法

```bash
PYTHONPATH=src python3 -m model_scheduler.benchmark --tasks 200 --seed 42
PYTHONPATH=src python3 -m model_scheduler.benchmark --tasks 500 --seed 42 --json out.json
```

- `--tasks`：任务数，默认 200（建议 100~1000）。
- `--seed`：随机种子，默认 42；同 seed 同输入 → 同输出，测试可复现。
- `--json PATH`：额外输出完整 JSON 报告（含每任务决策明细）。

## 三种策略

| 策略 | 实现 | 说明 |
|---|---|---|
| `utility` | `route_with_utility` + `HardConstraints(exclude_in_cooldown=False)` | v0.4/v0.5 评分制。免费额度未耗尽时，在仍有额度的免费候选内按效用评分选择；免费候选全部耗尽后转付费池评分。 |
| `chain` | `route_model(difficulty, urgent, *, quota_snapshot=...)` | v0.3 角色链制。按难度/紧急度选择固定 role 链，免费额度耗尽后自动沿链回退付费模型。 |
| `round_robin` | 朴素轮询 | 基线。按候选列表顺序轮转，不检查额度；若选中已耗尽免费模型，simulate_route 会标记失败并尝试 fallback。 |

## 任务集生成

`generate_tasks(cfg)` 使用 `random.Random(cfg.seed)` 生成任务，覆盖：

- `task_type`：`text` / `coding` / `image` / `batch` / `maintenance`（均匀采样）。
- `priority`：`high` / `normal` / `low`，默认权重 `(0.2, 0.5, 0.3)`。
- `deadline`：默认 30% 概率携带，值为固定基准时间 `BENCHMARK_BASE_NOW` 之后 5 分钟 ~ 2 小时。
- `payload.text`：内置样本集，覆盖简单 / 日常 / 复杂 / 紧急等难度。

## 模拟执行

`simulate_route(route_fn, tasks, cfg)` 对每个任务：

1. 调用 `route_fn(task, *, now, quota_left)` 获取 `{"model": ..., "provider": ...}`。
2. 按候选画像模拟一次调用：
   - 成本：`free = 0.0`，`paid = 1.0`。
   - 延迟：以候选 `health.p95` 为均值、`latency_base_ms * 0.3` 为标准差的正态分布。
   - 失败：`fail_rate` 概率命中 5xx/429；命中后按 `fallback_chain`（缺省为其他可用候选）尝试一次 fallback。
3. 免费模型额度在任务间递减：每次选中免费候选并实际调用后，该候选 `quota_left` 减 1。

## 指标定义

| 指标 | 定义 |
|---|---|
| `success_rate` | 主选或 fallback 至少一次成功的任务占比 |
| `total_cost` | 所有实际调用（主选 + fallback）的成本总和 |
| `p95_latency` | 所有实际调用任务总延迟的最近秩 P95 |
| `fallback_rate` | 发生 fallback 的任务占比 |
| `quota_exhausted` | 决策时至少有一个免费候选额度耗尽的任务数 |
| `quota_degraded` | 上述任务中成功避开耗尽候选的任务数 |

## 报告示例

```bash
PYTHONPATH=src python3 -m model_scheduler.benchmark --tasks 200 --seed 42
```

```markdown
# Benchmark Report

| strategy | tasks | success_rate | total_cost | p95_latency_ms | fallback_rate | quota_exhausted | quota_degraded |
|---|---:|---:|---:|---:|---:|---:|---:|
| utility | 200 | 1.0000 | 6.0000 | 539.5 | 0.0300 | 0 | 0 |
| chain | 200 | 1.0000 | 23.0000 | 928.1 | 0.0300 | 0 | 0 |
| round_robin | 200 | 1.0000 | 86.0000 | 864.0 | 0.0300 | 0 | 0 |
```

默认配置下 `utility.success_rate >= chain.success_rate` 为硬断言；`round_robin` 允许浮动。
成本与 P95 维度上，评分制 utility 明显优于固定角色链 chain 与朴素轮询 round_robin。

## 可复现性

- 同 `seed` → 相同任务集、相同路由决策、相同模拟结果（测试断言 `generate_tasks` 同 seed 同结果）。
- 路由与模拟使用固定基准时钟 `BENCHMARK_BASE_NOW`，不依赖系统当前时间。
- 不读/写任何模型策略、额度或偏好状态文件；模拟额度完全在内存中维护。

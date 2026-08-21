# model-scheduler

[English](README.md) | [中文](README.zh-CN.md)

**MIT License** · **Python 3.10+** · **Zero Dependencies** · **283 tests passing**

---

model-scheduler 是一个**零第三方依赖的 LLM 调度库**：给「当前任务选哪个模型」做可解释决策。从 v0.2 的规则式路由，演进到 v0.4+ 的 **Utility 评分制**（质量/成本/延迟/健康/额度/截止六维评分 + 硬约束先砍后评），并包含任务系统、Policy Compiler 自然语言意图、看板与 Benchmark 工具。

**一句话定位**：从「哪个模型还能用」到「哪个模型最划算」。

## 为什么做这个库

多模型接入的真实痛点：

- 有些模型免费但有调用上限（滑动窗口配额）；付费模型高峰时段价格翻倍；
- 同一个 `model id` 可能由多个 provider 提供（`gpt-4o-mini@openai` vs `gemini-2.0-flash@google`）；
- 复杂任务想用免费旗舰，但额度耗尽/限流时必须自动降级；
- 每个调用方都自己写「选哪个模型」的判断 → 规则散落、难调参、难测试。

model-scheduler 把决策固化进一个纯标准库组件：模型画像可 JSON 覆盖、决策可解释、降级可追溯，任何 OpenAI 兼容调用方都能接入。

## 核心概念

### `id@provider` 唯一键

模型用 `id@provider` 唯一标识，避免同名模型跨 provider 歧义（如 `glm-5.2@sensenova` 与 `glm-5.2@zhipu` 是两个候选）。

### 模型画像表

模型能力/成本/角色用 JSON 描述，**改配置不改代码**：

```json
{
  "models": [
    {
      "key": "gpt-4o@openai",
      "id": "gpt-4o",
      "provider": "openai",
      "tier": "S",
      "cost": "paid",
      "role": "stable",
      "scenarios": ["complex", "daily"]
    }
  ]
}
```

画像字段：`tier`（S+/S/A/A-/B+/B/C 能力档）、`cost`（free/paid）、`role`（能力标签）、`scenarios`（适用场景）、`quota_per_window`（免费额度）、`peak_hours`（per-model 峰谷覆盖）、`capability`（0-1 能力分，能力硬约束用）。

### 决策链（三档可选）

| 方式 | 版本 | 说明 |
|---|---|---|
| **Utility 评分制** | v0.4+（推荐） | 六维归一化评分 + 硬约束先砍后评，breakdown 可解释 |
| **Policy Compiler** | v0.5+ | 自然语言意图 → 硬约束 + 权重 |
| role 链 | v0.2（兼容层） | 难度分档 → 固定 fallback 链 |

### Utility 评分制（v0.4+，推荐）

候选集内六分项 **min-max 归一化** 后乘权重加权，选分最高者：

- `quality_fit` 质量匹配（期望 tier + scenarios 能力）
- `cost_penalty` 成本惩罚（free=0 / paid=0.6，峰谷翻倍）
- `latency_penalty` 延迟惩罚（ProviderHealth p95）
- `failure_risk` 失败风险（健康档案）
- `quota_pressure` 额度压力
- `deadline_pressure` 截止压力

**硬约束先砍后评**（不可谈判）：cost 上限 / quota 耗尽 / cooldown / deadline 不可行 / health 红线 / 能力上下限（`max_latency_ms`、`min_quality_tier`、`min_capability_pct`）；image/vision 任务强制视觉能力校验（文本模型 quality_fit=0，宁缺毋滥）。

### Policy Compiler（v0.5+，自然语言意图）

```
用户说「要便宜点的」→ compile_intent → {cost_max: "free", cost-first 权重, explanation}
```

中英文规则模板（无需 NLP）：速度（尽快/3秒 → latency-first+max_latency_ms）、成本（便宜/免费 → cost-first+cost_max=free）、质量（高质量/旗舰 → quality-first+min_quality_tier）、能力百分比（"达到 gpt-4o 的 80%" → pct+reference）；无匹配回退 balanced。

### 任务系统（v0.3+）

`Task`（task_id/task_type/priority/deadline/defer_until/status/payload）+ 状态机 + TaskStore 持久化（Json / SQLite 双后端）：

- 状态机：queued → running → done/failed；deferred 按 defer_until 转 queued；过期 → expired
- 降级矩阵：错误分类（429→冷却重试 / 5xx→切换候选 / 400-403→不重试）写入 `last_error.action_taken`
- ProviderHealth：滑动窗口健康档案（success_rate / p50 / p95 / failure_risk）
- SQLite 后端 `BEGIN IMMEDIATE` 跨进程原子

### 看板（taskserver，v0.3+）

Opportunistic Scheduling 单页：任务列表/提交/手动 tick/偏好四档/模型画像/推荐试算器 + A2A 三端点（提交/查询/取结果）。纯 stdlib，无 CDN/外部资源。

### 任务理解层（task_type 判定：库 vs 接入层）

**本库是零依赖纯标准库**，不内置任何 LLM 调用。任务理解分成两层：

| 层 | 职责 | 实现 |
|---|---|---|
| **调用方（接入层）** | 把任务描述 → `task_type`（coding / image / text / batch / maintenance） | 接入层自行实现；推荐链路：LLM Judge（免费快速模型，OpenAI 兼容可插拔）→ 多特征本地分类器（文本长度/附件/代码块/工具/上下文/历史/会话状态）→ 关键词白名单 → text；无 LLM 也能较智能（WebUI 接入层有参考实现） |
| **本库** | 接收 `task_type`，做质量匹配/评分 | `task_type_tier_expectation(task_type)` 决定期望 tier，`quality_fit` 计算匹配度；image/vision 任务强制视觉能力校验 |

本库不猜任务类型——`task_type` 由调用方显式传入或由接入层推断。v0.2 的 `assess_difficulty(text)`（0-5 难度分）是兼容层，proxy 内部使用，新项目不需要。

## 快速开始

### 安装

```bash
pip install model-scheduler
```

### 最简路由（Utility 评分制，推荐）

```python
from model_scheduler import route_with_utility
from model_scheduler.policy import list_models
from model_scheduler.utility import HardConstraints

# 全部画像为候选：先过硬约束（只要免费），再六维评分选最优
result = route_with_utility(
    {"task_type": "coding", "priority": "high", "deadline": None},
    list_models(),
    constraints=HardConstraints(cost_max="free"),
)
print(result)
# → {model, provider, score, breakdown: {质量/成本/延迟/健康/额度/截止}, why}
```

### 自然语言意图（Policy Compiler）

```python
from model_scheduler import route_with_intent

result = route_with_intent(
    {"task_type": "coding", "priority": "high", "deadline": None},
    list_models(),
    "要便宜点的",   # 自然语言 → 硬约束 + 权重
)
```

### 任务调度

```python
from model_scheduler.task import TaskStore
from model_scheduler.scheduler import TaskScheduler
from model_scheduler.executor import MockExecutor

store = TaskStore(state_dir, backend="sqlite")   # json / sqlite
scheduler = TaskScheduler(store, MockExecutor())
now = time.time()
# deadline 为绝对时间戳（10 分钟后）→ tick 未过期 → 正常执行
task = scheduler.submit("text", {}, priority="high", deadline=now + 600)
scheduler.tick(now=now + 1)                      # 未过期 → 执行 → done
print(store.get(task.task_id).status)            # → done
```

### 看板

```bash
PYTHONPATH=src python -m model_scheduler.taskserver --port 8099
```

### 代理层（OpenAI 兼容，v0.2+）

```bash
model-scheduler serve --config model-policy.json --host 127.0.0.1 --port 8765
```

任意 OpenAI 兼容客户端把 `base_url` 指向代理即可（自动获得额度跟踪/峰谷感知/失败冷却），零改码。

### v0.2 兼容层（旧 API）

`assess_difficulty` / `route_model` / `recommend_for_session` 仍然可用（proxy 层内部使用），但新项目推荐 Utility 评分制：

```python
from model_scheduler import assess_difficulty, route_model

decision = route_model(assess_difficulty("帮我写一个 Python 脚本"), urgent=False)
```

## 作为库接入（Integration）最佳实践

> 来自上游第三方应用（如 WebUI 模型选择器）实测经验，所有示例用公开通用模型名。

### 0. 接入前必做清单（必要动作，缺一步都会出问题）

| # | 动作 | 说明 | 不做会怎样 |
|---|---|---|---|
| 1 | **配置 state 目录** | `configure_state_dir()` 或环境变量 `LLM_ROUTER_STATE_DIR`；确保目录**可写** | 状态落在默认目录，多实例/多进程互相覆盖 |
| 2 | **写自己的模型画像** | `model-policy.json` 覆盖内置示例画像；内置 5 个公开示例（claude-3-5-sonnet/deepseek-chat/gemini/gpt-4o-mini/gpt-4o）只是机制演示，**不是你的真实模型** | 路由到不存在的模型 / 额度跟踪无效 |
| 3 | **provider 连接配置就绪** | 画像里的 `id@provider` 对应的 provider 必须有 base_url + api_key（接入层配置）；key 用 `env:VAR` 引用，**不硬编码** | 推荐选了模型但上游调用 401/403 |
| 4 | **传 `task_type` 或实现接入层推断** | 库不猜任务类型——`route_with_utility` 的 task 必须带 `task_type`（coding/image/text/batch/maintenance）或由接入层先推断 | 缺 task_type 时 quality_fit 退化为无区分，路由质量差 |
| 5 | **接好失败上报** | 上游调用失败必须调 `record_failure(model, provider, reason, status)`；成功可调 `record_result` 更新健康档案 | 冷却不生效，故障模型持续被选 |
| 6 | **验证免费额度字段** | 免费模型画像要配 `quota_per_window`，否则额度跟踪无意义 | 额度耗尽后仍被路由，触发上游 429 |

**最小接入骨架**（组合上述动作）：

```python
import model_scheduler as ms
ms.configure_state_dir("/var/lib/myapp/ms-state")   # 动作 1
# 动作 2: model-policy.json 放真实画像（含 quota_per_window）
# 动作 3: provider 配置在接入层，key 走 env
# 动作 4: 调用前先 task_type（接入层推断或显式）

from model_scheduler import route_with_utility
from model_scheduler.policy import list_models

task = {"task_type": "coding", "priority": "high", "deadline": None}
result = route_with_utility(task, list_models())
model, provider = result["model"], result["provider"]

# ... 上游调用 ...
# 动作 5: 失败必须上报
ms.record_failure(model, provider, reason="rate_limit", status=429)
```

### 1. 可选依赖：懒加载 + 优雅降级

```python
def _load_lib():
    try:
        import model_scheduler as ms
        return ms
    except ImportError:
        return None
```

### 2. 启用开关：单一权威

用一个总开关控制（如 settings 的 `model_scheduler_enabled`）；关闭时不产生任何推荐，不报错。

### 3. 推荐结果应用

`route_with_utility` 返回 `{model, provider, score, breakdown, why}`；调用方拿到 model/provider 后设置到发送链路。推荐是 advisor，用户可手动覆盖。

### 4. 键格式：`id@provider` 与 UI `provider/model`

库内部用 `id@provider`；UI 选择器常用 `provider/model`。用 `format_model_key` / `parse_model_key` / `format_selector_key` / `parse_selector_key` 转换。

### 5. 失败冷却链路

上游失败时调 `record_failure(model, provider, reason, status)`；下一次路由自动绕过冷却中的模型。错误分类：429/5xx 进冷却，400/401/403 不冷却。

### 6. 推荐缓存

同文本 60s TTL 缓存，避免每条消息重复计算（接入层实现）。

## 配置覆盖（三选一）

1. **JSON 文件**：`model-policy.json` 覆盖默认画像（改配置不改代码）
2. **环境变量**：`LLM_ROUTER_STATE_DIR` 指定 state 目录；`MODEL_SCHEDULER_JUDGE_MODEL` 等可覆盖接入层参数
3. **代码参数**：`configure_state_dir()` / 构造函数 `state_dir`

## Benchmark

```bash
PYTHONPATH=src python -m model_scheduler.benchmark --tasks 300 --seed 42
```

可复现任务集对比 utility vs role 链 vs round-robin。实测（300 任务）：

| 策略 | 成本 | P95 延迟 |
|---|---|---|
| **utility（评分制）** | **11** | **536ms** |
| chain（角色链） | 35 | 943ms |
| round_robin（轮询） | 130 | 858ms |

## 测试运行

```bash
python -m pytest tests -q          # 需要 pytest
PYTHONPATH=src python -m unittest discover -s tests -v   # 纯标准库
```

真实外部依赖冒烟（可选，无 key 自动 skip）：

```bash
MODEL_SCHEDULER_SMOKE_BASE_URL=... MODEL_SCHEDULER_SMOKE_API_KEY=... MODEL_SCHEDULER_SMOKE_MODEL=... \
  python -m pytest tests/test_live_smoke.py -v
```

## 版本历史

见 [CHANGELOG.md](CHANGELOG.md)（详细变更）与 [RELEASES.md](docs/RELEASES.md)（发布说明）。API 契约见 [API.md](docs/API.md)。

## License

MIT License。全文见 `LICENSE`。

Copyright (c) 2026 llm-router contributors

# model-scheduler

[English](README.en.md) | [中文](README.md)

**MIT License** · **Python 3.10+** · **Zero Dependencies** · **279 tests passing**

---

model-scheduler 是一个**智能模型调度器**：模型画像 + 免费额度跟踪 + 路由决策 + 任务调度，拆成零第三方依赖的纯 Python 标准库组件，任何 OpenAI 兼容 API 调用方都可以接入。从「哪个模型还能用」到「哪个模型最划算」——Utility 评分制、硬约束、Policy Compiler 自然语言意图、任务系统与看板一应俱全。

## 为什么做这个库

在实际项目中，我们同时接入了多家模型提供方：

- 有些模型免费但 5 小时窗口内有次数上限；
- 有些模型付费，且高峰时段价格翻倍；
- 同一个 `model id` 可能由不同 provider 提供（例如 `gpt-4o-mini@openai` vs `gemini-2.0-flash@google`）；
- 复杂任务想用免费旗舰模型，但免费额度耗尽或限流时必须自动回退付费模型。

如果每个调用方都自己写一套「选哪个模型」的判断逻辑，规则会散落、很难调参、也很难测试。model-scheduler 把决策规则固化下来，并支持画像表 JSON 覆盖，让模型调度策略可以持续调参。

## 核心概念

### `id@provider` 唯一键

模型唯一键由 `id@provider` 组成，例如：

- `gpt-4o@openai`：OpenAI 付费旗舰；
- `gpt-4o-mini@openai`：OpenAI 付费经济型；
- `gemini-2.0-flash@google`：Google 免费量大模型；
- `deepseek-chat@deepseek`：DeepSeek 免费预览模型；
- `claude-3-5-sonnet@anthropic`：Anthropic 免费旗舰模型。

> 默认画像仅为「通用示例」，演示机制用。请按你的真实模型/额度修改 `model-policy.json` 覆盖，或直接改 `policy.py` 里的默认值。

### 模型画像表

画像表描述每个模型的能力档位（`tier`）、付费/免费（`cost`）、5 小时窗口配额（`quota_per_window`）、高峰是否安全（`peak_safe`）、额度耗尽降级链（`fallback_chain`）、场景标签（`scenarios`）和**路由角色**（`role`）。

`role` 是决策链的抽象层，与具体模型名解耦：

| role | 含义 | 示例默认模型 |
|---|---|---|
| `stable` | 付费最稳，紧急任务优先 | gpt-4o |
| `free-flagship` | 免费旗舰，复杂任务优先 | claude-3-5-sonnet |
| `free-bulk` | 免费量大，日常主力 | gemini-2.0-flash |
| `free-preview` | 免费预览，日常兜底 | deepseek-chat |
| `paid-fallback` | 付费兜底 | gpt-4o-mini |

### 滑动窗口配额

免费模型的调用次数按 **5 小时滑动窗口** 记录在 `model-quota.json` 中。`quota_left()` 返回当前窗口剩余次数；无画像记录或付费模型返回 `-1` 表示不受限。`reset_if_needed()` 会清理过期记录。线程安全 + 原子写。

### 决策链（role 驱动）

路由决策按难度分档，每档走一条 role 链（`ROUTE_CHAINS`，可配置）。免费模型在高峰/谷值都优先；免费模型不可用或额度耗尽时才回退付费模型，高峰回退时 reason 会包含「官方高峰翻倍」警告。

| 分支 | role 链（按优先级） |
|---|---|
| 紧急任务 | `stable` → `paid-fallback` |
| 难度 >= 4 | `free-flagship` → `stable` → `paid-fallback` |
| 难度 2-3 | `free-bulk` → `free-preview` → `free-flagship` → `paid-fallback` |
| 难度 0-1 | `free-bulk` → `free-preview` → `paid-fallback` |

换模型 = 改画像表 `role` 字段，**不用改路由代码**。

> **v0.4+ 升级路径**：role 链是 v0.2 的默认决策方式。v0.4 起提供 **Utility 评分制**（`route_with_utility`），在候选集中做六分项归一化加权评分（质量/成本/延迟/健康/额度/截止），并支持 HardConstraints 硬约束先砍后评。Benchmark 实测：utility 评分相比 role 链省 ~69% 成本、延迟低 43%。新接入建议直接用评分制，role 链保留向后兼容。

### Utility 评分制（v0.4+）

```python
from model_scheduler.utility import route_with_utility, HardConstraints
from model_scheduler.policy import list_models

result = route_with_utility(
    {"task_type": "coding", "priority": "high", "deadline": None},
    list_models(),
    constraints=HardConstraints(cost_max="free"),   # 先砍后评
)
# → {model, provider, score, breakdown: {quality_fit, cost_penalty, ...}, why}
```

- 六分项：quality_fit / cost_penalty / latency_penalty / failure_risk / quota_pressure / deadline_pressure
- 候选集内 min-max 归一化后再乘权重（权重相等 ≠ 影响相等）
- breakdown 三层（raw / normalized / weighted）可解释「为什么选 A 不选 B」
- 能力硬约束：`max_latency_ms` / `min_quality_tier` / `min_capability_pct`（速度/质量/延迟特化场景）；image/vision 任务强制视觉能力校验（文本模型 quality_fit=0）

### Policy Compiler（v0.5+，自然语言意图）

```python
from model_scheduler.policy_compiler import compile_intent, route_with_intent

cp = compile_intent("要便宜点的")
# → CompiledPolicy{constraints: {cost_max: "free"}, weights: {cost_penalty: 3.0}, mode: "cost-first", explanation}

result = route_with_intent(
    {"task_type": "coding", "priority": "high", "deadline": None},
    list_models(),
    "高质量但不要太贵",   # 自然语言 → 硬约束并集 + 质量权重
)
```

中英文关键词规则匹配（无需 NLP）：速度（尽快/3秒→latency-first+max_latency_ms）、成本（便宜/免费→cost-first+cost_max=free）、质量（高质量/旗舰→quality-first+min_quality_tier）、能力百分比（"达到 gpt-4o 的 80%"→pct+reference）、无匹配回退 balanced。

### 任务系统（v0.3+）

任务 model（task_id / task_type / priority / deadline / defer_until / status / payload）+ 状态机 + TaskStore 持久化：

```python
from model_scheduler.task import TaskStore, Task
from model_scheduler.scheduler import TaskScheduler
from model_scheduler.executor import MockExecutor

store = TaskStore(state_dir, backend="sqlite")   # json / sqlite 双后端
scheduler = TaskScheduler(store, MockExecutor())
task = scheduler.submit("text", {}, priority="high", deadline=1_800_000_000)
scheduler.tick(now=1_800_000_100)                # 处理 queued/deferred
```

- 状态机：queued → running → done/failed；deferred 按 defer_until 转 queued；过期 → expired
- 降级矩阵：错误分类（429→冷却重试 / 5xx→切换候选 / 400-403→不重试）写入 `last_error.action_taken`
- ProviderHealth：滑动窗口健康档案（success_rate / p50 / p95 / failure_risk）

### 看板（taskserver，v0.3+）

```bash
PYTHONPATH=src python -m model_scheduler.taskserver --port 8099
```

Opportunistic Scheduling 看板：任务列表/提交/手动 tick/偏好四档/模型画像/A2A 端点（提交任务/查询/取结果）。纯 stdlib，无 CDN/外部资源。

### 峰谷时段

- 高峰：北京时间 9:00-12:00、14:00-18:00（含边界，**默认**）
- 谷值：其余时间
- 时区：`Asia/Shanghai`（CST +0800）
- **全局可自定义**：在 `model-policy.json` 配 `peak_hours` 字段覆盖，如 `[[8, 10], [20, 22]]`；`[]` 表示无峰谷（全天平峰）
- **per-model 可自定义**：画像条目里加 `peak_hours` 字段，优先级 **模型级 > 全局 > 默认**：

```json
{
  "models": {
    "gemini-2.0-flash@google": { "peak_hours": [[22, 23]] },
    "deepseek-chat@deepseek":  { "peak_hours": [] }
  }
}
```

上面的例子：gemini 的峰谷是 22-23 点；deepseek-chat 全天无峰谷；其他模型走全局配置。
付费回退警告（reason 里的「高峰翻倍」）也按**回退目标模型**的峰谷判断，不是全局。

## 特性

- 纯 Python 标准库，零第三方运行时依赖；
- 路由决策三档可选：role 链（v0.2）/ Utility 评分制（v0.4+）/ Policy Compiler 自然语言意图（v0.5+）；
- 任务系统（v0.3+）：Task 状态机 + TaskStore 持久化（Json / SQLite 双后端，SQLite 跨进程原子）+ 降级矩阵 + ProviderHealth；
- 可解释决策：Utility 评分返回 breakdown 分项（质量/成本/延迟/健康/额度/截止），能回答「为什么选 A 不选 B」；
- 硬约束先砍后评：cost 上限 / quota 耗尽 / cooldown / deadline 不可行 / health 红线 / 能力上下限（延迟/质量档/能力百分比），image/vision 任务强制视觉能力校验；
- 看板（taskserver）：Opportunistic Scheduling 面板 + A2A 三端点（提交任务/查询/取结果），纯 stdlib 无外部资源；
- Benchmark 工具（v0.6+）：可复现任务集对比 utility vs role 链 vs round-robin（成功率/成本/P95/fallback rate）；
- state 目录可参数化：支持 `LLM_ROUTER_STATE_DIR` 环境变量、`configure_state_dir()`、构造函数 `state_dir` 参数；
- import 时零 I/O 副作用：首次读写 state 文件时才创建目录；
- 线程安全额度记录（`threading.Lock`）；SQLite 后端支持跨进程原子读改写；
- 原子写 JSON（`tmp` + `fsync` + `os.replace`）；
- 可注入 `now` / `quota_snapshot`，决策测试不依赖真实状态文件。

## 快速开始

### 安装（推荐：PyPI）

```bash
pip install model-scheduler
```

安装后直接 import（无需 clone 源码）——**推荐用 Utility 评分制（v0.4+）**：

```python
from model_scheduler import route_with_utility
from model_scheduler.policy import list_models
from model_scheduler.utility import HardConstraints

# 全部模型画像作为候选，先过硬约束再按六分项评分选最优
result = route_with_utility(
    {"task_type": "coding", "priority": "high", "deadline": None},
    list_models(),
    constraints=HardConstraints(cost_max="free"),   # 只要免费模型
)
print(result)
# → {model, provider, score, breakdown: {质量/成本/延迟/健康/额度/截止}, why}
```

自然语言意图入口（v0.5+，Policy Compiler）：

```python
from model_scheduler import route_with_intent

result = route_with_intent(
    {"task_type": "coding", "priority": "high", "deadline": None},
    list_models(),
    "要便宜点的",          # 自然语言 → 硬约束 + 权重
)
```

### 直接 import（源码运行）

```bash
git clone https://github.com/Odd-C/model-scheduler.git
cd model-scheduler
PYTHONPATH=src python
```

完整示例见 `examples/quickstart.py`。

### v0.2 兼容层（旧 API，向后兼容保留）

以下 v0.2 时代的 API 仍然可用，但**新项目建议用上面的 Utility 评分制**：

```python
from model_scheduler import assess_difficulty, route_model, recommend_for_session

text = "帮我写一个 Python 脚本"
decision = route_model(assess_difficulty(text), urgent=False)
print(decision)
# {'model': 'claude-3-5-sonnet', 'provider': 'anthropic', 'reason': '复杂任务，...', 'tier': 'S+', 'cost': 'free'}

rec = recommend_for_session(text, message_count=3, session_id="demo-session-1")
print(rec)
```

- `assess_difficulty(text)` / `route_model(difficulty, ...)`：关键词难度 → role 链决策（v0.2 默认方式）
- `recommend_for_session(...)`：会话级推荐（难度 + 角色链 + quota/cooldown）
- 代理层 CLI：`model-scheduler serve --config model-policy.json`（见下节，独立可用）

### 代理层（OpenAI 兼容，全 Agent 通用）

v0.2.0 起内置 **OpenAI 兼容代理层**：任何 OpenAI 兼容客户端（Hermes / Claude Code / Codex / OpenClaw / 任意 SDK）把 `base_url` 指向代理，即可自动获得难度评估 + 免费额度跟踪 + 峰谷感知 + 失败冷却降级，**零改码**。

```bash
pip install model-scheduler

# 准备配置（model-policy.json，需含 providers 段）
export OPENAI_API_KEY=your-openai-api-key
export DEEPSEEK_API_KEY=your-deepseek-api-key

# 启动代理（纯本地进程，零外部依赖，不收集遥测）
model-scheduler serve --config model-policy.json --host 127.0.0.1 --port 8765
```

任意客户端接入：

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"写个快排"}],"stream":true}'
```

```python
# OpenAI SDK 示例（base_url 指向代理即可，api_key 填任意占位值）
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="unused")
resp = client.chat.completions.create(model="auto", messages=[{"role": "user", "content": "写个快排"}])
```

代理层特性：
- `POST /v1/chat/completions`（流式 SSE + 非流式透传）
- `GET /v1/models`（只列出当前可用模型）
- `GET /v1/health`（健康检查）
- 每次调用自动记账（`record_call`），失败自动冷却（`record_failure` → 下次路由绕过）
- 密钥通过 `env:VAR` 引用环境变量，**不硬编码进配置**
- 纯 Python 标准库实现（`http.server` + `urllib`），零第三方依赖

**部署形态**：代理是纯本地进程，不依赖任何外部调度服务/中心节点；决策与状态（额度、峰谷、冷却）全部在库内 + 本地状态文件，不收集遥测。用户只需自备 provider 的 API key 和自己的 `model-policy.json`（默认画像仅为机制演示）。

## 决策规则说明（v0.2 兼容层）

> 本节描述 v0.2 的规则式决策（`assess_difficulty` / `route_model`）。它们仍然可用且被 proxy 层使用，但 **v0.4+ 的 Utility 评分制是推荐方式**（见「核心概念」）。了解本节有助于读懂旧集成代码与 proxy 层日志。

`assess_difficulty(text)` 是纯 CPU 规则打分，范围 0-5：

- 代码块 ```` ``` ````：+2
- 报错词（报错/error/exception/traceback/failed/崩溃/fail）：+2
- 源码引用（`.py`/`.js`/`.ts`/源码/函数/class/def/import/接口）：+1
- 强任务意图（写代码/开发/重构/修 bug/debug 等）：+3
- 弱意图词（代码/脚本/项目/bug/算法/模块/接口/前端/后端/数据库/部署/优化/重构/功能/系统/网站/应用/框架/demo/函数）：+1
- 文本 > 2000 字符：+1；> 8000 字符：+1
- 最终 clamp 到 `[0, 5]`

`assess_urgency(text)` 识别：紧急|马上|尽快|asap|urgent|立刻。

`route_model(difficulty, *, urgent, now=None, quota_snapshot=None)` 返回：

```json
{"model": "...", "provider": "...", "reason": "...", "tier": "...", "cost": "..."}
```

`quota_snapshot` 可传 `{"id@provider": remaining}`，传了之后决策不碰真实额度文件，便于测试和外部系统集成。

## 作为库接入（Integration）最佳实践

> 本节来自上游第三方应用（如 WebUI 模型选择器）接入 model-scheduler 的实测经验。所有示例均使用公开通用模型名，可直接运行。

### 1. 可选依赖接入：懒加载 + 优雅降级

把本库作为可选依赖时，应懒加载并在未安装时缓存 miss 标志，保证宿主应用不崩溃：

```python
try:
    import model_scheduler as ms
    _HAS_MODEL_SCHEDULER = True
except Exception:
    _HAS_MODEL_SCHEDULER = False
    ms = None

def get_recommendation(text, message_count=0, session_id=None):
    if not _HAS_MODEL_SCHEDULER:
        return None  # 未安装本库时优雅降级，由宿主应用自行兜底
    return ms.recommend_for_session(
        text,
        message_count=message_count,
        session_id=session_id,
    )
```

### 2. 启用开关：单一权威

接入方自己的总开关是唯一 gate；库 policy 文件里的 `enabled` 字段仅信息性，不参与路由决策（`router._route` 从不读它）。不要在 policy 文件里试图用 `enabled` 关闭调度：

```python
import model_scheduler as ms

SCHEDULER_ENABLED = True  # 接入方自己的总开关，唯一权威 gate

def maybe_recommend(text, message_count=0, session_id=None):
    if not SCHEDULER_ENABLED:
        return None
    return ms.recommend_for_session(text, message_count=message_count, session_id=session_id)
```

### 3. 推荐结果应用

`recommend_for_session(text, message_count=..., session_id=...)` 返回字段：
`difficulty` / `urgent` / `message_count` / `peak` / `model` / `provider` / `reason` / `tier` / `cost` / `key`（传入非空 `session_id` 时额外追加 `session_id`）。

本库定位是顾问：结果是否采用、是否展示给用户，由接入方决定。

```python
import model_scheduler as ms

rec = ms.recommend_for_session(
    "帮我写一个 Python 脚本",
    message_count=3,
    session_id="sess-123",
)
print(rec["model"], rec["provider"], rec["reason"], rec["key"])
```

### 4. 格式转换：内部 `id@provider` 与 UI `provider/model`

- 库内部唯一键：`id@provider`（画像/额度/冷却状态文件 key 也用它）—— `format_model_key` / `parse_model_key`
- UI 下拉选择器/外部系统常用：`provider/model` —— `format_selector_key` / `parse_selector_key`

```python
from model_scheduler import (
    format_model_key,
    parse_model_key,
    format_selector_key,
    parse_selector_key,
)

assert format_model_key("gpt-4o", "openai") == "gpt-4o@openai"
assert parse_model_key("gpt-4o@openai") == ("gpt-4o", "openai")

assert format_selector_key("gpt-4o", "openai") == "openai/gpt-4o"
assert parse_selector_key("openai/gpt-4o") == ("gpt-4o", "openai")
```

### 5. 失败冷却链路

上游真实 provider 失败（429 / quota 耗尽 / 鉴权失败等）时调用 `record_failure(model, provider)`，免费模型会自动进入冷却（默认 300s，`cooldown_seconds_left` 可查），路由期间自动绕过。网络断线、本地超时等非模型失败**不要**记录：

```python
import model_scheduler as ms

class UpstreamRateLimitError(Exception):
    pass

class NetworkError(Exception):
    pass

def call_model(model, provider):
    # 实际项目中替换为你的上游调用
    pass

def call_with_cooldown(model, provider):
    try:
        call_model(model, provider)
    except UpstreamRateLimitError:
        ms.record_failure(model, provider)
        raise
    except NetworkError:
        raise  # 网络断线等非模型失败不记录冷却
```

### 6. 推荐缓存

若接入方做发送前推荐缓存，缓存键必须按 会话ID + 文本 隔离；TTL 建议 60 秒：

```python
import time
import model_scheduler as ms

_cache = {}  # {(session_id, text): (expires_at, recommendation)}

def cached_recommendation(text, session_id="", ttl=60):
    key = (session_id or "", text)
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    rec = ms.recommend_for_session(
        text,
        message_count=0,
        session_id=session_id or None,
    )
    _cache[key] = (now + ttl, rec)
    return rec
```

## 配置覆盖方式（三选一）

### 1. JSON 文件覆盖

state 目录默认是 `~/.llm-router`，画像文件名为 `model-policy.json`。示例见 `examples/custom_policy.json`。合并规则：`models` 中每个键会与默认画像按 `id@provider` 合并，传入的非空字段覆盖默认值。

语言配置：在 `model-policy.json` 加 `"language": "zh" | "en"`（默认 `zh`），
路由决策的 `reason` 文案会按语言输出。画像里的 `label` 是展示名，默认英文，
中文用户可自行覆盖为中文。

### 2. 环境变量

```bash
export LLM_ROUTER_STATE_DIR=/path/to/state
```

库会从该目录读取/写入 `model-policy.json`、`model-quota.json` 和 `model-cooldown.json`。

### 3. 代码参数

```python
from pathlib import Path
from model_scheduler import ModelPolicy, QuotaTracker, ModelRouter

state = Path("/tmp/llm-router-state")
policy = ModelPolicy(state_dir=state)
quota = QuotaTracker(state_dir=state, policy_store=policy)
router = ModelRouter(state_dir=state)

# 或者直接配置模块级默认 state 目录
from model_scheduler import configure_state_dir
configure_state_dir("/tmp/llm-router-state")
```

## 测试运行方式

库本身零第三方运行时依赖；测试文件使用标准库 `unittest`，因此可以直接用标准库运行：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

也可以使用 pytest（仅测试期可选依赖）：

```bash
pip install pytest
python -m pytest tests -q
```

## Roadmap

**已完成（v0.3 → v0.6.2）**：

- ✅ v0.3 任务系统：Task 状态机 + TaskStore + Executor 抽象 + 错误分类（400/401/403 不冷却 / 429/5xx/transport 分类）
- ✅ v0.3 看板：Opportunistic Scheduling 面板（任务/调度规则/画像/试算器），纯 stdlib 无外部资源
- ✅ v0.4 Utility 评分：六分项 + min-max 归一化 + 权重配置化 + breakdown 可解释
- ✅ v0.4 硬约束：cost / quota / cooldown / deadline / health 红线 + 能力上下限（延迟/质量档/能力百分比）
- ✅ v0.4 ProviderHealth：滑动窗口健康档案（success_rate / p50 / p95）
- ✅ v0.5 Policy Compiler：自然语言意图 → 硬约束+权重（中英文规则模板，无 NLP）
- ✅ v0.5 A2A 三端点：提交任务 / 查询任务 / 获取结果
- ✅ v0.6 StateStore：Json / SQLite 双后端（SQLite BEGIN IMMEDIATE 跨进程原子）
- ✅ v0.6 Benchmark：可复现任务集对比 utility vs role 链 vs round-robin
- ✅ v0.6.2 能力校验：image/vision 任务强制视觉能力（文本模型 quality_fit=0）
- ✅ 接入层 task_type 判定（v2.1）：LLM Judge（免费快速模型，默认 GLM-4-Flash）+ 多特征本地分类器兜底（文本长度/附件/代码块/工具/上下文/历史/会话状态），无 LLM 也较智能——位于 WebUI 接入层（不在本库，库保持零依赖）

**后续方向**：

- RedisStateStore 后端（多实例共享状态）；
- 多租户：按租户隔离 state 目录与画像覆盖；
- OpenAI 兼容中间件：作为 API 网关插件形态（现有代理层是独立进程，缺"网关插件"形态）；
- 版本化手动标记：将「用户手动选择模型」的标记策略版本化（现有标记机制缺通用版本化与旧残留自动清理）。

## License

MIT License。全文见 `LICENSE`。

Copyright (c) 2026 llm-router contributors

# model-scheduler

model-scheduler 是一个**智能模型调度器**：模型画像 + 免费额度跟踪 + 路由决策三件事，拆成零第三方依赖的纯 Python 标准库组件，任何 OpenAI 兼容 API 调用方都可以接入。

## 为什么做这个库

在实际项目中，我们同时接入了多家模型提供方：

- 有些模型免费但 5 小时窗口内有次数上限；
- 有些模型付费，且高峰时段价格翻倍；
- 同一个 `model id` 可能由不同 provider 提供（例如 `gpt-4o-mini@openai` vs `gemini-2.0-flash@google`）；
- 复杂任务想用免费旗舰模型，但免费额度耗尽或限流时必须自动回退付费模型。

如果每个调用方都自己写一套「选哪个模型」的判断逻辑，规则会散落、很难调参、也很难测试。llm-router 把决策规则固化下来，并支持画像表 JSON 覆盖，让模型调度策略可以持续调参。

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
- state 目录可参数化：支持 `LLM_ROUTER_STATE_DIR` 环境变量、`configure_state_dir()`、构造函数 `state_dir` 参数；
- import 时零 I/O 副作用：首次读写 state 文件时才创建目录；
- 线程安全额度记录（`threading.Lock`）；
- 原子写 JSON（`tmp` + `fsync` + `os.replace`）；
- 可注入 `now` / `quota_snapshot`，决策测试不依赖真实状态文件。

## 快速开始

### 直接 import（源码运行）

```bash
git clone https://github.com/Odd-C/model-scheduler.git
cd model-scheduler
PYTHONPATH=src python
```

两行跑通：

```python
from llm_router import assess_difficulty, route_model

text = "帮我写一个 Python 脚本"
decision = route_model(assess_difficulty(text), urgent=False)
print(decision)
# {'model': 'claude-3-5-sonnet', 'provider': 'anthropic', 'reason': '复杂任务，Claude 3.5 Sonnet（示例：免费 S 级旗舰） 可用', 'tier': 'S+', 'cost': 'free'}
```

会话级推荐入口：

```python
from llm_router import recommend_for_session

rec = recommend_for_session("帮我写一个 Python 脚本", message_count=3)
print(rec)
```

完整示例见 `examples/quickstart.py`。

## 配置覆盖方式（三选一）

### 1. JSON 文件覆盖

state 目录默认是 `~/.llm-router`，画像文件名为 `model-policy.json`。示例见 `examples/custom_policy.json`。合并规则：`models` 中每个键会与默认画像按 `id@provider` 合并，传入的非空字段覆盖默认值。

### 2. 环境变量

```bash
export LLM_ROUTER_STATE_DIR=/path/to/state
```

库会从该目录读取/写入 `model-policy.json`、`model-quota.json` 和 `model-cooldown.json`。

### 3. 代码参数

```python
from pathlib import Path
from llm_router import ModelPolicy, QuotaTracker, ModelRouter

state = Path("/tmp/llm-router-state")
policy = ModelPolicy(state_dir=state)
quota = QuotaTracker(state_dir=state, policy_store=policy)
router = ModelRouter(state_dir=state)

# 或者直接配置模块级默认 state 目录
from llm_router import configure_state_dir
configure_state_dir("/tmp/llm-router-state")
```

## 决策规则说明

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

- 前端面板接入：可视化画像表编辑、额度状态、路由决策日志；
- 定时全局切换：按 schedule 规则自动切换默认模型；
- OpenAI 兼容中间件：作为 API 网关插件，在请求转发前自动选择模型；
- 多租户：按租户隔离 state 目录与画像覆盖；
- 版本化手动标记：将「用户手动选择模型」的标记策略版本化，避免旧版本残留标记误伤新策略。

## License

MIT License。全文见 `LICENSE`。

Copyright (c) 2026 llm-router contributors

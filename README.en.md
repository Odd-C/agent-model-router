# model-scheduler

[English](README.en.md) | [中文](README.md)

**model-scheduler** is an intelligent model scheduler: model profiling + free-quota tracking + routing decisions, packaged as a zero-dependency pure-Python standard-library library. Any OpenAI-compatible API consumer can plug it in.

## Why this library

In real projects you often work with multiple model providers at once:

- Some models are free but have a call cap per 5-hour window;
- Some models are paid, and prices double during peak hours;
- The same `model id` can be served by different providers (e.g. `gpt-4o-mini@openai` vs `gemini-2.0-flash@google`);
- For complex tasks you want the free flagship model, but when the free quota runs out or rate limits hit, you must automatically fall back to a paid model.

If every caller hand-rolls its own "which model to pick" logic, the rules get scattered, hard to tune, and hard to test. model-scheduler bakes the decision rules into a small library and supports JSON policy overrides so your routing strategy can keep evolving without touching code.

## Core concepts

### `id@provider` unique key

A model is uniquely identified by `id@provider`, e.g.:

- `gpt-4o@openai` — OpenAI paid flagship;
- `gpt-4o-mini@openai` — OpenAI paid economy;
- `gemini-2.0-flash@google` — Google free high-volume model;
- `deepseek-chat@deepseek` — DeepSeek free preview model;
- `claude-3-5-sonnet@anthropic` — Anthropic free flagship model.

> The default profile is a **generic example** to demonstrate the mechanism. Override it with your real models/quotas via `model-policy.json`, or edit the defaults in `policy.py`.

### Model profile table

Each profile entry describes: capability tier (`tier`), paid/free (`cost`), 5-hour window quota (`quota_per_window`), peak-safety (`peak_safe`), fallback chain (`fallback_chain`), scenario tags (`scenarios`) and the **routing role** (`role`).

`role` is the abstraction layer of the decision chain, decoupled from concrete model names:

| role | meaning | default example model |
|---|---|---|
| `stable` | paid, most reliable, used for urgent tasks | gpt-4o |
| `free-flagship` | free flagship, preferred for complex tasks | claude-3-5-sonnet |
| `free-bulk` | free high-volume, daily workhorse | gemini-2.0-flash |
| `free-preview` | free preview, daily fallback | deepseek-chat |
| `paid-fallback` | paid fallback | gpt-4o-mini |

### Sliding-window quota

Free-model calls are tracked in a **5-hour sliding window** in `model-quota.json`. `quota_left()` returns remaining calls in the current window; models without a profile entry (or paid models) return `-1` (unlimited). `reset_if_needed()` prunes expired records. Thread-safe + atomic writes.

### Decision chains (role-driven)

Routing groups difficulty into branches, each walking a role chain (`ROUTE_CHAINS`, configurable). Free models win during both peak and off-peak; only when free models are unavailable or exhausted does routing fall back to paid models, and peak-hour fallbacks append a "peak price doubled" warning to `reason`.

| branch | role chain (priority order) |
|---|---|
| urgent | `stable` → `paid-fallback` |
| difficulty >= 4 | `free-flagship` → `stable` → `paid-fallback` |
| difficulty 2-3 | `free-bulk` → `free-preview` → `free-flagship` → `paid-fallback` |
| difficulty 0-1 | `free-bulk` → `free-preview` → `paid-fallback` |

Switching models = editing the `role` field in the profile table, **no routing code changes**.

### Peak hours

- Peak: Beijing time 9:00-12:00, 14:00-18:00 (inclusive, **default**)
- Off-peak: everything else
- Timezone: `Asia/Shanghai` (CST +0800)
- **Globally configurable**: set `peak_hours` in `model-policy.json`, e.g. `[[8, 10], [20, 22]]`; `[]` means no peak hours (flat all day)
- **Per-model configurable**: add a `peak_hours` field to a profile entry, priority **model-level > global > default**:

```json
{
  "models": {
    "gemini-2.0-flash@google": { "peak_hours": [[22, 23]] },
    "deepseek-chat@deepseek":  { "peak_hours": [] }
  }
}
```

In the example above: gemini peaks at 22-23; deepseek-chat has no peak hours; every other model follows the global config.
The paid-fallback warning ("peak price doubled" in `reason`) is also evaluated against the **fallback target model's** peak hours, not the global setting.

## Features

- Pure Python standard library, zero third-party runtime dependencies;
- Parameterizable state directory: `LLM_ROUTER_STATE_DIR` env var, `configure_state_dir()`, or constructor `state_dir` argument;
- Zero I/O side effects on import: directories are only created on first state read/write;
- Thread-safe quota tracking (`threading.Lock`);
- Atomic JSON writes (`tmp` + `fsync` + `os.replace`);
- Injectable `now` / `quota_snapshot` so decision tests never touch real state files.

## Quick start

### Direct import (run from source)

```bash
git clone https://github.com/Odd-C/model-scheduler.git
cd model-scheduler
PYTHONPATH=src python
```

Two lines to get going:

```python
from llm_router import assess_difficulty, route_model

text = "帮我写一个 Python 脚本"
decision = route_model(assess_difficulty(text), urgent=False)
print(decision)
# {'model': 'claude-3-5-sonnet', 'provider': 'anthropic', 'reason': '复杂任务，Claude 3.5 Sonnet（免费 S 级旗舰） 可用', 'tier': 'S+', 'cost': 'free'}
```

Session-level recommendation entry:

```python
from llm_router import recommend_for_session

rec = recommend_for_session("帮我写一个 Python 脚本", message_count=3)
print(rec)
```

Full example: `examples/quickstart.py`.

## Configuration overrides (pick one)

### 1. JSON file override

Default state dir is `~/.llm-router`, profile file is `model-policy.json`. See `examples/custom_policy.json`. Merge rule: each key under `models` merges with the default profile by `id@provider`; non-empty fields in the override win.

### 2. Environment variable

```bash
export LLM_ROUTER_STATE_DIR=/path/to/state
```

The library reads/writes `model-policy.json`, `model-quota.json` and `model-cooldown.json` in that directory.

### 3. Code arguments

```python
from pathlib import Path
from llm_router import ModelPolicy, QuotaTracker, ModelRouter

state = Path("/tmp/llm-router-state")
policy = ModelPolicy(state_dir=state)
quota = QuotaTracker(state_dir=state, policy_store=policy)
router = ModelRouter(state_dir=state)

# Or configure the module-level default state dir
from llm_router import configure_state_dir
configure_state_dir("/tmp/llm-router-state")
```

## Decision rules

`assess_difficulty(text)` is a pure-CPU rule scorer, range 0-5:

- Code block ```` ``` ````: +2
- Error words (报错/error/exception/traceback/failed/崩溃/fail): +2
- Source references (`.py`/`.js`/`.ts`/源码/函数/class/def/import/接口): +1
- Strong task intent (写代码/开发/重构/修 bug/debug etc.): +3
- Weak intent words (代码/脚本/项目/bug/算法/模块/接口/前端/后端/数据库/部署/优化/重构/功能/系统/网站/应用/框架/demo/函数): +1
- Text > 2000 chars: +1; > 8000 chars: +1
- Clamped to `[0, 5]`

`assess_urgency(text)` detects: 紧急|马上|尽快|asap|urgent|立刻.

`route_model(difficulty, *, urgent, now=None, quota_snapshot=None)` returns:

```json
{"model": "...", "provider": "...", "reason": "...", "tier": "...", "cost": "..."}
```

`quota_snapshot` accepts `{"id@provider": remaining}`; when provided, decisions never touch real quota files — handy for tests and external integration.

## Running tests

The library itself has zero third-party runtime dependencies; tests use the stdlib `unittest`, so you can run them directly:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Or with pytest (optional, test-time only):

```bash
pip install pytest
python -m pytest tests -q
```

## Roadmap

- Frontend panel: visualize profile editing, quota status, routing decision logs;
- Scheduled global switching: switch the default model automatically by `schedule` rules;
- OpenAI-compatible middleware: an API-gateway plugin that picks the model before forwarding requests;
- Multi-tenancy: isolate state dirs and profile overrides per tenant;
- Versioned manual-override markers: version the "user manually picked a model" marker so stale markers from older versions can't poison new policies.

## License

MIT License. See `LICENSE`.

Copyright (c) 2026 model-scheduler contributors

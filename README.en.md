# model-scheduler

[English](README.en.md) | [中文](README.md)

**MIT License** · **Python 3.10+** · **Zero Dependencies** · **279 tests passing**

---

**model-scheduler** is an intelligent model scheduler: model profiling + free-quota tracking + routing decisions + task scheduling, packaged as a zero-dependency pure-Python standard-library library. Any OpenAI-compatible API consumer can plug it in. From "which model still works" to "which model is most cost-effective" — Utility scoring, hard constraints, Policy Compiler natural-language intents, a task system, and a dashboard are all included.

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
| Difficulty 2-3 | `free-bulk` → `free-preview` → `free-flagship` → `paid-fallback` |
| Difficulty 0-1 | `free-bulk` → `free-preview` → `paid-fallback` |

Switch models by editing the `role` field in the profile table — **no routing code changes**.

> **v0.4+ upgrade path**: role chains are the v0.2 default. Since v0.4, **Utility scoring** (`route_with_utility`) scores candidates across six normalized dimensions (quality/cost/latency/health/quota/deadline) with HardConstraints applied before scoring. Benchmarks: utility scoring saves ~69% cost and cuts latency by 43% vs role chains. New integrations should use scoring; role chains remain for backward compatibility.

### Utility scoring (v0.4+)

```python
from model_scheduler.utility import route_with_utility, HardConstraints
from model_scheduler.policy import list_models

result = route_with_utility(
    {"task_type": "coding", "priority": "high", "deadline": None},
    list_models(),
    constraints=HardConstraints(cost_max="free"),   # filter first, then score
)
# → {model, provider, score, breakdown: {quality_fit, cost_penalty, ...}, why}
```

- Six dimensions: quality_fit / cost_penalty / latency_penalty / failure_risk / quota_pressure / deadline_pressure
- Candidate-set min-max normalization before weighting (equal weights ≠ equal impact)
- Breakdown exposes three layers (raw / normalized / weighted) to explain "why A over B"
- Capability constraints: `max_latency_ms` / `min_quality_tier` / `min_capability_pct`; image/vision tasks enforce vision capability (text-only models get quality_fit=0)

### Policy Compiler (v0.5+, natural-language intents)

```python
from model_scheduler.policy_compiler import compile_intent, route_with_intent

cp = compile_intent("要便宜点的")   # also: "make it cheap"
# → CompiledPolicy{constraints: {cost_max: "free"}, weights: {cost_penalty: 3.0}, mode: "cost-first", explanation}

result = route_with_intent(
    {"task_type": "coding", "priority": "high", "deadline": None},
    list_models(),
    "高质量但不要太贵",   # natural language → hard-constraint union + quality weights
)
```

Keyword-rule matching in Chinese and English (no NLP needed): speed ("尽快"/"3秒" → latency-first + max_latency_ms), cost ("便宜"/"free" → cost-first + cost_max=free), quality ("高质量"/"flagship" → quality-first + min_quality_tier), capability percentage ("达到 gpt-4o 的 80%" → pct + reference); unmatched falls back to balanced.

### Task system (v0.3+)

Task model (task_id / task_type / priority / deadline / defer_until / status / payload) + state machine + TaskStore persistence:

```python
from model_scheduler.task import TaskStore, Task
from model_scheduler.scheduler import TaskScheduler
from model_scheduler.executor import MockExecutor

store = TaskStore(state_dir, backend="sqlite")   # json / sqlite backends
scheduler = TaskScheduler(store, MockExecutor())
task = scheduler.submit("text", {}, priority="high", deadline=1_800_000_000)
scheduler.tick(now=1_800_000_100)                # process queued/deferred
```

- State machine: queued → running → done/failed; deferred → queued by defer_until; expired on deadline
- Degradation matrix: error classification (429→cooldown-retry / 5xx→switch candidate / 400-403→no retry) recorded in `last_error.action_taken`
- ProviderHealth: sliding-window health profile (success_rate / p50 / p95 / failure_risk)

### Dashboard (taskserver, v0.3+)

```bash
PYTHONPATH=src python -m model_scheduler.taskserver --port 8099
```

Opportunistic Scheduling dashboard: task list / submit / manual tick / four preference modes / model profiles / A2A endpoints (submit task / query / fetch result). Pure stdlib, no CDN or external resources.

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
- Three routing options: role chains (v0.2) / Utility scoring (v0.4+) / Policy Compiler natural-language intents (v0.5+);
- Task system (v0.3+): Task state machine + TaskStore persistence (Json / SQLite backends, SQLite cross-process atomic) + degradation matrix + ProviderHealth;
- Explainable decisions: Utility scoring returns a breakdown (quality/cost/latency/health/quota/deadline) that answers "why A over B";
- Hard constraints filter before scoring: cost cap / quota exhausted / cooldown / deadline infeasible / health red-line / capability bounds (latency / quality tier / capability percentage); image/vision tasks enforce vision capability;
- Dashboard (taskserver): Opportunistic Scheduling panel + A2A endpoints (submit / query / fetch result), pure stdlib, no external resources;
- Benchmark tool (v0.6+): reproducible task sets comparing utility vs role chains vs round-robin (success rate / cost / P95 / fallback rate);
- Parameterized state directory: `LLM_ROUTER_STATE_DIR` env var, `configure_state_dir()`, or constructor `state_dir`;
- Zero I/O side effects on import: state files are created on first read/write;
- Thread-safe quota recording (`threading.Lock`); SQLite backend supports cross-process atomic read-modify-write;
- Atomic JSON writes (`tmp` + `fsync` + `os.replace`);
- Injectable `now` / `quota_snapshot`: decisions are testable without real state files.

## Quick start

### Install (recommended: PyPI)

```bash
pip install model-scheduler
```

Import directly after install (no need to clone the source) — **Utility scoring (v0.4+) is recommended**:

```python
from model_scheduler import route_with_utility
from model_scheduler.policy import list_models
from model_scheduler.utility import HardConstraints

# All model profiles as candidates; filter by hard constraints, then score across six dimensions
result = route_with_utility(
    {"task_type": "coding", "priority": "high", "deadline": None},
    list_models(),
    constraints=HardConstraints(cost_max="free"),   # free models only
)
print(result)
# → {model, provider, score, breakdown: {quality/cost/latency/health/quota/deadline}, why}
```

Natural-language intent entry (v0.5+, Policy Compiler):

```python
from model_scheduler import route_with_intent

result = route_with_intent(
    {"task_type": "coding", "priority": "high", "deadline": None},
    list_models(),
    "make it cheap",         # natural language → hard constraints + weights
)
```

### Direct import (run from source)

```bash
git clone https://github.com/Odd-C/model-scheduler.git
cd model-scheduler
PYTHONPATH=src python
```

Full example: `examples/quickstart.py`.

### v0.2 compatibility layer (old API, kept for backward compatibility)

These v0.2-era APIs still work, but **new projects should use the Utility scoring above**:

```python
from model_scheduler import assess_difficulty, route_model, recommend_for_session

text = "帮我写一个 Python 脚本"
decision = route_model(assess_difficulty(text), urgent=False)
print(decision)
# {'model': 'claude-3-5-sonnet', 'provider': 'anthropic', 'reason': 'Complex task, ...', 'tier': 'S+', 'cost': 'free'}

rec = recommend_for_session(text, message_count=3, session_id="demo-session-1")
print(rec)
```

- `assess_difficulty(text)` / `route_model(difficulty, ...)`: keyword-difficulty → role chain (v0.2 default)
- `recommend_for_session(...)`: session-level recommendation (difficulty + role chain + quota/cooldown)
- Proxy CLI: `model-scheduler serve --config model-policy.json` (see next section, independently usable)

### Proxy layer (OpenAI-compatible, works with any agent)

Since v0.2.0, model-scheduler ships an **OpenAI-compatible proxy layer**: any OpenAI-compatible client (Hermes / Claude Code / Codex / OpenClaw / any SDK) can point its `base_url` at the proxy and automatically get difficulty assessment + free-quota tracking + peak-hour awareness + failure cooldown fallback — **zero code changes**.

```bash
pip install model-scheduler

# Prepare config (model-policy.json must include a providers section)
export OPENAI_API_KEY=your-openai-api-key
export DEEPSEEK_API_KEY=your-deepseek-api-key

# Start the proxy (pure local process, zero external dependencies, no telemetry)
model-scheduler serve --config model-policy.json --host 127.0.0.1 --port 8765
```

Plug in any client:

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"write a quicksort"}],"stream":true}'
```

```python
# OpenAI SDK example (point base_url at the proxy; api_key can be any placeholder)
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="unused")
resp = client.chat.completions.create(model="auto", messages=[{"role": "user", "content": "write a quicksort"}])
```

Proxy features:
- `POST /v1/chat/completions` (streaming SSE + non-streaming pass-through)
- `GET /v1/models` (lists only currently available models)
- `GET /v1/health` (health check)
- Auto-accounting on every call (`record_call`), automatic cooldown on failure (`record_failure` → routing skips the model)
- Keys referenced via `env:VAR` — **never hardcoded into config**
- Pure Python standard library (`http.server` + `urllib`), zero third-party dependencies

**Deployment model**: the proxy is a pure local process — no external scheduler service or central node; decisions and state (quota, peak hours, cooldown) live in the library + local state files, no telemetry collected. You only need your own provider API keys and your own `model-policy.json` (the default profile is just a mechanism demo).

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

## Integration best practices (using as a library)

> This section captures lessons learned from integrating model-scheduler into third-party applications (e.g. a web UI model picker). All examples use public generic model names and are directly runnable.

### 1. Optional dependency: lazy import + graceful degradation

When model-scheduler is an optional dependency, import it lazily and cache a miss flag so the host app never crashes when the library is not installed:

```python
try:
    import model_scheduler as ms
    _HAS_MODEL_SCHEDULER = True
except Exception:
    _HAS_MODEL_SCHEDULER = False
    ms = None

def get_recommendation(text, message_count=0, session_id=None):
    if not _HAS_MODEL_SCHEDULER:
        return None  # graceful degradation: caller keeps its own default behavior
    return ms.recommend_for_session(
        text,
        message_count=message_count,
        session_id=session_id,
    )
```

### 2. Single source of truth for the enable switch

The caller's own master switch is the only gate; the `enabled` field in the policy file is informational and never used by routing (`router._route` does not read it). Do not try to disable scheduling via the policy file's `enabled` field:

```python
import model_scheduler as ms

SCHEDULER_ENABLED = True  # the caller's own master switch, the single authority

def maybe_recommend(text, message_count=0, session_id=None):
    if not SCHEDULER_ENABLED:
        return None
    return ms.recommend_for_session(text, message_count=message_count, session_id=session_id)
```

### 3. Applying recommendations

`recommend_for_session(text, message_count=..., session_id=...)` returns:
`difficulty` / `urgent` / `message_count` / `peak` / `model` / `provider` / `reason` / `tier` / `cost` / `key` (plus `session_id` when a non-empty session id is supplied).

This library is an advisor, not a butler: whether to apply or display the recommendation is entirely the caller's decision.

```python
import model_scheduler as ms

rec = ms.recommend_for_session(
    "帮我写一个 Python 脚本",
    message_count=3,
    session_id="sess-123",
)
print(rec["model"], rec["provider"], rec["reason"], rec["key"])
```

### 4. Key formats: internal `id@provider` vs UI `provider/model`

- Internal unique key: `id@provider` (also used for policy/quota/cooldown state-file keys) — `format_model_key` / `parse_model_key`
- UI dropdown selector / external system value: `provider/model` — `format_selector_key` / `parse_selector_key`

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

### 5. Failure cooldown integration

Call `record_failure(model, provider)` for real upstream provider failures (429 / quota exhausted / authentication failure, etc.). Free models then enter cooldown automatically (default 300s; `cooldown_seconds_left` can be checked) and routing skips them. Do **not** record non-model failures such as network disconnections or local timeouts:

```python
import model_scheduler as ms

class UpstreamRateLimitError(Exception):
    pass

class NetworkError(Exception):
    pass

def call_model(model, provider):
    # Replace with your actual upstream call
    pass

def call_with_cooldown(model, provider):
    try:
        call_model(model, provider)
    except UpstreamRateLimitError:
        ms.record_failure(model, provider)
        raise
    except NetworkError:
        raise  # non-model failures are not recorded for cooldown
```

### 6. Recommendation cache

If you cache recommendations before sending, the cache key must be isolated by session id + text; a 60s TTL is recommended:

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

## Configuration overrides (pick one)

### 1. JSON file override

Default state dir is `~/.llm-router`, profile file is `model-policy.json`. See `examples/custom_policy.json`. Merge rule: each key under `models` merges with the default profile by `id@provider`; non-empty fields in the override win.

Language: set `"language": "zh" | "en"` in `model-policy.json` (default `zh`).
The `reason` strings produced by routing follow this setting. The `label` field in
the profile table is a display name (English by default; override per model if needed).

### 2. Environment variable

```bash
export LLM_ROUTER_STATE_DIR=/path/to/state
```

The library reads/writes `model-policy.json`, `model-quota.json` and `model-cooldown.json` in that directory.

### 3. Code arguments

```python
from pathlib import Path
from model_scheduler import ModelPolicy, QuotaTracker, ModelRouter

state = Path("/tmp/llm-router-state")
policy = ModelPolicy(state_dir=state)
quota = QuotaTracker(state_dir=state, policy_store=policy)
router = ModelRouter(state_dir=state)

# Or configure the module-level default state dir
from model_scheduler import configure_state_dir
configure_state_dir("/tmp/llm-router-state")
```

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

**Completed (v0.3 → v0.6.2)**:

- ✅ v0.3 Task system: Task state machine + TaskStore + Executor abstraction + error classification (400/401/403 no cooldown / 429/5xx/transport classified)
- ✅ v0.3 Dashboard: Opportunistic Scheduling panel (tasks/schedule rules/profiles/tester), pure stdlib, no external resources
- ✅ v0.4 Utility scoring: six dimensions + min-max normalization + configurable weights + explainable breakdown
- ✅ v0.4 Hard constraints: cost / quota / cooldown / deadline / health red-line + capability bounds (latency/quality tier/capability percentage)
- ✅ v0.4 ProviderHealth: sliding-window health profile (success_rate / p50 / p95)
- ✅ v0.5 Policy Compiler: natural-language intents → hard constraints + weights (Chinese/English rule templates, no NLP)
- ✅ v0.5 A2A endpoints: submit task / query task / fetch result
- ✅ v0.6 StateStore: Json / SQLite backends (SQLite BEGIN IMMEDIATE cross-process atomic)
- ✅ v0.6 Benchmark: reproducible task sets comparing utility vs role chains vs round-robin
- ✅ v0.6.2 Capability check: image/vision tasks enforce vision capability (text-only models get quality_fit=0)
- ✅ Access-layer task_type classification (v2.1): LLM Judge (free fast model, default GLM-4-Flash) + multi-feature local classifier fallback (text length/attachments/code blocks/tools/context/history/session state), smart even without an LLM — lives in the WebUI access layer (not in this library; the library stays zero-dependency)

**Future directions**:

- RedisStateStore backend (shared state across instances);
- Multi-tenancy: isolate state dirs and profile overrides per tenant;
- OpenAI-compatible middleware as an API-gateway plugin (the current proxy layer is a standalone process; the "gateway plugin" form is not done yet);
- Versioned manual-override markers (the current marker mechanism lacks generic versioning and automatic stale-marker cleanup).

## License

MIT License. See `LICENSE`.

Copyright (c) 2026 model-scheduler contributors

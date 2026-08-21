# agent-model-router

[English](README.md) | [中文](README.zh-CN.md)

**MIT License** · **Python 3.10+** · **Zero Dependencies** · **283 tests passing**

---

**agent-model-router** is a **zero-dependency LLM scheduling library** that makes explainable "which model for this task" decisions. It evolved from v0.2 rule-based routing to the **Utility scoring** (v0.4+) — a six-dimension normalized score (quality / cost / latency / health / quota / deadline) with hard constraints filtered first — plus a task system, a Policy Compiler for natural-language intents, a dashboard, and a benchmark tool.

**One-liner**: from "which model still works" to "which model is most cost-effective".

## Decision module

```text
route_with_intent(task, models, "make it cheap")
  → Policy Compiler:  "make it cheap" → {cost_max: "free", cost-first}
  → hard constraints: quota / cooldown / deadline / health / capability
  → utility scoring:  quality / cost / latency / health / quota / deadline
  → recommendation:   {model, provider, score, breakdown, why}
```

Model profiles are declared in `model-policy.json`; every decision returns a `breakdown` and a `why`.

## Why this library

Real pain points when integrating multiple model providers:

- Some models are free but have sliding-window call caps; paid models double in price during peak hours;
- The same `model id` can be served by different providers (`gpt-4o-mini@openai` vs `gemini-2.0-flash@google`);
- Complex tasks want the free flagship, but must auto-fall back when quota runs out or rate limits hit;
- Every caller hand-rolling its own "which model to pick" logic scatters rules, makes tuning and testing hard.

agent-model-router bakes decisions into one pure-stdlib component: JSON-overridable model profiles, explainable decisions, traceable degradation, and pluggable by any OpenAI-compatible caller.

## Core concepts

### `id@provider` unique key

Models are uniquely identified as `id@provider`, avoiding cross-provider ambiguity (e.g. `glm-5.2@sensenova` and `glm-5.2@zhipu` are distinct candidates).

### Model profile table

Model capability/cost/role lives in JSON — **change config, not code**:

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

Profile fields: `tier` (S+/S/A/A-/B+/B/C capability grade), `cost` (free/paid), `role` (capability label), `scenarios`, `quota_per_window` (free quota), `peak_hours` (per-model override), `capability` (0-1 score for capability hard constraints).

### Decision options (three)

| Mode | Version | Description |
|---|---|---|
| **Utility scoring** | v0.4+ (recommended) | six-dimension normalized scoring + hard constraints first; explainable breakdown |
| **Policy Compiler** | v0.5+ | natural-language intents → hard constraints + weights |
| role chain | v0.2 (compat layer) | difficulty tiers → fixed fallback chains |

### Utility scoring (v0.4+, recommended)

Candidates are **min-max normalized** within the candidate set across six dimensions, then weighted:

- `quality_fit` — tier expectation + scenario capability
- `cost_penalty` — free=0 / paid=0.6, peak-hour multiplier
- `latency_penalty` — ProviderHealth p95
- `failure_risk` — health profile
- `quota_pressure` — remaining quota
- `deadline_pressure` — deadline proximity

**Hard constraints first** (non-negotiable): cost cap / quota exhausted / cooldown / deadline infeasible / health red-line / capability bounds (`max_latency_ms`, `min_quality_tier`, `min_capability_pct`); image/vision tasks enforce vision capability (text-only models get quality_fit=0 — better none than wrong).

### Policy Compiler (v0.5+, natural-language intents)

```
"make it cheap" → compile_intent → {cost_max: "free", cost-first weights, explanation}
```

Keyword-rule matching in Chinese and English (no NLP): speed ("尽快"/"3秒" → latency-first + max_latency_ms), cost ("便宜"/"free" → cost-first + cost_max=free), quality ("高质量"/"flagship" → quality-first + min_quality_tier), capability percentage ("达到 gpt-4o 的 80%" → pct + reference); unmatched falls back to balanced.

### Task system (v0.3+)

`Task` (task_id / task_type / priority / deadline / defer_until / status / payload) + state machine + TaskStore persistence (Json / SQLite backends):

- State machine: queued → running → done/failed; deferred → queued by defer_until; expired on deadline
- Degradation matrix: error classification (429→cooldown-retry / 5xx→switch candidate / 400-403→no retry) recorded in `last_error.action_taken`
- ProviderHealth: sliding-window health profile (success_rate / p50 / p95 / failure_risk)
- SQLite backend uses `BEGIN IMMEDIATE` for cross-process atomicity

### Dashboard (taskserver, v0.3+)

Opportunistic Scheduling single page: task list / submit / manual tick / four preference modes / model profiles / recommendation tester + A2A endpoints (submit / query / fetch result). Pure stdlib, no CDN or external resources.

### Task understanding (task_type: library vs access layer)

**This library is a zero-dependency pure-stdlib package — it makes no LLM calls itself.** Task understanding is split into two layers:

| Layer | Responsibility | Implementation |
|---|---|---|
| **Caller (access layer)** | map task description → `task_type` (coding / image / text / batch / maintenance) | access layer's own; recommended chain: LLM Judge (free fast model, pluggable OpenAI-compatible) → multi-feature local classifier (text length / attachments / code blocks / tools / context / history / session state) → keyword whitelist → text; works smartly even without an LLM (reference implementation in the WebUI access layer) |
| **This library** | receives `task_type`, does quality matching / scoring | `task_type_tier_expectation(task_type)` decides expected tiers, `quality_fit` computes the match; image/vision tasks enforce vision capability |

The library does not guess the task type — `task_type` is passed explicitly by the caller or inferred by the access layer. v0.2's `assess_difficulty(text)` (0-5 difficulty score) is a compatibility layer used internally by the proxy; new projects don't need it.

## Quick start

### Install

```bash
pip install agent-model-router
```

### Minimal routing (Utility scoring, recommended)

```python
from agent_model_router import route_with_utility
from agent_model_router.policy import list_models
from agent_model_router.utility import HardConstraints

# All profiles as candidates: filter by hard constraints (free only), then score across six dimensions
result = route_with_utility(
    {"task_type": "coding", "priority": "high", "deadline": None},
    list_models(),
    constraints=HardConstraints(cost_max="free"),
)
print(result)
# → {model, provider, score, breakdown: {quality/cost/latency/health/quota/deadline}, why}
```

### Natural-language intent (Policy Compiler)

```python
from agent_model_router import route_with_intent

result = route_with_intent(
    {"task_type": "coding", "priority": "high", "deadline": None},
    list_models(),
    "make it cheap",   # natural language → hard constraints + weights
)
```

### Task scheduling

```python
from agent_model_router.task import TaskStore
from agent_model_router.scheduler import TaskScheduler
from agent_model_router.executor import MockExecutor

store = TaskStore(state_dir, backend="sqlite")   # json / sqlite
scheduler = TaskScheduler(store, MockExecutor())
now = time.time()
# deadline is an absolute timestamp (10 min out) → tick before expiry → executes
task = scheduler.submit("text", {}, priority="high", deadline=now + 600)
scheduler.tick(now=now + 1)                      # not expired → executed → done
print(store.get(task.task_id).status)            # → done
```

### Dashboard

```bash
PYTHONPATH=src python -m agent_model_router.taskserver --port 8099
```

### Proxy layer (OpenAI-compatible, v0.2+)

```bash
agent-model-router serve --config model-policy.json --host 127.0.0.1 --port 8765
```

Any OpenAI-compatible client points `base_url` at the proxy (gets quota tracking / peak awareness / failure cooldown), zero code changes.

### v0.2 compatibility layer (old API)

`assess_difficulty` / `route_model` / `recommend_for_session` still work (used internally by the proxy), but new projects should use Utility scoring:

```python
from agent_model_router import assess_difficulty, route_model

decision = route_model(assess_difficulty("Write a Python script"), urgent=False)
```

## Integration best practices

> Field-tested by upstream third-party apps (e.g. WebUI model selector). All examples use public generic model names.

### 0. Pre-integration checklist (required actions; missing any one causes problems)

| # | Action | Why | Failure mode |
|---|---|---|---|
| 1 | **Configure the state directory** | `configure_state_dir()` or env `LLM_ROUTER_STATE_DIR`; make sure it is **writable** | state lands in the default dir; multi-instance/multi-process state overwrites each other |
| 2 | **Write your own model profiles** | `model-policy.json` overrides the built-in sample profiles; the 5 built-in public samples (claude-3-5-sonnet/deepseek-chat/gemini/gpt-4o-mini/gpt-4o) are **mechanism demos, not your real models** | routes to nonexistent models / quota tracking is meaningless |
| 3 | **Provider connection ready** | every `id@provider` in the profiles needs a provider with base_url + api_key (in the access layer); reference keys via `env:VAR`, **never hardcode** | the recommendation picks a model but upstream returns 401/403 |
| 4 | **Pass `task_type` or implement access-layer inference** | the library does not guess task types — the task passed to `route_with_utility` must carry `task_type` (coding/image/text/batch/maintenance) or be inferred by the access layer first | quality_fit degrades to no discrimination; poor routing |
| 5 | **Wire up failure reporting** | on upstream failure call `record_failure(model, provider, reason, status)`; on success call `record_result` to update the health profile | cooldown never kicks in; failing models keep getting selected |
| 6 | **Validate free-quota fields** | free models need `quota_per_window` in their profile, otherwise quota tracking is meaningless | exhausted models keep being routed → upstream 429s |

**Minimal integration skeleton** (combining the actions):

```python
import agent_model_router as ms
ms.configure_state_dir("/var/lib/myapp/ms-state")   # action 1
# action 2: model-policy.json with your real profiles (incl. quota_per_window)
# action 3: provider config lives in the access layer; keys via env
# action 4: resolve task_type before calling (access layer or explicit)

from agent_model_router import route_with_utility
from agent_model_router.policy import list_models

task = {"task_type": "coding", "priority": "high", "deadline": None}
result = route_with_utility(task, list_models())
model, provider = result["model"], result["provider"]

# ... upstream call ...
# action 5: report failures
ms.record_failure(model, provider, reason="rate_limit", status=429)
```

### 1. Optional dependency: lazy import + graceful degradation

```python
def _load_lib():
    try:
        import agent_model_router as ms
        return ms
    except ImportError:
        return None
```

### 2. Single source of truth for the enable switch

Use one master switch (e.g. settings `model_scheduler_enabled`); when off, produce no recommendation and never raise.

### 3. Applying recommendations

`route_with_utility` returns `{model, provider, score, breakdown, why}`; set the sending chain from model/provider. The recommendation is an advisor; users can manually override.

### 4. Key formats: `id@provider` vs UI `provider/model`

Library uses `id@provider`; UI selectors often use `provider/model`. Convert with `format_model_key` / `parse_model_key` / `format_selector_key` / `parse_selector_key`.

### 5. Failure cooldown integration

On upstream failure call `record_failure(model, provider, reason, status)`; the next route skips models in cooldown. Classification: 429/5xx → cooldown; 400/401/403 → no cooldown.

### 6. Recommendation cache

60s TTL per text, avoiding repeated computation per message (access-layer implementation).

## Configuration (pick one)

1. **JSON file**: `model-policy.json` overrides default profiles (change config, not code)
2. **Environment**: `LLM_ROUTER_STATE_DIR` sets state dir; access-layer params like `MODEL_SCHEDULER_JUDGE_MODEL` can be overridden
3. **Code**: `configure_state_dir()` / constructor `state_dir`

## Benchmark

```bash
PYTHONPATH=src python -m agent_model_router.benchmark --tasks 300 --seed 42
```

Reproducible task sets comparing utility vs role chains vs round-robin. Measured (300 tasks):

| Strategy | Cost | P95 latency |
|---|---|---|
| **utility (scoring)** | **11** | **536ms** |
| chain (role chain) | 35 | 943ms |
| round_robin | 130 | 858ms |

## Running tests

```bash
python -m pytest tests -q          # requires pytest
PYTHONPATH=src python -m unittest discover -s tests -v   # pure stdlib
```

Optional live smoke test against a real OpenAI-compatible endpoint (skips when envs unset):

```bash
MODEL_SCHEDULER_SMOKE_BASE_URL=... MODEL_SCHEDULER_SMOKE_API_KEY=... MODEL_SCHEDULER_SMOKE_MODEL=... \
  python -m pytest tests/test_live_smoke.py -v
```

## Version history

See [CHANGELOG.md](CHANGELOG.md) (detailed changes) and [RELEASES.md](docs/RELEASES.md) (release notes). API contract: [API.md](docs/API.md).

## License

MIT License. See `LICENSE`.

Copyright (c) 2026 llm-router contributors

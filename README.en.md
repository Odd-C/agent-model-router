# model-scheduler

[English](README.en.md) | [中文](README.md)

**MIT License** · **Python 3.10+** · **Zero Dependencies** · **283 tests passing**

---

**model-scheduler** is a **zero-dependency LLM scheduling library** that makes explainable "which model for this task" decisions. It evolved from v0.2 rule-based routing to the **Utility scoring** (v0.4+) — a six-dimension normalized score (quality / cost / latency / health / quota / deadline) with hard constraints filtered first — plus a task system, a Policy Compiler for natural-language intents, a dashboard, and a benchmark tool.

**One-liner**: from "which model still works" to "which model is most cost-effective".

## Why this library

Real pain points when integrating multiple model providers:

- Some models are free but have sliding-window call caps; paid models double in price during peak hours;
- The same `model id` can be served by different providers (`gpt-4o-mini@openai` vs `gemini-2.0-flash@google`);
- Complex tasks want the free flagship, but must auto-fall back when quota runs out or rate limits hit;
- Every caller hand-rolling its own "which model to pick" logic scatters rules, makes tuning and testing hard.

model-scheduler bakes decisions into one pure-stdlib component: JSON-overridable model profiles, explainable decisions, traceable degradation, and pluggable by any OpenAI-compatible caller.

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
pip install model-scheduler
```

### Minimal routing (Utility scoring, recommended)

```python
from model_scheduler import route_with_utility
from model_scheduler.policy import list_models
from model_scheduler.utility import HardConstraints

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
from model_scheduler import route_with_intent

result = route_with_intent(
    {"task_type": "coding", "priority": "high", "deadline": None},
    list_models(),
    "make it cheap",   # natural language → hard constraints + weights
)
```

### Task scheduling

```python
from model_scheduler.task import TaskStore
from model_scheduler.scheduler import TaskScheduler
from model_scheduler.executor import MockExecutor

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
PYTHONPATH=src python -m model_scheduler.taskserver --port 8099
```

### Proxy layer (OpenAI-compatible, v0.2+)

```bash
model-scheduler serve --config model-policy.json --host 127.0.0.1 --port 8765
```

Any OpenAI-compatible client points `base_url` at the proxy (gets quota tracking / peak awareness / failure cooldown), zero code changes.

### v0.2 compatibility layer (old API)

`assess_difficulty` / `route_model` / `recommend_for_session` still work (used internally by the proxy), but new projects should use Utility scoring:

```python
from model_scheduler import assess_difficulty, route_model

decision = route_model(assess_difficulty("Write a Python script"), urgent=False)
```

## Integration best practices

> Field-tested by upstream third-party apps (e.g. WebUI model selector). All examples use public generic model names.

### 1. Optional dependency: lazy import + graceful degradation

```python
def _load_lib():
    try:
        import model_scheduler as ms
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
PYTHONPATH=src python -m model_scheduler.benchmark --tasks 300 --seed 42
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

See [RELEASES.md](docs/RELEASES.md). API contract: [API.md](docs/API.md).

## License

MIT License. See `LICENSE`.

Copyright (c) 2026 llm-router contributors

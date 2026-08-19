# Changelog

## [0.2.0] - 2026-08-19

### Added
- **OpenAI 兼容代理层**（`model_scheduler.server`）：任何 OpenAI 兼容客户端（Hermes / Claude Code / Codex / OpenClaw / 任意 SDK）把 `base_url` 指向代理即可获得智能调度能力，零改码
  - `POST /v1/chat/completions`：流式 SSE + 非流式透传，请求进入后自动难度评估 → 路由 → 改写 model/headers → 转发目标 provider
  - `GET /v1/models`：只列出当前可用模型（排除冷却中/额度耗尽/无 provider 配置）
  - `GET /v1/health`：健康检查
  - 每次调用自动记账（`record_call`）；上游 4xx/5xx/超时自动冷却（`record_failure` → 下次路由绕过）
  - 响应头 `X-Model-Scheduler: <id@provider>` 便于观测实际路由结果
- **providers 配置段**：`model-policy.json` 新增顶层 `providers`，支持 `env:VAR` 引用环境变量（密钥不硬编码）
- **CLI 入口**：`model-scheduler serve --config ... --host ... --port ...`
- 单元测试 + mock provider 集成测试（非流式/流式/失败降级全链路）

### Changed
- **包名重命名**：`llm_router` → `model_scheduler`（`pip install model-scheduler` 后 `import model_scheduler`，import 名与发布名对齐）
- `ModelPolicy` 新增 `get_providers()` / `provider_config()` / `has_provider()`（实例级 + 模块级）

### Fixed
- 无

## [0.1.0] - 2026-08-19

### Added
- 零依赖纯 Python 标准库：模型画像表（policy）+ 免费额度跟踪（quota）+ role 链驱动决策引擎（router）
- `assess_difficulty()` / `assess_urgency()` / `route_model()` / `recommend_for_session()`
- 5 小时滑动窗口配额、失败冷却、峰谷时段判断（全局 + per-model 可配置）
- `id@provider` 唯一键、JSON 画像覆盖、i18n reason（zh/en）
- 50 个单元测试

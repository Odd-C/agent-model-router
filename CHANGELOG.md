# Changelog

## [0.2.2] - 2026-08-19

### Changed
- 内部重构（无行为变化）：合并 `recommend_for_session` 模块级函数与 `ModelRouter` 方法的重复实现（提取共享 `_recommend_core`）；精简 `make_handler`（提取 `_send_error` / `_send_upstream_unreachable` / `_send_upstream_response` 公共 helper）
- 源码瘦身：`src/model_scheduler/` 2112 → 2063 行；公开 API 签名、返回形状、HTTP 行为完全不变（88 测试全过）

## [0.2.1] - 2026-08-19

### Added
- `recommend_for_session` 新增 `session_id` 可选透传参数：模块级函数与 `ModelRouter.recommend_for_session` 方法签名同步支持；传入非空会话标识时结果末尾追加 `session_id` 字段，不参与难度评估/路由决策/配额判断
- 新增 selector 格式转换 helper：`format_selector_key(model, provider)` / `parse_selector_key(value)`，用于 UI 选择器常见的 `provider/model` 格式与库内部 `id@provider` 唯一键格式之间的转换
- README 新增「作为库接入（Integration）最佳实践」章节，覆盖可选依赖懒加载、启用开关单一权威、推荐结果应用、格式转换、失败冷却链路、推荐缓存等内容

### Changed
- 文档化 `policy.enabled` 字段语义：该字段为信息性字段，不参与路由 gate，启用/禁用由接入方自己的开关控制；`get_policy()` / `update_policy()` docstring 同步说明
- 版本号统一为 0.2.1（`pyproject.toml` / `src/model_scheduler/__init__.py` / `src/model_scheduler/server.py` 三处一致）

### Fixed
- 无

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

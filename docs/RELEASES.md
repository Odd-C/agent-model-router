# Release Notes

本仓库的版本发布记录。GitHub Releases 页面上 v0.3.0 起的条目由本文件同步生成。

## v0.6.2 — image/vision 能力约束（2026-08-21）

### 修复
- **纯文本模型被路由到图片任务**（如文本模型被选做「生成海报」）：`DEFAULT_TASK_TYPE_TIER_EXPECTATION` 补 image/vision/batch/maintenance；`quality_fit` 对 image/vision 任务强制视觉能力校验（scenarios 含 vision/image 或 role 含 vision），无能力 → 0.0；`route_with_utility` 硬过滤 `quality_fit <= 0.0`（宁缺毋滥）
- benchmark 补合成视觉候选保持基准可比

### 验证
- `quality_fit("image", 文本模型) == 0.0`
- `quality_fit("image", 视觉模型) >= 0.9`
- 图片任务路由 → 视觉模型（如 glm-4.6v-flash）

### 测试
274 → 279 passed

---

## v0.6.1 — 最终统一审查修复（2026-08-20）

独立审查（request-changes）发现的地基问题全部修复：

### P0
- **TaskStore SQLite 后端多进程丢数据**：add/update/remove 的 `_load()+_save()` 两次独立调用改 `StateStore.atomic_update`（SQLite `BEGIN IMMEDIATE` 单事务跨进程原子）。复现测试：两进程各加 30 条 → 60 条全在

### P1
- **scheduler.tick 并发不安全**：加 `_tick_lock` 调度锁，并发 tick 不再抛非法迁移
- **deadline 边界不一致**：submit/tick 统一 `deadline <= now` 判过期
- **quota_left=-1 哨兵误判**：-1（不受限/未知）不再被当耗尽排除，quota_pressure=0

### P2（批量）
- NaN 拦截（math.isfinite）/ record_call 规范化 / health 延迟分位修正 / taskserver 500 去 detail + 1MB body 上限 / __all__ 补齐 / task_type 非空 + payload dict 校验

### 测试
251 → 274 passed

---

## v0.6.0 — 稳定基础设施（2026-08-20）

### 新增
- **StateStore 抽象**：`Protocol` + `JsonStateStore` + `SQLiteStateStore`（stdlib sqlite3，零依赖）+ `create_store` 工厂；TaskStore 支持 `backend="json"|"sqlite"`
- **Benchmark 工具**：可复现任务集（seed）+ 三策略对比（utility vs role 链 vs round-robin）+ 结构化报告（成功率/成本/P95/fallback rate）
- **docs/API.md**：taskserver 全部端点稳定契约

### Benchmark 实测（300 任务）
| 策略 | 成本 | P95 延迟 |
|---|---|---|
| **utility（评分制）** | **11** | **536ms** |
| chain（角色链） | 35 | 943ms |
| round_robin（轮询） | 130 | 858ms |

评分制比角色链省 69% 成本、快 43% 延迟——「哪个模型最划算」的量化验证成立。

### 测试
223 → 251 passed

---

## v0.5.0 — Policy Compiler + A2A（2026-08-20）

### 新增
- **Policy Compiler**：用户自然语言意图 → 硬约束 + 权重（`compile_intent("要便宜点的")` → `{cost_max: "free", cost-first}`）。中英文关键词规则模板（速度/成本/质量/能力百分比），无 NLP 依赖，无匹配回退 balanced
- **route_with_intent**：task + candidates + 自然语言 → 完整决策流水线
- **A2A 轻量三端点**：POST /api/tasks（提交）/ GET /api/tasks/{id}（查询）/ GET /api/tasks/{id}/result（获取结果，pending/done/404 语义）
- **看板自然语言偏好**：输入「尽快出结果」→ 翻译并应用 latency-first

### 测试
198 → 223 passed

---

## v0.4.0 — Utility 评分制（2026-08-20）

### 新增
- **Utility(task, candidate, time)**：六分项评分（quality_fit / cost_penalty / latency_penalty / failure_risk / quota_pressure / deadline_pressure）
- **Normalize Features**：候选集内 min-max 相对归一化后再乘权重（权重相等 ≠ 影响相等——修复 balanced 下成本绝对值反超质量优势的问题）
- **breakdown 三层可解释**：raw / normalized / weighted + why 文本，能回答「为什么选 A 不选 B」
- **HardConstraints 硬约束**：先砍后评——cost 上限 / quota 耗尽 / cooldown / deadline 不可行 / health 红线
- **ProviderHealth**：滑动窗口健康档案（success_rate / p50 / p95 / failure_risk）
- **降级矩阵**：错误分类 → abort / cooldown_retry / retry_then_fallback / fallback，写入 `last_error.action_taken`

### 测试
160 → 198 passed

---

## v0.3.1 — 看板修复（2026-08-20）

### 新增
- **taskserver**：单文件 HTTP 看板（纯 stdlib，无 CDN/外部资源）——任务列表 / 提交 / 手动 tick / 偏好四档 / 模型画像

### 修复
- 看板 JS onclick 引号转义 bug（浏览器实测抓到，API 测试测不到）→ 改 **data-action 事件委托**，无引号嵌套问题，防 XSS
- 版本纪律：v0.3.0 tag 后新增功能 → bump 0.3.1

### 测试
160 passed 基线保持

---

## v0.3.0 — 任务系统后端（2026-08-20）

### 新增
- **错误分类**：HTTP >=400 不再一刀切进 cooldown——400/401/403（请求/认证错）不冷却；429/5xx/timeout 进冷却且带 `reason` 分类
- **Task 模型**：`task_id / task_type / priority / deadline / defer_until / status / payload` + 状态机（queued → running → done/failed，deferred 按 defer_until 转 queued，过期 → expired）
- **TaskStore**：JSON 持久化（原子写：tmp + fsync + os.replace）
- **TaskScheduler**：submit / defer / tick / 重试上限 / expired 扫描
- **Executor 抽象**：`Executor` Protocol + `MockExecutor` / `CommandExecutor`（shell=False）
- **Preferences**：quality-first / cost-first / latency-first / balanced 四档权重，JSON 可覆盖

### 测试
128 → 160 passed（含 v0.2.x 全量回归）

---

## v0.2.x（历史版本）

v0.2.0 / v0.2.1 / v0.2.2 / v0.2.4 的 release notes 见 GitHub Releases 页面（项目早期版本，已发布）。

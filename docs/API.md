# API Contract (model-scheduler taskserver v0.6.0)

本文档是 `model_scheduler.taskserver` 的稳定 HTTP API 契约，与当前实现严格一致。
taskserver 是单文件看板后端 + 内嵌 HTML 前端，仅使用标准库，零第三方依赖。

- Base URL：`http://<host>:<port>`
- 请求/响应体：除 HTML 页面外均为 `application/json; charset=utf-8`
- 错误响应形状（所有非 2xx 统一）：

```json
{
  "error": {
    "message": "human readable message",
    "type": "model_scheduler.<error_type>"
  }
}
```

- 500 响应额外携带 `detail` 字段（内部异常信息）。
- 未实现的 method 返回 `405`，错误类型 `model_scheduler.method_not_allowed`。
- 未匹配路径返回 `404`，错误类型 `model_scheduler.not_found`。
- `{task_id}` 为任务 ID 占位符。

## 页面

| 路径 | 方法 | 用途 | 说明 |
|---|---|---|---|
| `/` | GET | 看板 | 返回内嵌 HTML 看板页 |
| `/index.html` | GET | 看板 | 同上 |

## 健康检查

| 路径 | 方法 | 用途 | 成功响应 |
|---|---|---|---|
| `/api/health` | GET | 看板/运维 | `{"status":"ok","version":"0.6.0"}` |

## 任务

### 创建任务

`POST` `/api/tasks`

用途：A2A / 看板创建任务。

请求体：

```json
{
  "task_type": "text",
  "payload": {"prompt": "hello"},
  "priority": "normal",
  "deadline": null
}
```

- `task_type`：非空字符串。
- `payload`：JSON 对象。
- `priority`：`high` / `normal` / `low`，缺省 `normal`。
- `deadline`：可空；ISO 8601 字符串或 epoch 秒。

成功响应 `200`：

```json
{"task_id": "0123...", "status": "queued", "defer_until": null}
```

错误码：`400`（`model_scheduler.invalid_request` / `invalid_priority` / `invalid_deadline` / `invalid_json`）。

### 任务列表

`GET` `/api/tasks`

用途：看板。

Query 参数：

| 参数 | 类型 | 缺省 | 说明 |
|---|---|---|---|
| `status` | string | 空 | 按状态过滤（queued/deferred/running/done/failed/cancelled/expired） |
| `task_type` | string | 空 | 按类型过滤 |
| `offset` | int | 0 | 非负整数 |
| `limit` | int | 空 | 非负整数，空表示不限制 |

成功响应 `200`：

```json
{
  "tasks": [
    {
      "task_id": "0123...",
      "task_type": "text",
      "priority": "normal",
      "deadline": null,
      "defer_until": null,
      "status": "queued",
      "attempts": 0,
      "last_error": null,
      "created_at": 1700000000.0,
      "updated_at": 1700000000.0,
      "result": null,
      "cost": 0.0
    }
  ]
}
```

错误码：`400`（`model_scheduler.invalid_status` / `invalid_query`）。

### 任务详情

`GET` `/api/tasks/{task_id}`

用途：看板。

成功响应 `200`：单个任务公共字段（同列表元素，不含 `payload`）。

错误码：`404`（`model_scheduler.not_found`）。

### 任务结果

`GET` `/api/tasks/{task_id}/result`

用途：A2A / 看板。

成功响应 `200`：

```json
{
  "task_id": "0123...",
  "status": "done",
  "result": {"output": "ok"},
  "cost": 0.0,
  "error": null,
  "updated_at": 1700000000.0
}
```

未完成任务会返回 `"result": null`、`"error": null`、`"pending": true`。
已失败/取消任务返回 `"result": null` 与 `"error": {...}`。

错误码：`404`（`model_scheduler.not_found`）。

### 取消任务

`POST` `/api/tasks/{task_id}/cancel`

用途：管理。

成功响应 `200`：`{"ok": true}` 或 `{"ok": false}`。

### 删除任务

`DELETE` `/api/tasks/{task_id}`

用途：管理。

成功响应 `200`：`{"ok": true}` 或 `{"ok": false}`。

## 调度

### 手动 tick

`POST` `/api/tick`

用途：管理/调度。

成功响应 `200`：

```json
{"processed": ["0123..."], "now": 1700000000.0}
```

## 统计

`GET` `/api/stats`

用途：看板。

成功响应 `200`：

```json
{
  "total": 10,
  "by_status": {"queued": 2, "done": 8},
  "by_type": {"text": 10},
  "total_cost": 0.0
}
```

## 偏好

### 读取偏好

`GET` `/api/preferences`

用途：看板/管理。

成功响应 `200`：

```json
{"mode": "balanced", "weights": {"quality_fit": 1.0, "cost_penalty": 1.0, "latency_penalty": 1.0, "failure_risk": 1.0, "quota_pressure": 1.0, "deadline_pressure": 1.0}}
```

### 写入偏好

`PUT` `/api/preferences`

用途：管理。

请求体：

```json
{"mode": "cost-first", "weights": {"cost_penalty": 3.0}}
```

- `mode`：`quality-first` / `cost-first` / `latency-first` / `balanced`。
- `weights`：可空；只允许覆盖六项权重。

成功响应 `200`：同 `GET` `/api/preferences`。

错误码：`400`（`model_scheduler.invalid_mode` / `invalid_preferences` / `invalid_json`）。

### 自然语言偏好翻译

`POST` `/api/preferences/compile`

用途：管理。

请求体：

```json
{"text": "我要高质量但不要太贵"}
```

成功响应 `200`：

```json
{
  "mode": "balanced",
  "weights": {"quality_fit": 2.0},
  "constraints": {"cost_max": null, "min_quota_left": 0, "exclude_in_cooldown": true, "max_failure_risk": null, "deadline_slack_seconds": 0.0, "max_latency_ms": null, "min_quality_tier": null, "min_capability_pct": null, "capability_reference": null},
  "explanation": "..."
}
```

错误码：`400`（`model_scheduler.invalid_request` / `invalid_json`）。

## 错误码汇总

| 错误码 | 触发场景 | error.type 示例 |
|---|---|---|
| 400 | 非法 query/body/priority/deadline/mode/JSON | `model_scheduler.invalid_request` 等 |
| 404 | 路径不存在或任务不存在 | `model_scheduler.not_found` |
| 405 | method 不允许 | `model_scheduler.method_not_allowed` |
| 500 | 未捕获内部异常 | `model_scheduler.internal_error` |

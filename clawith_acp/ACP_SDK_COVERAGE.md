# ACP SDK 能力对照（Clawith 瘦客户端 × 仓库代码）

对照基准：本机 `agent-client-protocol`（`acp` 包）中 `**meta.AGENT_METHODS` / `meta.CLIENT_METHODS**` 与 `**interfaces.Agent` / `interfaces.Client**`。  
实现位置：`**integrations/clawith-ide-acp/server.py**`（`ClawithThinClientAgent`）；云端桥：`**backend/app/plugins/clawith_acp/router.py**`。

**图例**


| 符号  | 含义                        |
| --- | ------------------------- |
| ✅   | 已接入且主路径可用（或与云端/IDE 有实质联动） |
| ⚠️  | 有实现但语义不全、仅占位、或仅部分参数/类型    |
| ❌   | 未实现、未调用 IDE，或 noop 无产品效果  |


---

## 1. `AGENT_METHODS`（IDE → 瘦客户端 Agent）


| `meta` 键                    | JSON-RPC 方法                 | `Agent` 方法          | Clawith 状态 | 说明                                                                                  |
| --------------------------- | --------------------------- | ------------------- | ---------- | ----------------------------------------------------------------------------------- |
| `initialize`                | （初始化）                       | `initialize`        | ✅          | 返回 `agent_info`；`agent_capabilities` 含 `load_session`、`prompt_capabilities.image` 等 |
| `session_new`               | `session/new`               | `new_session`       | ✅          | 本地 `uuid4` 生成 `session_id`                                                          |
| `session_load`              | `session/load`              | `load_session`      | ⚠️         | 仅返回成功；历史由云端在 **首次 `prompt`** 时从 DB 水合                                               |
| `session_list`              | `session/list`              | `list_sessions`     | ✅          | 经 WS 调后端，过滤 `source_channel=ide_acp`                                                |
| `session_prompt`            | `session/prompt`            | `prompt`            | ⚠️         | 文本 + 图片 + 部分 `resource_link` / 嵌入资源；**音频块丢弃**；未用 `message_id` 扩展                    |
| `authenticate`              | （认证）                        | `authenticate`      | ⚠️         | 占位；实际鉴权为 WS **query token**                                                         |
| `session_set_mode`          | `session/set_mode`          | `set_session_mode`  | ⚠️         | 空响应，无 Clawith 映射                                                                    |
| `session_set_model`         | `session/set_model`         | `set_session_model` | ⚠️         | 空响应，无云端换模型                                                                          |
| `session_set_config_option` | `session/set_config_option` | `set_config_option` | ⚠️         | 空响应，无持久化配置                                                                          |
| `session_close`             | `session/close`             | `close_session`     | ⚠️         | 空响应；未清理云端内存 map 等                                                                   |
| `session_cancel`            | `session/cancel`            | `cancel`            | ✅          | 向云端 WS 发 `cancel`（需 `session_id`）；见 `MANUAL_TEST_IDE_ACP.md` TC-11                  |
| `session_fork`              | `session/fork`              | `fork_session`      | ⚠️         | 仅返回 **新随机 `session_id`**，无 DB fork 语义                                               |
| `session_resume`            | `session/resume`            | `resume_session`    | ⚠️         | 空 `ResumeSessionResponse`，无云端协同                                                     |
| —                           | —                           | `ext_method`        | ⚠️         | 返回 `{}`                                                                             |
| —                           | —                           | `ext_notification`  | ⚠️         | 无操作                                                                                 |
| —                           | —                           | `on_connect`        | ✅          | 保存 `Client` 引用                                                                      |


---

## 2. `CLIENT_METHODS`（瘦客户端 → IDE Client）


| `meta` 键                     | JSON-RPC 方法                  | `Client` 方法              | Clawith 状态 | 说明                                                                                                                                                                                                                                                               |
| ---------------------------- | ---------------------------- | ------------------------ | ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session_update`             | `session/update`             | `session_update`         | ⚠️         | 使用子类型：`AgentMessageChunk`（文本流）、`AgentThoughtChunk`（`thinking` WS）、`ToolCallStart` / `ToolCallProgress`（工具 UI）。**未用**：`UserMessageChunk`、`AgentPlanUpdate`、`AvailableCommandsUpdate`、`CurrentModeUpdate`、`ConfigOptionUpdate`、`SessionInfoUpdate`、`UsageUpdate` 等 |
| `fs_read_text_file`          | `fs/read_text_file`          | `read_text_file`         | ✅          | 云端可传 `limit`/`line`，瘦客户端已转发至 IDE                                                                                                                                                                                                                                 |
| `fs_write_text_file`         | `fs/write_text_file`         | `write_text_file`        | ✅          | `path` + `content`                                                                                                                                                                                                                                               |
| `terminal_create`            | `terminal/create`            | `create_terminal`        | ⚠️         | `ide_execute_command` 路径已传 **cwd**（会话目录）；`env` / `output_byte_limit` 仍可选未接                                                                                                                                                                                       |
| `terminal_wait_for_exit`     | `terminal/wait_for_exit`     | `wait_for_terminal_exit` | ✅          | `ide_execute_command` 路径                                                                                                                                                                                                                                         |
| `terminal_output`            | `terminal/output`            | `terminal_output`        | ✅          | 同上                                                                                                                                                                                                                                                               |
| `terminal_release`           | `terminal/release`           | `release_terminal`       | ✅          | 经云端 `ide_release_terminal`（需权限）                                                                                                                                                                                                                                  |
| `terminal_kill`              | `terminal/kill`              | `kill_terminal`          | ✅          | 经云端 `ide_kill_terminal`（需权限）                                                                                                                                                                                                                                     |
| `session_request_permission` | `session/request_permission` | `request_permission`     | ✅          | 云端 `permission_request` 时瘦客户端弹出 IDE 确认                                                                                                                                                                                                                           |
| —                            | —                            | `ext_method`             | ❌          | 瘦客户端不向 IDE 发扩展 RPC                                                                                                                                                                                                                                               |
| —                            | —                            | `ext_notification`       | ❌          | 同上                                                                                                                                                                                                                                                               |
| —                            | —                            | `on_connect`             | ✅          | 由 SDK/`run_agent` 与 IDE 侧处理                                                                                                                                                                                                                                      |


---

## 3. `session/prompt` 内容块（`PromptRequest` 与 `interfaces.Agent.prompt`）


| Schema 块类型                              | Clawith 状态 | 说明                                                        |
| --------------------------------------- | ---------- | --------------------------------------------------------- |
| `TextContentBlock`                      | ✅          | 经 `prompt_parts` 或纯 `text` 上云                             |
| `ImageContentBlock`                     | ✅          | 经 `prompt_parts` → 云端 vision 管道（受模型 `supports_vision` 约束） |
| `ResourceContentBlock`（`resource_link`） | ⚠️         | 转为文本说明上云                                                  |
| `EmbeddedResourceContentBlock`          | ⚠️         | 文本嵌入可读；**小图**可转 vision；其它二进制仅摘要                           |
| `AudioContentBlock`                     | ❌          | 瘦客户端 **省略**，不打到 WS                                        |


---

## 4. 云端插件（`clawith_acp`）补充说明

- **不在 `acp` meta 表里**：WS 消息类型 `prompt` / `prompt_parts`、`chunk`、`thinking`、`tool_call_start` / `tool_call_update`、`execute_tool` / `tool_result`、`list_sessions` 等为 **Clawith 私有云桥**，与 ACP stdio JSON-RPC **并行**。
- `**call_llm`**：已接 `supports_vision`、`on_thinking`、`on_tool_call`（含 `tool_call_id`）等，与上表 Client/云端配合。

---

## 5. 维护方式

升级 `**agent-client-protocol` / `acp`** 后请重读：

```text
<venv>/site-packages/acp/meta.py
<venv>/site-packages/acp/interfaces.py
```

并 diff 本文件的 **§1–§2** 与 `server.py`。
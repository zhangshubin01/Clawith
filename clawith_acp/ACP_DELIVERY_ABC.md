# ACP A/B/C 交付：需求评审 · 代码评审 · 自测 · 测试用例

本文档对应「工程固化 + P3 能力 + IDE 工具/schema/瘦客户端对齐」一轮交付，并与实现代码同步维护。

## 1. 需求评审（纪要）


| 编号  | 需求项                   | 结论                                                                                                                                |
| --- | --------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| R1  | IDE 工具与云端 LLM 并发      | 必须在 `call_llm` 阻塞等待 `tool_result` 时仍能读取 WebSocket，否则死锁。                                                                           |
| R2  | 取消                    | IDE `cancel` 应到达云端，并在下一轮工具循环前中止 `call_llm`。                                                                                       |
| R3  | 敏感 `ide_*`            | `ide_write_file`、`ide_execute_command`、终端创建/杀/释放等需经 IDE `request_permission`（瘦客户端转发 `permission_request`/`permission_result`）。    |
| R4  | 读文件参数                 | `ide_read_file` 暴露 `limit`/`line`，瘦客户端转发至 `read_text_file`。                                                                       |
| R5  | 终端 cwd                | `ide_execute_command` 使用的 `create_terminal` 应带会话 `cwd`（由 `new_session`/`load_session` 等记录）。                                       |
| R6  | 协议版本                  | `schemaVersion` 当前为 **3**（含 `cancelled` 与跨连接 cancel 注册表）。                                                                         |
| R7  | `execute_tool` 出站     | 与 chunk/done 一致，统一经 `schemaVersion` 信封。                                                                                           |
| R8  | 跨连接 cancel            | 任意持有合法 token 的 WS 可对 `(agent,user,session)` 发 `cancel`，与跑 prompt 的连接不必是同一条。                                                       |
| R9  | 流式中途取消                | `call_llm` 将 `cancel_event` 传入 `client.stream`，OpenAI 兼容 / Anthropic / Gemini 原生 SSE 在读行循环中 `aclose` 并 `finish_reason=cancelled`。 |
| R10 | `cancelled` vs `done` | 用户取消时发 `**type: cancelled`**，不再发 `done`；正常结束仍发 `done`。                                                                            |


## 2. 代码评审（Checklist）

- `acp_websocket`：独立 `receive_loop` 处理 `tool_result` / `permission_result` / `cancel`，业务消息进 `main_queue`。
- 断开连接时：未完成 `pending_tools` / `pending_permissions` 的 Future 被收尾，避免永久挂起。
- `call_llm(..., cancel_event=...)`：轮次边界检查 + 传入 `client.stream(cancel_event=...)`；OpenAI Responses 伪流式仅在 `complete` 前检查取消。
- `_acp_cancel_registry`：同用户同 agent 同 `session_id` 仅允许**一个**在途 prompt；并发第二个返回 error。
- `_custom_execute_tool`：先权限（若需要）再 `execute_tool`；出站 `_acp_ws_envelope`。
- 瘦客户端：`prompt` 期间设置 `_active_prompt_ws`，`cancel` 可发送；`permission_request` 必须应答。
- 环境变量 `CLAWITH_ACP_PERMISSION`：`ide`（默认，走 IDE 对话框）、`allow`、`deny`（自动化/CI）。

## 3. 详细自测（建议手顺）

1. **连通**：瘦客户端连云端，`list_sessions` 返回 200 信封 `schemaVersion: 3`。
2. **读文件**：模型调用 `ide_read_file` 带 `limit`，确认 IDE 侧收到对应参数。
3. **权限**：`CLAWITH_ACP_PERMISSION=ide`，触发 `ide_write_file`，应出现 IDE 权限 UI；选拒绝后云端得到「Permission denied」类工具结果。
4. **取消**：IDE `cancel`（或第二条 WS 对同 session 发 `cancel`）后，应收到 `**cancelled`**（非 `done`），历史里助手内容可为 `[Cancelled]`。长流式时应在较短时间内停止继续吐 chunk（视供应商连接关闭速度）。
5. **终端**：`new_session(cwd=...)` 后 `ide_execute_command`，确认子进程 cwd 正确；`ide_create_terminal` → `ide_kill_terminal` / `ide_release_terminal` 链路可执行。

## 4. ACP 功能测试用例（自动化 + 手工）

### 4.1 自动化（`backend/tests/plugins/test_clawith_acp.py`）


| 用例                                                       | 断言要点                                                                   |
| -------------------------------------------------------- | ---------------------------------------------------------------------- |
| `test_acp_ws_envelope_`*                                 | 出站含 `schemaVersion`                                                    |
| `test_custom_get_tools_appends_ide_tools_when_ws_active` | 含 `ide_create_terminal` / `ide_kill_terminal` / `ide_release_terminal` |
| `test_thin_cloud_msg_matches_router_version`             | 瘦客户端与路由 `schemaVersion` 一致                                             |
| `test_thin_cancel_sends_when_prompt_ws_active`           | `cancel` 发往云端且带 `schemaVersion`                                        |
| `test_thin_new_session_records_cwd`                      | 会话 cwd 缓存                                                              |


### 4.2 手工 / wscat（补充）


| ID  | 步骤                                                                               | 期望                                          |
| --- | -------------------------------------------------------------------------------- | ------------------------------------------- |
| H1  | WS 连接后发送 `{"schemaVersion":3,"type":"list_sessions","cwd":"/tmp"}`               | `list_sessions_result` 且 `schemaVersion: 3` |
| H2  | 发送 `prompt` 后观察云端日志与 IDE 工具调用                                                    | `execute_tool` 消息含 `schemaVersion`          |
| H3  | 第二条 WS（同 token、同 agent）对同一 `session_id` 发 `{"type":"cancel","session_id":"..."}` | 在途 prompt 结束并收到 `cancelled`（非 `done`）       |


> WebSocket 示例中的 `schemaVersion` 请使用 **3**。

## 5. 实现映射（便于审计）


| 区域                                     | 路径                                                                  |
| -------------------------------------- | ------------------------------------------------------------------- |
| 云端 WS + IDE 工具 + 权限                    | `backend/app/plugins/clawith_acp/router.py`                         |
| `call_llm` 取消 + `stream(cancel_event)` | `backend/app/api/websocket.py`、`backend/app/services/llm_client.py` |
| 瘦客户端                                   | `integrations/clawith-ide-acp/server.py`                            |


OpenViking retrieval unavailable in this turn（未调用 MCP recall；以仓库内代码与 pytest 结果为准）。
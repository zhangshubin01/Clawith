# Clawith × ACP 全能集成路线图

本文档对照 **Agent Client Protocol（`agent-client-protocol` / `acp` Python SDK）** 与 **Clawith 现有框架**，包含：**研发全流程门禁**、**十轮代码/SDK 深度对照**、**能力矩阵与分阶段路线**。实施以 JetBrains 实际 RPC 为准做裁剪。

---

## 0. 研发全流程（需求 → 功能评审 → 改代码 → 测试 → 发布）


| 阶段       | 产出 / 门禁                                                                          | Clawith / ACP 关注点                           |
| -------- | -------------------------------------------------------------------------------- | ------------------------------------------- |
| **需求**   | PRD：IDE 场景（会话恢复、多模态、权限、取消）、部署形态（直连 / 反代 / wss）、合规（密钥不落盘）                         | 与 Web Chat、OpenAI 兼容接口的边界写清，避免重复建设          |
| **功能评审** | 评审表：每项对应「ACP SDK 方法」「Clawith 已有能力」「缺口类型（适配/新建/产品）」                               | 强制过一遍本文 **§2 能力矩阵** + **§十轮分析** 相关轮次        |
| **改代码**  | 分支 + 设计笔记：瘦客户端、插件路由、`call_llm`、DB 迁移（若有）                                         | Monkey-patch 改为可测的注入；WS 消息加 `schemaVersion` |
| **测试**   | 单测 / 集成：mock `AgentSideConnection`；staging 真机 Android Studio；回归 WebSocket 主 Chat | 多 worker 下禁止依赖「导入副作用」的隐式全局状态（见第 3 轮）        |
| **发布**   | CHANGELOG、升级说明（`acp.json` 路径、env、后端插件版本）、回滚策略                                    | 文档：`README.md` + 运维 Runbook（反代 WebSocket）   |


---

## 十轮深度分析（ACP SDK 源码 vs Clawith 实现）

**对照基准**：`acp` 包内 `agent/router.py`（`build_agent_router`）、`router.py`（`_resolve_handler` / `Route.handle`）、`interfaces.py`（`Agent` / `Client` Protocol）、`meta.py`（`AGENT_METHODS` / `CLIENT_METHODS`）；Clawith 侧 `backend/app/plugins/clawith_acp/router.py`、`integrations/clawith-ide-acp/server.py`、`app/api/websocket.py::call_llm`。

### 第 1 轮：双通道传输模型


| SDK                                                                                 | Clawith                                                                                  |
| ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| IDE ↔ 瘦客户端：**stdio + JSON-RPC**，由 `AgentSideConnection` + `Connection.main_loop` 驱动 | 瘦客户端 ↔ 云端：**独立 WebSocket**，自定义 JSON（`prompt` / `chunk` / `execute_tool` / `tool_result`） |
| 协议版本：`PROTOCOL_VERSION`（`meta.py`）                                                  | WS 未显式携带 `protocolVersion`；仅靠 URL query 鉴权                                               |


**结论**：存在 **ACP（JSON-RPC）** 与 **Clawith 云桥（私有 JSON）** 两层；「全能」若指协议统一，需定义 WS 信封版本与错误码，与 `RequestError` 语义对齐。  
**改代码**：WS 首包或每条消息带 `v:1`；文档声明与 ACP 规范的关系。  
**测试**：抓包确认 IDE 侧仍为 stdio JSON-RPC，云侧可独立演进。  
**发布**：说明「云协议非 ACP 子集，为 Clawith 扩展」。

### 第 2 轮：`build_agent_router` 与未实现 Agent 方法

`build_agent_router` 为 `initialize`、`session/new`、`session/load`、`session/list`、`session/prompt`、`session/cancel` 等注册路由；`_make_func` 若 `getattr(agent, name)` 非可调用则 `func is None`，`Route.handle` 对非 optional 请求 **抛出 `RequestError.method_not_found`**。

**Clawith**：`ClawithThinClientAgent` 仅实现 `initialize`、`new_session`、`prompt`（及 `on_connect`）。  
**结论**：IDE 一旦调用 `load_session` / `cancel` 等且无 noop，**进程级失败**。  
**改代码**：在瘦客户端为高频 RPC 补 **显式 async def**（返回规范空响应或 `PromptResponse(stop_reason=...)`），或开启 `use_unstable_protocol` 并逐项实现。  
**测试**：用 JetBrains「Get ACP Logs」+ 本地 stub Client 压测。  
**发布**：列出「已支持 RPC 白名单」。

### 第 3 轮：模块导入即 Monkey-patch

`router.py` 在 **import 时** 执行 `agent_tools.get_agent_tools_for_llm = _custom_get_tools`（约第 111–112 行），全局替换。

**风险**：多 worker（uvicorn workers>1）每进程各 patch 一次尚可；**单元测试 import 顺序**会污染；**热重载**可能重复 patch（取决于 import 机制）。  
**改代码（长期）**：用 ContextVar + `call_llm` 显式传 `tool_executor`；短期在插件 `register()` 内 patch 并文档化。  
**测试**：`pytest` 独立进程；禁止依赖「未加载 clawith_acp」的测试与加载后的测试混跑无隔离。  
**发布**：运维须知「插件启用即全局影响 agent_tools」。

### 第 4 轮：`session_id` 与持久化

瘦客户端 `new_session` 使用 `uuid4().hex`（32 位十六进制）。Python `uuid.UUID(session_id)` **接受**该格式，可与 `ChatSession.id` 对齐。  
云端 `session_messages[session_id]` **仅内存**；WS 断线后历史丢失，除非 DB 已写入且下次 `load_session` 从 DB 拉取（**当前未实现**）。

**改代码**：P1 实现 `load_session` + 可选 `source_channel='ide_acp'`；断线重连用 DB 为准。  
**测试**：同 session_id 二次连接，上下文一致。  
**发布**：说明会话生命周期与 Web UI 一致条件。

### 第 5 轮：`messages` 结构与工具轮次

`call_llm` 消费 `list[dict]`（role/content/tool_calls）。主 WebSocket 历史含 `tool_call` 序列化回放（见 `websocket.py`）。  
ACP 路由仅追加简单 `user`/`assistant` 字符串，**不保留** OpenAI 式 `tool_calls` + `tool` 多轮结构。

**结论**：多轮 IDE 工具后，**历史若仅文本摘要**，模型可能丢失严格 tool 协议形状（依模型而异）。  
**改代码**：持久化与内存 history 与 Web Chat 对齐结构，或接受「单轮工具在 `call_llm` 内闭环」的约束并在文档写明。  
**测试**：连续 3 轮 `ide_read_file` + 回复，重进会话是否可复现。  
**发布**：已知限制写入 README。

### 第 6 轮：`InitializeResponse` 与能力协商

`InitializeResponse`（`schema.py`）含 `agent_capabilities`、`agent_info`、`auth_methods` 等。原先瘦客户端仅回传 `protocol_version`。

**已改进**：瘦客户端现返回 `agent_info`（name/version/title），便于 IDE 排障；仍未声明 `agent_capabilities`（多模态、MCP 等）。

**改代码**：按产品声明 `AgentCapabilities`（与 `call_llm` vision 开关一致）。  
**测试**：Registry / 日志中可见 Agent 名称版本。  
**发布**：无。

### 第 7 轮：`prompt` 内容块类型

ACP `PromptRequest` 支持 `Text` / `Image` / `Audio` / `Resource` / `EmbeddedResource` 等块。瘦客户端仅 `getattr(block, "text")` 拼字符串，**非文本块静默丢弃**。

**改代码**：分支序列化为 `[image_data:...]` 或 OpenAI multipart（与 `call_llm` vision 路径对齐）；音频走 URL 或「不支持」提示。  
**测试**：IDE 粘贴图片进 Chat。  
**发布**：标明支持的内容类型。

### 第 8 轮：`ide_read_file` 与 `ReadTextFileRequest` 参数

SDK `Client.read_text_file` 支持 `limit`、`line`。云端 `IDE_TOOLS` 仅 `path`；瘦客户端未向 IDE 传可选参数。

**改代码**：扩展 WS `execute_tool` 的 `args` 与工具 schema；瘦客户端转发。  
**测试**：大文件截断与行号读取。  
**发布**：工具契约版本 bump。

### 第 9 轮：终端 `cwd` / `env` / `kill` / `release`

`CreateTerminalRequest` 含 `cwd`、`env`、`output_byte_limit`。当前仅传 `command`+`args`，**未转发工作目录**；未使用 `release_terminal` / `kill_terminal`。

**改代码**：从云端工具参数或会话 meta 注入 `cwd`；长运行命令路径支持 kill。  
**测试**：在子目录执行 `gradle`、中断任务。  
**发布**：Android 工程多模块场景说明。

### 第 10 轮：取消、流控、安全发布

- `**session/cancel`**：需服务端 `call_llm` 协作 `asyncio.CancelledError` / 事件位；瘦客户端停止消费 WS。  
- **流控**：`websockets` / 反代 `max_size`；大 `tool_result` 需分块或落盘引用。  
- **安全**：`token` 在 query string 可能进访问日志；长期改 Header 子协议或 WSS + 短效票据。

**改代码**：分阶段见 P3；日志脱敏。  
**测试**：取消后无额外 chunk；1MB+ 文件读写。  
**发布**：安全审计清单。

---

## 文档同步的代码修正（维护者在 diff 中核对）

以下与上述分析直接对应，已落库（后续发布请写入 CHANGELOG）：

1. **持久化任务引用**：`router.py` 使用 `_acp_background_tasks` 持有 `asyncio.create_task(_persist_chat_turn(...))`，避免未引用 Task 被 GC 提前取消（对齐 `gateway.py` 模式）。
2. **初始化与签名**：瘦客户端 `InitializeResponse` 增加 `agent_info`；`new_session` 的 `mcp_servers` 与 Protocol 一致允许 `None`。

---

## 1. 目标与边界


| 目标           | 说明                                                                                                  |
| ------------ | --------------------------------------------------------------------------------------------------- |
| **全能集成**     | 在 IDE 侧完整实现 `Agent` / `Client` 协议中业务需要的方法；云端与 Clawith 的会话、模型、工具、多模态、审计对齐。                           |
| **边界**       | ACP **extension**（`ext_method` / `ext_notification`）依赖 IDE 私有扩展；仅在与 JetBrains 约定一致时实现。              |
| **unstable** | `fork` / `resume` / `set_model` 等在 `acp` 中标记为 unstable，需 `use_unstable_protocol=True` 且确认 IDE 版本行为。 |


---

## 2. 能力矩阵（Clawith 是否支持）

图例：**已支持** = 框架或现有通道已有；**部分** = 需接 ACP 或改协议；**需建设** = 新表/新 API/新产品决策。

### 2.1 Agent 侧（IDE 调瘦客户端）


| ACP 方法                            | 当前瘦客户端     | Clawith 后端 / 框架                                                         | 集成要点                                                              |
| --------------------------------- | ---------- | ----------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `initialize`                      | 有          | 无独立逻辑（握手在 stdio）                                                        | 可扩展返回 `AgentCapabilities`（声明多模态、MCP 等）                            |
| `new_session`                     | 有（本地 UUID） | ACP WS 仅内存 `session_messages`                                           | 与 DB `ChatSession` 对齐时需明确 **ACP session_id ↔ conversation_id**    |
| `prompt`                          | 有（文本）      | `call_llm` + 工具循环 **已支持**                                               | 多模态需把 ACP content blocks 映射为 `call_llm` 的 `messages` / vision 格式  |
| `load_session`                    | **无**      | **部分**：`chat_sessions` + `chat_messages` 可查，但 ACP 路由 **未按 session 拉历史** | 需在 WS 或新消息类型中按 `session_id` 加载历史并注入 `call_llm`                    |
| `list_sessions`                   | **无**      | **需建设**：列表接口已有 HTTP，ACP 需 **按 cwd/user 过滤** 的语义产品化                      | 定义「IDE 会话列表」是否等于 Web 会话或独立 `source_channel=ide_acp`               |
| `close_session`                   | **无**      | 可复用会话关闭语义                                                               | 清理内存 map、可选更新 DB `last_message_at`                                |
| `cancel`                          | **无**      | **需建设**：`call_llm` **无统一 cancel token**                                 | 为单次 prompt 引入 `asyncio.Event` / task cancel，并通知 LLM 客户端（视供应商是否支持） |
| `authenticate`                    | **无**      | **部分**：已有 `cw-` / JWT；ACP 另有 **Env/Terminal auth** 流程                   | 若 IDE 要求 OAuth/设备码，需新增 Clawith OAuth 或代理说明「仅用 query token」        |
| `set_session_mode`                | **无**      | **需产品**：Clawith 智能体无一等「模式」与 ACP mode 映射                                 | 映射到 prompt 后缀、或不同 `agent_id`、或忽略并返回 null                          |
| `set_session_model`               | **无**      | **部分**：智能体绑定 `primary_model_id`；用户级切换需权限模型                              | 可能仅允许在「用户有多个可用模型」时切换子模型                                           |
| `set_config_option`               | **无**      | **需建设**                                                                 | 映射到智能体参数 / 用户偏好表，或 noop                                           |
| `fork_session` / `resume_session` | **无**      | **需建设** + unstable                                                      | 会话分支、快照；Clawith 无现成 fork 语义                                       |
| `ext_method` / `ext_notification` | **无**      | 视扩展而定                                                                   | 与 JetBrains 文档对齐后再做                                               |


### 2.2 Client 侧（瘦客户端调 IDE）


| Client 方法                                                        | 当前使用     | Clawith 关联          | 集成要点                                             |
| ---------------------------------------------------------------- | -------- | ------------------- | ------------------------------------------------ |
| `session_update`                                                 | 文本 chunk | 审计 / Web UI 依赖云端持久化 | 已通；可补 `AgentThoughtChunk`、工具进度等映射到前端展示           |
| `read_text_file` / `write_text_file`                             | 有        | 经云端 `ide`_* 工具      | 可扩展 limit/line；大文件需与 WS 消息大小上限协调                 |
| `create_terminal` / `wait_for_terminal_exit` / `terminal_output` | 有        | 同上                  | 可补 `release_terminal` / `kill_terminal` 以匹配长运行进程 |
| `request_permission`                                             | **无**    | **需产品**：敏感操作二次确认    | 云端工具执行前阻塞等待 IDE 回调结果                             |
| `ext_`*                                                          | **无**    | 同上                  | 按需                                               |


### 2.3 Clawith `call_llm` 与多模态


| 能力                                             | 框架支持情况                    | 对接 ACP                                         |
| ---------------------------------------------- | ------------------------- | ---------------------------------------------- |
| 文本 + 工具循环                                      | **已支持**                   | 已用                                             |
| Vision（`supports_vision` + `[image_data:...]`） | **已支持**（见 `websocket.py`） | 将 ACP `ImageContentBlock` 转为同一标记或 OpenAI parts |
| Audio / Resource 块                             | **部分 / 视模型**              | 需查 `llm_utils` 与各 provider；可能降级为「不支持」提示        |
| 流式 thinking                                    | **已支持**（`on_thinking`）    | 映射到 `update_agent_thought`（ACP helper）         |
| 取消进行中推理                                        | **需建设**                   | 见上表 `cancel`                                   |


### 2.4 传输与运维


| 项                | Clawith 支持                            | 说明                                 |
| ---------------- | ------------------------------------- | ---------------------------------- |
| WebSocket ACP 路由 | **已支持** `/api/plugins/clawith-acp/ws` | 可扩展消息类型（如 `cancel`、`load_history`） |
| 鉴权               | **已支持** `verify_api_key_or_token`     | 可补充文档化 scope（仅 IDE）                |
| 反向代理 WebSocket   | 部署相关                                  | HTTPS 需 `wss://`；路径与 Upgrade 放行    |
| 插件加载             | **已支持** `clawith_acp` 插件              | 与版本发布绑定                            |


---

## 3. 分阶段路线图（建议）

### 阶段 P0 — 稳定性与契约（1–2 周）

- **目标**：不追求「全能」，保证主路径可靠、可观测。
- **内容**：
  - 为未实现的 Agent 方法提供 **显式 noop / 规范错误**（避免 IDE 偶发 `method_not_found` 崩溃），按 JetBrains 实测调用顺序调整优先级。
  - ACP WS 协议版本化与日志（session_id、user_id、耗时）。
  - 文档：哪些 IDE 版本会调 `cancel` / `load_session`。
- **Clawith**：框架已够；主要是 **插件 + 瘦客户端** 代码。

### 阶段 P1 — 会话与历史对齐（2–4 周）

- **目标**：`load_session` / 可选 `list_sessions` 与 Clawith DB 一致；Web UI 可看到同一会话延续。
- **内容**：
  - `new_session` 可选 **复用** 已存在的 `ChatSession.id`（或由云端分配 UUID 并返回给 IDE）。
  - `load_session`：服务端按 `session_id` 从 `chat_messages` 拉历史，填入 `call_llm` 的 `messages`。
  - `list_sessions`：调用现有会话列表逻辑，过滤 `source_channel` / `user_id` / `agent_id`。
- **Clawith**：**已支持** 数据模型与大部分查询；ACP 路由需 **新逻辑**，非新概念。

### 阶段 P2 — 多模态与富 UI（3–6 周）

- **目标**：图片 /（可选）文件引用进模型；IDE 内工具进度、思考过程更可读。
- **内容**：
  - 瘦客户端：解析 ACP prompt 中的 `ImageContentBlock` / `ResourceContentBlock`，序列化为 `call_llm` 可消费格式。
  - 云端：`call_llm(..., supports_vision=True)` 与模型能力联动（读 `LLMModel` 或 agent 配置）。
  - `session_update`：使用 `update_agent_thought`、`start_tool_call` / `update_tool_call` 等（与 `on_tool_call` / `on_thinking` 对齐）。
- **Clawith**：框架 **已具备** vision/thinking 管道；ACP 层做 **适配与开关**。

### 阶段 P3 — 权限、取消、终端生命周期（4–8 周）

- **目标**：企业场景下的可控与可中断。
- **内容**：
  - `request_permission`：云端在执行 `ide_write_file` / `ide_execute_command` 前发 RPC 到瘦客户端，等待 IDE 用户确认。
  - `cancel`：单次 `call_llm` 任务可取消；瘦客户端停止读 WS 并通知服务端丢弃后续 chunk。
  - `kill_terminal` / `release_terminal`：与 `ide_execute_command` 长任务配合。
- **Clawith**：**需建设** cancel 与权限流；工具层与 WS 协议需扩展。

### 阶段 P4 — 高级会话与 unstable（按需）

- `fork_session` / `resume_session` / `set_session_model` 等：依赖产品定义与 **unstable** 开关。
- **Clawith**：会话分支、多模型动态切换涉及 **权限与计费**；建议单独立项。

---

## 4. 风险与依赖

1. **JetBrains 行为差异**：Android Studio / IDEA 版本不同，调用的 ACP 子集不同；计划应以 **实测 + ACP 日志** 校准。
2. **WSL**：官方限制 ACP；Windows 计划应聚焦 **原生 Windows**。
3. **Monkey-patch 全局性**：`router.py` 对 `agent_tools` 的全局 patch 在多 worker / 热重载下需谨慎；长期可改为 **显式 context / 依赖注入**。
4. **WS 消息体大小**：大文件 read/write 可能触达网关或 `max_size`；需分块或走引用 ID。
5. **LLM 供应商**：cancel / 多模态 / audio 能力 **因供应商而异**，需在 Clawith 层做能力位图。

---

## 5. 验收建议（每阶段）


| 阶段  | 最小验收                                               |
| --- | -------------------------------------------------- |
| P0  | 主流程对话 + 三 `ide`_* 工具；IDE 切换会话、重连不白屏；关键错误有用户可读文案。   |
| P1  | 同一 `session_id` 在 Web 与 IDE 续聊上下文一致；新开会话策略符合产品文档。  |
| P2  | 带截图/粘贴图的 prompt 在支持 vision 的模型上可答；思考/工具状态在 IDE 可见。 |
| P3  | 危险写操作可配置为必须确认；用户点取消后不再追加模型输出。                      |


---

## 6. 小结

- **「全能」≠ 一次做完**：按 P0→P1→P2→P3 推进，且每一列都对照 **Clawith 是否已有管道**（`call_llm`、DB、权限、多模态）。
- **框架短板主要集中在**：会话与 ACP 的 **强一致映射**、**取消**、**权限 RPC**、**fork/resume 类产品语义**；其余多为 **瘦客户端 + ACP 路由的适配工作**。

与轻量说明一并阅读：`README.md`。
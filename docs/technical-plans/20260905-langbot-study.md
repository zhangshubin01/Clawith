# LangBot（langbot-app/LangBot）整库源码研究报告

日期：2026-09-05
状态：**完成**（分析基于本地仓库 `/Users/shubinzhang/Documents/UGit/LangBot` HEAD `ec63978`，`--depth 1` 浅克隆；Clawith 对照基于 `/Users/shubinzhang/Documents/agent/Clawith` 工作树）
定位：参考资料研究，非实现方案。对照 Clawith 的多 IM 通道（飞书 WS）、run_compactor 上下文压缩、token_tracker 计量、bwrap 沙箱租约与飞书审批流。

## 0. 项目概览

- **是什么**：LangBot（`langbot-app/LangBot`，Python，~30k★）——**开源的 LLM/Agent 驱动的 IM 机器人平台**。README 定位一句话：*"an open-source platform for building production IM bots backed by LLMs, agents, RAG, plugins, MCP tools, and a web management panel"*（`ARCHITECTURE.md`）。它**不绑定单一大模型**，而是把「多 IM 平台收消息 → pipeline engine 编排（LLM/工具/插件/知识库）→ 回消息」这条链路做成一个可管理、可插拔、可多租户的后端。
- **单进程架构**：一个 LangBot 进程拥有全部运行时组件——`main.py` 只 49 字节，转 `src/langbot/pkg/__main__.py` 的 `main()`，后者解析 `--standalone-runtime/--standalone-box/--debug/--migrate/--cloud` 等参数后建 event loop 调 `main_entry`。启动是**显式 stage 编排**：`core/boot.py` 的 `stage_order=[LoadConfigStage, GenKeysStage, SetupLoggerStage, BuildAppStage, ShowNotesStage]`，`make_app` 失败时 `ap.shutdown` 清理，并处理 SIGINT/SIGTERM。核心是 `core/app.py` 的 **`Application` 服务定位器**（service locator）——平台管理、pipeline controller、HTTP/MCP 控制器、插件 runtime、telemetry 心跳、资源维护 loop 全部挂在它上面，`run()` 启动、`shutdown()` 按序释放。
- **核心结论**：LangBot 最有借鉴价值的**不是 LLM 编排本身（那是各家都有的 pipeline），而是它在「多租户 IM 机器人」这个和 Clawith 同构的问题上，把三件事打磨成了生产级契约**：① **placement_generation 单调代际**——贯穿 platform/pipeline/plugin/box/rag 全链路，任何陈旧执行上下文都会被 `assert_execution_active` 关掉；② **飞书流式卡片**——用 `streaming_mode + update_multi` 卡片绕过普通消息编辑限流，实现「打字机式」流式回显；③ **插件沙箱的「期望态 reconcile + 短生命周期 admission grant」**——插件进程/沙箱不是「开起来就不管」，而是持续对账期望态、用 300s 级别的短租约控制资源授权。**这三样都直接对应 Clawith 现有的飞书通道、run 状态仲裁、沙箱租约**。
- **对标关系（三重同构 + 一处关键差异）**：LangBot 与 Clawith 三重同构——① 多 IM 通道（均含 **Lark/飞书**）；② 插件系统（Plugin Runtime + Box Runtime 沙箱 vs Clawith bwrap 沙箱）；③ `/mcp` 把服务层子集暴露成 MCP 工具。**关键差异在形态**：LangBot 是**单进程内多 workspace 分片**（一个 `Application` 服务定位器 + 内存态按 `workspace_uuid` 分片），Clawith 是**多租户服务端**（FastAPI/LangGraph/PG，`agent_runs` 台账 + checkpoint）。**但必须如实指出**：LangBot 并非纯单租户——`docs/multi-tenant/` 下 9 份文档（`workspace-multi-user-architecture.md` 49KB、`pending-architecture-decisions.md`、`implementation-decisions.md`、`verification-report.md`、`cloud-runtime-soak-gate.md` 等）证明它有 **OSS 单 workspace + Cloud 多租户** 双形态，`placement_generation` + tenant RLS + entitlement 一整套已经落地。因此「单进程 vs 多租户」的差异要精细化，不能简单二元对立。

---

## 1. 启动链路与单进程服务定位器

- **入口**：`main.py` → `src/langbot/pkg/__main__.py` `main()`（argparse 解析 `--standalone-runtime/--standalone-box/--debug/--migrate/--cloud`）→ `main_entry`。
- **stage 编排启动**：`core/boot.py` 的 `stage_order` 把启动拆成 `LoadConfigStage → GenKeysStage → SetupLoggerStage → BuildAppStage → ShowNotesStage` 五个显式 stage，`make_app` 失败时 `ap.shutdown` 兜底清理，SIGINT/SIGTERM 有专门处理——**启动/关闭是一条可控、可回滚的显式生命周期**，而非散落的 import 副作用。
- **服务定位器**：`core/app.py` `Application` 持有 `platform_mgr`（平台/机器人管理）、`ctrl`（HTTP 控制）、`pipeline controller`、插件 runtime、telemetry 心跳、资源维护 loop；`run()` 全部拉起，`shutdown()` 按序释放。

### 1.1 对 Clawith

- Clawith 的启动/生命周期分散在 FastAPI lifespan（`backend/app/main.py`）+ `RuntimeCommandDaemon` + 各后台 task，**没有 LangBot 这种显式 `stage_order` 的启动编排**。Clawith 的「部署杀 run → 重放分叉」教训里，一部分根源正是**启动/关闭顺序不显式**（谁先停、谁先恢复不可控）。LangBot 的 stage 编排 + 失败时 `ap.shutdown` 清理，是一条可借鉴的「启动失败也要干净收尾」范式。
- **对照结论**：可迁移「显式启动 stage + 失败即清理」的生命周期骨架到 Clawith 的 runtime daemon 侧，降低部署/重启时的半启动态。

---

## 2. 消息平台 adapter 抽象 + Lark/飞书适配器（重点）

LangBot 的 adapter 抽象在 `src/langbot/pkg/platform/`，`abstract_platform_adapter.py` 定义 `AbstractMessageConverter`（消息转换）、`AbstractEventConverter`（事件转换）、`AbstractMessagePlatformAdapter`（平台适配器）三层；`sources/` 下是各平台实现（discord/telegram/slack/wechat/qq/wecom/**lark**/dingtalk/kook/line/satori/matrix）。

### 2.1 Lark 适配器（`platform/sources/lark.py`，3284 行）

- **防阻塞 WS 客户端**：`NonBlockingLarkWSClient`（`:315`）把 lark-oapi SDK 的**同步 `requests.post` 挪到 `asyncio.to_thread`**，避免阻塞 event loop——这是把官方同步 SDK 接到 asyncio 栈时的关键手法。
- **双向转换**：`LarkMessageConverter`（`:368`）与 `LarkEventConverter`（`:830`）做 SDK 类型 ↔ `MessageChain`/`Event` 的 `target2yiri/yiri2target` 双向转换；媒体文件有 `_MAX_LARK_MEDIA_BYTES = 10MB` 硬上限（`:37`），下载时按 `content_length` 预检 + 读 `+1` 字节探测超限（`:73-79`）。
- **事件分发与有界调度**：`LarkAdapter`（`:1023`，pydantic 模型）通过 `EventDispatcherHandler` 注册 `p2_im_message_receive_v1`（私聊消息）+ `p2_card_action_trigger`（卡片按钮回调）；AES 解密（encrypt-key）；ISV 模式 `tenant_access_token` 用 TTL 缓存、上限 `_MAX_TENANT_ACCESS_TOKENS=1024`（`:1071`）。入站事件用 `_MAX_INBOUND_EVENTS=100` 有界（`:1070`），`_schedule_threadsafe_event`（`:1353`）用 `run_coroutine_threadsafe` 把 SDK 回调线程的事件投递回 event loop——**入站事件有界、不无限制排队**。
- **卡片流式（核心可迁移点）**：`create_card_id`（`:1699`）创建 `streaming_mode:True`、`print_strategy:'fast'`、`print_frequency_ms:70`、`update_multi:True` 的卡片（`config.streaming_config`），`create_message_card`（`:1901`）创建回复卡片。**流式输出走「卡片更新」，绕过普通消息编辑的限流**，实现打字机式回显。`stream_card_content`/`set_card_streaming_mode` 是配套的卡片更新原语。
- **反馈按钮 HITL**：卡片带「有帮助/无帮助」按钮 → 触发 `FeedbackEvent`（`:1235-1279`）；并支持 Dify 表单 action 的 HITL（form_token/workflow_run_id/session_key 透传）。
- **线程感知会话隔离**：`get_launcher_id`（`:1466`）对群聊 thread 消息返回 `{group_id}_{thread_id}`，保证**同一话题线程内会话上下文稳定**——飞书群内不同 thread 互不串话。
- **生命周期**：`run_async`（`:3220`）webhook 关闭时转 WS 长连（`_auto_reconnect` 重连）；`kill`（`:3244`）断开连接 + 取消所有入站任务 + 清空 token 缓存。

### 2.2 平台管理（`platform/botmgr.py`）

- **`RuntimeBot`**（`:35`）包裹 adapter + `ExecutionContext`（`instance_uuid/workspace_uuid/placement_generation/bot_uuid/pipeline_uuid/query_uuid`）。
- **`assert_execution_active`**（`:91`）：对**陈旧 placement**（代际不符）直接失败关闭——这是全链路「陈旧执行体自灭」的闸门。
- **`resolve_pipeline_uuid`**（`:122`）：按路由规则（`launcher_type/launcher_id/message_content/message_has_element`，operator 支持 `eq/neq/contains/not_contains/starts_with/regex`，命中 `__discard__` 则丢弃）把入站消息路由到具体 pipeline。
- **`tenant_scoped_listener`**（`:267`）：把 adapter 回调**绑定到 workspace**，多租户下事件不串。
- **`_observe_execution_context`**（`:646`）+ **单调代际防回滚**（`:655-670`）：`previous_generation is not None and context.placement_generation < previous_generation` 直接拒绝——**代际只增不减，绝不允许回滚到旧 placement**。placement 前进时关闭旧 adapter。

### 2.3 对 Clawith

- Clawith 飞书侧是 `FeishuWSManager`（`backend/app/services/feishu_ws.py:98-467`）：`auto_reconnect=True` 交 SDK 内部重连，但**首次握手失败 SDK 不重试**，故自建指数退避 `_initial_retry_delay=10 → max 300`（`:331-352`），`_no_proxy_ctx`（`:29-74`）bypass macOS 系统代理，30s health-watch 仅记日志不重连（`:360-404`）。
- **对照结论**：
  - **LangBot 的 `NonBlockingLarkWSClient`（同步 SDK 调 `asyncio.to_thread`）** 与 Clawith 直接复用 lark-oapi 的方式同源，但 Clawith 若遇到 SDK 同步调用阻塞 event loop 的问题，这个「to_thread 隔离」是现成解法。
  - **飞书流式卡片**：Clawith 已有 `stream_card_content`/`set_card_streaming_mode`（`backend/app/services/feishu_service.py:1057/1140`），LangBot 的 `streaming_mode + update_multi` 参数组合（`lark.py:1699-1725`）可直接对齐——两者本质是同一套飞书卡片流式 API，LangBot 的 `print_strategy:'fast' + print_frequency_ms:70` 参数是 Clawith 可参考的调优起点。
  - **线程感知会话隔离 `{group_id}_{thread_id}`**（`lark.py:1466`）是 Clawith 飞书群聊目前最值得补的一点：Clawith 的会话粒度若只到「群」级别，飞书群内多个 thread 会互相污染上下文，LangBot 的 topic-scoped launcher_id 是直接可抄的隔离键设计。
  - **有界入站调度 `_MAX_INBOUND_EVENTS=100` + `run_coroutine_threadsafe`**：Clawith 的飞书 WS 事件消费若无界，高流量下会内存堆积——可借鉴「入站队列有界 + 超限丢弃/背压」。

---

## 3. pipeline engine（入站 → LLM/工具/插件 → 回复）

- **`Controller` 单 consumer 循环**（`pipeline/controller.py:14`）：全局 `semaphore = asyncio.Semaphore(concurrency['pipeline'])`（`:24`）＋ 每会话 `session._semaphore` **二级并发**——先限全局、再限单会话（`:125-170` 的 acquire 顺序可见）。
- **`QueryPool` workspace-scoped 队列**（`pipeline/pool.py:114`）：`max_queries=1000`、`max_queries_per_workspace=100`（`:129-130`），**过载丢弃最旧 query**（`_drop_selected_query`，`controller.py:99`），`cached_queries` 键为 `(workspace_uuid, query_uuid)`——**全局 + 单 workspace 双层配额，过载降级而不是崩溃**。
- **stage 注册与流式契约**（`pipeline/stage.py`）：`preregistered_stages` 装饰器（`:11/:16`）注册 stage 类；`process()`（`:35`）返回 `StageProcessResult` **或 `AsyncGenerator`**——**返回生成器 = 流式 stage**，这是「同一条 pipeline 既能流式也能非流式」的契约点。
- **`RuntimePipeline` 物化**（`pipeline/pipelinemgr.py:67`）：把 DB 里的 pipeline config 物化成 stage 链；`_execute_from_stage`（`:286`）责任链 + 生成器分叉；**每两个 stage 之间 `_assert_execution_active`（`:152`）重验 placement**——执行中途 placement 变了，pipeline 立即停。
- **`ChatMessageHandler`**（`pipeline/process/handlers/chat.py:29`）：emit `PersonNormalMessageReceived`/`GroupNormalMessageReceived` 插件事件（`:59-61`）；`event_ctx.is_prevented_default()` 插件拦截（`:80`）、`user_message_alter` 插件改写用户消息（`:99-107`）；runner 从 `preregistered_runners` 选（`:117`）；流式/非流式；`trim_conversation_messages` 按 max-round 裁剪（`:190`）；response 上限 `max_generated_chars`（默认 1MB，`:46`）+ `max_stream_chunks`（`:145`）。
- **`MessageAggregator` 消息去抖聚合**（`pipeline/aggregator.py`）：`MAX_BUFFER_MESSAGES=10`（`:23`）、默认 delay 1.5s、workspace-scoped（`:64`）——**把用户连发的多条消息聚合成一批再进 pipeline**，避免 LLM 被碎片消息打爆。

### 3.1 对 Clawith

- Clawith 的「责任链」是 LangGraph 图（`backend/app/services/agent_runtime/graph.py`），不是 LangBot 的手写 `_execute_from_stage`；但**两者都面临「执行中途被部署/取消打断」**，LangBot 的解法是**每个 stage 之间重验 placement 代际**，Clawith 的等价物是 `run_is_terminal()` + `attempt_count`/时间戳。
- **对照结论**：
  - **`QueryPool` 双层配额 + 过载丢弃最旧**（`pool.py:129-130`、`controller.py:99`）是 Clawith **run 排队/背压**可借鉴的样板：Clawith 的 `claim_next_command` 抢命令 + `scheduling_lane` 已有全局 FIFO，但**缺「单租户/单 workspace 配额」这一层**——一个租户狂发消息可能占满全局 lane，LangBot 的 `max_queries_per_workspace` 是现成答案。
  - **`MessageAggregator` 去抖聚合**：Clawith 群聊/多端连发场景没有「消息去抖聚合」这一层，连发碎片消息会逐条触发 run；LangBot 的 1.5s 聚合 + 10 条 buffer 可直接借鉴到 Clawith 的 chat_intake 侧。
  - **「返回 AsyncGenerator = 流式 stage」的契约**：Clawith 的流式靠 LangGraph `astream` + `answer_stream`/`chat_stream`，两者方向一致，LangBot 的「同一 stage 双形态」是更轻量的流式抽象，值得在 Clawith 的工具/插件执行链里参考。

---

## 4. 插件系统（Plugin Runtime + 插件安装/工作策略）

- **运行时连接器**：`PluginRuntimeConnector(ManagedRuntimeConnector)`（`plugin/connector.py:154`）通过 stdio / WebSocket 连 Plugin Runtime 子进程；`ManagedRuntimeConnector`（`utils/managed_runtime.py:15`）管子进程生命周期（`_start_runtime_subprocess`/`_wait_until_ready`/`_close_managed_subprocess`）。
- **双 profile**：`runtime_profile` 为 `'oss_dev'`（本地 stdio）或 `'shared'`（云端共享 Runtime，`:184-185`，按 `deployment.mode == 'cloud'` 判定）。
- **控制 token 鉴权**：`secrets.token_urlsafe(48)` 生成 48 字符共享密钥（`:265`），`shared` 模式要求环境变量注入强共享密钥（`:267-280`）。
- **工作策略硬限制**：`plugin.worker` 默认 `max_cpus=1.0/max_memory_mb=512/max_pids=128/max_open_files=256/max_file_size_mb=512/require_hard_limits=False`（`core/stages/load_config.py:43-59`），由 `_load_worker_policy`（`connector.py:235-260`）装载成 `PluginWorkerPolicy`（定义在外部 `langbot-plugin` SDK，`connector.py:49-54` 导入）；**Cloud 模式强制 `require_hard_limits=true`**（`cloud/bootstrap.py:143-144`）——OSS 可软限，Cloud 必硬限。
- **心跳 + 指数退避重连**：`heartbeat_loop`（`:834`）20s 心跳、3 次失败触发重连（`:845`）；`schedule_reconnect`/`_reconnect_loop`（`:1017/:1024`）指数退避 1s→60s。
- **期望态 reconcile**：`InstallationBinding`（instance_uuid/workspace_uuid/placement_generation/installation_uuid/runtime_revision/artifact_digest）与 `PluginInstallationDesiredState`（SDK 定义）；`reconcile_projected_workspaces`（`:714`）在 PG RLS 下重放控制面投影出的 workspace 集，逐 workspace 对账插件安装态；插件包（lbpkg）存 BinaryStorage tenant blob + sha256 校验（`_store_artifact_package` `:332`、`_artifact_unique_key` `:319`）。

### 4.1 对 Clawith

- Clawith 没有「插件运行时子进程 + 期望态 reconcile」这一整套，Clawith 的「外部执行体」是 **run-scoped bwrap 沙箱**（`backend/app/services/sandbox/`）+ `RuntimeCommandDaemon`。但**「desired-state reconcile」这条范式对 Clawith 的工具/沙箱租约 reconcile 有直接映射**。
- **对照结论**：
  - **期望态对账**（`reconcile_projected_workspaces` + `InstallationBinding`）对应 Clawith 的「tool lease reconcile / workspace 物化对账」——LangBot 用「期望态 + 代际 + digest」三件套对账，Clawith 目前靠 `TempWorkspaceManifestEntry` + `_materialized_run_ids` 去重，缺「期望态逐项对账」的统一抽象。
  - **心跳 20s + 3 失败阈值 + 指数退避重连**（`connector.py:834-845/1017`）与 Clawith 的 `_heartbeat`（`command_worker.py:520-561`，`AGENT_RUNTIME_COMMAND_CLAIM_RENEW_SECONDS=20`）同构，LangBot 的「失败阈值触发重连 + 指数退避上限 60s」是 Clawith 心跳侧可对齐的参数形态。

---

## 5. Box Runtime 沙箱（admission + policy）

- **`BoxService` 门面**（`box/service.py:102`）：cloud 模式走 **nsjail 后端 + admission + entitlement**（`:300-303` 校验 `backend_info.name == 'nsjail'`，否则 `BoxValidationError`）。
- **共享文件系统挑战**：`_challenge_cloud_shared_workspace`（`:305`）用**高熵 no-follow marker**验证 Core 与 Box Runtime 之间共享文件系统真实可达——防止「以为共享了其实没共享」的静默错误。
- **`_managed_policy_payload`（`:482`）拒租户伪造字段**：强制用宿主硬策略覆盖 `plan/subscription/managed_sandbox/entitlement/session_id/network/extra_mounts/mount_path/host_path` 等租户可控字段——**租户传什么都不能改变宿主的安全策略**。
- **三层策略**（`box/policy.py`）：`SandboxPolicy`（`:39`，where——沙箱边界）、`ToolPolicy`（`:57`，which——允许哪些工具）、`ElevatedPolicy`（`:85`，exec 提权）。
- **`SandboxAdmissionController`**（`box/admission.py:32`）：Cloud entitlement → **短生命周期 Box Runtime grant**；`_revoke_locked`（`:81`）单调 revocation revision；per-workspace 锁（`_workspace_lock` `:59`）；`_grant_expiry`（`:119`）`ttl = min(policy.max_grant_ttl_sec, _MAX_GRANT_TTL_SEC)`（默认 300s）；`require`（`:127`）强制 nsjail + global session + 1 session + 0 managed process。
- **attachment 进出箱**：inbox/outbox host-fs 直写优先，else base64-through-exec 兜底；`_purge_attachment_dirs`（`:831`）启动时清理（query_id 重启归零）。

### 5.1 对 Clawith

- Clawith 的沙箱是 **bwrap**（`backend/app/services/sandbox/`）：`security.py` 的 `check_code_safety`（`:62`）是**黑名单模式匹配，注释明说「不是安全边界」**（`:3-7`），真正的隔离边界是 bubblewrap/容器本身；`execution_lease.py` 的 `SandboxExecutionLease`（`:32`）用 **Redis Lua 脚本 `_RENEW_SCRIPT`/`_RELEASE_SCRIPT`（`:17-28`）做 value 匹配续租/释放**，`start_heartbeat`（`:56`）按 `ttl//3` 心跳，`acquire`（`:98`）`nx=True + px=ttl*1000`，默认 ttl 60s（`:102`）。
- **对照结论**：
  - **LangBot 的 `_managed_policy_payload` 拒租户伪造字段**（`box/service.py:482`）与 Clawith 的沙箱策略设计**同一目的**：租户侧传入的 `mount_path/host_path/network` 等字段绝不能覆盖宿主硬策略。Clawith 的 `workspace_policy.py`（147 行，`PublishClass` + `redact_git_secrets`）已有类似「宿主裁决」思路，但**沙箱执行参数的「租户可控字段白名单」**可再收紧一层，对齐 LangBot 的显式拒伪造清单。
  - **短生命周期 admission grant + 单调 revocation revision**（`admission.py:81/119/127`）对应 Clawith 的 `SandboxExecutionLease`：两者都是「短租约 + 心跳续租」，但 **LangBot 的 `revocation_revision` 单调计数器**是 Clawith 租约侧缺的「代际仲裁」——Clawith 的 lease 靠 Redis value 匹配（`execution_lease.py:17-28`），若租约被抢占、旧 holder 再写，只有 value 不匹配兜底；引入单调 revision 可让「旧 grant 的迟到写」被显式拒绝。
  - **`_challenge_cloud_shared_workspace` 高熵 marker 探活**：Clawith 的 bwrap 挂载/bundle 注入路径可用类似「marker 探活」做挂载后的自检，防「挂载声明了但实际不可达」的静默故障。

---

## 6. `/mcp` 服务层

- **`LangBotMCPServer`**（`api/mcp/server.py:58`）包装 FastMCP：**`stateless_http=True, json_response=True`（`:69-70`）**——注释明说 `stateless_http` 是为了**免 sticky session、可被负载均衡器任意分发**，`json_response` 避免 SSE 流、保持响应简单。约 25 个工具直接调 service 层（`get_system_info/list_bots/get_bot/create_bot/update_bot/delete_bot/list_pipelines/.../list_knowledge_bases/retrieve_knowledge_base/...`，`_register_tools` `:77`），全部带 Permission 校验（`WORKSPACE_VIEW/RESOURCE_VIEW/RESOURCE_MANAGE`），secrets 一律 redact。
- **`MCPMount` ASGI dispatcher**（`api/mcp/mount.py:51`）：`/mcp` 前缀 API key 鉴权（`X-API-Key` 或 `Authorization: Bearer`，`:40`），`tenant_scope` 隔离，**entitlement 校验**（`:120-127` 调 `entitlement_resolver.resolve(workspace_uuid)` 取 `entitlement_revision`，失败返回 `entitlement_unavailable`）。

### 6.1 对 Clawith

- Clawith 的 MCP 是**对外消费 MCP server**（工具 map 见 skill `clawith-local-dev`），而不是「把自身服务层暴露成 MCP」。**LangBot 的「精选子集暴露 + stateless_http 免 sticky session」是一个 Clawith 可参考的对外接口形态**：如果 Clawith 未来要把「某租户的 agent 能力」暴露给外部 agent 消费，`/mcp` + `stateless_http` + API key + entitlement 校验是现成的轻量方案。
- **对照结论**：可迁移「**精选工具子集 + 权限校验 + stateless HTTP**」作为 Clawith 对外暴露服务层（而非全量 REST）的候选形态，尤其配合 Clawith 已有的 `tenant_id` 隔离与 Langfuse 观测。

---

## 7. 持久化 / RAG / 向量库

- **三模式持久化**（`persistence/mgr.py`）：`PersistenceMode.OSS_COMPAT / CLOUD_RUNTIME / RELEASE_MIGRATION`（`:112-114`）——SQLite 默认、PG for cloud、release migration 走 advisory lock（`_RELEASE_MIGRATION_ADVISORY_LOCK_ID` `:100`）。`_ALEMBIC_TENANT_TABLES`（`:52`）清单做 tenant RLS；`tenant_uow`/`tenant_scope` 包事务边界；PG least-privilege role 大量校验查询。
- **RAG 知识库**（`rag/knowledge/kbmgr.py`）：知识库由 **Knowledge Engine 插件**驱动（RAG 实际走插件，非内置 VDB）。`RuntimeKnowledgeBase`（`:32`）`store_file`（`:230`）对 ZIP 解压做**安全限制**（max entries/files/bytes/uncompressed/compression-ratio，`:349` 是 compression-ratio 校验）；`retrieve`（`:421`）top_k=5；全链路 placement generation 围栏。`RAGManager`（`:617`）键 `(workspace_uuid, kb_uuid)`。
- **向量库**（`vector/vdb.py`）：`VectorDatabase` ABC（`:44`），`SearchType.VECTOR/FULL_TEXT/HYBRID`（`:39-41`）；`vector/mgr.py` 6 后端（chroma/qdrant/seekdb/valkey_search/milvus/pgvector）——**注意：RAG 实际走插件，内置 VDB 偏 legacy/并行**。

### 7.1 对 Clawith

- Clawith 的持久化是 SQLModel + Alembic + tenant RLS（`__tenant_scoped__`），RAG 走 `cross_session_retrieval.py` + 外部 embedding，与 LangBot 的「知识库插件驱动」不同构但目标一致。
- **对照结论**：**LangBot 的 ZIP 解压安全限制**（`kbmgr.py:349`）对 Clawith 的「上传文档/工作区产物进上下文」是一条直接可抄的防线——解压炸弹（compression ratio）、条目数、文件数、单文件大小四项上限，Clawith 的 workspace 上传/产物读取路径可对齐。

---

## 8. 多租户 Cloud 模式（placement_generation / tenant RLS / entitlement）

**这是对任务背景「LangBot 单进程单租户」假设的重要修正**，报告必须如实记录：

- **`placement_generation` 单调代际**：`ExecutionContext` 携带 `placement_generation`（`botmgr.py:56`），`_observe_execution_context`（`:646`）拒绝回滚（`:655-670`），placement 前进关旧 adapter——**全链路（platform/pipeline/plugin/box/rag）都以代际为准判断「这个执行体还是不是最新」**。
- **`assert_execution_active`**（`botmgr.py:91`、`pipelinemgr.py:152`）作为贯穿每层的闸门：陈旧 placement 一律失败关闭。
- **tenant RLS + entitlement resolver**：`persistence/mgr.py` 的 tenant RLS 清单、`api/mcp/mount.py:120-127` 的 entitlement 校验、`box/admission.py` 的 entitlement→grant——**LangBot 的 Cloud 形态已具备多租户隔离/授权/配额**。
- **形态定位**：OSS 单 workspace（默认 SQLite、软限）→ Cloud 多租户（PG + RLS + placement_generation + entitlement + nsjail + 硬限）。

### 8.1 对 Clawith

- **这是全报告最重要的对照点**：LangBot 的 `placement_generation` 单调代际，**正是 Clawith 目前缺的「run 状态代际」**。Clawith 在 orca 研究（`20260905-orca-study.md` §2/§4）里已两次点出「`expectedRuntimeFence`/`runtimeFence` 单调计数 vs Clawith 的 attempt_count + 时间戳」——**LangBot 用 `placement_generation` 把同一件事落在了多租户 IM 场景里，且贯穿全部执行链**。Clawith 的 `attempt_count`（`agent_runs`）目前是「次数计数」而非「严格单调的状态代际」，在「部署杀在途 run → 重放分叉」的仲裁里不如 `placement_generation` 干净。
- **对照结论**：**引入一个严格单调的「run 状态代际」字段**（对应 LangBot `placement_generation`、orca `runtimeFence`），在 command claim / checkpoint 写入 / 沙箱 lease 三处用「代际不匹配即拒绝」替换「时间戳 + attempt_count 近似仲裁」——这是三个参考项目（orca/LangBot）共同指向的同一结论，可信度最高。

---

## 9. Clawith 侧对照汇总（工具核实）

| LangBot 机制 | LangBot 文件:行 | Clawith 对标 | Clawith 文件:行 |
|---|---|---|---|
| 显式 stage 编排启动 + 失败清理 | `core/boot.py` stage_order | FastAPI lifespan + RuntimeCommandDaemon（无显式 stage） | — |
| 飞书同步 SDK 调 `asyncio.to_thread` | `platform/sources/lark.py:315` | lark-oapi 直用 | `feishu_ws.py:98-467` |
| 飞书流式卡片 `streaming_mode+update_multi` | `lark.py:1699-1725` | `stream_card_content`/`set_card_streaming_mode` | `feishu_service.py:1057/1140` |
| 线程感知会话隔离 `{group_id}_{thread_id}` | `lark.py:1466` | **无**（群级会话） | — |
| 有界入站调度 `_MAX_INBOUND_EVENTS=100` | `lark.py:1070/1353` | 飞书 WS 事件消费 | `feishu_ws.py` |
| QueryPool 双层配额 + 过载丢弃 | `pipeline/pool.py:129-130`、`controller.py:99` | claim FIFO + scheduling_lane（缺单租户配额） | `persistence.py:701-739` |
| MessageAggregator 消息去抖聚合 | `pipeline/aggregator.py:23/64` | **无** | — |
| stage 返回 AsyncGenerator = 流式 | `pipeline/stage.py:35` | LangGraph astream + answer_stream | `graph.py`、`answer_stream.py` |
| 插件期望态 reconcile + digest | `plugin/connector.py:714/332` | workspace 物化去重 | `run_workspace.py:1-246` |
| 心跳 20s + 3 失败 + 指数退避 | `connector.py:834-845/1017` | command claim 心跳续租 | `command_worker.py:520-561` |
| `_managed_policy_payload` 拒租户伪造字段 | `box/service.py:482` | 沙箱策略宿主裁决 | `workspace_policy.py:1-147` |
| admission 短 grant + revocation revision | `box/admission.py:81/119/127` | Redis 租约 value 匹配 | `execution_lease.py:17-28/98` |
| `placement_generation` 单调代际 | `botmgr.py:56/646-670` | attempt_count + 时间戳 | `agent_run.py` |
| MCP 精选子集 + stateless_http | `api/mcp/server.py:69-70` | 消费 MCP（非暴露） | skill clawith-local-dev |
| ZIP 解压安全限制 | `rag/knowledge/kbmgr.py:349` | workspace 上传/产物读取 | — |
| Box 沙箱 nsjail 后端（真实隔离边界） | `box/service.py:300-303` | bwrap 隔离 + `check_code_safety` 黑名单（明言非边界） | `sandbox/security.py:3-7/62` |
| 反馈按钮 HITL（FeedbackEvent / Dify 表单） | `lark.py:1235-1279` | 飞书审批 create 授权 HMAC + compare_digest | `feishu_approval_authorization.py:44/154` |

---

## 10. 可迁移点 → Clawith 映射

| # | LangBot 机制（文件） | Clawith 对标点 | 可借鉴要点 |
|---|---|---|---|
| 1 | `placement_generation` 单调代际贯穿全链（`botmgr.py:56/646-670`） | attempt_count + 时间戳仲裁 | 引入严格单调「run 状态代际」，claim/checkpoint/lease 三处代际不匹配即拒绝（与 orca `runtimeFence` 结论合流） |
| 2 | 飞书线程感知会话隔离 `{group_id}_{thread_id}`（`lark.py:1466`） | 群级会话 | 群内多 thread 上下文隔离键，防串话污染 |
| 3 | 飞书流式卡片参数 `streaming_mode+update_multi+print_strategy:fast`（`lark.py:1699-1725`） | `stream_card_content`/`set_card_streaming_mode` | 对齐流式卡片参数，绕过消息编辑限流的打字机回显 |
| 4 | 同步 SDK 调 `asyncio.to_thread`（`lark.py:315`） | lark-oapi 直用 | 防 SDK 同步调用阻塞 event loop |
| 5 | QueryPool 双层配额 + 过载丢弃最旧（`pool.py:129-130`、`controller.py:99`） | claim FIFO + scheduling_lane | 补「单租户/单 workspace 配额」层，过载降级不崩溃 |
| 6 | MessageAggregator 去抖聚合（`aggregator.py:23/64`） | chat_intake 直连 | 连发碎片消息聚合，降 LLM 调用碎片化 |
| 7 | `_managed_policy_payload` 拒租户伪造字段（`box/service.py:482`） | 沙箱执行参数 | 显式「租户可控字段白名单」，宿主硬策略不可被覆盖 |
| 8 | admission 短 grant + 单调 revocation revision（`admission.py:81/119/127`） | Redis 租约 value 匹配 | 租约引入单调代际，旧 grant 迟到写显式拒绝 |
| 9 | 插件期望态 reconcile + digest（`connector.py:714/332`） | workspace 物化去重 | 「期望态 + 代际 + digest」统一对账抽象 |
| 10 | MCP 精选子集 + stateless_http 免 sticky（`server.py:69-70`） | 消费 MCP | 对外暴露服务层的轻量形态（API key + entitlement） |
| 11 | ZIP 解压安全限制（`kbmgr.py:349`） | workspace 上传/产物读取 | 解压炸弹四项上限（ratio/条目/文件/单文件） |
| 12 | 显式 stage 编排启动 + 失败清理（`core/boot.py`） | FastAPI lifespan | 启动/关闭生命周期骨架，失败即干净收尾 |
| 13 | 有界入站调度 + `run_coroutine_threadsafe`（`lark.py:1070/1353`） | 飞书 WS 事件消费 | 入站队列有界，防高流量内存堆积 |
| 14 | 心跳失败阈值 + 指数退避重连（`connector.py:834-845/1017`） | command claim 心跳 | 失败阈值触发重连 + 退避上限参数形态 |

---

## 11. 局限（诚实记录）

- **双仓拆分导致部分关键实现不在本仓库**：插件运行时/沙箱的 `PluginWorkerPolicy`、`InstallationBinding`、`PluginInstallationDesiredState` 等核心类型定义在外部 SDK `langbot-plugin`（v0.5.7，`connector.py:49-54` 导入），本仓库只有**消费侧**（`_load_worker_policy`/`_binding_from_setting`/reconcile），SDK 内部实现细节本次未读。要深挖插件/沙箱运行时需另开 `langbot-plugin-sdk` 仓库。
- **RAG 主路径不在内置 VDB**：知识库实际由「Knowledge Engine 插件」驱动，`vector/` 下的 6 后端 VDB 偏 legacy/并行实现——「向量库选型」对 Clawith 的借鉴价值低于「知识库的 placement 围栏 + 解压安全限制」。
- **架构形态差异仍在**：LangBot 是「单进程内多 workspace 分片 + 一个 `Application` 服务定位器」，Clawith 是「多租户服务端 + LangGraph checkpoint」——即便 LangBot 有 Cloud 多租户形态，其**无 LangGraph 运行时、无 checkpoint、无多端断流恢复语义**，这些 Clawith 已自研得更深，不在本研究可迁移范围内。
- **本次未深入**：`web/`（前端管理面板）、`tests/`、各非飞书平台的 adapter 细节（discord/telegram/slack/wechat/qq/wecom/dingtalk/kook/line/satori/matrix 只做了清单级确认）、`cloud/` 部署/扩容细节、`skills/` 目录（in-repo agent skills + `skills.index.json`，与 Clawith 的 skill 机制同构但本次未对照展开）。
- **浅克隆限制**：HEAD `ec63978`（`--depth 1`）无完整历史，无法追踪「为何选 placement_generation 而非中心锁」「Cloud 多租户的演进路径」等设计决策，仅能从代码 + `docs/multi-tenant/` 文档推断。
- **行号精度**：所有 LangBot/Clawith 行号均经 `read_file`/`grep -n` 核实（截至 2026-09-05 工作树，LangBot 源码在 `src/langbot/pkg/` 前缀下）；LangBot 上游仍在快速迭代，行号随版本漂移。

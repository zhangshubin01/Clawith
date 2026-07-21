# ACP 架构深度分析 & 生产级修复方案

> 分析日期：2026-07-21 | 分支：feature/user-api-key-shubin

## 1. 架构全景

### 1.1 连接生命周期

```
IDE 插件 → ws://host:port/ws/acp (Bearer JWT)
  → router.py: JWT 验证 + Origin 白名单
  → AcpHandler 实例化
  → initialize: 协议版本协商 + 能力宣告
  → session/new: 创建 ChatSession(source_channel="acp")
  → session/prompt: LLM 调用 + 流式推送 + 工具路由
  → cleanup: 取消活跃任务 + 关闭 WebSocket
```

### 1.2 关键文件

| 文件 | 行数 | 职责 |
|------|------|------|
| `plugins/clawith_acp/__init__.py` | 32 | 插件注册入口 |
| `plugins/clawith_acp/router.py` | 60 | `/ws/acp` WebSocket 端点 |
| `plugins/clawith_acp/acp_handler.py` | 1821 | JSON-RPC 2.0 消息循环、LLM 调用、流式推送、熔断器 |
| `plugins/clawith_acp/tool_bridge.py` | 2412 | 工具路由引擎：路径校验、参数构建、去重缓存、终端代理 |
| `plugins/clawith_acp/tool_hooks.py` | 1055 | 链式 Handler + ~800 行工具定义 + 猴子补丁 |
| `plugins/clawith_acp/acp_session.py` | 255 | 会话 CRUD、历史加载、轮次持久化 |
| `plugins/clawith_acp/acp_routes.py` | 80 | 工具名 → ACP 方法名映射表 |
| `plugins/clawith_acp/turn_budget.py` | 168 | 双时钟预算（workflow + compute） |
| `plugins/clawith_acp/terminal_policy.py` | 71 | 终端命令分类：blocking vs streaming |

### 1.3 工具路由全链路

```
LLM 调用工具
  → caller.py: execute_tool(tool_name, args)
  → tool_hooks.py: _chained_execute_tool 遍历 _bridge_handlers
  → _acp_aware_execute_tool: 检查 current_acp_handler ContextVar
  → tool_bridge.py: _try_acp_execute: 路径校验 → 去重 → 参数构建 → 速率限制
  → acp_handler.py: send_request → JSON-RPC over WebSocket → 等待 Future
  → IDE 插件执行工具 → JSON-RPC 响应 → Future.set_result
  → tool_bridge.py: 结果格式化 → 返回给 LLM
```

---

## 2. 发现的严重问题

### 2.1 运行时阻断问题（crash）

**问题 1**：`router.py:12` 导入 `from app.core.security import verify_api_key_or_token` 但该函数在代码库中**不存在**。

**影响**：`/ws/acp` 端点完全不可用，Python import 阶段即崩溃。

**修复**：在 `app/core/security.py` 中实现该函数，复用已有 `verify_token` + `verify_api_key` 逻辑。

**问题 2**：`__init__.py:9` 导入 `from app.plugins.base import ClawithPlugin` 但 `app/plugins/base.py` **不存在**。

**影响**：插件注册失败，FastAPI 启动时不会挂载 `/ws/acp` 路由。

**修复**：新建 `app/plugins/base.py`，定义 `ClawithPlugin` 抽象基类。

### 2.2 两条并行执行路径

```
标准 Web 路径:  WS → RuntimeToolStepService → call_llm → execute_tool → runtime 编排
ACP 路径:       /ws/acp → AcpHandler → call_llm → _chained_execute_tool → IDE WebSocket
```

ACP 通过 `ContextVar(current_acp_handler)` 在 `execute_tool` 层面拦截，**完全绕过 RuntimeToolStepService**。

这意味着 ACP 缺失：
- `AgentToolExecution` 租赁（无跨 worker 故障转移）
- `RuntimeEventStream`（无断连恢复）
- 分布式工具执行协调
- 运行时生命周期的超时/取消传播

### 2.3 ~800 行重复工具定义

`tool_hooks.py:74-883` 的 `_ACP_IDE_TOOLS` 与 `builtin_tool_definitions.py` 重复。每个工具定义需双向手动同步。

### 2.4 tool_bridge.py 2412 行单体

`_try_acp_execute` 在一个巨大 `if/elif` 链中处理了 ~10 件事：路径验证、速率限制、DAG 去重、搜索缓存、参数构建、超时管理、结果格式化、失败去重。

### 2.5 猴子补丁污染

```python
# tool_hooks.py:1043 — 直接覆盖
agent_tools.get_agent_tools_for_llm = _acp_aware_get_tools

# tool_hooks.py:1048 — 跨模块修补
_caller_mod.get_agent_tools_for_llm = _acp_aware_get_tools
```

无重新加载支持，无热插拔，绕过链式 Handler 模式。

### 2.6 无 WebSocket keepalive

无 `session/ping` 或 WebSocket ping frame。IDE 静默断开后后端仅在 flush 抛 `ConnectionClosedError` 时才能发现。长空闲期后死连接一直存活。

### 2.7 NES stub 永远返回空

`acp_nes.py` 总是返回空建议，但 NES 默认启用 (`acp_features.py`)，每条 prompt 都走一遍空调用。

---

## 3. 生产级修复方案

### 阶段 0：紧急修复（阻断问题，最高优先级）

| 修复 | 位置 | 改动 |
|------|------|------|
| 补全 `verify_api_key_or_token` | `app/core/security.py` | 新增函数（~20 行），复用已有验证逻辑 |
| 创建 `app/plugins/base.py` | 新建 | 定义 `ClawithPlugin` 抽象基类（~15 行） |
| 添加 WebSocket keepalive | `acp_handler.py` | 每 30s ping frame，120s 无 pong 断连（~40 行） |

### 阶段 1：消除重复

- `builtin_tool_definitions.py` 新增 `get_acp_tool_definitions()` 派生函数
- `tool_hooks.py` 删除 ~800 行手工定义，改为调用派生函数
- `acp_handler.py` 的 `_kind_map` / `_TOOL_CN_NAME` 移到 `acp_routes.py` 统一管理

### 阶段 2：打通 Runtime V2 租赁

- `tool_step_service.py` 将 `_acquire_tool_execution` / `_settle_tool_execution` 提取为公开函数
- `tool_bridge.py` 在 `_try_acp_execute` 首尾插入租赁调用
- ACP 超时/异常触发故障标记

### 阶段 3：清理猴子补丁 + 拆分单体

- 新建 `tool_registry.py`（~50 行），消除 `caller.py` 的猴子补丁
- 提取 `AcpPathGuard` → `acp_path_guard.py`
- 提取 `AcpResultFormatter` → `acp_result_formatter.py`
- `tool_bridge.py` 从 2412 行降到 ~800 行

### 改动量估算

| 阶段 | 新增行 | 删除行 | 文件数 |
|------|--------|--------|--------|
| 阶段 0 | ~80 | 0 | 3 |
| 阶段 1 | ~80 | ~800 | 4 |
| 阶段 2 | ~120 | ~20 | 2 |
| 阶段 3 | ~500 | ~300 | 6 |
| **合计** | **~780** | **~1120** | **15** |

### 不做的事

| 决策 | 原因 |
|------|------|
| ACP 完全迁移到 RuntimeGraphState | graph 编译 + state schema 变更 >2000 行 |
| ACP 提示走 RuntimeEventStream | 需大幅重构 JSON-RPC 推送机制 |
| NES 真实实现 | 暂无业务需求 |
| 多 worker 缓存共享（上 Redis） | 当前单节点够用 |
| 移除 ContextVar 模式 | 模式本身合理 |

---

## 4. IDE 插件端现状

| 模块 | 详情 |
|------|------|
| ACP SDK | 0.25.0-SNAPSHOT（本地 JAR） |
| 工具注册 | 30+ 工具，6 线程池执行 |
| 认证 | JWT + PasswordSafe + 10min Token 刷新 |
| 协议 | WebSocket + JSON-RPC 2.0 |
| 会话 | PluginSettings (clawith-acp.xml) 持久化 |

IDE 插件端架构合理，无重大修改需求。仅需配合后端 API 变更。

---

## 5. 从 feature/user-api-key 移植文件清单

### 5.1 ACP 插件（20 文件，全新增）

```
backend/app/plugins/__init__.py
backend/app/plugins/base.py                    ← 解决 ImportError
backend/app/plugins/clawith_acp/__init__.py
backend/app/plugins/clawith_acp/README.md
backend/app/plugins/clawith_acp/plugin.json
backend/app/plugins/clawith_acp/acp_document.py
backend/app/plugins/clawith_acp/acp_features.py
backend/app/plugins/clawith_acp/acp_handler.py
backend/app/plugins/clawith_acp/acp_nes.py
backend/app/plugins/clawith_acp/acp_routes.py
backend/app/plugins/clawith_acp/acp_session.py
backend/app/plugins/clawith_acp/coalesce_keys.py
backend/app/plugins/clawith_acp/history_cache.py
backend/app/plugins/clawith_acp/list_dedup.py
backend/app/plugins/clawith_acp/router.py
backend/app/plugins/clawith_acp/search_dedup.py
backend/app/plugins/clawith_acp/terminal_policy.py
backend/app/plugins/clawith_acp/tool_bridge.py
backend/app/plugins/clawith_acp/tool_hooks.py
backend/app/plugins/clawith_acp/turn_budget.py
```

### 5.2 支撑代码（4 文件，需合并）

| 文件 | 变更内容 |
|------|---------|
| `backend/app/core/security.py` | 新增 `verify_api_key_or_token()`（解决 ImportError） |
| `backend/app/main.py` | ACP router + ide_plugin router 注册 |
| `backend/app/config.py` | `CTX_OUTPUT_SHAPER_PATHS: str = "acp,ws,feishu"` |
| `backend/app/api/ide_plugin.py` | IDE 插件 REST API（新增文件） |

### 5.3 数据库迁移（7 文件，需合并）

```
backend/alembic/versions/add_ide_plugin_configs.py
backend/alembic/versions/add_ide_plugin_fields_to_chat_sessions.py
backend/alembic/versions/add_open_files_column_to_chat_session.py
backend/alembic/versions/add_chat_session_soft_delete_and_status.py
backend/alembic/versions/061_remove_lsp4j_sessions_and_plugin_configs.py
backend/alembic/versions/004_add_chat_sessions.py
backend/alembic/versions/482391030754_merge_add_ide_plugin_configs_head.py
```

### 5.4 移植命令

```bash
# 从 feature/user-api-key 提取指定文件
git checkout feature/user-api-key -- \
  backend/app/plugins/ \
  backend/app/core/security.py \
  backend/app/main.py \
  backend/app/config.py \
  backend/app/api/ide_plugin.py \
  backend/alembic/versions/add_ide_plugin_configs.py \
  backend/alembic/versions/add_ide_plugin_fields_to_chat_sessions.py \
  backend/alembic/versions/add_open_files_column_to_chat_session.py \
  backend/alembic/versions/add_chat_session_soft_delete_and_status.py \
  backend/alembic/versions/061_remove_lsp4j_sessions_and_plugin_configs.py \
  backend/alembic/versions/482391030754_merge_add_ide_plugin_configs_head.py
```

### 5.5 注意事项

- `main.py`、`config.py`、`security.py` 和 `004_add_chat_sessions.py` 是**合并**而非覆盖——当前分支已有改动
- ACP 插件 20 文件全是新增，直接 checkout 即可
- 迁移文件需要手动检查版本链是否符合当前 `alembic/versions/` 顺序

---

## 6. LangGraph 深度分析

LangGraph 是 Clawith Agent Runtime 的底层执行引擎。

**源码位置：** `/Users/shubinzhang/Documents/UGit/langgraph/libs/langgraph/langgraph/`
**版本：** LangGraph v10+ (monorepo 结构: `libs/langgraph/`, `libs/checkpoint/`, `libs/checkpoint-postgres/`)

### 6.1 Clawith 集成点

```
backend/app/services/agent_runtime/
├── graph.py              → build_agent_runtime_graph() 构建 StateGraph
├── langgraph_driver.py   → 调用 PregelLoop 驱动执行
├── checkpointer.py       → AsyncPostgresSaver 封装
├── node_executor.py      → 自定义节点执行器
├── state.py              → RuntimeState 定义
└── worker_service.py     → Worker 服务
```

### 6.2 执行模型 — Pregel BSP

LangGraph 实现 **Pregel-like Bulk Synchronous Parallel** 模型，每步三阶段：

| 阶段 | 方法 | 行为 |
|------|------|------|
| **Plan** | `prepare_next_tasks()` | 对比 `versions_seen` vs `channel_versions`，决定哪些节点执行 |
| **Execute** | 线程池并行 | 所有选中节点并行执行，写操作对同批次不可见 |
| **Update** | `apply_writes()` | 应用写入到 channels，更新版本号，触发下一轮 Plan |

`PregelLoop._loop.py` 循环直到：无剩余 task、`interrupt_before/after` 触发、`recursion_limit` 超出、或 `Command(goto=)` 重路由。

### 6.3 StateGraph 构建

**`StateGraph`** (`graph/state.py:130`) → `compile()` → **`CompiledStateGraph`** (`_algo.py:1391` → extends `Pregel`)

Clawith 在 `graph.py:build_agent_runtime_graph()` 中构建：
- `add_node(name, runnable)` 添加 LLM 调用节点 + 工具执行节点
- `add_edge` / `add_conditional_edges` 连线
- `compile(checkpointer=...)` 生成可执行图

### 6.4 Channels 状态通道

| Channel | 行为 | Clawith 用途 |
|---------|------|-------------|
| **`LastValue`** | 存最后单值 | 消息列表、模型配置 |
| **`BinaryOperatorAggregate`** | reducer 模式 | `operator.add` 追加消息 |
| **`Topic`** | PubSub 去重 | 工具调用分发 |
| **`EphemeralValue`** | 仅存活一步 | 步骤间临时输入 |

### 6.5 Checkpoint 持久化

**`AsyncPostgresSaver`** (`libs/checkpoint-postgres/aio.py:40`)

Clawith 在 `checkpointer.py` 中封装：

| 表 | 用途 |
|---|------|
| `checkpoints` | 主状态快照（`thread_id`, `checkpoint_ns`, `checkpoint_id` PK） |
| `checkpoint_blobs` | 大 value 二进制存储 |
| `checkpoint_writes` | 中间写入（task 级） |

每次 `PregelLoop. tick()` 后：
1. `version_seen` 更新 → 记录节点看到了哪些 channel 版本
2. `channel_values` 快照 → 可回退到任意历史步骤
3. `pending_writes` → 存储中断值（HITL）

### 6.6 中断系统 (HITL)

**`interrupt(value)`** (`types.py:811`)：
1. 首次调用 → 抛 `GraphInterrupt`，checkpoint 保存
2. 下次 resume（`Command(resume=value)`）→ 重跑节点，`interrupt()` 返回 resume 值

**`Command`** (`types.py:758`)：
- `Command(resume=value)` — 恢复中断
- `Command(update={...})` — 无节点直接更新状态
- `Command(goto="node")` — 跳转到指定节点

### 6.7 流式输出

V3 流式架构（`stream/_mux.py`）：
- **`StreamMux`** — 中央事件分发 + transformer pipeline
- **`StreamTransformer`** — 将原始事件投影到命名 channel
- 内置 transformers: `ValuesTransformer`, `MessagesTransformer`, `DebugTransformer` 等

Clawith 在 `langgraph_driver.py` 中使用 `graph.stream_events()` 获取流式输出。

### 6.8 关键配置

| 参数 | 作用 |
|------|------|
| `recursion_limit` | 最大步数（超出抛 `GraphRecursionError`） |
| `interrupt_before` / `interrupt_after` | HITL 节点 |
| `checkpointer` | 持久化后端 |
| `retry_policy` | 节点级重试策略 |
| `timeout` | 节点级超时 |
| `durability` | sync/async/exit 持久化时机 |

### 6.9 Clawith 中的实践风险

| 风险 | 说明 |
|------|------|
| `recursion_limit` 默认太小 | 默认 25 步，复杂任务可能不够 |
| `AsyncPostgresSaver` 连接池耗尽 | 高并发下 `aput` 竞争 |
| checkpoint 膨胀 | 每步全量快照，大上下文下 DB 膨胀快 |
| V3 流式兼容性 | `stream_events(version="v3")` API 较新，可能有不稳定 edge case |

---

## 7. LangGraph 生产最佳实践（全网汇总）

### 7.1 Checkpoint 选择

| Checkpointer | 场景 | 生产就绪 |
|---|---|---|
| `InMemorySaver` | 开发/测试 | ❌ 进程重启丢失 |
| `SqliteSaver` | 单机部署 | 部分 |
| `PostgresSaver` / `AsyncPostgresSaver` | **多实例生产** | ✅ |
| `RedisSaver` (v0.1.0) | 高性能低延迟 | ✅ (GET 速度和list 速度分别是 PostgreSQL 12倍、31倍) |

**Clawith 当前：** `AsyncPostgresSaver` ✅

### 7.2 连接池（关键）

❌ 生产环境**禁止**直接传 `Connection` 给 `PostgresSaver`—— 连接在长时间运行中会超时断开。

✅ 必须用 `AsyncConnectionPool`：

```python
from psycopg_pool import AsyncConnectionPool

pool = AsyncConnectionPool(
    conn_string,
    max_size=10,         # 根据并发负载调整
    min_size=2,          # 保持热连接
    max_idle=300.0,      # 低于 PG server idle_session_timeout
    max_lifetime=1800.0, # 硬上限，强制回收
    kwargs={"autocommit": True, "row_factory": dict_row}
)
checkpointer = AsyncPostgresSaver(pool)
```

**防连接超时措施：**
- `max_idle` < PG `idle_session_timeout` 设置
- TCP keepalive: `keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=3`
- 健康检查回调 `check=check_connection`

### 7.3 State Schema 优化（最大性能杠杆）

**规则：一切写进 state 的东西都会被序列化到每个 checkpoint。**

| ✅ 放 state 里 | ❌ 不放 state 里 |
|---|---|
| 消息 ID 引用 | 完整文档内容 |
| 枚举/标志位/索引 | 原始 LLM 响应 + metadata |
| 小型聚合值 | 大二进制数据（图片/PDF） |
| S3 keys / 外部引用 | Embedding 向量 |
| 简短工具结果摘要 | 重复的配置对象 |

**性能基准：**
- 精简 schema (<10KB) → ~12ms/checkpoint
- 膨胀 schema (500KB+) → 300-800ms/checkpoint

**Clawith 风险：** 当前 `RuntimeState` 包含完整 `messages` 列表，长对话下每步全量序列化。

### 7.4 Context 窗口管理

长对话下两个问题同时发生：checkpoint 膨胀 + LLM context 超限。

**Trimming（简单，有损）：** 设 `max_token` 上限，旧消息从 LLM 调用排除但保留在 state。

**Summarization（保留语义）：** 当消息数超阈值时触发摘要节点，用二次 LLM 调用将旧消息压缩为摘要，替换原始历史。

**最佳实践：** 两者结合 — trimming 做快速截断，summarization 做语义保留。

### 7.5 流式 v3 + HITL 生产模式

```python
from langgraph.types import Command

while True:
    stream = graph.stream_events(input, config=config, version="v3")

    for message in stream.messages:
        for token in message.text:
            display(token)  # 实时流式 LLM token

    if not stream.interrupted:
        final_state = stream.output
        break

    user_response = get_input(stream.interrupts[0].value)
    input = Command(resume=user_response)
```

**关键点：**
- `stream.interrupted` bool 检测暂停
- `stream.interrupts` 携带暂停负载
- `Command(resume=value)` 恢复
- 同一个 `thread_id` 恢复
- 节点从顶部重新执行，`interrupt()` 前的代码需幂等

### 7.6 常见生产错误

| 错误 | 后果 | 修复 |
|------|------|------|
| 重复调用时传入完整初始 state | `Annotated[list, add]` 合并重复数据 | 从 checkpoint 恢复时传 `None` |
| 无 iteration limit | 条件循环无限执行 | `recursion_limit` + `iteration_count` 硬退出 |
| Subgraph 有独立 checkpointer | 双重存储 + 命名空间冲突 | 仅父图配 checkpointer |
| UUID 用字符串存 PostgreSQL | 类型不匹配查询失败 | 用 `uuid.UUID` 对象 |
| 无 TTL / 清理策略 | `checkpoints` 表无限膨胀 | 定期清理 + 配置 TTL |

### 7.7 Clawith 针对性建议

基于 LangGraph 特性和 Clawith 架构，建议：

1. **State 瘦身** — `RuntimeState.messages` 在长对话下每个 checkpoint 全量序列化，应增加 summarization 节点
2. **连接池化** — 确认 `checkpointer.py` 中 `AsyncPostgresSaver` 是通过连接池构造
3. **recursion_limit 检查** — 当前默认值是否足够复杂 Agent 任务
4. **checkpoint 清理** — 是否有 TTL 或定期清理已完成的 thread
5. **durability 模式** — Clawith 当前用的是 `durability="sync"` (默认)，是否需要评估改为 `"exit"` 减少写开销

### 7.8 Clawith 实际状态 vs 最佳实践

**当前 checkpointer 配置** (`checkpointer.py:169-177`)：
- 使用 `AsyncPostgresSaver.from_conn_string()` ✅ 推荐方式
- 自定义序列化 (`checkpoint_serializer`) ✅

**当前 State Schema** (`state.py:123-136`)：
- `messages: Annotated[list[AnyMessage], add_messages]` ✅ 标准 reducer
- 但完整 messages 每步全量序列化 — 长对话下 checkpoint 膨胀

**当前缺失的最佳实践：**
- ❌ 无 summarization 节点（长对话下 state 和 checkpoint 持续膨胀）
- ❌ 无 checkpoint TTL 清理策略
- ❌ `recursion_limit` 配置未见显式设置
- ❌ 无 durability mode 评估

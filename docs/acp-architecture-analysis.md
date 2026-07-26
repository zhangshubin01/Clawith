# ACP 架构深度分析 & 生产级修复方案

> **状态汇总** (更新于 2026-07-26)
> - **Phase 0 (紧急修复)**: ✅ **已全部完成** — `verify_api_key_or_token`, `base.py`, WebSocket keepalive, `auth/refresh`, `ide_plugin.py`, Origin 守卫, router 注册, `tool_execution_policy.py`, `history_hydrate.py`
> - **Phase 6 (acp_ 前缀重命名)**: ✅ **已完成** — 全部 35+ 工具名改为 `acp_` 前缀, `ACP_METHOD_MAP`/`ACP_TOOL_MAP`/`ACP_KIND_MAP` 同步更新
> - **Phase 1 (消除重复)**: ❌ 未开始 — Phase 6 完成，已就绪
> - **Phase 2 (可观测层提取)**: ❌ 未开始
> - **Phase 3 (猴子补丁清理)**: ❌ 未开始
> - **Phase 7 (_ACP_ 命名统一)**: ❌ 未开始
>
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

**修复**：在 `app/core/security.py` 末尾追加 ~60 行：

```python
async def verify_api_key_or_token(token: str | None) -> uuid.UUID:
    """验证 JWT token 或 API Key，返回 user_id。

    优先级：JWT Bearer token → Clawith API Key → Agent API Key
    """
    if not token:
        raise HTTPException(status_code=401, detail="缺少认证凭据")

    # 1. JWT Bearer token
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if user_id:
            return uuid.UUID(user_id)
    except HTTPException:
        pass

    # 2. Clawith API Key（settings.CLAWITH_API_KEY）
    clawith_key = getattr(settings, "CLAWITH_API_KEY", None)
    if clawith_key and token == clawith_key:
        from app.models.user import User
        from app.database import async_session
        async with async_session() as db:
            result = await db.execute(
                select(User).where(User.is_active.is_(True)).limit(1)
            )
            user = result.scalar_one_or_none()
            if user:
                return user.id
        raise HTTPException(status_code=401, detail="无活跃用户")

    # 3. Agent API Key（plaintext + hash 双模式）
    try:
        from app.api.gateway import _hash_key
        from app.models.agent import Agent
        from app.database import async_session
        async with async_session() as db:
            key_hash = _hash_key(token)
            result = await db.execute(
                select(Agent).where(
                    Agent.api_key_hash.in_([token, key_hash]),
                    Agent.status != "error",
                )
            )
            agent = result.scalar_one_or_none()
            if agent and agent.creator_id:
                return agent.creator_id
    except Exception:
        pass

    raise HTTPException(status_code=401, detail="无效的认证凭据")
```

**问题 2**：`__init__.py:9` 导入 `from app.plugins.base import ClawithPlugin` 但 `app/plugins/base.py` **不存在**。

**影响**：插件注册失败，FastAPI 启动时不会挂载 `/ws/acp` 路由。

**修复**：新建 `backend/app/plugins/base.py`：

```python
"""Clawith 插件系统基类。"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

class ClawithPlugin(ABC):
    """Clawith 插件抽象基类。"""
    name: str = "unnamed"
    version: str = "0.1.0"
    description: str = ""

    @abstractmethod
    def register(self, app: FastAPI) -> None:
        """向 FastAPI app 注册插件路由和钩子。"""
        ...
```

**问题 3**：无 WebSocket keepalive（`acp_handler.py`）

**影响**：IDE 静默断开后，后端仅在 flush 抛异常时才能发现。长空闲后死连接一直存活。

**修复**：改为后台 watchdog 任务（替代简单 last_active 检查）：

```python
# AcpHandler 中:
async def _keepalive_watchdog(self):
    while True:
        await asyncio.sleep(30)
        if time.monotonic() - self._last_active > 120:
            logger.warning("[ACP] keepalive timeout conn={}", self.conn_id)
            await self.ws.close(code=1001)
            break

async def run(self):
    self._last_active = time.monotonic()
    watchdog = asyncio.create_task(self._keepalive_watchdog())
    try:
        async for raw in self.ws.iter_text():
            self._last_active = time.monotonic()
            # ... 现有 dispatch ...
    finally:
        watchdog.cancel()
        current_acp_handler.reset(token)
```

### 2.2 新增：3 个运行时 ImportError 阻断（审查第二轮发现）

**问题 4**：`tool_bridge.py:28` 导入 `from app.services.llm.tool_execution_policy import WORKSPACE_WRITE_TOOLS`，该文件**不存在**。

**影响**：`tool_bridge` 模块 import 时崩溃，所有 ACP 工具路由不可用。

**修复**：新建 `backend/app/services/llm/tool_execution_policy.py`：
```python
"""ACP 工作区写操作工具分类。"""
WORKSPACE_WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file", "edit_file", "delete_file", "move_file",
    "refactor_rename", "safe_delete", "reformat_code",
    "optimize_imports", "convert_java_to_kotlin",
})
```

**问题 5**：`tool_bridge.py:987-997` 惰性导入 `check_tool_autonomy` 但该函数在 `agent_tools.py` 中**不存在**。

**影响**：导入静默失败，`check_fn = None`，所有 ACP 写入工具跳过自主权闸门检查——写入操作无权限审批。

**修复**：在 `agent_tools.py` 中实现 `check_tool_autonomy(agent_id, tool_name, args) -> bool`，或从 ACP 路径中移除该检查（改用 IDE 插件端的权限 UI）。

**问题 6**：`router.py:27` Origin 检查 `if origin and origin not in allowed_origins` 在空 Origin 时被绕过（空字符串是 Python falsy 值）。非浏览器 WebSocket 客户端不发送 Origin 标头。

**影响**：恶意脚本可通过不发送 Origin 标头绕过 CSWSH 保护。

**修复**：改为 `if origin not in allowed_origins`（移除 `and origin` 条件，空 Origin 视为需要检查）。

### 2.3 新增：审查第三轮发现

**问题 7**（CRITICAL）：`ToolExecutionObserver` 用 `uuid5(session_id)` 作 `run_id` 但无 `AgentRun` 行。

**影响**：`agent_tool_executions` 表的外键 `(tenant_id, run_id) -> agent_runs` 在 INSERT 时抛 `ForeignKeyViolation`。

**修复**：在 `record_start()` 中先查询或创建 `AgentRun` 行，或改用 `NULL` run_id。

**问题 8**（HIGH）：`main.py` 未注册 ACP router。`/ws/acp` 端点未挂载到 FastAPI。

**修复**：在 `main.py` 添加 `app.include_router(acp_router)`。

**问题 9**（HIGH）：`history_hydrate.py` 模块不存在。

**修复**：新建 `backend/app/services/llm/history_hydrate.py`，实现 `hydrate_history_tool_results`。

**问题 10**（CRITICAL）：`acp_session.py:168` 调用 `convert_chat_messages_to_llm_format(rows, ctx_window=X, path='acp')` 传了不存在的参数。

**修复**：移除 `ctx_window` 和 `path` 参数，或扩展函数签名。

**问题 11**（CRITICAL）：`acp_session.py` 的 ChatSession 创建缺少 `tenant_id`、`session_type` 等 NOT NULL 列。

**修复**：补全必需字段，确保对应 Alembic 迁移文件存在。

**问题 12**（HIGH）：`tool_bridge.py:1542,2125` 调用 `check_tool_autonomy` 无异常保护。

**修复**：加 try/except 包装，函数不存在时安全降级。

### 2.3b：审查第五轮发现 — 插件登录/智能体列表/租户列表

**问题 13**（CRITICAL）：`POST /api/auth/refresh` 端点不存在。IDE 插件每 10 分钟调用刷新 JWT，后端返回 404，所有刷新静默失败。JWT 过期后强制重新登录。

**修复**：在 `backend/app/api/auth.py` 添加 `/api/auth/refresh` 端点：
```python
@router.post("/api/auth/refresh")
async def refresh_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    payload = decode_access_token(credentials.credentials)
    new_token = create_access_token(payload["sub"], payload["role"])
    return {"access_token": new_token}
```

**问题 14**（CRITICAL）：`GET /api/ide-plugin/agents` 端点不存在。IDE 插件通过此 REST 端点获取智能体列表，当前返回 404 → IDE 设置中智能体列表永远为空。

**修复**：从 `feature/user-api-key` 分支移植 `backend/app/api/ide_plugin.py`，或新建该文件实现智能体列表 API。

**问题 15**（HIGH）：`POST /api/auth/switch-tenant` 后 IDE 插件未持久化选中的 `tenant_id`。REST API 调用（listModels, listSessions）缺少租户上下文，跨 UI 导航可能丢失。

**修复**：在 `AuthManager.switchTenant()` 后调用 `PluginSettings` 持久化 `tenant_id`。

### 2.4 安全缺陷（审查第二轮发现）

| 严重性 | 位置 | 问题 | 修复 |
|--------|------|------|------|
| CRITICAL | `verify_api_key_or_token`（方案代码） | `CLWITH_API_KEY` 路径返回**第一个活跃用户**，API Key 持有者可能以任意用户身份认证 | 删除共享 API Key 路径，或绑定到特定 user_id |
| HIGH | `verify_api_key_or_token`（方案代码） | 使用 `==` 而非 `hmac.compare_digest()` 比较 API Key，通过定时侧信道泄露密钥长度 | 改用 `hmac.compare_digest(token, key)` |
| HIGH | `tool_bridge.py:1053` | `os.path.normpath()` 不解析符号链接，指向项目根之外的 symlink 可绕过路径守卫 | 改用 `os.path.realpath()` 解析后再做边界检查 |
| MEDIUM | `tool_bridge.py:981` | `AcpRateLimiter` 使用进程内存滑动窗口，多 worker 下速率限制 = workers × limit | 改为 Redis 或 ASGI 中间件统一限流 |
| MEDIUM | `acp_handler.py:1449` | `_send_result()` 以 DEBUG 级别记录完整 JSON-RPC 响应（含文件内容、git diff） | 生产环境关闭 DEBUG 或脱敏 |
| MEDIUM | `acp_handler.py:78` | 硬编码调试文件路径 + 固定 session ID `f3071f` | 删除硬编码，改为环境变量控制 |
| LOW | `acp_handler.py:189` | `conn_id` 仅 8 位 hex（32-bit 熵） | 改为完整 UUID |

### 2.5 两条并行执行路径

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

### 2.6 ~800 行重复工具定义

`tool_hooks.py:74-883` 的 `_ACP_IDE_TOOLS` 与 `builtin_tool_definitions.py` 重复。每个工具定义需双向手动同步。

### 2.7 tool_bridge.py 2412 行单体

`_try_acp_execute` 在一个巨大 `if/elif` 链中处理了 ~10 件事：路径验证、速率限制、DAG 去重、搜索缓存、参数构建、超时管理、结果格式化、失败去重。

### 2.8 猴子补丁污染

```python
# tool_hooks.py:1043 — 直接覆盖
agent_tools.get_agent_tools_for_llm = _acp_aware_get_tools

# tool_hooks.py:1048 — 跨模块修补
_caller_mod.get_agent_tools_for_llm = _acp_aware_get_tools
```

无重新加载支持，无热插拔，绕过链式 Handler 模式。

### 2.9 无 WebSocket keepalive → 修复见 2.1 问题 3

### 2.10 NES stub 永远返回空

`acp_nes.py` 总是返回空建议，但 NES 默认启用 (`acp_features.py`)，每条 prompt 都走一遍空调用。

---

## 3. 生产级修复方案

### 阶段执行顺序（审查修正后）

```
Phase 0  → ✅ 已完成（verify_api_key_or_token + base.py + keepalive + auth/refresh + ide_plugin.py + 其余紧急修复）
Phase 6  → ✅ 已完成（acp_ 前缀重命名 — 工具名不再冲突）
Phase 1  → 消除重复（安全删除 ACP_OVERLAP_BASE_TOOL_NAMES）
Phase 2  → 提取 ToolExecutionObserver（ACP + 标准路径共用）
Phase 3  → 清理猴子补丁 + 拆分 tool_bridge
Phase 7  → _ACP_ 命名统一（可选，低优先级）
```

> ⚠️ Phase 1 依赖 Phase 6：重命名后工具名不再冲突，才能安全删除过滤逻辑。

### 阶段 0：紧急修复

详见 2.1 节代码级方案。

| 修复 | 位置 | 改动量 |
|------|------|--------|
| ✅ `verify_api_key_or_token()` | `app/core/security.py` | ~60 行新增 |
| ✅ `app/plugins/base.py` | 新建 | ~30 行 |
| ✅ WebSocket keepalive watchdog | `acp_handler.py:run()` | ~20 行 |
| ✅ `POST /api/auth/refresh` | `app/api/auth.py` | ~15 行新增 |
| ✅ `GET /api/ide-plugin/agents` | `app/api/ide_plugin.py` | 移植或新建 |

**涵盖问题**：1-4、6-9、12-14。✅ 全部完成，验证通过。
（问题 5 分配至阶段 1，问题 10-11 分配至阶段 1，问题 15 分配至 IDE 插件侧）
**总改动**：7 文件，~150 行。✅ 已全部实施。

> ⚠️ 2.1 节代码示例中已知问题已在实施过程中修正：`verify_api_key_or_token` 使用特定异常类型（`ValueError, SQLAlchemyError`）替代 `except Exception`；共享 API Key 路径已移除（仅保留 JWT + Agent API Key 两路径）。

**非阶段 0 的问题分配**：

| 问题 | 分配阶段 | 原因 |
|------|---------|------|
| 问题 5 `check_tool_autonomy` | 阶段 1 | 与工具定义去重同期处理 |
| 问题 10 `convert_chat_messages` 参数 | 阶段 1 | 与 acp_session 重构同期 |
| 问题 11 ChatSession NOT NULL | 阶段 1 | 需 Alembic 迁移 |
| 问题 15 tenant_id 持久化 | IDE 插件侧 | 非后端改动 |

### 阶段 1：消除重复 + 路径安全

- `builtin_tool_definitions.py` 新增 `get_acp_tool_definitions()` 派生函数
- `tool_hooks.py` 删除 ~800 行手工定义，改为调用派生函数
- `acp_handler.py` 的 `_kind_map` / `_TOOL_CN_NAME` 移到 `acp_routes.py` 统一管理
- `tool_bridge.py`: `os.path.normpath()` → `os.path.realpath()` 修复 symlink 路径穿越（问题 HIGH, 2.4 节）

### 阶段 2：提取可观测层（详见第 8 节 — 策略 B）

**策略**：从 RuntimeToolStepService 提取 `ToolExecutionObserver`，ACP 和标准路径共用。

- 新建 `backend/app/services/agent_runtime/tool_observer.py`（~70 行）
- `tool_bridge.py:_try_ACP_execute()` 注入 `observer.record_start/record_complete`
- `tool_step_service.py` 改为调用 observer 替换内联租赁逻辑
- ACP 使用 `uuid.uuid5(NAMESPACE_OID, session_id)` 生成稳定 `run_id`
- 总改动：1 新文件 + 2 改动文件，~150 行

### 阶段 3：清理猴子补丁 + 拆分单体

- 新建 `tool_registry.py`（~50 行），消除 `caller.py` 的猴子补丁
- 提取 `AcpPathGuard` → `acp_path_guard.py`
- 提取 `AcpResultFormatter` → `acp_result_formatter.py`
- `tool_bridge.py` 从 2412 行降到 ~800 行

### 改动量估算

| 阶段 | 新增行 | 删除行 | 文件数 |
|------|--------|--------|--------|
| ✅ 阶段 0 | ~150 | 0 | 7 | 已完成 |
| ✅ 阶段 6 (acp_ 前缀) | ~40 | ~40 | 3 | 已完成 |
| 阶段 1 | ~80 | ~800 | 4 |
| 阶段 2 | ~150 | ~20 | 3 |
| 阶段 3 | ~500 | ~1600 | 6 |
| **合计** | **~920** | **~2460** | **23** |

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

### 5.5 额外新建文件依赖

以下文件在 `feature/user-api-key` 分支中不存在，需要新建：

| 文件 | 原因 |
|------|------|
| `backend/app/core/security.py` — `verify_api_key_or_token()` | 问题 1 |
| `backend/app/plugins/base.py` — `ClawithPlugin` | 问题 2 |
| `backend/app/services/llm/tool_execution_policy.py` | 问题 4 |
| `backend/app/services/llm/history_hydrate.py` — `hydrate_history_tool_results()` | 问题 9 |
| `backend/app/services/agent_runtime/tool_observer.py` | 阶段 2 |
| `backend/app/api/auth.py` — 新增 `POST /api/auth/refresh` | 问题 13 |
| `backend/app/api/ide_plugin.py` — 移植或新建 | 问题 14 |

### 5.6 注意事项

- `main.py`、`config.py`、`security.py` 和 `004_add_chat_sessions.py` 是**合并**而非覆盖——当前分支已有改动
- ACP 插件 20 文件全是新增，直接 checkout 即可
- 迁移文件需要手动检查版本链是否符合当前 `alembic/versions/` 顺序

---


## 6. ACP 工具添加 acp_ 前缀（命名空间隔离）

> ✅ **已完成**：所有 35+ 工具已重命名为 `acp_` 前缀，`acp_routes.py` / `tool_hooks.py` / `acp_handler.py` 已同步更新。

### 6.1 当前问题

ACP 工具名与基础工具名**直接冲突**：

```
read_file   → ACP 覆盖基础 read_file
write_file  → ACP 覆盖基础 write_file
edit_file   → ACP 覆盖基础 edit_file
...
```

当前通过 `ACP_OVERLAP_BASE_TOOL_NAMES` 从 LLM 工具列表中**过滤**基础版本，但工具名仍相同。这导致：
- 无法在同一次会话中同时提供 ACP 工具和非 ACP 工具
- 工具名冲突依赖隐式过滤，脆弱
- 日志/追踪中无法区分是 ACP 调用还是本地调用

### 6.2 目标命名

```
acp_read_file        — ACP IDE 读文件
acp_write_file       — ACP IDE 写文件
acp_edit_file        — ACP IDE 编辑文件
acp_delete_file      — ACP IDE 删除文件
acp_list_files       — ACP IDE 列出目录
acp_find_file        — ACP IDE 查找文件
acp_search_text      — ACP IDE 文本搜索
acp_find_class       — ACP IDE 类搜索
acp_find_symbol      — ACP IDE 符号搜索
acp_find_references  — ACP IDE 查找引用
acp_find_definition  — ACP IDE 查找定义
acp_diagnostics      — ACP IDE 诊断
acp_build_project    — ACP IDE 构建项目
acp_execute_command  — ACP IDE 终端命令
acp_git_status       — ACP IDE Git 状态
acp_git_diff         — ACP IDE Git 差异
...
```

### 6.3 需要改动的文件

| 文件 | 改动 |
|------|------|
| `acp_routes.py:7-45` | `ACP_METHOD_MAP` 的 key 全部加 `acp_` 前缀 |
| `acp_routes.py:48-52` | `ACP_TOOL_MAP` 的 key 同样加前缀 |
| `acp_routes.py:55-65` | `ACP_OVERLAP_BASE_TOOL_NAMES` **可删除** — acp_ 前缀后不再冲突 |
| `tool_hooks.py:74-883` | `_ACP_IDE_TOOLS` 每个工具的 `"name"` 加 `acp_` 前缀 |
| `tool_hooks.py:938-1017` | `_acp_aware_execute_tool` 中的路由表更新 |
| `tool_hooks.py:1028-1032` | 移除 `_ACP_OVERLAP_BASE_TOOL_NAMES` 过滤逻辑 — 不再需要 |
| `acp_handler.py:110-131` | `_kind_map` 和 `_TOOL_CN_NAME` 的 key 加 `acp_` 前缀 |
| `tool_bridge.py` | 可选：参数构建器 key 对应更新 |

### 6.4 注意事项

- IDE 插件端**不需要改动** — ACP WebSocket method 字符串（如 `fs/read_text_file`）不变，只有 LLM 可见的工具名加前缀
- 改动后 LLM 看到 `acp_read_file` 而非 `read_file`，需在 description 中说明前缀含义
- 改动后可删除 `ACP_OVERLAP_BASE_TOOL_NAMES` 和 `_ACP_IRRELEVANT_TOOL_NAMES` 过滤逻辑，架构更清晰

### 6.5 完整 ACP 工具清单（36 + 2 别名 + 2 终端 = 40）

| 当前工具名 | ACP 方法 | 新 acp_ 名称 |
|-----------|---------|-------------|
| `read_file` | `fs/read_text_file` | `acp_read_file` |
| `write_file` | `fs/write_text_file` | `acp_write_file` |
| `edit_file` | `fs/edit_text_file` | `acp_edit_file` |
| `delete_file` | `fs/safe_delete` | `acp_delete_file` |
| `list_files` | `fs/list_directory` | `acp_list_files` |
| `find_file` | `fs/find_file` | `acp_find_file` |
| `search_text` | `fs/search_text` | `acp_search_text` |
| `find_class` | `fs/find_class` | `acp_find_class` |
| `find_symbol` | `fs/find_symbol` | `acp_find_symbol` |
| `index_status` | `ide/index_status` | `acp_index_status` |
| `find_references` | `fs/find_references` | `acp_find_references` |
| `find_definition` | `fs/find_definition` | `acp_find_definition` |
| `find_implementations` | `fs/find_implementations` | `acp_find_implementations` |
| `find_super_methods` | `fs/find_super_methods` | `acp_find_super_methods` |
| `call_hierarchy` | `fs/call_hierarchy` | `acp_call_hierarchy` |
| `type_hierarchy` | `fs/type_hierarchy` | `acp_type_hierarchy` |
| `diagnostics` | `fs/diagnostics` | `acp_diagnostics` |
| `refactor_rename` | `fs/refactor_rename` | `acp_refactor_rename` |
| `move_file` | `fs/move_file` | `acp_move_file` |
| `reformat_code` | `fs/reformat_code` | `acp_reformat_code` |
| `optimize_imports` | `fs/optimize_imports` | `acp_optimize_imports` |
| `safe_delete` | `fs/safe_delete` | `acp_safe_delete` |
| `convert_java_to_kotlin` | `fs/convert_java_to_kotlin` | `acp_convert_java_to_kotlin` |
| `sync_files` | `ide/sync_files` | `acp_sync_files` |
| `active_file` | `ide/active_file` | `acp_active_file` |
| `open_file` | `ide/open_file` | `acp_open_file` |
| `file_structure` | `fs/file_structure` | `acp_file_structure` |
| `build_project` | `ide/build_project` | `acp_build_project` |
| `get_documentation` | `fs/get_documentation` | `acp_get_documentation` |
| `apply_quickfix` | `ide/apply_quickfix` | `acp_apply_quickfix` |
| `git_status` | `git/status` | `acp_git_status` |
| `git_diff` | `git/diff` | `acp_git_diff` |
| `git_stage` | `git/stage` | `acp_git_stage` |
| `git_commit` | `git/commit` | `acp_git_commit` |
| `ide_screenshot` | `ide/screenshot` | `acp_ide_screenshot` |
| `find_files` （别名→list_files） | `fs/list_directory` | `acp_find_files` |
| `search_files` （别名→search_text） | `fs/search_text` | `acp_search_files` |
| `execute_command` | 终端(blocking/streaming) | `acp_execute_command` |
| `bash` | 终端(blocking/streaming) | `acp_bash` |

**共计 40 个工具需要重命名。**

---

## 7. 命名规范统一：全用 `_ACP_` 前缀

### 7.1 当前混乱状态

```python
# ❌ _acp_ 小写前缀 — 30+ 个变量/函数
_acp_aware_execute_tool
_acp_aware_get_tools
_acp_file_exec
_acp_a2a_obs_log
_try_acp_execute
_try_acp_terminal
_try_acp_terminal_streaming
_try_acp_find_files
_try_acp_search_files
_normalize_acp_project_path
_normalize_acp_tool_args
_guard_acp_command_paths
_guard_acp_dangerous_command
_timeout_for_acp_method
current_acp_handler          # ContextVar
install_acp_tool_hooks()     # 公开函数

# ✅ _ACP_ 大写前缀 — 7 个常量（OK）
_ACP_IDE_TOOLS
_ACP_OVERLAP_BASE_TOOL_NAMES
_ACP_IRRELEVANT_TOOL_NAMES
_ACP_TOOL_MAP
_ACP_A2A_OBS_LOG
_ACP_METHOD_MAP
_ACP_PARAM_BUILDERS
```

### 7.2 统一规范

所有 ACP 内部符号统一用 `_ACP_` 大写前缀：

```python
# tool_hooks.py
_ACP_aware_execute_tool          ← _acp_aware_execute_tool
_ACP_aware_get_tools             ← _acp_aware_get_tools
_ACP_file_exec                    ← _acp_file_exec
install_ACP_tool_hooks()          ← install_acp_tool_hooks()

# acp_handler.py
_ACP_a2a_obs_log                  ← _acp_a2a_obs_log

# tool_bridge.py
_try_ACP_execute                  ← _try_acp_execute
_try_ACP_terminal                 ← _try_acp_terminal
_try_ACP_terminal_streaming       ← _try_acp_terminal_streaming
_try_ACP_find_files               ← _try_acp_find_files
_try_ACP_search_files             ← _try_acp_search_files
_normalize_ACP_project_path       ← _normalize_acp_project_path
_normalize_ACP_tool_args          ← _normalize_acp_tool_args
_guard_ACP_command_paths          ← _guard_acp_command_paths
_guard_ACP_dangerous_command      ← _guard_acp_dangerous_command
_timeout_for_ACP_method           ← _timeout_for_acp_method
current_ACP_handler               ← current_acp_handler  (ContextVar)

# ⚠️ 例外：跨模块导出的 ContextVar 本身是大写
# 但变量名保持 current_ACP_handler（大写 ACP）
```

### 7.3 改动量

| 文件 | 需改名 | 类型 |
|------|--------|------|
| `tool_hooks.py` | 5 个 | 函数/变量名 |
| `acp_handler.py` | 2 个 | 函数/变量名 + 所有引用点 |
| `tool_bridge.py` | 10 个 | 函数名 + 所有调用点 |
| `__init__.py` | 1 个 | 调用点 |
| 字符串引用（注释/日志） | ~15 处 | grep 替换 |

**注意**：所有 `.pyi` stub 文件和 IDE 插件端**不需要改** — 仅改 Python 源码。

---

## 8. ACP 适配 Runtime V2 租赁体系方案

### 8.1 业界最佳实践

LangGraph 社区处理"非标准协议桥接"场景时，通用模式是 **轻量适配器（Lightweight Adapter）**：不强制走完整 graph pipeline，而是在桥接层注入核心可观测性原语（lease、event、retry），保持协议本身的低延迟特性。

ACP 场景与标准 LangGraph 路径的核心差异：

| 差异点 | 标准路径 | ACP 路径 | 影响 |
|--------|---------|---------|------|
| 执行模型 | LangGraph StateGraph 节点 | `call_llm_with_failover` 直接调用 | ACP 无 `run_id`/`thread_id` 概念 |
| 工具传输 | 本地执行 | WebSocket JSON-RPC → IDE | ACP 工具延迟更高（5-50ms RTT） |
| 租赁回滚 | `lease_ttl` 过期自动回收 | WebSocket 双向实时，断连即失败 | ACP 超时 = IDE 不可达 |
| 事件流 | `AgentRunEvent` 写入 DB | `session/update` 推送 IDE | 语义等价但格式不同 |

**业界共识**：协议桥接层应提供与原路径**等价的可观测性**，而非强行统一执行路径。

### 8.2 当前 Clawith 架构中的集成点

```
标准路径:  graph invoke → RuntimeToolStepService.execute_pending()
              → reserve_tool_execution()  ← 租赁
              → execute_builtin_tool_outcome()  ← 执行
              → settle_tool_execution_lease()  ← 结算
              → insert AgentRunEvent  ← 事件

ACP 路径:   AcpHandler._handle_prompt → call_llm_with_failover()
              → _chained_execute_tool → _try_ACP_execute()
              → handler.send_request() → IDE WebSocket
              → 结果直接返回 caller
              ↑ 所有租赁/事件/重试跳过
```

### 8.3 适配方案：三种策略对比

| 策略 | 描述 | 改动量 | 延迟增加 | 运维收益 |
|------|------|--------|---------|---------|
| **A: 轻量注入**（已提案） | `_try_ACP_execute` 首尾调 `reserve/settle` | ~40 行 | ~10ms | 工具调用可追踪 |
| **B: 提取可观测层** | 从 RuntimeToolStepService 提取 `ToolExecutionObserver` 抽象 | ~150 行 | ~10ms | 双重用，未来扩展 |
| **C: 强制路由** | ACP 通过 graph invoke 走完整 RuntimeToolStepService | >500 行 | ~50ms | 100% 对等 |

**推荐策略 B** — 提取可观测层。原因：
- 改动适中（~150 行）
- ACP 和标准路径复用同一套 lease/event 逻辑
- 不增加 ACP 工具调用延迟
- 未来 MCP 等其他协议桥接可直接复用

### 8.4 代码方案：提取 `ToolExecutionObserver`

**新建文件**：`backend/app/services/agent_runtime/tool_observer.py`

```python
"""工具执行可观测层 — ACP 和标准路径共用的 lease/event 录入。

RuntimeToolStepService 和 ACP _try_ACP_execute 都调用此模块，
确保 AgentToolExecution 租赁和 AgentRunEvent 事件统一录入。
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_tool_execution import AgentToolExecution
from app.models.agent_run_event import AgentRunEvent


class ToolExecutionObserver:
    """工具执行观察者：记录租赁、事件、结算。"""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def record_start(
        self,
        *,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        tool_call_id: str,
        tool_name: str,
        arguments: dict,
        lease_owner: str,
        lease_ttl_seconds: int = 60,
        source: str = "acp",
    ) -> AgentToolExecution:
        """创建 AgentToolExecution 租赁记录。"""
        now = datetime.now(timezone.utc)
        record = AgentToolExecution(
            tenant_id=tenant_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            status="running",
            lease_owner=lease_owner,
            lease_expires_at=now + timedelta(seconds=lease_ttl_seconds),
            started_at=now,
            source=source,
        )
        self._db.add(record)
        await self._db.flush()
        return record

    async def record_event(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        event_type: str,
        summary: str,
        payload: dict,
    ) -> None:
        """写入 AgentRunEvent 事件。"""
        self._db.add(
            AgentRunEvent(
                run_id=run_id,
                tenant_id=tenant_id,
                event_type=event_type,
                summary=summary,
                payload=payload,
            )
        )

    async def record_complete(
        self,
        tool_call_id: str,
        *,
        status: str,
        result: str = "",
        error: str = "",
    ) -> None:
        """结算租赁：标记成功/失败。"""
        # 直接 update，避免二次 select
        from sqlalchemy import update as sql_update
        values = {"status": status, "completed_at": datetime.now(timezone.utc)}
        if result:
            values["result"] = result[:8000]  # 截断防止膨胀
        if error:
            values["error"] = error[:2000]
        await self._db.execute(
            sql_update(AgentToolExecution)
            .where(AgentToolExecution.tool_call_id == tool_call_id)
            .values(**values)
        )
```

**改动 `tool_bridge.py`**：在 `_try_ACP_execute()` 中注入 observer（~30 行）

```python
async def _try_ACP_execute(tool_name, args, handler) -> str | None:
    # ... 现有路径校验 ...
    call_id = uuid.uuid4().hex[:12]
    session_id = getattr(handler, "session_id", None)
    tenant_id = getattr(handler, "_tenant_id", None)
    run_id = uuid.uuid5(uuid.NAMESPACE_OID, session_id) if session_id else None

    # ── 可观测层注入 ──
    observer = None
    if run_id and tenant_id:
        try:
            from app.services.agent_runtime.tool_observer import ToolExecutionObserver
            from app.database import async_session
            async with async_session() as db:
                async with db.begin():
                    observer = ToolExecutionObserver(db)
                    await observer.record_start(
                        tenant_id=tenant_id, run_id=run_id,
                        tool_call_id=call_id, tool_name=tool_name,
                        arguments=args,
                        lease_owner=f"acp:{call_id}",
                    )
        except Exception:
            observer = None  # 非关键路径

    # ... 现有发送到 IDE + 等待响应 ...

    # ── 结算 ──
    if observer:
        try:
            async with async_session() as db:
                async with db.begin():
                    observer = ToolExecutionObserver(db)
                    await observer.record_complete(
                        call_id,
                        status="succeeded" if not is_error else "failed",
                        result=result or "",
                    )
        except Exception:
            pass
```

**改动 `tool_step_service.py`**：标准路径也改用 `ToolExecutionObserver`（~50 行替换）

将 `_acquire_tool_execution()` / `_settle_tool_execution_lease()` 的调用替换为 `ToolExecutionObserver.record_start/record_complete`。

### 8.5 改动评估

| 维度 | 值 |
|------|-----|
| 新文件 | 1 个（`tool_observer.py`, ~70 行） |
| 改动文件 | 2 个（`tool_bridge.py` + `tool_step_service.py`） |
| 总新增代码 | ~150 行 |
| DB 表 | 复用 `agent_tool_executions` + `agent_run_events` |
| ACP 延迟增加 | ~10ms（每次工具调用 1 次额外 DB 写入） |
| 标准路径性能影响 | 无（替换等价调用） |

### 8.6 不做的事

| 不做 | 原因 |
|------|------|
| ACP 创建 `AgentRun` | `ChatSession` 已是持久化锚点，`uuid5(session_id)` 提供稳定 `run_id` |
| ACP 走 `RuntimeEventStream` | ACP 的 `session/update` 推送等价 |
| ACP 走完整 graph pipeline | ACP 无 StateGraph 上下文，强制路由增加 >50ms 延迟且需 >500 行改动 |

---

## 9. 修复方案波及分析

> 审查：10 智能体交叉验证 — 检查对记忆/skills/MCP/A2A/自进化的影响

### 9.1 确认不受波及

| 系统 | 文件 | 原因 |
|------|------|------|
| Agent 记忆读取 (soul.md/memory.md) | `agent_context.py` | 自包含，无 ACP 导入 |
| Agent 记忆保存 (memory.md 写入) | `agent_tools.py` 走基础路径 | `write_file` 在 ACP 激活时路由到 IDE（不是 server workspace），ACP 路径下**不支持保存 memory.md**。这是预期行为——IDE 插件场景中 agent 不修改自身配置 |
| 技能系统 | `skill_seeder.py` | 启动时独立运行 |
| MCP 配置 | `config.py` | 无 ACP 依赖字段 |
| A2A | `a2a_runtime.py` | 走基础工具路径 |
| 自进化/经验学习 | `experience_retrieval.py` | `ToolExecutionObserver` 不干涉 |

**结论**：ACP 修复方案不破坏现有记忆读取/skills/MCP/A2A/自进化。ACP 会话中**不支持修改 agent 自身配置**（memory.md/soul.md），这是 IDE 插件场景的正确行为。

### 9.1b：目标 4 — 插件登录/智能体列表/租户列表

| 功能 | 状态 | 修复 |
|------|:--:|------|
| 插件登录 Clawith | ⚠️ JWT 刷新失败 | 阶段 0：新增 `POST /api/auth/refresh`（问题 13） |
| 获取智能体列表 | ❌ REST 端点 404 | 阶段 0：移植 `ide_plugin.py`（问题 14） |
| 获取公司/租户列表 | ✅ 正常工作 | 无需修改 |
| 租户切换后持久化 | ⚠️ tenant_id 未持久化 | 插件侧 `PluginSettings` 持久化（问题 15） |

**结论**：ACP 修复方案不破坏现有记忆/skills/MCP/A2A/自进化功能。

### 9.2 修复方案内的缺陷（需修正）

#### CRITICAL-1: `ToolExecutionObserver.record_start` 违反 CheckConstraint

- **文件**: 文档第 8 节代码，`status="running"`
- **问题**: `AgentToolExecution.status` 只允许 `'started'`/`'succeeded'`/`'failed'`/`'unknown'`
- **修复**: 改为 `status="started"`

#### CRITICAL-2: `ToolExecutionObserver` 缺少必填字段

- `assistant_message_id` (NOT NULL) — 未设置
- `arguments_hash` (NOT NULL) — 未设置
- `timedelta` 导入缺失
- `record_event()` 缺 `idempotency_key`
- **修复**: 补充所有必填字段

#### CRITICAL-3: `verify_api_key_or_token` 完整性问题

- 捕获所有 Exception 隐藏 DB 错误 → 改为捕获特定异常
- 无 tenant_context 注入 → 成功后注入
- 内联 import 违反编码规范 → 移到文件顶部
- `CLAWITH_API_KEY` 在 config.py 不存在 → 显式添加

#### HIGH-1: WebSocket keepalive 被 `_dispatch_prompt` 阻塞

- prompt 持续几分钟期间 `last_active` 不更新
- **修复**: 改用后台 watchdog 任务，对 `_dispatch_prompt` 本身设超时

#### HIGH-2: 缺少认证速率限制

- 无限制 token 重试 → 风险资源耗尽
- **修复**: 加 `slowapi` 速率限制器 `10/minute`

#### HIGH-3: `record_complete` 仅按 `tool_call_id` 过滤

- 高并发下可能错误覆盖另一运行中的记录
- **修复**: 同时匹配 `run_id` + `tool_call_id`

### 9.3 LangGraph 运行时缺口

> 来源：第二轮审查 — 结合 LangGraph 源码搜索

Clawith 在 LangGraph 使用上整体正确（`durability="sync"`、`PostgresSaver`、`Command(resume=...)`、per-node `retry_policy`）。以下缺口与 ACP 无直接关系但影响整体稳定性：

| 优先级 | 缺口 | 源码位置 | 建议 |
|--------|------|---------|------|
| HIGH | 无 `recursion_limit` 显式设置 | `pregel/_config.py` 默认 25 | 设 `recursion_limit=150` |
| HIGH | 无 per-node `TimeOoutPolicy` | `pregel/_retry.py:422-517` | model 节点 `run_timeout=120`, tool `idle_timeout=30` |
| HIGH | 无 `error_handler` | `errors.py:148-165` | 添加全局 error_handler |
| MEDIUM | 无 `RunControl.request_drain()` | `pregel/main.py:3012-3015` | SIGTERM → `control.request_drain()` |
| MEDIUM | checkpoint 无清理 | `checkpoint/base/__init__.py:374-415` | 定期 `aprune()` |

### 9.4 阶段顺序修正

审查发现文档的阶段顺序有误。正确顺序：

```
Phase 0:  紧急修复 (verify_api_key_or_token + base.py + keepalive)
Phase 6:  acp_ 前缀重命名（工具定义改名，此时不再冲突）
Phase 1:  消除重复（安全删除 ACP_OVERLAP_BASE_TOOL_NAMES）
Phase 2:  可观测层提取（ToolExecutionObserver）
Phase 3:  猴子补丁清理 + 拆分 tool_bridge
Phase 7:  _ACP_ 命名统一（低价值，建议推迟到功能完成后）
```

**关键依赖**：Phase 1 安全执行的前提是 Phase 6 已完成。否则删除过滤逻辑会暴露冲突的工具定义。

### 9.5 审查结论

| 维度 | 结论 |
|------|------|
| 记忆/skills/MCP/A2A/自进化 | ✅ 不受波及 |
| Docker & Compose | ⚠️ 1 CRITICAL + 6 HIGH 配置缺陷 |
| 缓存设计 | ⚠️ 1 HIGH JDK 缓存失效 + 1 MEDIUM SDK 组件缺失 |
| 修复方案代码 | ❌ 3 CRITICAL 代码缺陷需修正 |
| 修复方案设计 | ⚠️ 阶段顺序需重排，Phase 7 建议推迟 |

**下一步**：修正文档中 Phase 0 和 Phase 2 的代码示例，更新阶段顺序。

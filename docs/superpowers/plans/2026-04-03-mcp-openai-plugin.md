# MCP Plugin & OpenAI-Compatible API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将所有 MCP 服务代码整合为一个可插拔插件 `clawith_mcp`，支持全部主流 AI 编码客户端接入，并提供 OpenAI 兼容的 `/v1/chat/completions` 端点，可安装到任意 Clawith 实例。

**支持的客户端：**

| 客户端 | 协议 | 入口 |
|--------|------|------|
| Claude Code / Claude Desktop | stdio MCP | `clawith_mcp/server.py`（已有，维持不变） |
| Cursor | HTTP MCP Streamable | `POST /mcp` |
| Android Studio + Continue 插件 | HTTP MCP SSE | `GET /mcp/sse` + `POST /mcp/messages` |
| 任意 OpenAI 兼容客户端 | REST / SSE | `POST /v1/chat/completions` |

**Architecture:** 在 `backend/app/plugins/` 下建立简单的目录扫描插件系统。每个插件是一个 Python 包，包含 `plugin.json` 元数据和一个 `plugin` 实例（暴露 `register(app)` 方法）。`clawith_mcp` 是第一个插件，将 stdio 服务器、Streamable HTTP MCP、SSE MCP、OpenAI 兼容端点四合一。

**Tech Stack:** FastAPI, SQLAlchemy (async), httpx, mcp SDK, Pydantic v2

---

## 文件结构

```
backend/app/plugins/
  __init__.py                              NEW  插件加载器（目录扫描 + register）
  base.py                                  NEW  ClawithPlugin ABC

backend/app/plugins/clawith_mcp/
  __init__.py                              NEW  插件入口 + ClawithMcpPlugin 实例
  plugin.json                              NEW  插件元数据（名称/版本/路由说明）
  tools.py                                 NEW  共享工具定义 + HTTP 客户端辅助函数
  server_stdio.py                          NEW  stdio MCP 服务器（Claude Code / Claude Desktop）
  mcp_endpoint.py                          NEW  HTTP MCP 端点：
                                                  POST /mcp          ← Streamable HTTP（Cursor）
                                                  GET  /mcp/sse      ← SSE 建立连接（Continue / Android Studio）
                                                  POST /mcp/messages ← SSE 消息发送（Continue / Android Studio）
  openai_compat.py                         NEW  POST /v1/chat/completions（所有 OpenAI 兼容客户端）
  router.py                                NEW  组合上面所有路由器

backend/app/main.py                        MODIFY  末尾加 load_plugins(app)
clawith_mcp/server.py                      MODIFY  改为薄启动器，委托给 server_stdio.py

tests/plugins/test_plugin_loader.py        NEW  插件加载器测试
tests/plugins/test_mcp_endpoint.py         NEW  HTTP MCP 端点测试（Streamable + SSE）
tests/plugins/test_openai_compat.py        NEW  OpenAI 兼容端点测试
```

---

## Task 1: Plugin Base Infrastructure

**Files:**
- Create: `backend/app/plugins/base.py`
- Create: `backend/app/plugins/__init__.py`
- Test: `tests/plugins/test_plugin_loader.py`

- [ ] **Step 1: 创建 base.py（ClawithPlugin ABC）**

```python
# backend/app/plugins/base.py
from abc import ABC, abstractmethod
from fastapi import FastAPI


class ClawithPlugin(ABC):
    """所有 Clawith 插件的基类。"""
    name: str = ""
    version: str = "1.0.0"
    description: str = ""

    @abstractmethod
    def register(self, app: FastAPI) -> None:
        """向 FastAPI app 注册路由、启动钩子等。"""
        ...
```

- [ ] **Step 2: 创建插件加载器 `__init__.py`**

```python
# backend/app/plugins/__init__.py
import importlib
from pathlib import Path

from fastapi import FastAPI
from loguru import logger

_PLUGINS_DIR = Path(__file__).parent


def load_plugins(app: FastAPI) -> None:
    """扫描 plugins/ 目录，加载每个含 plugin.json 的插件。"""
    for item in sorted(_PLUGINS_DIR.iterdir()):
        if not item.is_dir() or item.name.startswith("_"):
            continue
        if not (item / "plugin.json").exists():
            continue
        try:
            module = importlib.import_module(f"app.plugins.{item.name}")
            plugin_instance = getattr(module, "plugin", None)
            if plugin_instance is None:
                logger.warning(f"[plugin] {item.name}: 未导出 'plugin' 实例，跳过")
                continue
            plugin_instance.register(app)
            logger.info(f"[plugin] 已加载: {plugin_instance.name} v{plugin_instance.version}")
        except Exception as exc:
            logger.error(f"[plugin] 加载 {item.name} 失败: {exc}")
```

- [ ] **Step 3: 写加载器测试**

```python
# tests/plugins/test_plugin_loader.py
import pytest
from fastapi import FastAPI
from app.plugins import load_plugins


def test_load_plugins_no_crash_on_empty():
    """插件目录为空时不应抛出异常。"""
    app = FastAPI()
    load_plugins(app)  # 不应 raise


def test_load_plugins_registers_clawith_mcp():
    """clawith_mcp 插件应成功注册路由。"""
    app = FastAPI()
    load_plugins(app)
    routes = [r.path for r in app.routes]
    assert "/mcp" in routes
    assert "/v1/chat/completions" in routes
```

- [ ] **Step 4: 运行测试，确认失败（插件尚未创建）**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith/backend
.venv/bin/pytest tests/plugins/test_plugin_loader.py -v 2>&1 | tail -10
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.plugins.clawith_mcp'`

- [ ] **Step 5: Commit**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith
git add backend/app/plugins/__init__.py backend/app/plugins/base.py tests/plugins/test_plugin_loader.py
git commit -m "feat(plugins): add plugin loader infrastructure"
```

---

## Task 2: Plugin Metadata & Entry Point

**Files:**
- Create: `backend/app/plugins/clawith_mcp/plugin.json`
- Create: `backend/app/plugins/clawith_mcp/__init__.py`

- [ ] **Step 1: 创建 plugin.json**

```json
{
  "name": "clawith-mcp",
  "version": "1.0.0",
  "description": "MCP server (stdio + HTTP) and OpenAI-compatible API for Clawith agents",
  "author": "Clawith",
  "min_clawith_version": "1.0.0",
  "routes": [
    {
      "path": "/mcp",
      "methods": ["POST"],
      "description": "MCP Streamable HTTP endpoint (for Cursor / Windsurf)"
    },
    {
      "path": "/v1/chat/completions",
      "methods": ["POST"],
      "description": "OpenAI-compatible chat completions"
    }
  ]
}
```

- [ ] **Step 2: 创建插件入口 `__init__.py`**

```python
# backend/app/plugins/clawith_mcp/__init__.py
from fastapi import FastAPI
from app.plugins.base import ClawithPlugin


class ClawithMcpPlugin(ClawithPlugin):
    name = "clawith-mcp"
    version = "1.0.0"
    description = "MCP server (stdio + HTTP) and OpenAI-compatible API for Clawith agents"

    def register(self, app: FastAPI) -> None:
        from app.plugins.clawith_mcp.router import router
        app.include_router(router)


plugin = ClawithMcpPlugin()
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/plugins/clawith_mcp/plugin.json backend/app/plugins/clawith_mcp/__init__.py
git commit -m "feat(plugins): add clawith_mcp plugin skeleton"
```

---

## Task 3: Shared Tool Definitions & HTTP Client Helpers

**Files:**
- Create: `backend/app/plugins/clawith_mcp/tools.py`

`tools.py` 只依赖 `httpx`，无 FastAPI/SQLAlchemy 导入，因此 stdio 服务器可在没有后端上下文时安全导入它。

- [ ] **Step 1: 创建 tools.py**

```python
# backend/app/plugins/clawith_mcp/tools.py
"""共享工具定义和 HTTP 客户端辅助函数。

由 server_stdio.py（stdio MCP 服务器）使用，通过 HTTP 调用 Clawith 后端。
mcp_endpoint.py 引用 TOOL_DEFINITIONS 作为 tools/list 响应。
"""
from __future__ import annotations
import httpx

# MCP 工具 Schema（OpenAPI inputSchema 格式）
TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "list_agents",
        "description": "列出你有权限访问的所有 Clawith 智能体",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "call_agent",
        "description": "向 Clawith 智能体发送消息，等待并返回完整回复。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "智能体 UUID，可通过 list_agents 获取",
                },
                "message": {
                    "type": "string",
                    "description": "要发送给智能体的消息内容",
                },
                "session_id": {
                    "type": "string",
                    "description": "（可选）指定会话 ID，不传则自动复用最近会话",
                },
            },
            "required": ["message"],
        },
    },
]


async def http_list_agents(client: httpx.AsyncClient) -> str:
    """通过 HTTP 调用 Clawith API 获取智能体列表，返回格式化文本。"""
    resp = await client.get("/api/agents/")
    resp.raise_for_status()
    agents = resp.json()
    if not agents:
        return "暂无可用智能体"
    lines = ["可用智能体列表：\n"]
    for a in agents:
        status = "🟢" if a.get("status") == "running" else "⚪"
        lines.append(f"{status} {a['name']}")
        lines.append(f"   ID: {a['id']}")
        if a.get("role_description"):
            lines.append(f"   职能: {a['role_description'][:80]}")
        lines.append("")
    return "\n".join(lines)


async def http_call_agent(
    client: httpx.AsyncClient,
    agent_id: str,
    message: str,
    session_id: str | None = None,
) -> str:
    """通过 HTTP 调用 Clawith 智能体，返回回复文本（含 session_id 尾注）。"""
    if not agent_id:
        raise ValueError("未指定 agent_id，请先用 list_agents 获取")
    payload: dict = {"message": message}
    if session_id:
        payload["session_id"] = session_id
    resp = await client.post(f"/api/agents/{agent_id}/chat", json=payload)
    if resp.status_code == 404:
        raise ValueError(f"智能体 {agent_id} 不存在")
    if resp.status_code == 403:
        raise ValueError("无权访问该智能体，请检查 API Key")
    resp.raise_for_status()
    data = resp.json()
    reply = data.get("reply", "")
    new_session_id = data.get("session_id", "")
    if new_session_id:
        reply += f"\n\n---\n_session_id: {new_session_id}_"
    return reply
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/plugins/clawith_mcp/tools.py
git commit -m "feat(plugins/clawith_mcp): add shared tool definitions and HTTP helpers"
```

---

## Task 4: stdio MCP Server（迁移 clawith_mcp/server.py）

**Files:**
- Create: `backend/app/plugins/clawith_mcp/server_stdio.py`
- Modify: `clawith_mcp/server.py` → 改为薄启动器

- [ ] **Step 1: 创建 server_stdio.py（从原 server.py 重构）**

```python
# backend/app/plugins/clawith_mcp/server_stdio.py
"""Stdio MCP 服务器，供 Claude Code 全局 MCP 配置使用。

用法:
    CLAWITH_URL=http://localhost:8008 CLAWITH_API_KEY=cw-xxx python server_stdio.py

Claude Code ~/.claude/settings.json 配置:
    {
      "mcpServers": {
        "clawith": {
          "command": "/opt/homebrew/bin/node",
          "args": ["...（见 clawith_mcp/server.py）"]
        }
      }
    }
"""
import asyncio
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from app.plugins.clawith_mcp.tools import (
    TOOL_DEFINITIONS,
    http_list_agents,
    http_call_agent,
)

CLAWITH_URL = os.environ.get("CLAWITH_URL", "http://localhost:8008").rstrip("/")
CLAWITH_API_KEY = os.environ.get("CLAWITH_API_KEY", "")
CLAWITH_DEFAULT_AGENT_ID = os.environ.get("CLAWITH_DEFAULT_AGENT_ID", "")

_mcp_app = Server("clawith")


@_mcp_app.list_tools()
async def _list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["inputSchema"],
        )
        for t in TOOL_DEFINITIONS
    ]


@_mcp_app.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if not CLAWITH_API_KEY:
        raise ValueError("CLAWITH_API_KEY 未设置，请在环境变量中配置")
    headers = {"X-Api-Key": CLAWITH_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(
        base_url=CLAWITH_URL, headers=headers, timeout=120.0
    ) as client:
        if name == "list_agents":
            text = await http_list_agents(client)
        elif name == "call_agent":
            agent_id = arguments.get("agent_id") or CLAWITH_DEFAULT_AGENT_ID
            text = await http_call_agent(
                client,
                agent_id=agent_id,
                message=arguments["message"],
                session_id=arguments.get("session_id"),
            )
        else:
            raise ValueError(f"未知工具: {name}")
    return [types.TextContent(type="text", text=text)]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await _mcp_app.run(
            read_stream, write_stream, _mcp_app.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 将 clawith_mcp/server.py 改为薄启动器**

```python
# clawith_mcp/server.py
"""薄启动器 — 委托给插件中的 stdio 服务器。

所有 MCP 逻辑位于:
    backend/app/plugins/clawith_mcp/server_stdio.py

用法:
    CLAWITH_URL=http://localhost:8008 CLAWITH_API_KEY=cw-xxx python server.py

Claude Code ~/.claude/settings.json:
    {
      "mcpServers": {
        "clawith": {
          "command": "/opt/homebrew/bin/node",
          "args": ["/path/to/clawith_mcp/server.py"]
        }
      }
    }
"""
import sys
import os

# 将 backend/ 加入 sys.path，使 `app.*` 导入可用
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "backend"))

from app.plugins.clawith_mcp.server_stdio import main  # noqa: E402
import asyncio

asyncio.run(main())
```

- [ ] **Step 3: 验证 stdio 服务器可独立启动**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith
CLAWITH_URL=http://localhost:8008 CLAWITH_API_KEY=test-key \
  clawith_mcp/.venv/bin/python clawith_mcp/server.py 2>&1 &
sleep 2
# 应看到进程挂起等待 stdin（无报错）
kill %1
```

Expected: 进程启动无报错，等待 stdin 输入（MCP stdio 协议正常）

- [ ] **Step 4: Commit**

```bash
git add backend/app/plugins/clawith_mcp/server_stdio.py clawith_mcp/server.py
git commit -m "feat(plugins/clawith_mcp): migrate stdio MCP server into plugin"
```

---

## Task 5: HTTP MCP Endpoint（Streamable HTTP + SSE 双协议）

MCP 有两种 HTTP 传输协议，需同时支持：

| 协议 | 端点 | 适用客户端 |
|------|------|-----------|
| **Streamable HTTP**（新） | `POST /mcp` | Cursor, 新版 Continue |
| **SSE**（旧，仍广泛使用） | `GET /mcp/sse` + `POST /mcp/messages` | 旧版 Continue, Android Studio, 部分 Claude 插件 |

SSE 传输流程：
1. 客户端 `GET /mcp/sse` → 建立 SSE 连接，服务器发送 `event: endpoint` + POST URL
2. 客户端 `POST /mcp/messages?session_id=xxx` + JSON-RPC body → 服务器处理后通过 SSE 推送响应

**Files:**
- Create: `backend/app/plugins/clawith_mcp/mcp_endpoint.py`
- Test: `tests/plugins/test_mcp_endpoint.py`

- [ ] **Step 1: 写测试（TDD）**

```python
# tests/plugins/test_mcp_endpoint.py
import pytest
from httpx import AsyncClient
from app.main import app


# ── Streamable HTTP 测试 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_initialize(auth_headers):
    """initialize 应返回正确的 protocolVersion 和 capabilities。"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            headers=auth_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "test", "version": "1.0"},
                    "capabilities": {},
                },
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 1
    assert data["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in data["result"]["capabilities"]


@pytest.mark.asyncio
async def test_mcp_tools_list(auth_headers):
    """tools/list 应返回 list_agents 和 call_agent 两个工具。"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            headers=auth_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
    assert resp.status_code == 200
    tools = resp.json()["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "list_agents" in tool_names
    assert "call_agent" in tool_names


@pytest.mark.asyncio
async def test_mcp_requires_auth():
    """未认证请求应返回 401。"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert resp.status_code == 401


# ── SSE 传输测试 ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_sse_connect_returns_endpoint(auth_headers):
    """GET /mcp/sse 应建立 SSE 连接并发送 endpoint 事件。"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        async with client.stream("GET", "/mcp/sse", headers=auth_headers) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            # 读取第一个事件
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    assert "/mcp/messages" in line  # endpoint 事件包含 POST URL
                    break


@pytest.mark.asyncio
async def test_mcp_sse_messages_tools_list(auth_headers):
    """POST /mcp/messages 应处理 tools/list 并返回 200。"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post(
            "/mcp/messages",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert resp.status_code == 200
```

- [ ] **Step 2: 运行测试，确认失败（路由尚未创建）**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith/backend
.venv/bin/pytest tests/plugins/test_mcp_endpoint.py -v 2>&1 | tail -15
```
Expected: FAIL — `404 Not Found` for `/mcp`

- [ ] **Step 3: 创建 mcp_endpoint.py**

```python
# backend/app/plugins/clawith_mcp/mcp_endpoint.py
"""HTTP MCP 端点（Streamable HTTP 传输协议）。

Cursor / Windsurf 等 MCP HTTP 客户端连接方式：
    POST http://localhost:8008/mcp
    Header: X-Api-Key: cw-xxx

Cursor ~/.cursor/mcp.json 配置示例:
    {
      "mcpServers": {
        "clawith": {
          "url": "http://localhost:8008/mcp",
          "headers": {"X-Api-Key": "cw-xxx"}
        }
      }
    }
"""
import uuid as _uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import select, func as _func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.plugins.clawith_mcp.tools import TOOL_DEFINITIONS

router = APIRouter(tags=["mcp"])


# ── JSON-RPC 2.0 帮助函数 ───────────────────────────────────────────────────

def _ok(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ── 方法分发 ────────────────────────────────────────────────────────────────

async def _dispatch(method: str, params: dict, user: User, db: AsyncSession) -> dict:
    """分发单个 JSON-RPC 方法，返回 result dict。"""
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "clawith", "version": "1.0.0"},
            "capabilities": {"tools": {}},
        }

    if method in ("notifications/initialized", "ping"):
        return {}

    if method == "tools/list":
        return {"tools": TOOL_DEFINITIONS}

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        return await _execute_tool(tool_name, arguments, user, db)

    raise ValueError(f"Unknown method: {method}")


async def _execute_tool(name: str, arguments: dict, user: User, db: AsyncSession) -> dict:
    """执行工具，返回 {content: [{type, text}]}。"""
    from app.models.agent import Agent

    if name == "list_agents":
        result = await db.execute(
            select(Agent).where(Agent.creator_id == user.id).order_by(Agent.name)
        )
        agents = result.scalars().all()
        if not agents:
            lines = ["暂无可用智能体"]
        else:
            lines = ["可用智能体列表：\n"]
            for a in agents:
                status = "🟢" if a.status == "running" else "⚪"
                lines.append(f"{status} {a.name}")
                lines.append(f"   ID: {a.id}")
                if a.role_description:
                    lines.append(f"   职能: {a.role_description[:80]}")
                lines.append("")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    if name == "call_agent":
        agent_id_raw = arguments.get("agent_id", "")
        message = arguments.get("message", "")
        session_id_raw = arguments.get("session_id")

        if not message:
            raise ValueError("message is required")

        # 解析 agent（UUID 或名称）
        from app.models.agent import Agent as AgentModel
        agent = None
        if agent_id_raw:
            try:
                aid = _uuid.UUID(str(agent_id_raw))
                r = await db.execute(select(AgentModel).where(AgentModel.id == aid))
                agent = r.scalar_one_or_none()
            except ValueError:
                r = await db.execute(
                    select(AgentModel).where(
                        _func.lower(AgentModel.name) == agent_id_raw.lower()
                    )
                )
                agent = r.scalar_one_or_none()

        if not agent:
            raise ValueError(
                f"Agent '{agent_id_raw}' not found. Use list_agents to see available agents."
            )

        # 加载 LLM 模型
        from app.models.llm import LLMModel
        model_result = await db.execute(
            select(LLMModel).where(LLMModel.id == agent.primary_model_id)
        )
        llm_model = model_result.scalar_one_or_none()
        if not llm_model or not llm_model.enabled:
            raise ValueError("Agent LLM model is unavailable")

        # 查找或创建会话
        from app.models.chat_session import ChatSession
        from app.models.audit import ChatMessage

        if session_id_raw:
            session_id_str = str(session_id_raw)
        else:
            sess_r = await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.agent_id == agent.id,
                    ChatSession.user_id == user.id,
                    ChatSession.source_channel == "web",
                )
                .order_by(
                    ChatSession.last_message_at.desc().nulls_last(),
                    ChatSession.created_at.desc(),
                )
                .limit(1)
            )
            sess = sess_r.scalar_one_or_none()
            if not sess:
                now = datetime.now(timezone.utc)
                sess = ChatSession(
                    id=_uuid.uuid4(),
                    agent_id=agent.id,
                    user_id=user.id,
                    title=f"MCP {now.strftime('%m-%d %H:%M')}",
                    source_channel="web",
                    created_at=now,
                )
                db.add(sess)
                await db.commit()
                await db.refresh(sess)
            session_id_str = str(sess.id)

        # 加载历史消息
        hist_r = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == session_id_str)
            .order_by(ChatMessage.created_at.desc())
            .limit(20)
        )
        history = list(reversed(hist_r.scalars().all()))
        messages = [
            {"role": m.role, "content": m.content or ""}
            for m in history
            if m.role in ("user", "assistant")
        ]
        messages.append({"role": "user", "content": message})

        # 保存用户消息
        db.add(ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="user",
            content=message,
            conversation_id=session_id_str,
        ))
        await db.commit()

        # 调用 LLM
        from app.api.websocket import call_llm
        reply = await call_llm(
            model=llm_model,
            messages=messages,
            agent_name=agent.name,
            role_description=agent.role_description or "",
            agent_id=str(agent.id),
            user_id=str(user.id),
            session_id=session_id_str,
        )

        # 保存回复 + 更新会话时间
        db.add(ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="assistant",
            content=reply,
            conversation_id=session_id_str,
        ))
        sess_upd = await db.execute(
            select(ChatSession).where(ChatSession.id == _uuid.UUID(session_id_str))
        )
        sess_obj = sess_upd.scalar_one_or_none()
        if sess_obj:
            sess_obj.last_message_at = datetime.now(timezone.utc)
        await db.commit()

        text = reply + f"\n\n---\n_session_id: {session_id_str}_"
        return {"content": [{"type": "text", "text": text}]}

    raise ValueError(f"Unknown tool: {name}")


# ── 路由处理器 ───────────────────────────────────────────────────────────────

@router.post("/mcp")
async def mcp_handler(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """MCP Streamable HTTP 端点。

    接受单个或批量 JSON-RPC 2.0 请求。
    兼容 Cursor、Windsurf 及任何 MCP HTTP 客户端。

    认证方式:
        X-Api-Key: cw-xxx
        Authorization: Bearer <jwt>
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    async def handle_one(req: dict) -> dict:
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}
        is_notification = req_id is None
        try:
            result = await _dispatch(method, params, current_user, db)
            if is_notification:
                return {}
            return _ok(req_id, result)
        except Exception as exc:
            logger.warning(f"[MCP] {method} error: {exc}")
            if is_notification:
                return {}
            return _err(req_id, -32603, str(exc))

    if isinstance(body, list):
        responses = [await handle_one(req) for req in body]
        responses = [r for r in responses if r]  # 过滤通知的空响应
        return JSONResponse(responses)

    response = await handle_one(body)
    if not response:
        return JSONResponse({}, status_code=202)
    return JSONResponse(response)


# ── SSE 传输（旧协议，供 Continue / Android Studio 使用）─────────────────────

import asyncio as _asyncio
from fastapi.responses import StreamingResponse as _SSEStreamingResponse

# session_id → asyncio.Queue（用于 SSE 响应推送）
_sse_sessions: dict[str, _asyncio.Queue] = {}


@router.get("/mcp/sse")
async def mcp_sse_connect(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """SSE 传输：建立长连接，发送 endpoint 事件告知客户端 POST URL。

    适用于: Android Studio + Continue 插件, 旧版 Cursor, 任何使用 SSE MCP 的客户端。

    Continue ~/.continue/config.json 配置:
        {
          "mcpServers": [
            {
              "name": "clawith",
              "transport": {
                "type": "sse",
                "url": "http://localhost:8008/mcp/sse",
                "requestOptions": {
                  "headers": {"X-Api-Key": "cw-xxx"}
                }
              }
            }
          ]
        }
    """
    session_id = str(_uuid.uuid4())
    queue: _asyncio.Queue = _asyncio.Queue()
    _sse_sessions[session_id] = queue

    # POST URL 告知客户端在哪里发消息
    post_url = f"/mcp/messages?session_id={session_id}"

    async def event_stream():
        # 发送 endpoint 事件（MCP SSE 协议规定的握手）
        yield f"event: endpoint\ndata: {post_url}\n\n"
        try:
            while True:
                # 检查客户端是否断开
                if await request.is_disconnected():
                    break
                try:
                    # 等待来自 /mcp/messages 的响应推送
                    message = await _asyncio.wait_for(queue.get(), timeout=30.0)
                    import json as _json
                    yield f"event: message\ndata: {_json.dumps(message)}\n\n"
                except _asyncio.TimeoutError:
                    # 心跳，保持连接
                    yield ": ping\n\n"
        finally:
            _sse_sessions.pop(session_id, None)

    return _SSEStreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁止 nginx 缓冲
        },
    )


@router.post("/mcp/messages")
async def mcp_sse_messages(
    request: Request,
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SSE 传输：接收客户端 JSON-RPC 消息，处理后通过 SSE 推送响应。"""
    import json as _json

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}
    is_notification = req_id is None

    try:
        result = await _dispatch(method, params, current_user, db)
        if not is_notification:
            response = _ok(req_id, result)
            # 通过 SSE 队列推送响应
            queue = _sse_sessions.get(session_id)
            if queue:
                await queue.put(response)
    except Exception as exc:
        logger.warning(f"[MCP/SSE] {method} error: {exc}")
        if not is_notification:
            response = _err(req_id, -32603, str(exc))
            queue = _sse_sessions.get(session_id)
            if queue:
                await queue.put(response)

    return JSONResponse({"status": "accepted"}, status_code=202)
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith/backend
.venv/bin/pytest tests/plugins/test_mcp_endpoint.py -v 2>&1 | tail -15
```
Expected: `test_mcp_initialize PASSED`, `test_mcp_tools_list PASSED`, `test_mcp_requires_auth PASSED`

- [ ] **Step 5: 用 curl 手动验证**

```bash
# 需要先获取一个有效的 API Key
API_KEY="cw-your-key-here"

# initialize
curl -s -X POST http://localhost:8008/mcp \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","clientInfo":{"name":"test","version":"1.0"},"capabilities":{}}}' | python3 -m json.tool

# tools/list
curl -s -X POST http://localhost:8008/mcp \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | python3 -m json.tool

# list_agents tool
curl -s -X POST http://localhost:8008/mcp \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_agents","arguments":{}}}' | python3 -m json.tool
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/plugins/clawith_mcp/mcp_endpoint.py tests/plugins/test_mcp_endpoint.py
git commit -m "feat(plugins/clawith_mcp): add HTTP MCP endpoint (Streamable HTTP)"
```

---

## Task 6: OpenAI-Compatible `/v1/chat/completions`

**Files:**
- Create: `backend/app/plugins/clawith_mcp/openai_compat.py`
- Test: `tests/plugins/test_openai_compat.py`

- [ ] **Step 1: 写测试（TDD）**

```python
# tests/plugins/test_openai_compat.py
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient
from app.main import app


@pytest.mark.asyncio
async def test_openai_compat_non_streaming_format(auth_headers, mock_agent, mock_llm_call):
    """非流式响应格式应符合 OpenAI 规范。"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": str(mock_agent.id),
                "messages": [{"role": "user", "content": "你好"}],
                "stream": False,
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert "choices" in data
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert "usage" in data


@pytest.mark.asyncio
async def test_openai_compat_model_by_name(auth_headers, mock_agent, mock_llm_call):
    """model 字段应支持智能体名称（不区分大小写）查找。"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": mock_agent.name.upper(),  # 全大写，测试不区分大小写
                "messages": [{"role": "user", "content": "测试"}],
            },
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_openai_compat_model_not_found(auth_headers):
    """model 不存在时应返回 404。"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "nonexistent-agent-xyz",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_openai_compat_streaming_format(auth_headers, mock_agent, mock_llm_call_stream):
    """流式响应应以 text/event-stream 返回 OpenAI delta 格式。"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": str(mock_agent.id),
                "messages": [{"role": "user", "content": "你好"}],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            chunks = []
            async for line in resp.aiter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    import json
                    chunk = json.loads(line[6:])
                    chunks.append(chunk)
            # 应有至少一个 chunk 和一个 finish_reason=stop 的 chunk
            assert any(
                c["choices"][0]["delta"].get("content")
                for c in chunks
            )
            assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith/backend
.venv/bin/pytest tests/plugins/test_openai_compat.py -v 2>&1 | tail -15
```
Expected: FAIL — `404 Not Found` for `/v1/chat/completions`

- [ ] **Step 3: 创建 openai_compat.py**

```python
# backend/app/plugins/clawith_mcp/openai_compat.py
"""OpenAI 兼容的 /v1/chat/completions 端点。

model 字段映射到 Clawith 智能体（UUID 或名称）。
支持流式（stream=true）和非流式响应。

Cursor 配置:
    Settings > Models > Add Model
    Base URL: http://localhost:8008
    API Key:  cw-xxx
    Model:    <智能体名称或 UUID>
"""
import asyncio
import json
import time
import uuid as _uuid
from datetime import datetime, timezone as tz_
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func as _func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User

router = APIRouter(tags=["openai-compat"])


# ── Pydantic 模型 ────────────────────────────────────────────────────────────

class OAIMessage(BaseModel):
    role: str
    content: str


class OAIChatRequest(BaseModel):
    model: str                            # 智能体 UUID 或名称
    messages: list[OAIMessage]
    stream: bool = False
    temperature: Optional[float] = None  # 接受但忽略（智能体使用自己的模型配置）
    max_tokens: Optional[int] = None     # 接受但忽略


# ── 格式化辅助函数 ────────────────────────────────────────────────────────────

def _completion_id() -> str:
    return f"chatcmpl-{_uuid.uuid4().hex[:20]}"


def _oai_response(cid: str, model: str, content: str) -> dict:
    return {
        "id": cid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _oai_chunk(cid: str, model: str, content: str, finish_reason=None) -> str:
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": content} if content else {},
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _oai_chunk_role(cid: str, model: str) -> str:
    """第一个 chunk 声明 role=assistant。"""
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": ""},
            "finish_reason": None,
        }],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


# ── 智能体解析 ───────────────────────────────────────────────────────────────

async def _resolve_agent(model: str, db: AsyncSession):
    """按 UUID 或名称（不区分大小写）查找智能体。"""
    from app.models.agent import Agent
    # 先尝试 UUID
    try:
        aid = _uuid.UUID(model)
        r = await db.execute(select(Agent).where(Agent.id == aid))
        agent = r.scalar_one_or_none()
        if agent:
            return agent
    except ValueError:
        pass
    # 再尝试名称（不区分大小写）
    r = await db.execute(
        select(Agent).where(_func.lower(Agent.name) == model.lower())
    )
    agent = r.scalar_one_or_none()
    if not agent:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{model}' not found. Use GET /api/agents/ to list available agents.",
        )
    return agent


# ── 主路由 ───────────────────────────────────────────────────────────────────

@router.post("/v1/chat/completions")
async def openai_chat_completions(
    body: OAIChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """OpenAI 兼容的聊天补全端点。

    将 model 字段映射到 Clawith 智能体（UUID 或名称）。
    支持 stream=true（SSE delta 格式）和 stream=false（完整 JSON）。

    认证:
        X-Api-Key: cw-xxx
        Authorization: Bearer <jwt>
    """
    from app.models.llm import LLMModel
    from app.models.chat_session import ChatSession
    from app.models.audit import ChatMessage
    from app.core.permissions import check_agent_access, is_agent_expired
    from app.api.websocket import call_llm

    agent = await _resolve_agent(body.model, db)
    await check_agent_access(db, current_user, agent.id)  # 权限检查，失败时抛 HTTPException

    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired")
    if not agent.primary_model_id:
        raise HTTPException(status_code=400, detail="Agent has no LLM model configured")

    model_result = await db.execute(
        select(LLMModel).where(LLMModel.id == agent.primary_model_id)
    )
    llm_model = model_result.scalar_one_or_none()
    if not llm_model or not llm_model.enabled:
        raise HTTPException(status_code=400, detail="Agent LLM model is unavailable")

    # 将 OAI messages 转为 dict 列表（call_llm 直接接受 OpenAI 格式）
    messages = [{"role": m.role, "content": m.content} for m in body.messages]

    # 查找或创建会话
    sess_r = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.agent_id == agent.id,
            ChatSession.user_id == current_user.id,
            ChatSession.source_channel == "web",
        )
        .order_by(
            ChatSession.last_message_at.desc().nulls_last(),
            ChatSession.created_at.desc(),
        )
        .limit(1)
    )
    sess = sess_r.scalar_one_or_none()
    if not sess:
        now = datetime.now(tz_.utc)
        sess = ChatSession(
            id=_uuid.uuid4(),
            agent_id=agent.id,
            user_id=current_user.id,
            title=f"OAI {now.strftime('%m-%d %H:%M')}",
            source_channel="web",
            created_at=now,
        )
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
    session_id_str = str(sess.id)

    # 保存最后一条用户消息
    last_user_content = next(
        (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
    )
    db.add(ChatMessage(
        agent_id=agent.id,
        user_id=current_user.id,
        role="user",
        content=last_user_content,
        conversation_id=session_id_str,
    ))
    await db.commit()

    cid = _completion_id()

    # ── 非流式 ─────────────────────────────────────────────────────────────
    if not body.stream:
        reply = await call_llm(
            model=llm_model,
            messages=messages,
            agent_name=agent.name,
            role_description=agent.role_description or "",
            agent_id=str(agent.id),
            user_id=str(current_user.id),
            session_id=session_id_str,
        )
        db.add(ChatMessage(
            agent_id=agent.id,
            user_id=current_user.id,
            role="assistant",
            content=reply,
            conversation_id=session_id_str,
        ))
        sess.last_message_at = datetime.now(tz_.utc)
        await db.commit()
        return _oai_response(cid, body.model, reply)

    # ── 流式（SSE） ─────────────────────────────────────────────────────────
    queue: asyncio.Queue = asyncio.Queue()

    async def on_chunk(text: str) -> None:
        await queue.put(("chunk", text))

    async def run_llm() -> None:
        try:
            reply = await call_llm(
                model=llm_model,
                messages=messages,
                agent_name=agent.name,
                role_description=agent.role_description or "",
                agent_id=str(agent.id),
                user_id=str(current_user.id),
                session_id=session_id_str,
                on_chunk=on_chunk,
            )
            await queue.put(("done", reply))
        except Exception as exc:
            await queue.put(("error", str(exc)))

    asyncio.create_task(run_llm())

    async def event_stream():
        yield _oai_chunk_role(cid, body.model)   # 第一个 chunk：声明 role
        full_reply = ""
        while True:
            kind, payload = await queue.get()
            if kind == "chunk":
                full_reply += payload
                yield _oai_chunk(cid, body.model, payload)
            elif kind == "done":
                if not full_reply:
                    full_reply = payload
                # 持久化
                from app.database import get_db as _get_db
                async for _db in _get_db():
                    _db.add(ChatMessage(
                        agent_id=agent.id,
                        user_id=current_user.id,
                        role="assistant",
                        content=full_reply,
                        conversation_id=session_id_str,
                    ))
                    s = await _db.execute(
                        select(ChatSession).where(ChatSession.id == _uuid.UUID(session_id_str))
                    )
                    s_obj = s.scalar_one_or_none()
                    if s_obj:
                        s_obj.last_message_at = datetime.now(tz_.utc)
                    await _db.commit()
                    break
                yield _oai_chunk(cid, body.model, "", finish_reason="stop")
                yield "data: [DONE]\n\n"
                break
            elif kind == "error":
                yield f"data: {json.dumps({'error': {'message': payload}})}\n\n"
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith/backend
.venv/bin/pytest tests/plugins/test_openai_compat.py -v 2>&1 | tail -20
```
Expected: 4 tests PASSED

- [ ] **Step 5: 用 curl 手动验证**

```bash
API_KEY="cw-your-key-here"
AGENT="your-agent-name"

# 非流式
curl -s -X POST http://localhost:8008/v1/chat/completions \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$AGENT\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}],\"stream\":false}" \
  | python3 -m json.tool

# 流式
curl -N -X POST http://localhost:8008/v1/chat/completions \
  -H "X-Api-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$AGENT\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}],\"stream\":true}"
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/plugins/clawith_mcp/openai_compat.py tests/plugins/test_openai_compat.py
git commit -m "feat(plugins/clawith_mcp): add OpenAI-compatible /v1/chat/completions"
```

---

## Task 7: Plugin Router

**Files:**
- Create: `backend/app/plugins/clawith_mcp/router.py`

- [ ] **Step 1: 创建组合路由器**

```python
# backend/app/plugins/clawith_mcp/router.py
"""组合 MCP 和 OpenAI 兼容路由器。"""
from fastapi import APIRouter

from app.plugins.clawith_mcp.mcp_endpoint import router as mcp_router
from app.plugins.clawith_mcp.openai_compat import router as openai_router

router = APIRouter()
router.include_router(mcp_router)
router.include_router(openai_router)
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/plugins/clawith_mcp/router.py
git commit -m "feat(plugins/clawith_mcp): add combined plugin router"
```

---

## Task 8: 接入 main.py

**Files:**
- Modify: `backend/app/main.py`

- [ ] **Step 1: 在 main.py 末尾 include_router 之后加 load_plugins**

在 `backend/app/main.py` 中，找到最后一个 `app.include_router(...)` 调用之后，添加：

```python
# 加载插件（放在所有 include_router 之后）
from app.plugins import load_plugins
load_plugins(app)
```

- [ ] **Step 2: 重启后端，确认插件加载日志**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith && bash restart.sh 2>&1 | tail -5
```

查看日志：
```bash
tail -20 /Users/shubinzhang/Documents/UGit/Clawith/.data/log/backend.log | grep plugin
```
Expected: `[plugin] 已加载: clawith-mcp v1.0.0`

- [ ] **Step 3: 确认两个端点已注册**

```bash
curl -s http://localhost:8008/openapi.json | python3 -c "
import json,sys
paths = json.load(sys.stdin)['paths']
for p in ['/mcp', '/v1/chat/completions']:
    print(p, '✅' if p in paths else '❌')
"
```
Expected:
```
/mcp ✅
/v1/chat/completions ✅
```

- [ ] **Step 4: 运行全部插件测试**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith/backend
.venv/bin/pytest tests/plugins/ -v 2>&1 | tail -20
```
Expected: 全部 PASSED

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py
git commit -m "feat(main): load plugins on startup"
```

---

## Task 9: 端到端验证 + 全客户端配置

- [ ] **Step 1: 获取 API Key**

登录 Clawith，进入「个人设置」→「API Key」，生成并复制 `cw-xxx`。

- [ ] **Step 2: 验证 MCP 端点（curl 冒烟测试）**

```bash
API_KEY="cw-your-key-here"

# Streamable HTTP: initialize
curl -s -X POST http://localhost:8008/mcp \
  -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","clientInfo":{"name":"test","version":"1.0"},"capabilities":{}}}' \
  | python3 -m json.tool

# Streamable HTTP: tools/list
curl -s -X POST http://localhost:8008/mcp \
  -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | python3 -m json.tool

# SSE: 连接建立（应看到 event: endpoint 事件）
curl -N http://localhost:8008/mcp/sse -H "X-Api-Key: $API_KEY" &
SSE_PID=$!
sleep 2 && kill $SSE_PID
```

Expected: `initialize` 返回 `protocolVersion: "2024-11-05"`；SSE 连接收到 `event: endpoint`。

- [ ] **Step 3: 配置 Claude Code（stdio MCP）**

编辑 `~/.claude/settings.json`：
```json
{
  "mcpServers": {
    "clawith": {
      "command": "/opt/homebrew/bin/node",
      "args": ["/Users/shubinzhang/Documents/UGit/Clawith/clawith_mcp/server.py"],
      "env": {
        "CLAWITH_URL": "http://localhost:8008",
        "CLAWITH_API_KEY": "cw-your-key-here"
      }
    }
  }
}
```
重启 Claude Code，运行 `/mcp` 命令，确认出现 `list_agents` 和 `call_agent`。

- [ ] **Step 4: 配置 Claude Desktop（stdio MCP）**

编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`：
```json
{
  "mcpServers": {
    "clawith": {
      "command": "python3",
      "args": ["/Users/shubinzhang/Documents/UGit/Clawith/clawith_mcp/server.py"],
      "env": {
        "CLAWITH_URL": "http://localhost:8008",
        "CLAWITH_API_KEY": "cw-your-key-here"
      }
    }
  }
}
```
重启 Claude Desktop，在对话框输入 `/` 确认工具出现。

- [ ] **Step 5: 配置 Cursor（Streamable HTTP MCP）**

编辑 `~/.cursor/mcp.json`：
```json
{
  "mcpServers": {
    "clawith": {
      "url": "http://localhost:8008/mcp",
      "headers": {
        "X-Api-Key": "cw-your-key-here"
      }
    }
  }
}
```
重启 Cursor，检查 MCP 面板是否出现 `list_agents` 和 `call_agent`。

- [ ] **Step 6: 配置 Android Studio + Continue 插件（SSE MCP）**

安装 Continue 插件：Android Studio → Plugins → 搜索 `Continue` → Install。

编辑 `~/.continue/config.json`，在 `mcpServers` 数组中添加：
```json
{
  "mcpServers": [
    {
      "name": "clawith",
      "transport": {
        "type": "sse",
        "url": "http://localhost:8008/mcp/sse",
        "requestOptions": {
          "headers": {
            "X-Api-Key": "cw-your-key-here"
          }
        }
      }
    }
  ]
}
```
重启 Android Studio，在 Continue 侧边栏选择 `@clawith` 确认工具列表出现。

- [ ] **Step 7: 配置各客户端使用 OpenAI 兼容接口**

**Cursor（自定义模型）：**
Settings → Models → Add Model：
- Base URL: `http://localhost:8008`
- API Key: `cw-your-key-here`
- Model: `<智能体名称或 UUID>`

**Continue 插件（自定义 LLM Provider）：**
编辑 `~/.continue/config.json`：
```json
{
  "models": [
    {
      "title": "Clawith - 我的智能体",
      "provider": "openai",
      "model": "<智能体名称>",
      "apiBase": "http://localhost:8008",
      "apiKey": "cw-your-key-here"
    }
  ]
}
```

**Android Studio AI Assistant（JetBrains）：**
Settings → Tools → AI Assistant → Custom Model：
- Endpoint: `http://localhost:8008/v1/chat/completions`
- API Key: `cw-your-key-here`
- Model: `<智能体名称>`

- [ ] **Step 8: 最终 Commit + Push**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith
git add -A
git commit -m "feat: complete MCP plugin + OpenAI-compatible API (Cursor, Android Studio, Claude)"
git push origin feature/user-api-key
```

---

## 插件安装指南（安装到其他 Clawith 实例）

完成上述步骤后，将插件安装到其他 Clawith 实例只需 3 步：

```bash
# 1. 复制插件目录到目标实例
cp -r backend/app/plugins/clawith_mcp /path/to/other-clawith/backend/app/plugins/

# 2. 确保目标实例的 main.py 有加载插件的代码
#    在 include_router 之后添加：
#    from app.plugins import load_plugins
#    load_plugins(app)

# 3. 重启目标实例
cd /path/to/other-clawith && bash restart.sh
```

无需数据库迁移，无需修改任何现有代码。

---

## 自检

| 需求 | 对应 Task | 状态 |
|------|----------|------|
| MCP 代码全部在一处 | Task 3-4（tools.py + server_stdio.py）| ✅ 全部在 `plugins/clawith_mcp/` |
| 可安装到其他版本 | Task 1-2（插件加载器 + plugin.json）| ✅ 复制目录即安装 |
| MCP 改为可插拔插件 | Task 1-8 | ✅ 完整插件架构 |
| OpenAI-compatible URL | Task 6（openai_compat.py）| ✅ `/v1/chat/completions` 含流式 |
| 保持 stdio MCP 向后兼容 | Task 4（server_stdio.py）| ✅ clawith_mcp/server.py 仍可用 |
| **Claude Code / Claude Desktop** | Task 4 + Task 9 Step 3-4 | ✅ stdio MCP |
| **Cursor** | Task 5 + Task 9 Step 5 | ✅ Streamable HTTP `/mcp` |
| **Android Studio + Continue** | Task 5 SSE + Task 9 Step 6-7 | ✅ SSE `/mcp/sse` + OpenAI compat |
| **任意 OpenAI 客户端** | Task 6 + Task 9 Step 7 | ✅ `/v1/chat/completions` |

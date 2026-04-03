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

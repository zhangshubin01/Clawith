# backend/tests/plugins/test_tools.py
import pytest
import httpx
import respx

from app.plugins.clawith_mcp.tools import (
    TOOL_DEFINITIONS,
    http_list_agents,
    http_call_agent,
)


def test_tool_definitions_structure():
    """TOOL_DEFINITIONS must have list_agents and call_agent with required fields."""
    names = [t["name"] for t in TOOL_DEFINITIONS]
    assert "list_agents" in names
    assert "call_agent" in names
    for tool in TOOL_DEFINITIONS:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
    call_agent = next(t for t in TOOL_DEFINITIONS if t["name"] == "call_agent")
    assert "message" in call_agent["inputSchema"]["required"]


@pytest.mark.asyncio
@respx.mock
async def test_http_list_agents_formats_output():
    """http_list_agents should return formatted agent list."""
    respx.get("http://test/api/agents/").mock(
        return_value=httpx.Response(200, json=[
            {"name": "Alice", "id": "abc-123", "role_description": "助手", "status": "running"},
            {"name": "Bob", "id": "def-456", "role_description": "", "status": "idle"},
        ])
    )
    async with httpx.AsyncClient(base_url="http://test") as client:
        result = await http_list_agents(client)
    assert "Alice" in result
    assert "abc-123" in result
    assert "🟢" in result  # running status
    assert "⚪" in result  # idle status


@pytest.mark.asyncio
@respx.mock
async def test_http_list_agents_empty():
    """http_list_agents should handle empty list gracefully."""
    respx.get("http://test/api/agents/").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient(base_url="http://test") as client:
        result = await http_list_agents(client)
    assert "暂无" in result


@pytest.mark.asyncio
@respx.mock
async def test_http_call_agent_success():
    """http_call_agent should return reply with session_id appended."""
    respx.post("http://test/api/agents/agent-1/chat").mock(
        return_value=httpx.Response(200, json={"reply": "Hello!", "session_id": "sess-1"})
    )
    async with httpx.AsyncClient(base_url="http://test") as client:
        result = await http_call_agent(client, "agent-1", "Hi", None)
    assert "Hello!" in result
    assert "sess-1" in result


@pytest.mark.asyncio
@respx.mock
async def test_http_call_agent_404():
    """http_call_agent should raise ValueError on 404."""
    respx.post("http://test/api/agents/bad-id/chat").mock(
        return_value=httpx.Response(404)
    )
    async with httpx.AsyncClient(base_url="http://test") as client:
        with pytest.raises(ValueError, match="不存在"):
            await http_call_agent(client, "bad-id", "Hi", None)


@pytest.mark.asyncio
async def test_http_call_agent_no_agent_id():
    """http_call_agent should raise ValueError when agent_id is empty."""
    async with httpx.AsyncClient(base_url="http://test") as client:
        with pytest.raises(ValueError, match="未指定"):
            await http_call_agent(client, "", "Hi", None)

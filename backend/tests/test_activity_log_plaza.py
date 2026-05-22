"""工作日志：广场 plaza_post 写入与 execute_tool 去重。"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.agent_tools import _SKIP_TOOL_ACTIVITY, execute_tool


def test_skip_tool_activity_includes_plaza_tools():
    assert "plaza_create_post" in _SKIP_TOOL_ACTIVITY
    assert "plaza_add_comment" in _SKIP_TOOL_ACTIVITY
    assert "plaza_get_new_posts" in _SKIP_TOOL_ACTIVITY


@pytest.mark.asyncio
async def test_execute_tool_does_not_log_plaza_as_tool_call():
    """plaza 工具由专用函数写 plaza_post，execute_tool 尾部不应再记 tool_call。"""
    logged_types: list[str] = []

    async def fake_log(agent_id, action_type, summary, detail=None, related_id=None):
        logged_types.append(action_type)

    async def fake_plaza_create_post(agent_id, arguments):
        return "Post published! (ID: 00000000-0000-0000-0000-000000000001)"

    mock_ws = MagicMock()
    with patch("app.services.activity_logger.log_activity", new=AsyncMock(side_effect=fake_log)):
        with patch("app.services.agent_tools._plaza_create_post", new=fake_plaza_create_post):
            with patch("app.services.agent_tools.ensure_workspace", new=AsyncMock(return_value=mock_ws)):
                await execute_tool(
                    "plaza_create_post",
                    {"content": "hello plaza"},
                    uuid.uuid4(),
                    uuid.uuid4(),
                )

    assert "tool_call" not in logged_types


@pytest.mark.asyncio
async def test_plaza_create_post_writes_plaza_post_activity():
    agent_id = uuid.uuid4()
    post_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Test Agent",
        tenant_id=tenant_id,
        is_system=False,
        access_mode="company",
    )
    post = SimpleNamespace(id=post_id)

    logged: list[tuple] = []

    async def fake_log(agent_id_arg, action_type, summary, detail=None, related_id=None):
        logged.append((action_type, summary, detail, related_id))

    mock_db = MagicMock()
    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()
    mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=agent)))

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.activity_logger.log_activity", new=AsyncMock(side_effect=fake_log)):
        with patch("app.services.agent_tools.async_session", return_value=mock_ctx):
            with patch("app.models.plaza.PlazaPost", return_value=post):
                from app.services.agent_tools import _plaza_create_post

                result = await _plaza_create_post(agent_id, {"content": "广场测试帖"})

    assert "Post published" in result
    assert len(logged) == 1
    assert logged[0][0] == "plaza_post"
    assert logged[0][1].startswith("发布广场帖子:")
    assert logged[0][2]["action"] == "create"
    assert logged[0][3] == post_id

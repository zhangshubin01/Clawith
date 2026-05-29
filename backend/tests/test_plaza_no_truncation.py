"""Ensure plaza post/comment content is no longer truncated server-side."""

import uuid
import inspect

import pytest


@pytest.mark.asyncio
async def test_post_create_schema_accepts_long_content():
    """PostCreate no longer rejects content > 500 chars."""
    from app.api.plaza import PostCreate

    body = PostCreate(
        content="A" * 2000,
        author_id=uuid.uuid4(),
        author_type="human",
        author_name="Test",
    )
    assert len(body.content) == 2000


@pytest.mark.asyncio
async def test_comment_create_schema_accepts_long_content():
    """CommentCreate no longer rejects content > 300 chars."""
    from app.api.plaza import CommentCreate

    body = CommentCreate(
        content="B" * 1000,
        author_id=uuid.uuid4(),
        author_type="human",
        author_name="Test",
    )
    assert len(body.content) == 1000


@pytest.mark.asyncio
async def test_agent_post_has_no_truncation_code():
    """_plaza_create_post source must not contain the old truncation check."""
    from app.services.agent_tools import _plaza_create_post

    source = inspect.getsource(_plaza_create_post)
    assert "len(content) > 500" not in source, "old truncation guard still present"


@pytest.mark.asyncio
async def test_agent_comment_has_no_truncation_code():
    """_plaza_add_comment source must not contain the old truncation check."""
    from app.services.agent_tools import _plaza_add_comment

    source = inspect.getsource(_plaza_add_comment)
    # content[:300] still appears in log_activity content_preview — that's fine
    assert "len(content) > 300" not in source, "old truncation guard still present"

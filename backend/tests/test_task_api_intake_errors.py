"""Task 创建端点：Runtime intake 配置缺失 → 400 + error_code，真实错误仍 500。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from fastapi import HTTPException

from app.api.tasks import create_task
from app.services.task_executor import TaskRuntimeIntakeError


def _task_data() -> SimpleNamespace:
    return SimpleNamespace(
        title="Prepare report",
        description="Use workspace evidence",
        type="todo",
        priority="medium",
        due_date=None,
        supervision_target_name=None,
        supervision_channel=None,
        remind_schedule=None,
    )


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass


@pytest.mark.asyncio
async def test_model_missing_intake_error_becomes_400_with_actionable_message():
    db = _Session()
    user = SimpleNamespace(id=uuid.uuid4())
    resolved_agent = SimpleNamespace(id=uuid.uuid4())

    with (
        patch(
            "app.api.tasks.check_agent_access",
            new=AsyncMock(return_value=(resolved_agent, "manage")),
        ),
        patch(
            "app.services.task_executor.enqueue_task_runtime",
            new=AsyncMock(
                side_effect=TaskRuntimeIntakeError(
                    "agent_model_missing",
                    "Runtime Task Agent has no configured primary model",
                )
            ),
        ),
        patch("app.api.tasks._enrich_task_out", new=AsyncMock(return_value="task-response")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await create_task(
                agent_id=resolved_agent.id,
                data=_task_data(),
                current_user=user,
                db=db,  # type: ignore[arg-type]
            )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "agent_model_missing"
    assert "模型" in exc_info.value.detail["message"]


@pytest.mark.asyncio
async def test_unexpected_runtime_error_still_propagates_as_internal():
    db = _Session()
    user = SimpleNamespace(id=uuid.uuid4())
    resolved_agent = SimpleNamespace(id=uuid.uuid4())

    with (
        patch(
            "app.api.tasks.check_agent_access",
            new=AsyncMock(return_value=(resolved_agent, "manage")),
        ),
        patch(
            "app.services.task_executor.enqueue_task_runtime",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("app.api.tasks._enrich_task_out", new=AsyncMock(return_value="task-response")),
        patch("asyncio.create_task", new=MagicMock()),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await create_task(
                agent_id=resolved_agent.id,
                data=_task_data(),
                current_user=user,
                db=db,  # type: ignore[arg-type]
            )

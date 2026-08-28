"""First-party native scores at a Run's terminal settle (ticket 03).

只测外部行为：结算时向 Langfuse client 写出哪些 score 事实（名称/取值/
类型/trace 归属），以及 observability disabled / 无 trace 上下文时的 no-op
安全。不测 SDK 内部机制。
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest

from app.services.agent_runtime.command_worker import (
    CheckpointObservation,
    RuntimeCommandRecord,
    RuntimeRunRecord,
)
from app.services.observability import scores

_TRACE_ID = "0123456789abcdef0123456789abcdef"
_UNSET = object()


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _FakeDb:
    """Dispatch on the compiled statement text (same spirit as the side-effects fake)."""

    def __init__(
        self,
        *,
        attempt_count: object = None,
        same_goal_run_id: object = None,
        prior_run_id: object = None,
    ) -> None:
        self.attempt_count = attempt_count
        self.same_goal_run_id = same_goal_run_id
        self.prior_run_id = prior_run_id
        self.statements: list[str] = []

    async def execute(self, statement: Any) -> _ScalarResult:
        text = str(statement)
        self.statements.append(text)
        if "FROM agent_run_commands" in text:
            return _ScalarResult(self.attempt_count)
        if "FROM agent_run_events" in text:
            return _ScalarResult(self.same_goal_run_id if "agent_runs.goal" in text else self.prior_run_id)
        raise AssertionError(f"unexpected statement: {text}")


class _FakeClient:
    def __init__(self) -> None:
        self.scores: list[dict[str, Any]] = []

    def create_score(self, **kwargs: Any) -> None:
        self.scores.append(kwargs)


def _run(
    *,
    goal: str = "answer",
    session_id: str | None = None,
) -> RuntimeRunRecord:
    tenant_id = uuid.uuid4()
    return RuntimeRunRecord(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        thread_id=str(uuid.uuid4()),
        runtime_type="langgraph",
        goal=goal,
        run_kind="foreground",
        source_type="chat",
        model_id=str(uuid.uuid4()),
        graph_name="runtime_graph",
        graph_version="v1",
        agent_id=str(uuid.uuid4()),
        session_id=session_id,
        system_role=None,
    )


def _command(
    run: RuntimeRunRecord,
    *,
    command_type: str = "start",
    payload: dict | None = None,
    actor_user_id: object = _UNSET,
    attempt_count: int = 1,
) -> RuntimeCommandRecord:
    return RuntimeCommandRecord(
        id=uuid.uuid4(),
        tenant_id=run.tenant_id,
        run_id=run.run_id,
        command_type=command_type,  # type: ignore[arg-type]
        payload=payload or {},
        actor_user_id=uuid.uuid4() if actor_user_id is _UNSET else cast("uuid.UUID | None", actor_user_id),
        actor_agent_id=None,
        attempt_count=attempt_count,
    )


def _checkpoint(run: RuntimeRunRecord, *, trace_id: str | None = _TRACE_ID) -> CheckpointObservation:
    return CheckpointObservation(
        checkpoint_id="checkpoint-1",
        state={},  # scores only read checkpoint metadata
        metadata={
            "clawith_run_id": str(run.run_id),
            "clawith_trace_id": trace_id,
        },
    )


def _score_names(client: _FakeClient) -> list[str]:
    return [score["name"] for score in client.scores]


def _score_by_name(client: _FakeClient, name: str) -> dict[str, Any] | None:
    return next((score for score in client.scores if score["name"] == name), None)


async def _record(
    monkeypatch: pytest.MonkeyPatch,
    *,
    db: _FakeDb,
    run: RuntimeRunRecord,
    command: RuntimeCommandRecord,
    checkpoint: CheckpointObservation,
    status: str,
    client: _FakeClient,
) -> None:
    monkeypatch.setattr(scores, "_get_client", lambda _tenant_id=None: client)
    await scores.record_terminal_scores(
        db,  # type: ignore[arg-type]
        run=run,
        command=command,
        checkpoint=checkpoint,
        status=status,
    )


@pytest.mark.asyncio
async def test_noop_without_trace_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """checkpoint 未携带 Langfuse trace id 时彻底 no-op——不建 client、不查库。"""
    run = _run()
    command = _command(run)
    checkpoint = _checkpoint(run, trace_id=None)
    db = _FakeDb()
    getter_calls: list[object] = []
    monkeypatch.setattr(
        scores,
        "_get_client",
        lambda _tenant_id=None: getter_calls.append(_tenant_id) or None,
    )

    await scores.record_terminal_scores(
        db,  # type: ignore[arg-type]
        run=run,
        command=command,
        checkpoint=checkpoint,
        status="completed",
    )

    assert getter_calls == []
    assert db.statements == []


@pytest.mark.asyncio
async def test_noop_when_observability_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run()
    command = _command(run)
    checkpoint = _checkpoint(run)
    db = _FakeDb()
    monkeypatch.setattr(scores, "_get_client", lambda _tenant_id=None: None)

    await scores.record_terminal_scores(
        db,  # type: ignore[arg-type]
        run=run,
        command=command,
        checkpoint=checkpoint,
        status="completed",
    )

    assert db.statements == []  # client 不存在时不做信号查询


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("completed", "succeeded"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ],
)
async def test_writes_run_outcome_and_attempt_count(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    outcome: str,
) -> None:
    run = _run()
    command = _command(run, attempt_count=3)
    checkpoint = _checkpoint(run)
    db = _FakeDb(attempt_count=2)
    client = _FakeClient()

    await _record(
        monkeypatch,
        db=db,
        run=run,
        command=command,
        checkpoint=checkpoint,
        status=status,
        client=client,
    )

    outcome_score = _score_by_name(client, "run_outcome")
    assert outcome_score is not None
    assert outcome_score["value"] == outcome
    assert outcome_score["data_type"] == "CATEGORICAL"
    assert outcome_score["trace_id"] == _TRACE_ID
    assert outcome_score["score_id"] == f"{run.run_id}:run_outcome"

    attempt_score = _score_by_name(client, "attempt_count")
    assert attempt_score is not None
    assert attempt_score["value"] == 2  # 结算时点 DB 现值（优先于内存快照）
    assert attempt_score["data_type"] == "NUMERIC"
    assert attempt_score["trace_id"] == _TRACE_ID
    assert attempt_score["score_id"] == f"{run.run_id}:attempt_count"

    assert "implicit_negative" not in _score_names(client)


@pytest.mark.asyncio
async def test_attempt_count_falls_back_to_command_record(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run()
    command = _command(run, attempt_count=4)
    checkpoint = _checkpoint(run)
    db = _FakeDb(attempt_count=None)  # 命令行不可见（异常态）→ 回退内存快照
    client = _FakeClient()

    await _record(
        monkeypatch,
        db=db,
        run=run,
        command=command,
        checkpoint=checkpoint,
        status="completed",
        client=client,
    )

    attempt_score = _score_by_name(client, "attempt_count")
    assert attempt_score is not None
    assert attempt_score["value"] == 4


@pytest.mark.asyncio
async def test_cancel_command_emits_explicit_cancel_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(session_id=str(uuid.uuid4()))
    command = _command(run, command_type="cancel", payload={"reason": "user_abort"})
    checkpoint = _checkpoint(run)
    client = _FakeClient()

    await _record(
        monkeypatch,
        db=_FakeDb(same_goal_run_id=uuid.uuid4()),  # 即使历史存在同 goal，cancel 优先
        run=run,
        command=command,
        checkpoint=checkpoint,
        status="cancelled",
        client=client,
    )

    negative = _score_by_name(client, "implicit_negative")
    assert negative is not None
    assert negative["value"] == "explicit_cancel"
    assert negative["data_type"] == "CATEGORICAL"
    assert negative["trace_id"] == _TRACE_ID


@pytest.mark.asyncio
async def test_same_goal_retry_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(goal="部署生产环境", session_id=str(uuid.uuid4()))
    command = _command(run)
    checkpoint = _checkpoint(run)
    client = _FakeClient()

    await _record(
        monkeypatch,
        db=_FakeDb(same_goal_run_id=uuid.uuid4()),
        run=run,
        command=command,
        checkpoint=checkpoint,
        status="completed",
        client=client,
    )

    negative = _score_by_name(client, "implicit_negative")
    assert negative is not None
    assert negative["value"] == "same_goal_retry"


@pytest.mark.asyncio
async def test_negation_retry_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(goal="不对，重新做一遍，把端口改成 8080", session_id=str(uuid.uuid4()))
    command = _command(run)
    checkpoint = _checkpoint(run)
    client = _FakeClient()

    await _record(
        monkeypatch,
        db=_FakeDb(prior_run_id=uuid.uuid4()),
        run=run,
        command=command,
        checkpoint=checkpoint,
        status="completed",
        client=client,
    )

    negative = _score_by_name(client, "implicit_negative")
    assert negative is not None
    assert negative["value"] == "negation_retry"


@pytest.mark.asyncio
async def test_resume_with_correcting_user_text_emits_negation_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(session_id=str(uuid.uuid4()))
    command = _command(
        run,
        command_type="resume",
        payload={"resume_type": "user_input", "payload": {"content": "错了，重试一次"}},
    )
    checkpoint = _checkpoint(run)
    client = _FakeClient()

    await _record(
        monkeypatch,
        db=_FakeDb(prior_run_id=uuid.uuid4()),
        run=run,
        command=command,
        checkpoint=checkpoint,
        status="completed",
        client=client,
    )

    negative = _score_by_name(client, "implicit_negative")
    assert negative is not None
    assert negative["value"] == "negation_retry"


@pytest.mark.asyncio
async def test_neutral_continuation_emits_no_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    """中性继续对话（不同 goal、无否定词）不算负反馈——窄口径。"""
    run = _run(goal="把测试报告发给我", session_id=str(uuid.uuid4()))
    command = _command(run)
    checkpoint = _checkpoint(run)
    client = _FakeClient()

    await _record(
        monkeypatch,
        db=_FakeDb(),
        run=run,
        command=command,
        checkpoint=checkpoint,
        status="completed",
        client=client,
    )

    assert "implicit_negative" not in _score_names(client)


@pytest.mark.asyncio
async def test_no_negative_signal_without_user_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    """非用户发起（trigger/heartbeat 等 actor 为空）不做负反馈判定。"""
    run = _run(goal="不对，重来", session_id=str(uuid.uuid4()))
    command = _command(run, actor_user_id=None)
    checkpoint = _checkpoint(run)
    db = _FakeDb(same_goal_run_id=uuid.uuid4(), prior_run_id=uuid.uuid4())
    client = _FakeClient()

    await _record(
        monkeypatch,
        db=db,
        run=run,
        command=command,
        checkpoint=checkpoint,
        status="completed",
        client=client,
    )

    assert "implicit_negative" not in _score_names(client)
    assert db.statements
    assert all("FROM agent_run_commands" in statement for statement in db.statements)
    assert not any("FROM agent_run_events" in statement for statement in db.statements)  # 只查 attempt，不做信号查询


@pytest.mark.asyncio
async def test_swallows_client_and_db_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """observability 内部失败绝不传导到结算链。"""

    class _BoomClient(_FakeClient):
        def create_score(self, **kwargs: Any) -> None:
            raise RuntimeError("boom")

    run = _run(session_id=str(uuid.uuid4()))
    command = _command(run)
    checkpoint = _checkpoint(run)
    monkeypatch.setattr(scores, "_get_client", lambda _tenant_id=None: _BoomClient())

    await scores.record_terminal_scores(
        _FakeDb(),  # type: ignore[arg-type]
        run=run,
        command=command,
        checkpoint=checkpoint,
        status="completed",
    )

    class _BoomDb:
        async def execute(self, statement: Any) -> Any:
            raise RuntimeError("db boom")

    monkeypatch.setattr(scores, "_get_client", lambda _tenant_id=None: _FakeClient())
    await scores.record_terminal_scores(
        _BoomDb(),  # type: ignore[arg-type]
        run=run,
        command=command,
        checkpoint=checkpoint,
        status="completed",
    )

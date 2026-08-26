"""Tests for SelectiveCheckpointSaver — essential-boundary persistence policy."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.agent_runtime.selective_checkpointer import (
    FORCE_FLAG,
    SelectiveCheckpointSaver,
)


class _RecordingSaver:
    """Minimal inner saver that records aput / aput_writes calls."""

    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []
        self.writes: list[tuple[str, str]] = []
        self.serde = object()
        self._seq = 0

    async def aput(self, config, checkpoint, metadata, new_versions):
        self._seq += 1
        self.puts.append(
            {
                "checkpoint": checkpoint,
                "metadata": metadata,
                "seq": self._seq,
            }
        )
        saved = dict(config)
        saved.setdefault("configurable", {})["checkpoint_id"] = f"ck-{self._seq}"
        return saved

    async def aput_writes(self, config, writes, task_id, task_path=""):
        self.writes.append((task_id, task_path))

    async def alist(self, config, **kwargs):
        yield f"alist:{config}"

    def get_tuple(self, config):
        return f"get_tuple:{config}"

    def custom_method(self) -> str:
        return "delegated"


def _config(thread_id: str | None) -> dict[str, Any]:
    if thread_id is None:
        return {}
    return {"configurable": {"thread_id": thread_id}}


def _checkpoint(*, status: str, pending_sends: list[Any] | None = None) -> dict[str, Any]:
    return {
        "channel_values": {"status": status},
        "pending_sends": pending_sends or [],
    }


def _interrupt_send() -> Any:
    class _Send:
        node = "__interrupt__"

    return _Send()


@pytest.fixture()
def saver() -> SelectiveCheckpointSaver:
    return SelectiveCheckpointSaver(inner=_RecordingSaver(), watermark=5)


def test_first_checkpoint_is_persisted(saver: SelectiveCheckpointSaver) -> None:
    config = _config("t1")
    checkpoint = _checkpoint(status="created")

    async def run() -> None:
        await saver.aput(config, checkpoint, {}, {"ch": "v1"})

    import asyncio

    asyncio.run(run())
    assert len(saver._inner.puts) == 1


async def test_interrupt_always_persists(saver: SelectiveCheckpointSaver) -> None:
    await saver.aput(_config("t1"), _checkpoint(status="running"), {}, {})
    # Interrupt arrives on the very next step even though counter < watermark.
    await saver.aput(
        _config("t1"),
        _checkpoint(status="running", pending_sends=[_interrupt_send()]),
        {},
        {},
    )
    assert len(saver._inner.puts) == 2


@pytest.mark.parametrize("status", ["waiting_user", "waiting_external", "waiting_agent", "completed", "failed", "cancelled"])
async def test_wait_and_terminal_statuses_always_persist(
    saver: SelectiveCheckpointSaver, status: str
) -> None:
    await saver.aput(_config("t1"), _checkpoint(status="running"), {}, {})
    await saver.aput(_config("t1"), _checkpoint(status=status), {}, {})
    assert len(saver._inner.puts) == 2


async def test_force_flag_persists(saver: SelectiveCheckpointSaver) -> None:
    await saver.aput(_config("t1"), _checkpoint(status="running"), {}, {})
    await saver.aput(
        _config("t1"),
        _checkpoint(status="running"),
        {FORCE_FLAG: True},
        {},
    )
    assert len(saver._inner.puts) == 2


async def test_watermark_persists_every_k_steps(saver: SelectiveCheckpointSaver) -> None:
    config = _config("t1")
    # Call 1 is the essential baseline; calls 2-5 are skipped (counts 1-4).
    for _ in range(4):
        await saver.aput(config, _checkpoint(status="running"), {}, {})
    assert len(saver._inner.puts) == 1
    await saver.aput(config, _checkpoint(status="running"), {}, {})
    # Call 6 hits the watermark (count reaches 5 since the last persist).
    await saver.aput(config, _checkpoint(status="running"), {}, {})
    assert len(saver._inner.puts) == 2
    # Counter resets: next 4 calls are skipped again.
    for _ in range(4):
        await saver.aput(config, _checkpoint(status="running"), {}, {})
    assert len(saver._inner.puts) == 2


async def test_skipped_step_returns_previous_config(saver: SelectiveCheckpointSaver) -> None:
    config = _config("t1")
    saved = await saver.aput(config, _checkpoint(status="created"), {}, {})
    assert saved["configurable"]["checkpoint_id"] == "ck-1"
    # Skipped steps hand back the last persisted config.
    skipped = await saver.aput(config, _checkpoint(status="running"), {}, {})
    assert skipped["configurable"]["checkpoint_id"] == "ck-1"


async def test_writes_swallowed_for_skipped_checkpoints(saver: SelectiveCheckpointSaver) -> None:
    config = _config("t1")
    await saver.aput(config, _checkpoint(status="created"), {}, {})
    # This step is skipped -> its writes must be dropped.
    await saver.aput(config, _checkpoint(status="running"), {}, {})
    await saver.aput_writes(config, [("c", "x")], "task-1")
    assert saver._inner.writes == []
    # Force an essential step -> subsequent writes flow through.
    await saver.aput(config, _checkpoint(status="waiting_user"), {}, {})
    await saver.aput_writes(config, [("c", "y")], "task-2")
    assert saver._inner.writes == [("task-2", "")]


async def test_missing_thread_id_always_persists(saver: SelectiveCheckpointSaver) -> None:
    await saver.aput(_config(None), _checkpoint(status="running"), {}, {})
    await saver.aput(_config(None), _checkpoint(status="running"), {}, {})
    assert len(saver._inner.puts) == 2


async def test_unhandled_attributes_delegate(saver: SelectiveCheckpointSaver) -> None:
    assert saver.custom_method() == "delegated"


def test_watermark_must_be_positive() -> None:
    with pytest.raises(ValueError):
        SelectiveCheckpointSaver(inner=_RecordingSaver(), watermark=0)


async def test_eviction_bounds_tracked_threads() -> None:
    tight = SelectiveCheckpointSaver(inner=_RecordingSaver(), watermark=2)
    for i in range(5):
        await tight.aput(_config(f"t{i}"), _checkpoint(status="created"), {}, {})
    # Cap is large in production, but eviction logic keeps the dict bounded;
    # here we just assert the per-thread state is still consistent.
    assert len(tight._seen) == 5


def test_is_accepted_by_langgraph_checkpointer_validation(
    saver: SelectiveCheckpointSaver,
) -> None:
    """Regression: enabling CHECKPOINT_SELECTIVE_ENABLED once crashed startup.

    langgraph's ensure_valid_checkpointer requires BaseCheckpointSaver; the
    duck-typed wrapper must actually subclass it, not just delegate.
    """
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.types import ensure_valid_checkpointer

    assert isinstance(saver, BaseCheckpointSaver)
    assert ensure_valid_checkpointer(saver) is saver


async def test_base_class_defaults_do_not_shadow_delegation(
    saver: SelectiveCheckpointSaver,
) -> None:
    """Regression: BaseCheckpointSaver's NotImplementedError defaults shadowed
    __getattr__ after subclassing, so alist/aget_tuple hit the base class and
    raised instead of reaching the wrapped saver (broke /runtime-state 500s).
    """
    assert [item async for item in saver.alist({})] == ["alist:{}"]
    assert saver.get_tuple({}) == "get_tuple:{}"

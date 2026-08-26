"""Selective checkpoint persistence — root-cause control of checkpoint growth.

LangGraph persists a full checkpoint after every super-step. Clawith's runtime
loop (control_guard -> model/tool/verify -> control_guard) produces 4-6
super-steps per message, so threads accumulate dozens of process snapshots
(observed avg 80 per thread, top 1,815) even though only boundary states are
ever read back:

- resume and the debugger UI read the LATEST checkpoint;
- the product reconciler reads the applied checkpoint of recent commands;
- waiting_user / waiting_external / waiting_agent interrupts must survive restarts.

This wrapper persists only ESSENTIAL checkpoints and skips the rest, cutting
checkpoint growth ~90% at the source. Policy (any condition persists):

1. first checkpoint for a thread (durability baseline);
2. interrupt pending (``__interrupt__`` in ``pending_sends``);
3. lifecycle status is a wait or terminal state
   (waiting_user / waiting_external / waiting_agent / completed / failed / cancelled);
4. watermark: every K-th super-step per thread (K configurable);
5. metadata force flag ``clawith_ckpt_essential`` (platform escape hatch).

When a checkpoint is skipped its ``aput_writes`` are swallowed too, so no writes
rows dangle against a checkpoint that was never stored; the next essential
``aput`` carries the fully consolidated state. Resume semantics are unchanged
except that a crash may re-execute up to K super-steps (LLM calls re-billed;
tool side effects stay idempotent via the product tables).

Assumptions: single-worker deployment (per-thread counters live in memory).
After a restart counters reset, which only yields a few extra essential
checkpoints. Counter state is capped to bound memory on long-lived processes.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

_CHECKPOINT_TASKS = frozenset({"__interrupt__"})
_WAIT_STATUSES = frozenset({"waiting_user", "waiting_external", "waiting_agent"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_ESSENTIAL_STATUSES = _WAIT_STATUSES | _TERMINAL_STATUSES
FORCE_FLAG = "clawith_ckpt_essential"

# Bounded in-process state. Eviction is safe: a forgotten thread simply
# re-persists from its counter reset, which is exactly post-restart behaviour.
_MAX_TRACKED_THREADS = 4096


def _thread_id(config: Any) -> str | None:
    configurable = (config or {}).get("configurable") or {}
    thread_id = configurable.get("thread_id")
    return str(thread_id) if thread_id else None


def _has_interrupt(checkpoint: Any) -> bool:
    for send in checkpoint.get("pending_sends") or []:
        node = send.get("node") if isinstance(send, dict) else getattr(send, "node", None)
        if node == "__interrupt__":
            return True
    return False


def _status_is_essential(checkpoint: Any) -> bool:
    status = (checkpoint.get("channel_values") or {}).get("status")
    return isinstance(status, str) and status in _ESSENTIAL_STATUSES


def _force_requested(metadata: Any) -> bool:
    value = (metadata or {}).get(FORCE_FLAG)
    return value is True or value == "true"


class SelectiveCheckpointSaver(BaseCheckpointSaver):
    """Wrapper that persists only essential checkpoints; everything else delegates.

    Subclasses ``BaseCheckpointSaver`` so that langgraph's
    ``ensure_valid_checkpointer`` accepts it at compile time; unhandled methods
    (get_tuple/alist/delete_thread/...) delegate to the wrapped saver via
    ``__getattr__``.
    """

    def __init__(self, *, inner: Any, watermark: int = 5) -> None:
        if watermark < 1:
            raise ValueError("watermark must be >= 1")
        self._inner = inner
        self._watermark = watermark
        self._counters: dict[str, int] = {}
        self._seen: dict[str, bool] = {}
        self._skip_flags: dict[str, bool] = {}
        self._last_config: dict[str, dict[str, Any]] = {}
        # Exposed explicitly so code probing `.serde` bypasses __getattr__.
        self.serde = inner.serde

    def __getattr__(self, name: str) -> Any:
        # Delegate every unhandled attribute/method (get_tuple, alist,
        # aget_state_history plumbing, delete_thread, config_specs, ...)
        # to the wrapped saver.
        return getattr(self._inner, name)

    def _evict_if_needed(self) -> None:
        if len(self._seen) <= _MAX_TRACKED_THREADS:
            return
        overflow = len(self._seen) - _MAX_TRACKED_THREADS
        for _ in range(overflow):
            key, _ = self._seen.popitem(last=False)
            self._counters.pop(key, None)
            self._skip_flags.pop(key, None)
            self._last_config.pop(key, None)

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: Any,
        metadata: dict[str, Any],
        new_versions: Any,
    ) -> dict[str, Any]:
        thread_id = _thread_id(config)
        if thread_id is None:
            # Untrackable thread: keep default persistence semantics.
            return await self._inner.aput(config, checkpoint, metadata, new_versions)

        self._evict_if_needed()
        first = thread_id not in self._seen
        self._seen[thread_id] = True
        count = self._counters.get(thread_id, 0) + 1
        self._counters[thread_id] = count

        essential = (
            first
            or _has_interrupt(checkpoint)
            or _status_is_essential(checkpoint)
            or _force_requested(metadata)
            or count >= self._watermark
        )
        if not essential:
            self._skip_flags[thread_id] = True
            return self._last_config.get(thread_id, config)

        saved = await self._inner.aput(config, checkpoint, metadata, new_versions)
        self._counters[thread_id] = 0
        self._skip_flags[thread_id] = False
        self._last_config[thread_id] = saved
        return saved

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Any,
        task_id: str,
        task_path: str = "",
    ) -> Any:
        thread_id = _thread_id(config)
        if thread_id is not None and self._skip_flags.get(thread_id):
            # Writes belong to a checkpoint that was never stored; drop them
            # with it. Interrupt writes never reach this branch: an interrupt
            # makes its own aput essential, clearing the skip flag first.
            return None
        return await self._inner.aput_writes(config, writes, task_id, task_path)

    # ── Explicit delegation over the BaseCheckpointSaver surface ──────────────
    # Subclassing BaseCheckpointSaver shadows __getattr__: every method the base
    # class defines (most raising NotImplementedError) now resolves to the base
    # implementation instead of delegating. Override them all so calls reach the
    # wrapped saver exactly as they did before the inheritance.

    def get_tuple(self, config: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        return self._inner.get_tuple(config, *args, **kwargs)

    async def aget_tuple(self, config: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        return await self._inner.aget_tuple(config, *args, **kwargs)

    def list(self, config: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        return self._inner.list(config, *args, **kwargs)

    async def alist(self, config: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        # alist is an async generator on AsyncPostgresSaver — forward yields,
        # do not await (awaiting a generator is a TypeError at the call site).
        async for item in self._inner.alist(config, *args, **kwargs):
            yield item

    def put(self, config: dict[str, Any], checkpoint: Any, metadata: dict[str, Any], new_versions: Any) -> Any:
        return self._inner.put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config: dict[str, Any], writes: Any, task_id: str, task_path: str = "") -> Any:
        return self._inner.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> Any:
        return self._inner.delete_thread(thread_id)

    async def adelete_thread(self, thread_id: str) -> Any:
        return await self._inner.adelete_thread(thread_id)

    def copy_thread(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.copy_thread(*args, **kwargs)

    async def acopy_thread(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.acopy_thread(*args, **kwargs)

    def delete_for_runs(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.delete_for_runs(*args, **kwargs)

    async def adelete_for_runs(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.adelete_for_runs(*args, **kwargs)

    def prune(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.prune(*args, **kwargs)

    async def aprune(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.aprune(*args, **kwargs)

    def get_next_version(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.get_next_version(*args, **kwargs)

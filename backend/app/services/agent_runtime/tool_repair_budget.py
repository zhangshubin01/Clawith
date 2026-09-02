"""Checkpoint-safe Tool repair episode transitions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from app.services.agent_runtime.state import JsonObject

SAME_FINGERPRINT_FAILURE_LIMIT = 10
TOOL_EPISODE_FAILURE_LIMIT = 10
WORKSPACE_SYNC_CONFLICT_LIMIT = 3
WORKSPACE_SYNC_CONFLICT_FAILURE_MESSAGE = (
    "Workspace publication conflicted 3 times in a row on the same Run; "
    "the Run was stopped to prevent further ineffective retries."
)
_REPAIRABLE_MODEL_ACTIONS = frozenset({"repair_arguments", "choose_other_tool"})
# Distinct from node_executor's _WORKSPACE_WRITE_TOOLS (memory-gate counting):
# these tools persist workspace progress, so a succeeded result means the
# workspace genuinely moved forward and may reset the conflict streak.
_DURABLE_WORKSPACE_PROGRESS_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "move_file",
        "delete_file",
        "execute_code",
        "android_compile",
    }
)
_CONFLICT_FINGERPRINT_CAP = 16


class ToolRepairBudgetError(ValueError):
    """Checkpoint repair episode state is malformed."""


@dataclass(frozen=True, slots=True)
class ToolRepairTransition:
    episodes: JsonObject
    counted: bool = False
    reset_tool_name: str | None = None
    pause_reason: str | None = None
    paused_tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceConflictTransition:
    budget: JsonObject
    terminal: bool = False


def _parse_conflict_budget(raw: object) -> tuple[int, dict[str, int]]:
    if raw in (None, {}):
        return 0, {}
    if not isinstance(raw, Mapping):
        raise ToolRepairBudgetError("workspace conflict budget must be an object")
    version = raw.get("version")
    if version not in (1, 2):
        raise ToolRepairBudgetError("workspace conflict budget requires version 1 or 2")
    count = raw.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ToolRepairBudgetError("workspace conflict budget count is invalid")
    fingerprints: dict[str, int] = {}
    if version == 2:
        raw_fingerprints = raw.get("fingerprints", {})
        if not isinstance(raw_fingerprints, Mapping):
            raise ToolRepairBudgetError("workspace conflict budget fingerprints is invalid")
        for raw_fingerprint, raw_count in raw_fingerprints.items():
            if not isinstance(raw_fingerprint, str) or not raw_fingerprint.strip():
                raise ToolRepairBudgetError("workspace conflict budget fingerprint is invalid")
            if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 1:
                raise ToolRepairBudgetError("workspace conflict budget fingerprint count is invalid")
            fingerprints[raw_fingerprint] = raw_count
        while len(fingerprints) > _CONFLICT_FINGERPRINT_CAP:
            del fingerprints[next(iter(fingerprints))]
    return count, fingerprints


def _is_durable_workspace_progress(message: Mapping[str, object]) -> bool:
    return message.get("name") in _DURABLE_WORKSPACE_PROGRESS_TOOLS and message.get("execution_status") == "succeeded"


def _content_fingerprint(name: str, error_code: str, content: str) -> str:
    return hashlib.sha256(f"{name}\0{error_code}\0{content}".encode()).hexdigest()


def apply_workspace_sync_conflict(
    raw_budget: object,
    message: Mapping[str, object],
    *,
    model_step: int,
) -> WorkspaceConflictTransition:
    """Count workspace_sync_conflict tool results (checkpoint-safe).

    Only durable workspace progress — a succeeded result from a tool in
    ``_DURABLE_WORKSPACE_PROGRESS_TOOLS`` — resets the streak count: the
    workspace genuinely moved forward.  Read-only successes (read_file and
    friends) and failures of any other kind leave the budget untouched, so the
    P0.5 remediation dance (read the current file, then edit it) cannot wash
    the streak out.  Per-content fingerprints (sha256 of
    name+error_code+content, computed inside the breaker — tool messages carry
    no content_hash) survive every count reset and accumulate: three
    conflicts on the same content triple trip the breaker even when a
    successful write keeps zeroing the streak (the ping-pong hole).  Reaching
    ``WORKSPACE_SYNC_CONFLICT_LIMIT`` on either the streak or a single
    fingerprint marks the transition terminal so the caller stops the Run
    instead of feeding the model another identical retry (the P0.5 breaker;
    repair-budget pauses do not fit because these failures carry
    ``model_action=continue`` and ``side_effect_state=unknown``).
    """
    if isinstance(model_step, bool) or not isinstance(model_step, int) or model_step < 0:
        raise ToolRepairBudgetError("model_step must be a non-negative integer")
    count, fingerprints = _parse_conflict_budget(raw_budget)
    is_conflict = message.get("execution_status") == "failed" and message.get("error_code") == "workspace_sync_conflict"
    if not is_conflict:
        if _is_durable_workspace_progress(message):
            count = 0
        return WorkspaceConflictTransition(budget=_json_conflict_budget(count, fingerprints))
    count += 1
    fingerprint = _content_fingerprint(
        str(message.get("name") or ""),
        str(message.get("error_code") or ""),
        str(message.get("content") or ""),
    )
    fingerprint_count = fingerprints.get(fingerprint, 0) + 1
    fingerprints[fingerprint] = fingerprint_count
    while len(fingerprints) > _CONFLICT_FINGERPRINT_CAP:
        del fingerprints[next(iter(fingerprints))]
    return WorkspaceConflictTransition(
        budget=_json_conflict_budget(count, fingerprints),
        terminal=(count >= WORKSPACE_SYNC_CONFLICT_LIMIT or fingerprint_count >= WORKSPACE_SYNC_CONFLICT_LIMIT),
    )


def _json_conflict_budget(count: int, fingerprints: Mapping[str, int] | None = None) -> JsonObject:
    return {
        "version": 2,
        "count": count,
        "fingerprints": dict(fingerprints or {}),
    }


def _text(value: object, *, field: str, max_length: int = 255) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolRepairBudgetError(f"{field} must be non-empty text")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ToolRepairBudgetError(f"{field} exceeds its length limit")
    return normalized


def _parse_episodes(raw: object) -> dict[str, dict]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, Mapping) or raw.get("version") != 1:
        raise ToolRepairBudgetError("tool repair episodes require version 1")
    by_tool = raw.get("by_tool")
    if not isinstance(by_tool, Mapping) or len(by_tool) > 256:
        raise ToolRepairBudgetError("tool repair episodes by_tool is invalid")
    parsed: dict[str, dict] = {}
    for raw_tool_name, raw_episode in by_tool.items():
        tool_name = _text(raw_tool_name, field="tool_name", max_length=200)
        if not isinstance(raw_episode, Mapping):
            raise ToolRepairBudgetError("tool repair episode must be an object")
        episode = dict(raw_episode)
        for field in ("total_failures", "same_fingerprint_failures"):
            value = episode.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ToolRepairBudgetError(f"repair episode {field} is invalid")
        _text(episode.get("episode_id"), field="episode_id")
        _text(episode.get("last_fingerprint"), field="last_fingerprint")
        _text(
            episode.get("last_call_instance_id"),
            field="last_call_instance_id",
        )
        updated_at = episode.get("updated_at_model_step")
        if isinstance(updated_at, bool) or not isinstance(updated_at, int) or updated_at < 0:
            raise ToolRepairBudgetError("repair episode updated_at_model_step is invalid")
        parsed[tool_name] = episode
    return parsed


def _json(by_tool: Mapping[str, dict]) -> JsonObject:
    return {
        "version": 1,
        "by_tool": {tool_name: dict(episode) for tool_name, episode in sorted(by_tool.items())},
    }


def _fingerprint(message: Mapping[str, object]) -> str:
    explicit = message.get("failure_fingerprint")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()[:255]
    payload = {
        "error_code": message.get("error_code"),
        "model_action": message.get("model_action"),
        "content": str(message.get("content") or "")[:2000],
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def apply_tool_result(
    raw_episodes: object,
    message: Mapping[str, object],
    *,
    model_step: int,
) -> ToolRepairTransition:
    """Apply one model-visible Tool Result without touching other budgets."""
    if isinstance(model_step, bool) or not isinstance(model_step, int) or model_step < 0:
        raise ToolRepairBudgetError("model_step must be a non-negative integer")
    by_tool = _parse_episodes(raw_episodes)
    tool_name = message.get("name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        return ToolRepairTransition(episodes=_json(by_tool))
    tool_name = tool_name.strip()
    status = message.get("execution_status")
    if status == "succeeded":
        reset = by_tool.pop(tool_name, None) is not None
        return ToolRepairTransition(
            episodes=_json(by_tool),
            reset_tool_name=tool_name if reset else None,
        )
    if (
        status != "failed"
        or message.get("model_action") not in _REPAIRABLE_MODEL_ACTIONS
        or message.get("side_effect_state") != "none"
    ):
        return ToolRepairTransition(episodes=_json(by_tool))

    call_instance_id = _text(
        message.get("tool_call_id") or message.get("call_instance_id"),
        field="call_instance_id",
    )
    fingerprint = _fingerprint(message)
    prior = by_tool.get(tool_name)
    total_failures = int(prior["total_failures"]) + 1 if prior else 1
    same_failures = (
        int(prior["same_fingerprint_failures"]) + 1 if prior and prior["last_fingerprint"] == fingerprint else 1
    )
    episode_id = (
        str(prior["episode_id"])
        if prior
        else "episode:" + hashlib.sha256(f"{tool_name}:{call_instance_id}".encode()).hexdigest()[:24]
    )
    by_tool[tool_name] = {
        "tool_name": tool_name,
        "episode_id": episode_id,
        "total_failures": total_failures,
        "last_fingerprint": fingerprint,
        "same_fingerprint_failures": same_failures,
        "last_call_instance_id": call_instance_id,
        "updated_at_model_step": model_step,
    }
    pause_reason = (
        "tool_repair_same_fingerprint_limit_reached"
        if same_failures >= SAME_FINGERPRINT_FAILURE_LIMIT
        else "tool_repair_episode_limit_reached"
        if total_failures >= TOOL_EPISODE_FAILURE_LIMIT
        else None
    )
    return ToolRepairTransition(
        episodes=_json(by_tool),
        counted=True,
        pause_reason=pause_reason,
        paused_tool_name=tool_name if pause_reason is not None else None,
    )


def reset_tool_repair_episodes(
    raw_episodes: object,
    *,
    tool_name: str | None = None,
) -> JsonObject:
    by_tool = _parse_episodes(raw_episodes)
    if tool_name is None:
        by_tool.clear()
    else:
        by_tool.pop(_text(tool_name, field="tool_name", max_length=200), None)
    return _json(by_tool)


__all__ = [
    "SAME_FINGERPRINT_FAILURE_LIMIT",
    "TOOL_EPISODE_FAILURE_LIMIT",
    "WORKSPACE_SYNC_CONFLICT_FAILURE_MESSAGE",
    "WORKSPACE_SYNC_CONFLICT_LIMIT",
    "ToolRepairBudgetError",
    "ToolRepairTransition",
    "WorkspaceConflictTransition",
    "apply_tool_result",
    "apply_workspace_sync_conflict",
    "reset_tool_repair_episodes",
]

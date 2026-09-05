"""Process-level no-progress detection (ADR-0016).

The existing circuit breakers are all *signature-level* — they ask "did the
model repeat the same action?" (`_trailing_identical_calls`, `detect_loop`,
`build_dup_read_ratio`).  They cannot catch the model "doing the same thing in
different disguises": a mix of git status/branch/checkout/fetch plus read_file,
each call with different arguments, so every signature hash differs and no
breaker fires — yet the task/world state never advances.

This module asks the other question: **"did the world state advance?"** — an
*evidence-gain* score over each model turn's tool executions, decided from the
execution ledger and workspace material change, not from call signatures.

Design contract (see docs/technical-plans/20260905-agent-no-progress-detection-plan.md):

- **Gain table.** read_file: new (path, content_hash) +1, repeat 0.
  execute_code: same command with a changed result hash +1, a previously
  failed command now succeeding +2, first observation of a command 0 (a
  *new* command is not evidence — that is exactly the "disguise" git
  status/branch/checkout/fetch uses), repeat 0.  write (``effect == "write"``)
  with a real mutation (``material_change``) +3, empty edit 0.  external_write
  +3 (no workspace evidence to verify, so assume forward progress).
- **Look-only cap.** ``look_only_cap`` consecutive turns with no real mutation
  force the turn's gain to zero even if it read new files / produced changed
  command output — sealing the "read every file it never opened" walk
  (Reasonix ``explorationRunLimit`` port; Clawith's material-change signal
  makes it stricter).
- **Zero new checkpoint state.** Everything is replayed from the ledger +
  ``material_change`` (resolved by the wiring layer from
  ``workspace_file_revisions`` before≠after); ``RoundState`` is a run-scoped
  fold.  ``fold_rounds`` returns the trailing zero-gain streak plus the
  nudge(3)/pivot(5)/stop(8) ladder level.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

GAIN_NEW_READ = 1
GAIN_NEW_RESULT = 1
GAIN_FAILURE_RECOVERY = 2
GAIN_MUTATION = 3

LADDER_NONE = "none"
LADDER_NUDGE = "nudge"
LADDER_PIVOT = "pivot"
LADDER_STOP = "stop"

_READ_TOOL = "read_file"
_CODE_TOOL = "execute_code"


@dataclass(frozen=True, slots=True)
class NoProgressConfig:
    """Ladder thresholds and the look-only cap (Q7-B / Q6)."""

    nudge_threshold: int = 3
    pivot_threshold: int = 5
    stop_threshold: int = 8
    look_only_cap: int = 6


@dataclass
class RoundState:
    """Run-scoped evidence seen-set (a replay fold — zero checkpoint state)."""

    read_keys: set[tuple[str, str]] = field(default_factory=set)
    command_results: dict[str, str] = field(default_factory=dict)
    command_failed: set[str] = field(default_factory=set)
    look_only_run: int = 0


@dataclass(frozen=True, slots=True)
class NoProgressSignal:
    """The trailing zero-gain streak and the ladder level it maps to."""

    streak: int
    level: str
    last_round_gain: int


def _command_key(arguments: Mapping[str, Any]) -> str:
    """A stable identity for a command's arguments (the 'which command' axis)."""
    try:
        return json.dumps(
            dict(arguments), sort_keys=True, ensure_ascii=False, default=str
        )
    except (TypeError, ValueError):
        return repr(arguments)


def _read_gain(execution: Any, state: RoundState) -> int:
    if getattr(execution, "status", None) != "succeeded":
        return 0
    arguments = getattr(execution, "sanitized_arguments", None)
    metadata = getattr(execution, "result_metadata", None)
    path = arguments.get("path") if isinstance(arguments, Mapping) else None
    content_hash = (
        metadata.get("content_hash") if isinstance(metadata, Mapping) else None
    )
    if not isinstance(path, str) or not path:
        return 0
    if not isinstance(content_hash, str) or not content_hash:
        return 0
    key = (path, content_hash)
    if key in state.read_keys:
        return 0
    state.read_keys.add(key)
    return GAIN_NEW_READ


def _code_gain(execution: Any, state: RoundState) -> int:
    arguments = getattr(execution, "sanitized_arguments", None)
    metadata = getattr(execution, "result_metadata", None)
    key = _command_key(arguments if isinstance(arguments, Mapping) else {})
    status = getattr(execution, "status", None)
    if status != "succeeded":
        state.command_failed.add(key)
        return 0
    result_hash = (
        metadata.get("content_hash") if isinstance(metadata, Mapping) else None
    )
    if not isinstance(result_hash, str):
        result_hash = None
    if key in state.command_failed:
        # A command that failed before now succeeds: located and fixed the bug.
        state.command_failed.discard(key)
        state.command_results[key] = result_hash
        return GAIN_FAILURE_RECOVERY
    was_seen = key in state.command_results
    previous = state.command_results.get(key)
    state.command_results[key] = result_hash
    if was_seen and previous != result_hash:
        return GAIN_NEW_RESULT
    return 0


def _write_gain(
    execution: Any,
    material_change_of: Callable[[Any], bool] | None,
) -> int:
    effect = getattr(execution, "effect", None)
    if effect == "write":
        changed = (
            material_change_of(execution)
            if material_change_of is not None
            else bool(getattr(execution, "material_change", False))
        )
        return GAIN_MUTATION if changed else 0
    if effect == "external_write":
        return GAIN_MUTATION
    return 0


def score_round(
    executions: Sequence[Any],
    state: RoundState,
    *,
    config: NoProgressConfig = NoProgressConfig(),
    material_change_of: Callable[[Any], bool] | None = None,
) -> int:
    """Score ONE model turn's tool executions for evidence gain.

    Mutates ``state`` in place (the run-scoped seen-set), returns the turn's
    total gain.  A turn with a real mutation resets the look-only counter; a
    turn without one increments it, and once it reaches ``look_only_cap`` the
    turn is forced to zero gain regardless of fresh reads / changed output.
    ``material_change_of`` (optional) resolves whether a write really mutated
    workspace content; default reads ``execution.material_change``.
    """
    gain = 0
    material = False
    for execution in executions:
        tool = getattr(execution, "tool_name", None)
        if tool == _READ_TOOL:
            gain += _read_gain(execution, state)
        elif tool == _CODE_TOOL:
            gain += _code_gain(execution, state)
        else:
            write_gain = _write_gain(execution, material_change_of)
            gain += write_gain
            if write_gain > 0:
                material = True
    if material:
        state.look_only_run = 0
    else:
        state.look_only_run += 1
        if config.look_only_cap > 0 and state.look_only_run >= config.look_only_cap:
            return 0
    return gain


def classify_streak(
    streak: int,
    *,
    config: NoProgressConfig = NoProgressConfig(),
) -> str:
    """Map a zero-gain streak to the ladder level (none/nudge/pivot/stop)."""
    if streak >= config.stop_threshold:
        return LADDER_STOP
    if streak >= config.pivot_threshold:
        return LADDER_PIVOT
    if streak >= config.nudge_threshold:
        return LADDER_NUDGE
    return LADDER_NONE


def fold_rounds(
    rounds: Sequence[Sequence[Any]],
    *,
    config: NoProgressConfig = NoProgressConfig(),
    material_change_of: Callable[[Any], bool] | None = None,
) -> NoProgressSignal:
    """Fold ordered turns into the trailing zero-gain streak + ladder level.

    ``rounds`` is the ordered sequence of the current run's turns, each turn
    an ordered sequence of its tool executions.  A positive-gain turn resets
    the streak; a zero-gain turn extends it.
    """
    state = RoundState()
    streak = 0
    last_gain = 0
    for round_executions in rounds:
        gain = score_round(
            round_executions,
            state,
            config=config,
            material_change_of=material_change_of,
        )
        last_gain = gain
        streak = streak + 1 if gain == 0 else 0
    return NoProgressSignal(
        streak=streak,
        level=classify_streak(streak, config=config),
        last_round_gain=last_gain,
    )


def build_no_progress_signal(
    executions: Sequence[Any],
    *,
    run_id: str | None = None,
    config: NoProgressConfig = NoProgressConfig(),
    material_change_of: Callable[[Any], bool] | None = None,
) -> NoProgressSignal | None:
    """Group the current run's settled executions into model turns and fold.

    A model turn issues several tool calls that share one ``assistant_message_id``,
    so turns are those id groups in ledger order (``_load`` orders executions by
    ``started_at, id``).  ``run_id`` scopes the merged ledger down to the current
    run (prior runs' executions are appended for run isolation and must not be
    scored).  Executions without a usable assistant_message_id fall back to a
    singleton turn keyed by tool_call_id.  Returns None when no scored turn
    remains.
    """
    by_turn: dict[str, list[Any]] = {}
    order: list[str] = []
    for execution in executions:
        if run_id is not None and str(getattr(execution, "run_id", "")) != run_id:
            continue
        turn = getattr(execution, "assistant_message_id", None)
        if not isinstance(turn, str) or not turn:
            turn = f"call:{getattr(execution, 'tool_call_id', '')}"
        if turn not in by_turn:
            by_turn[turn] = []
            order.append(turn)
        by_turn[turn].append(execution)
    if not order:
        return None
    return fold_rounds(
        [by_turn[turn] for turn in order],
        config=config,
        material_change_of=material_change_of,
    )


def no_progress_message(signal: NoProgressSignal) -> str | None:
    """The injected guidance for a non-"none" ladder level; None when "none".

    Imperative, byte-deterministic, no conditional escape clause — mirrors the
    soft-loop / duplicate-read reminder tone.  Stop is prompt-only guidance
    (stop exploring, deliver the answer), never a hard termination (Q8-B).
    """
    streak = signal.streak
    if signal.level == LADDER_NUDGE:
        return (
            f"注意：你已经连续 {streak} 轮工具执行没有产生实质进展"
            "（没有真实文件变更，也没有新的有效结果）。请检查当前策略是否有效，"
            "换一种方式推进，而不是重复类似的检查或读取。"
        )
    if signal.level == LADDER_PIVOT:
        return (
            f"注意：你已经连续 {streak} 轮工具执行没有产生实质进展。"
            "必须改变策略：停止重复的检查、读取与巡检命令，明确下一步要"
            "做出什么实质变更，然后直接执行。"
        )
    if signal.level == LADDER_STOP:
        return (
            f"注意：你已经连续 {streak} 轮工具执行没有产生实质进展。"
            "停止继续探索，直接基于已有信息输出最终答案，或如实向用户报告"
            "当前已完成与未完成的部分。"
        )
    return None

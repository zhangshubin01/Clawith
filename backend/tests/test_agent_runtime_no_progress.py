"""过程无进展检测（ADR-0016）—— ScoreRound 纯函数红绿测试。

设计契约见 `docs/technical-plans/20260905-agent-no-progress-detection-plan.md`：

- 增益分落在「任务/世界状态是否前进」，非签名匹配；
- read 新 (path, content_hash) +1 / 重复 0；execute_code 同 command 结果哈希变化
  +1 / 失败→成功 +2 / 新命令首次观察或重复 0；write 真实变更 +3 / 空编辑 0；
  external_write 假定前进 +3；
- 连续读全新文件不累积 streak（正常探索不误杀）；读遍后重复读（零增益）才累积；
- 阶梯 nudge(3)/pivot(5)/stop(8)。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.agent_runtime.no_progress import (
    LADDER_NONE,
    LADDER_NUDGE,
    LADDER_PIVOT,
    LADDER_STOP,
    NoProgressSignal,
    RoundState,
    build_no_progress_signal,
    classify_streak,
    fold_rounds,
    no_progress_message,
    score_round,
)


def _execution(
    *,
    tool_name: str,
    status: str = "succeeded",
    path: str | None = None,
    content_hash: str | None = None,
    command: str | None = None,
    effect: str = "read",
    material_change: bool = False,
    tool_call_id: str | None = None,
    assistant_message_id: str | None = None,
    run_id: str | None = None,
) -> SimpleNamespace:
    arguments: dict = {}
    if path is not None:
        arguments["path"] = path
    if command is not None:
        arguments["command"] = command
    metadata: dict = {}
    if content_hash is not None:
        metadata["content_hash"] = content_hash
    return SimpleNamespace(
        tool_name=tool_name,
        status=status,
        sanitized_arguments=arguments,
        result_metadata=metadata,
        effect=effect,
        material_change=material_change,
        tool_call_id=tool_call_id,
        assistant_message_id=assistant_message_id,
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# 增益分：read_file
# ---------------------------------------------------------------------------


def test_new_read_scores_one() -> None:
    state = RoundState()
    assert (
        score_round(
            [_execution(tool_name="read_file", path="a.py", content_hash="h1")],
            state,
        )
        == 1
    )


def test_repeat_read_scores_zero() -> None:
    state = RoundState()
    execution = _execution(tool_name="read_file", path="a.py", content_hash="h1")
    score_round([execution], state)
    assert score_round([execution], state) == 0


def test_read_different_path_or_hash_scores_one() -> None:
    state = RoundState()
    score_round([_execution(tool_name="read_file", path="a.py", content_hash="h1")], state)
    # 不同路径 → 新证据
    assert (
        score_round([_execution(tool_name="read_file", path="b.py", content_hash="h1")], state)
        == 1
    )
    # 同路径不同内容（写后重读）→ 新证据
    assert (
        score_round([_execution(tool_name="read_file", path="a.py", content_hash="h2")], state)
        == 1
    )


def test_failed_read_scores_zero_and_is_not_seen() -> None:
    state = RoundState()
    assert (
        score_round(
            [_execution(tool_name="read_file", path="a.py", status="failed")],
            state,
        )
        == 0
    )
    # 失败读不消耗「已读」证据：同路径成功后仍算新读
    assert (
        score_round(
            [_execution(tool_name="read_file", path="a.py", content_hash="h1")],
            state,
        )
        == 1
    )


def test_read_missing_hash_or_path_is_never_scored() -> None:
    state = RoundState()
    assert (
        score_round([_execution(tool_name="read_file", path="a.py")], state) == 0
    )
    assert (
        score_round([_execution(tool_name="read_file", content_hash="h1")], state) == 0
    )


# ---------------------------------------------------------------------------
# 增益分：execute_code
# ---------------------------------------------------------------------------


def test_execute_code_new_command_first_observation_scores_zero() -> None:
    # 换姿势绕圈的本质：每个新命令（git status/branch/checkout/fetch）各是
    # 「首次观察」，不算证据 → 零增益。不靠「command 首次=+1」。
    state = RoundState()
    assert (
        score_round(
            [_execution(tool_name="execute_code", command="git status", content_hash="r1")],
            state,
        )
        == 0
    )


def test_execute_code_same_command_result_change_scores_one() -> None:
    state = RoundState()
    score_round(
        [_execution(tool_name="execute_code", command="pytest", content_hash="r1")],
        state,
    )
    # 同 command 结果哈希变化 → 输出真的变了 = 进展
    assert (
        score_round(
            [_execution(tool_name="execute_code", command="pytest", content_hash="r2")],
            state,
        )
        == 1
    )


def test_execute_code_repeat_scores_zero() -> None:
    state = RoundState()
    execution = _execution(tool_name="execute_code", command="pytest", content_hash="r1")
    score_round([execution], state)
    assert score_round([execution], state) == 0


def test_execute_code_failure_then_success_scores_two() -> None:
    state = RoundState()
    assert (
        score_round(
            [_execution(tool_name="execute_code", command="make", status="failed")],
            state,
        )
        == 0
    )
    # 曾失败的命令现在成功 = 定位并修复错误，比「输出变化」更重
    assert (
        score_round(
            [_execution(tool_name="execute_code", command="make", content_hash="ok")],
            state,
        )
        == 2
    )


# ---------------------------------------------------------------------------
# 增益分：write / external_write
# ---------------------------------------------------------------------------


def test_real_mutation_scores_three() -> None:
    state = RoundState()
    assert (
        score_round(
            [_execution(tool_name="edit_file", effect="write", material_change=True)],
            state,
        )
        == 3
    )


def test_empty_edit_scores_zero() -> None:
    state = RoundState()
    assert (
        score_round(
            [_execution(tool_name="edit_file", effect="write", material_change=False)],
            state,
        )
        == 0
    )


def test_external_write_scores_three() -> None:
    # 外部副作用无 workspace 证据可验 → 假定前进，宁可计分防误杀真实外部交付
    state = RoundState()
    assert (
        score_round(
            [_execution(tool_name="feishu_send", effect="external_write")],
            state,
        )
        == 3
    )


# ---------------------------------------------------------------------------
# 阶梯
# ---------------------------------------------------------------------------


def test_classify_streak_thresholds() -> None:
    assert classify_streak(2) == LADDER_NONE
    assert classify_streak(3) == LADDER_NUDGE
    assert classify_streak(4) == LADDER_NUDGE
    assert classify_streak(5) == LADDER_PIVOT
    assert classify_streak(7) == LADDER_PIVOT
    assert classify_streak(8) == LADDER_STOP
    assert classify_streak(50) == LADDER_STOP


# ---------------------------------------------------------------------------
# fold_rounds：尾部零增益 streak + 阶梯
# ---------------------------------------------------------------------------


def test_fold_rounds_trailing_streak_and_level() -> None:
    rounds = [
        # round 0：真实变更 → streak 0
        [_execution(tool_name="edit_file", effect="write", material_change=True)],
        # round 1..5：空编辑（零增益）→ streak 累计到 5
        [_execution(tool_name="edit_file", effect="write", material_change=False)],
        [_execution(tool_name="edit_file", effect="write", material_change=False)],
        [_execution(tool_name="edit_file", effect="write", material_change=False)],
        [_execution(tool_name="edit_file", effect="write", material_change=False)],
        [_execution(tool_name="edit_file", effect="write", material_change=False)],
    ]
    signal = fold_rounds(rounds)
    assert signal.streak == 5
    assert signal.level == LADDER_PIVOT
    assert signal.last_round_gain == 0


def test_fold_rounds_positive_gain_resets_streak() -> None:
    rounds = [
        [_execution(tool_name="edit_file", effect="write", material_change=False)],
        [_execution(tool_name="edit_file", effect="write", material_change=False)],
        # 一次真实变更即清零
        [_execution(tool_name="edit_file", effect="write", material_change=True)],
    ]
    signal = fold_rounds(rounds)
    assert signal.streak == 0
    assert signal.level == LADDER_NONE
    assert signal.last_round_gain == 3


def test_dc557d91_real_edits_do_not_trigger_stop() -> None:
    """回归：run dc557d91 有 14 次 edit_file 真实变更，绝不应判零增益循环。"""
    rounds = [
        [_execution(tool_name="edit_file", effect="write", material_change=True)]
        for _ in range(14)
    ]
    signal = fold_rounds(rounds)
    assert signal.streak == 0
    assert signal.level == LADDER_NONE


def test_git_inspection_loop_triggers_stop() -> None:
    """核心场景：git status/branch/checkout/fetch 混杂，参数每次不同但零材料进度。

    每个都是新命令 → 首次观察 0 增益 → streak 累计到 8 → stop。
    """
    commands = ["git status", "git branch", "git checkout main", "git fetch"]
    # 结果哈希按命令本身稳定（同命令同输出），绕圈重跑不会误判为「结果变化」
    rounds = [
        [_execution(tool_name="execute_code", command=cmd, content_hash=f"h-{cmd}")]
        for cmd in commands * 2
    ]
    signal = fold_rounds(rounds)
    assert signal.streak == 8
    assert signal.level == LADDER_STOP
    assert signal.last_round_gain == 0


def test_continuous_new_reads_do_not_accumulate_streak() -> None:
    """正常探索不误杀：每轮读一个全新文件，streak 恒为 0（不再有 look-only 归零）。"""
    rounds = [
        [_execution(tool_name="read_file", path=f"f{i}.py", content_hash=f"h{i}")]
        for i in range(14)
    ]
    signal = fold_rounds(rounds)
    assert signal.streak == 0
    assert signal.level == LADDER_NONE


def test_read_walk_repeat_reads_triggers_stop() -> None:
    """读遍文件后回头重复读：新读不累积，重复读（零增益）累积 → stop。"""
    rounds = [
        [_execution(tool_name="read_file", path=f"f{i}.py", content_hash=f"h{i}")]
        for i in range(6)
    ]
    # 回头重读同样 6 个文件 8 轮 → 零增益 → streak 8 → stop
    rounds += [
        [_execution(tool_name="read_file", path=f"f{i % 6}.py", content_hash=f"h{i % 6}")]
        for i in range(8)
    ]
    signal = fold_rounds(rounds)
    assert signal.streak == 8
    assert signal.level == LADDER_STOP


# ---------------------------------------------------------------------------
# build_no_progress_signal：ledger → 轮分组 → 折叠（接线接缝）
# ---------------------------------------------------------------------------


def test_build_signal_groups_turns_by_assistant_message_id() -> None:
    # 一轮 = 同一 assistant_message_id 的多个工具调用；不同轮按 ledger 顺序
    executions = [
        _execution(
            tool_name="read_file",
            path="a.py",
            content_hash="h1",
            tool_call_id="c1",
            assistant_message_id="msg-1",
            run_id="run-x",
        ),
        _execution(
            tool_name="read_file",
            path="b.py",
            content_hash="h2",
            tool_call_id="c2",
            assistant_message_id="msg-1",
            run_id="run-x",
        ),
        _execution(
            tool_name="edit_file",
            effect="write",
            material_change=True,
            tool_call_id="c3",
            assistant_message_id="msg-2",
            run_id="run-x",
        ),
    ]
    signal = build_no_progress_signal(executions, run_id="run-x")
    assert signal is not None
    # msg-1 一轮读两个新文件 gain=2，msg-2 真实变更 gain=3 → streak 0
    assert signal.streak == 0
    assert signal.level == LADDER_NONE
    assert signal.last_round_gain == 3


def test_build_signal_scopes_to_current_run() -> None:
    # 旧 run 的执行行（run 隔离）不得计入新 run 的零增益 streak
    executions = [
        _execution(
            tool_name="execute_code",
            command="git status",
            content_hash="r1",
            tool_call_id="old",
            assistant_message_id="old-msg",
            run_id="old-run",
        ),
        _execution(
            tool_name="execute_code",
            command="git status",
            content_hash="r1",
            tool_call_id="c1",
            assistant_message_id="msg-1",
            run_id="run-x",
        ),
    ]
    signal = build_no_progress_signal(executions, run_id="run-x")
    assert signal is not None
    assert signal.streak == 1  # 仅当前 run 的 1 轮零增益
    assert signal.level == LADDER_NONE


def test_build_signal_returns_none_when_no_scored_turns() -> None:
    assert build_no_progress_signal([]) is None
    assert (
        build_no_progress_signal(
            [_execution(tool_name="read_file", path="a.py", run_id="run-x")],
            run_id="other-run",
        )
        is None
    )


def test_build_signal_material_change_resolver_override() -> None:
    # 接线层用 resolver 把 WorkspaceFileRevision 解析成布尔；纯函数不查库
    def resolver(execution: object) -> bool:
        args = getattr(execution, "sanitized_arguments", {}) or {}
        return args.get("path") == "changed.md"

    executions = [
        _execution(
            tool_name="write_file",
            path="changed.md",
            effect="write",
            tool_call_id="c1",
            assistant_message_id="msg-1",
            run_id="run-x",
        ),
        _execution(
            tool_name="write_file",
            path="same.md",
            effect="write",
            tool_call_id="c2",
            assistant_message_id="msg-2",
            run_id="run-x",
        ),
    ]
    signal = build_no_progress_signal(
        executions, run_id="run-x", material_change_of=resolver
    )
    assert signal is not None
    # msg-1 真实变更 gain=3，msg-2 空编辑 gain=0 → streak 1
    assert signal.streak == 1
    assert signal.last_round_gain == 0


def test_build_signal_without_assistant_message_id_falls_back_to_call_id() -> None:
    # 无 assistant_message_id 的执行退化为按 tool_call_id 单例轮
    executions = [
        _execution(
            tool_name="read_file",
            path="a.py",
            content_hash="h1",
            tool_call_id="c1",
            run_id="run-x",
        ),
        _execution(
            tool_name="read_file",
            path="a.py",
            content_hash="h1",
            tool_call_id="c2",
            run_id="run-x",
        ),
    ]
    signal = build_no_progress_signal(executions, run_id="run-x")
    assert signal is not None
    # 两轮：c1 新读 +1，c2 重复读 0 → streak 1
    assert signal.streak == 1


# ---------------------------------------------------------------------------
# no_progress_message：阶梯措辞
# ---------------------------------------------------------------------------


def test_no_progress_message_none_for_ladder_none() -> None:
    signal = fold_rounds([])
    assert signal.level == LADDER_NONE
    assert no_progress_message(signal) is None


def test_no_progress_message_wording_per_level() -> None:
    nudge = no_progress_message(
        NoProgressSignal(streak=3, level=LADDER_NUDGE, last_round_gain=0)
    )
    pivot = no_progress_message(
        NoProgressSignal(streak=5, level=LADDER_PIVOT, last_round_gain=0)
    )
    stop = no_progress_message(
        NoProgressSignal(streak=8, level=LADDER_STOP, last_round_gain=0)
    )
    assert nudge is not None and "连续 3 轮" in nudge and "换一种方式" in nudge
    assert pivot is not None and "连续 5 轮" in pivot and "必须改变策略" in pivot
    assert stop is not None and "连续 8 轮" in stop and "停止继续探索" in stop
    # 字节确定性：同信号 → 同消息（前缀缓存友好）
    assert nudge == no_progress_message(
        NoProgressSignal(streak=3, level=LADDER_NUDGE, last_round_gain=0)
    )



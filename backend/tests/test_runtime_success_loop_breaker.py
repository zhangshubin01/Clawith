"""工具调用死循环熔断器 _trailing_identical_success_loop 的单元测试。

消息形态与图状态通道一致（convert_to_openai_messages 输出）：
assistant.tool_calls = [{id, type, function: {name, arguments: JSON字符串}}]，
tool 消息只有 role/tool_call_id/name/content（无 execution_status）。
熔断器只统计「当前 run 台账（ledger）内的 tool_call_id」——同一 thread
上其他 run 的历史调用不得计入。
"""

import json

from app.services.agent_runtime.model_step_service import (
    _soft_loop_reminder,
    _soft_loop_reminder_message,
    _trailing_identical_success_loop,
)


def _assistant_with_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": "收到，执行：",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
    }


def _tool_result(call_id: str, name: str = "android_compile") -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": "Android build succeeded: assembleDebug",
    }


ARGS = {"task": "assembleDebug", "java_version": "17", "project_path": "workspace/x"}


def _cycle(i: int, *, args: dict | None = None, name: str = "android_compile") -> list[dict]:
    call_id = f"call-{i}"
    return [
        _assistant_with_call(call_id, name, ARGS if args is None else args),
        _tool_result(call_id, name),
    ]


def _ledger_for(messages, *, tool_name="android_compile") -> dict:
    return {
        m["tool_call_id"]: {"tool_name": tool_name, "status": "succeeded"}
        for m in messages
        if isinstance(m, dict) and m.get("role") == "tool"
    }


def test_identical_loop_detected_on_real_shape() -> None:
    messages = []
    for i in range(7):
        messages.extend(_cycle(i))
    result = _trailing_identical_success_loop(messages, _ledger_for(messages))
    assert result is not None
    assert result == ("android_compile", 7)


def test_below_threshold_returns_none() -> None:
    messages = []
    for i in range(4):
        messages.extend(_cycle(i))
    assert _trailing_identical_success_loop(messages, _ledger_for(messages)) is None


def test_old_thread_history_outside_ledger_does_not_trigger() -> None:
    """同一 thread 上历史 run 的 16 连相同调用 + 新 run 空台账 → 不误伤。

    这是 2026-08-19 的线上事故回归：新 run 被历史调用秒杀（tool_success_loop）。
    """
    messages = []
    for i in range(16):
        messages.extend(_cycle(i))
    # 新 run 刚开始：台账为空（尚未执行任何工具）
    assert _trailing_identical_success_loop(messages, {}) is None
    # 台账只含新 run 自己的 1 条 → 同样不触发
    new_messages = list(messages) + _cycle(9000)
    new_ledger = {
        "call-9000": {"tool_name": "android_compile", "status": "succeeded"},
    }
    assert _trailing_identical_success_loop(new_messages, new_ledger) is None


def test_argument_key_order_is_normalized() -> None:
    # arguments JSON 字符串键序不同但内容相同 → 签名一致，仍判循环
    messages = []
    for i in range(3):
        messages.extend(_cycle(i, args=ARGS))
    messages.extend(_cycle(100, args={"java_version": "17", "task": "assembleDebug", "project_path": "workspace/x"}))
    messages.extend(_cycle(101, args={"project_path": "workspace/x", "java_version": "17", "task": "assembleDebug"}))
    result = _trailing_identical_success_loop(messages, _ledger_for(messages))
    assert result is not None
    assert result[0] == "android_compile"


def test_different_arguments_break_the_run() -> None:
    messages = []
    for i in range(3):
        messages.extend(_cycle(i))
    # 尾部换成不同参数，且只有 3 个（不足阈值）→ 不判循环
    for i in range(4, 7):
        messages.extend(_cycle(i, args={"task": "clean", "project_path": "workspace/x"}))
    assert _trailing_identical_success_loop(messages, _ledger_for(messages)) is None


def test_mixed_tools_break_the_run() -> None:
    messages = []
    for i in range(3):
        messages.extend(_cycle(i))
    messages.extend(_cycle(100, name="read_file"))
    for i in range(4, 8):
        messages.extend(_cycle(i))
    # 尾部只有 4 个相同（前面隔着 read_file）→ 不足阈值
    assert _trailing_identical_success_loop(messages, _ledger_for(messages)) is None


def test_loop_recovered_after_other_tool_exits_window() -> None:
    # 其他工具出现后，只要尾部窗口内又积累 ≥5 个相同调用，仍判循环
    messages = []
    messages.extend(_cycle(900, name="read_file"))
    for i in range(6):
        messages.extend(_cycle(i))
    result = _trailing_identical_success_loop(messages, _ledger_for(messages))
    assert result is not None
    assert result[0] == "android_compile"


def test_malformed_messages_are_ignored() -> None:
    messages = ["not a dict", None, 42, {"role": "tool", "tool_call_id": "ghost"}]
    for i in range(6):
        messages.extend(_cycle(i))
    result = _trailing_identical_success_loop(messages, _ledger_for(messages))
    assert result is not None
    assert result[0] == "android_compile"


def _run_marker(run_id: str) -> dict:
    return {
        "id": f"current-input-{run_id}",
        "role": "user",
        "content": "compile",
        "runtime_input": "current",
        "runtime_run_id": run_id,
    }


def test_prior_run_loop_does_not_kill_the_new_run_even_with_merged_ledger() -> None:
    """H-1 隔离：旧 run 死在循环中途（16 连相同调用），其执行行已并入台账。

    prior-incomplete 非空时 _load 会把旧 run 全部执行行并入台账——若检测器
    只按台账认领消息，新 run 首步就会被旧 run 的尾部循环秒杀（7c70b3f1
    同类误伤路径）。按 run 起点边界切片后不得触发。
    """
    prior_messages = []
    for i in range(16):
        prior_messages.extend(_cycle(i))
    new_run_id = "run-9000"
    messages = prior_messages + [_run_marker(new_run_id)] + _cycle(9000)
    merged_ledger = _ledger_for(prior_messages) | _ledger_for(_cycle(9000))
    assert (
        _trailing_identical_success_loop(
            messages,
            merged_ledger,
            current_run_id=new_run_id,
        )
        is None
    )
    # 正例对照：新 run 自己再累计到 5 连相同调用 → 正常触发。
    for i in range(1, 5):
        messages.extend(_cycle(9000 + i))
    merged_ledger.update(
        _ledger_for([m for i in range(1, 5) for m in _cycle(9000 + i)])
    )
    assert _trailing_identical_success_loop(
        messages,
        merged_ledger,
        current_run_id=new_run_id,
    ) == ("android_compile", 5)


def test_async_poll_cycles_are_exempt_from_the_loop_breaker() -> None:
    """H-2 轮询豁免：平台轮询周期每周期追加同 poll_call_id 同参数的
    tool 消息 + runtime_intent=async_poll 的 assistant 提案，不得被当成
    模型循环误杀（已复现 ('android_build_poll', 6) 误杀）。"""
    run_id = "run-poll"

    def poll_cycle() -> list[dict]:
        return [
            _tool_result("poll-call-1", name="android_build_poll"),
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "poll-call-1",
                        "type": "function",
                        "function": {
                            "name": "android_build_poll",
                            "arguments": json.dumps(
                                {"operation_key": "op-1"}, ensure_ascii=False
                            ),
                        },
                    }
                ],
                "runtime_intent": "async_poll",
                "runtime_run_id": run_id,
            },
        ]

    messages = [_run_marker(run_id)]
    for _ in range(6):
        messages.extend(poll_cycle())
    ledger = {
        "poll-call-1": {"tool_name": "android_build_poll", "status": "succeeded"},
    }
    assert (
        _trailing_identical_success_loop(
            messages,
            ledger,
            current_run_id=run_id,
        )
        is None
    )
    # 正例对照：轮询结束后模型自己又真循环 5 次 → 仍能触发。
    for i in range(5):
        messages.extend(_cycle(8000 + i))
    ledger.update(
        {
            f"call-{8000 + i}": {"tool_name": "android_compile", "status": "succeeded"}
            for i in range(5)
        }
    )
    assert _trailing_identical_success_loop(
        messages,
        ledger,
        current_run_id=run_id,
    ) == ("android_compile", 5)


def test_soft_reminder_fires_at_three_identical_calls() -> None:
    """L2 软提醒：3 连相同调用即触发，状态取自执行台账而非图状态通道。"""
    run_id = "run-soft"
    messages = [_run_marker(run_id)]
    for i in range(3):
        messages.extend(_cycle(i))
    ledger = _ledger_for(messages)
    assert _soft_loop_reminder(messages, ledger, current_run_id=run_id) == (
        "android_compile",
        "succeeded",
        3,
    )


def test_soft_reminder_wording_matches_the_ledger_status() -> None:
    """措辞按台账 status 区分成败；命令式、不带条件逃逸、字节确定。"""
    run_id = "run-soft"
    messages = [_run_marker(run_id)]
    for i in range(3):
        messages.extend(_cycle(i))
    failed_ledger = {
        f"call-{i}": {
            "tool_name": "android_compile",
            "status": "failed",
            "error_code": "sandbox_error",
        }
        for i in range(3)
    }
    signal = _soft_loop_reminder(messages, failed_ledger, current_run_id=run_id)
    assert signal == ("android_compile", "failed", 3)
    message = _soft_loop_reminder_message(signal)
    assert message.role == "user"
    assert "连续 3 次" in message.content
    assert "android_compile" in message.content
    assert "均执行失败" in message.content
    assert "停止重试" in message.content
    assert "如实向用户报告" in message.content
    # 成功措辞对照：直接汇报，不出现「重试」
    ok = _soft_loop_reminder_message(("android_compile", "succeeded", 3))
    assert "均已执行" in ok.content
    assert "停止重复调用" in ok.content
    assert "直接基于已有结果输出最终回复" in ok.content
    # 字节确定性：同信号 → 同消息（前缀缓存友好）
    assert ok == _soft_loop_reminder_message(("android_compile", "succeeded", 3))


def test_soft_reminder_respects_run_isolation_and_threshold() -> None:
    """跨 run 不注入；低于阈值不注入。"""
    # 旧 run 的 6 连在边界之前 → 不注入提醒
    prior = []
    for i in range(6):
        prior.extend(_cycle(i))
    run_id = "run-soft-new"
    messages = prior + [_run_marker(run_id)] + _cycle(7000)
    merged = _ledger_for(prior) | _ledger_for(_cycle(7000))
    assert _soft_loop_reminder(messages, merged, current_run_id=run_id) is None
    # 低于阈值（2 连）→ 不注入
    few = [_run_marker(run_id)] + _cycle(0) + _cycle(1)
    assert _soft_loop_reminder(few, _ledger_for(few), current_run_id=run_id) is None

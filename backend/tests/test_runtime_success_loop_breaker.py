"""工具调用死循环熔断器 _trailing_identical_success_loop 的单元测试。

消息形态与图状态通道一致（convert_to_openai_messages 输出）：
assistant.tool_calls = [{id, type, function: {name, arguments: JSON字符串}}]，
tool 消息只有 role/tool_call_id/name/content（无 execution_status）。
熔断器只统计「当前 run 台账（ledger）内的 tool_call_id」——同一 thread
上其他 run 的历史调用不得计入。
"""

from app.services.agent_runtime.model_step_service import (
    _trailing_identical_success_loop,
)


def _assistant_with_call(call_id: str, name: str, arguments: dict) -> dict:
    import json

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

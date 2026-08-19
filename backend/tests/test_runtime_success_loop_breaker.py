"""成功型工具调用死循环熔断器 _trailing_identical_success_loop 的单元测试。"""

from app.services.agent_runtime.model_step_service import (
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
                "function": {"name": name, "arguments": arguments},
                "name": name,
                "arguments": arguments,
            }
        ],
    }


def _tool_result(call_id: str, status: str = "succeeded") -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "android_compile",
        "content": "Android build succeeded",
        "execution_status": status,
    }


def _cycle(i: int, *, args: dict | None = None, status: str = "succeeded", name: str = "android_compile") -> list[dict]:
    args = {"task": "assembleDebug", "project_path": "workspace/x"} if args is None else args
    call_id = f"call-{i}"
    return [_assistant_with_call(call_id, name, args), _tool_result(call_id, status)]


def test_identical_success_loop_detected() -> None:
    messages = []
    for i in range(7):
        messages.extend(_cycle(i))
    result = _trailing_identical_success_loop(messages)
    assert result is not None
    name, count = result
    assert name == "android_compile"
    assert count >= 5


def test_below_threshold_returns_none() -> None:
    messages = []
    for i in range(4):
        messages.extend(_cycle(i))
    assert _trailing_identical_success_loop(messages) is None


def test_different_arguments_break_the_run() -> None:
    messages = []
    for i in range(3):
        messages.extend(_cycle(i, args={"task": "assembleDebug", "project_path": "workspace/x"}))
    # 尾部换成不同参数的成功调用 → 不应判循环
    messages.extend(_cycle(100, args={"task": "clean", "project_path": "workspace/x"}))
    messages.extend(_cycle(101, args={"task": "clean", "project_path": "workspace/x"}))
    messages.extend(_cycle(102, args={"task": "clean", "project_path": "workspace/x"}))
    messages.extend(_cycle(103, args={"task": "clean", "project_path": "workspace/x"}))
    assert _trailing_identical_success_loop(messages) is None


def test_failed_calls_do_not_count() -> None:
    messages = []
    for i in range(5):
        messages.extend(_cycle(i, status="failed"))
    assert _trailing_identical_success_loop(messages) is None


def test_mixed_tools_break_the_run() -> None:
    messages = []
    for i in range(3):
        messages.extend(_cycle(i))
    messages.extend(_cycle(100, name="read_file"))
    for i in range(4, 8):
        messages.extend(_cycle(i))
    # 尾部 4 个相同，前面隔着 read_file → 尾部计数不足阈值
    assert _trailing_identical_success_loop(messages) is None


def test_other_progress_between_identical_calls_breaks_the_run() -> None:
    # 相同调用之间插入了其他工具 → 不构成「连续一致」循环
    messages = []
    for i in range(2):
        messages.extend(_cycle(i))
    messages.extend(_cycle(50, name="read_file"))
    for i in range(3, 7):
        messages.extend(_cycle(i))
    assert _trailing_identical_success_loop(messages) is None


def test_malformed_messages_are_ignored() -> None:
    messages = [{"role": "tool", "execution_status": "succeeded"}, "not a dict", None, 42]
    for i in range(6):
        messages.extend(_cycle(i))
    result = _trailing_identical_success_loop(messages)
    assert result is not None
    assert result[0] == "android_compile"

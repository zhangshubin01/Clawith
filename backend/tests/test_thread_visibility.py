"""Run-boundary thread windowing tests.

Direct Chat shares one LangGraph Thread across multiple Runs. The model window
must have a hard left edge at the current Run's marker so a new Run never
inherits a previous Run's directive or tool facts verbatim. These tests pin the
pure ``bound_current_run_window`` seam, including the prior-Run summary bridge
that inlines resolved ``tool-result://`` content as bounded excerpts.
"""

import uuid
from collections.abc import Awaitable, Callable

from app.services.agent_runtime.thread_visibility import (
    bound_current_run_window,
    summary_is_stale_for_run,
)

_TOOL_RESULT_REF = "tool-result://" + str(uuid.UUID(int=1))


def _marker(run_id: str, content: str) -> dict:
    return {
        "id": f"current-input-{run_id}",
        "role": "user",
        "content": content,
        "runtime_input": "current",
        "runtime_run_id": run_id,
    }


def _assistant(run_id: str, message_id: str, call_ids: list[str]) -> dict:
    return {
        "id": message_id,
        "role": "assistant",
        "content": None,
        "runtime_run_id": run_id,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": f"tool_{call_id}", "arguments": "{}"},
            }
            for call_id in call_ids
        ],
    }


def _result(run_id: str, message_id: str, call_id: str, *, result_ref: str | None = None) -> dict:
    message = {
        "id": message_id,
        "role": "tool",
        "tool_call_id": call_id,
        "content": f"result:{call_id}",
    }
    if result_ref is not None:
        message["result_ref"] = result_ref
    return message


def _plain(run_id: str, message_id: str, content: str) -> dict:
    return {"id": message_id, "role": "assistant", "content": content, "runtime_run_id": run_id}


def _fake_resolver(
    contents: dict[str, str] | None = None,
    *,
    fail_with: Exception | None = None,
) -> Callable[[str], Awaitable[str | None]]:
    """Deterministic async resolver standing in for ``ToolResultStore.resolve``."""

    async def resolve(ref: str) -> str | None:
        if fail_with is not None:
            raise fail_with
        return (contents or {}).get(ref)

    return resolve


async def test_single_run_thread_returns_whole_thread_without_summary():
    messages = [
        _marker("run-a", "compile"),
        _assistant("run-a", "a1", ["call-1"]),
        _result("run-a", "r1", "call-1"),
    ]

    summary, current = await bound_current_run_window(messages, current_run_id="run-a")

    assert summary is None
    assert [message["id"] for message in current] == [
        "current-input-run-a",
        "a1",
        "r1",
    ]


async def test_prior_run_is_collapsed_into_a_summary_and_current_run_is_preserved():
    prior = [
        _marker("run-prior", "重新编译项目"),
        _assistant("run-prior", "p1", ["call-compile"]),
        _result("run-prior", "p2", "call-compile", result_ref="artifact://app-debug-20260819-1457.apk"),
        _plain("run-prior", "p3", "✅ 项目重新编译完成"),
    ]
    current = [
        _marker("run-current", "优化这个Android项目"),
        _assistant("run-current", "c1", ["call-read"]),
        _result("run-current", "c2", "call-read"),
    ]
    messages = [*prior, *current]

    summary, window = await bound_current_run_window(messages, current_run_id="run-current")

    assert summary is not None
    assert summary["role"] == "user"
    assert isinstance(summary["id"], str) and summary["id"]
    # The previous run's directive and its artifact are summarized, not dropped.
    assert "重新编译项目" in summary["content"]
    assert "app-debug-20260819-1457.apk" in summary["content"]
    # The window starts exactly at the current run's marker.
    assert [message["id"] for message in window] == [
        "current-input-run-current",
        "c1",
        "c2",
    ]
    # Prior run messages never leak into the window verbatim.
    prior_ids = {"current-input-run-prior", "p1", "p2", "p3"}
    assert prior_ids.isdisjoint({message["id"] for message in window})


async def test_no_current_marker_returns_whole_thread_without_summary():
    # Legacy single-run threads without a marker must not be corrupted.
    messages = [
        {"id": "m1", "role": "user", "content": "hi"},
        {"id": "m2", "role": "assistant", "content": "hello"},
    ]

    summary, current = await bound_current_run_window(messages, current_run_id="run-x")

    assert summary is None
    assert [message["id"] for message in current] == ["m1", "m2"]


async def test_summary_drops_prior_plain_assistant_reply_and_keeps_only_goal_and_artifacts():
    # Behavior changed with the R4 bridge: the artifact ref is a
    # ``tool-result://`` URI and is inlined as a resolved excerpt, still
    # without the prior Run's closing reply.
    resolver = _fake_resolver({_TOOL_RESULT_REF: "compile output\nBUILD OK"})
    prior = [
        _marker("run-prior", "重新编译项目"),
        _assistant("run-prior", "p1", ["call-compile"]),
        _result("run-prior", "p2", "call-compile", result_ref=_TOOL_RESULT_REF),
        _plain("run-prior", "p3", "✅ 项目重新编译完成 … app-debug.apk"),
    ]
    messages = [*prior, _marker("run-current", "优化")]

    summary, _ = await bound_current_run_window(
        messages, current_run_id="run-current", resolve_tool_result=resolver
    )

    # The prior run's own closing reply must not be replayed as a directive.
    assert "重新编译完成" not in summary["content"]
    assert "重新编译项目" in summary["content"]
    # The opaque URI is replaced by the resolved content excerpt.
    assert _TOOL_RESULT_REF not in summary["content"]
    assert "BUILD OK" in summary["content"]


async def test_summary_is_history_context_not_a_new_instruction():
    # Regression: the summary is a user-role message, so its wording must not
    # read as a new directive. "目标：重新编译项目" made the model treat the
    # prior goal as the current task (it literally replied "用户说…目标：重新
    # 编译项目" and recompiled). The wording must be past-tense history, marked
    # as not-the-current-task, and must never use the imperative "目标：".
    prior = [
        _marker("run-prior", "重新编译项目"),
        _assistant("run-prior", "p1", ["call-compile"]),
        _result("run-prior", "p2", "call-compile", result_ref="artifact://apk"),
    ]
    messages = [*prior, _marker("run-current", "优化现在的app项目")]

    summary, _ = await bound_current_run_window(messages, current_run_id="run-current")

    content = summary["content"]
    assert "目标：" not in content
    assert "历史" in content or "已完成" in content
    assert "非当前" in content or "已结束" in content
    assert "重新编译项目" in content
    assert "artifact://apk" in content


def test_summary_without_watermark_is_not_stale():
    messages = [
        _marker("run-prior", "旧任务"),
        _marker("run-current", "新任务"),
    ]
    assert not summary_is_stale_for_run(
        messages,
        current_run_id="run-current",
        summary_covered_through_message_id=None,
    )


def test_single_run_thread_summary_is_not_stale():
    messages = [
        _marker("run-a", "compile"),
        _assistant("run-a", "a1", ["call-1"]),
        _result("run-a", "r1", "call-1"),
    ]
    assert not summary_is_stale_for_run(
        messages,
        current_run_id="run-a",
        summary_covered_through_message_id="r1",
    )


def test_no_marker_summary_is_not_stale():
    messages = [
        {"id": "m1", "role": "user", "content": "hi"},
        {"id": "m2", "role": "assistant", "content": "hello"},
    ]
    assert not summary_is_stale_for_run(
        messages,
        current_run_id="run-x",
        summary_covered_through_message_id="m1",
    )


def test_stale_summary_when_prior_run_messages_precede_marker():
    # The watermark message is gone (compaction RemoveMessage'd it), and the
    # current Run's marker is not the first message — prior-Run messages still
    # precede it, so the summary covers only pre-Run history.
    prior = [
        _marker("run-prior", "旧任务"),
        _assistant("run-prior", "p1", ["call-1"]),
        _result("run-prior", "p2", "call-1"),
    ]
    current = [
        _marker("run-current", "新任务"),
        _assistant("run-current", "c1", ["call-2"]),
    ]
    messages = [*prior, *current]
    assert summary_is_stale_for_run(
        messages,
        current_run_id="run-current",
        summary_covered_through_message_id="compacted-away-message-id",
    )


def test_summary_retained_when_prior_run_compacted():
    # After the prior Run is compacted away, the current Run's marker is the
    # first message; the summary covers (or belongs to) the current Run and
    # must stay visible.
    current = [
        _marker("run-current", "新任务"),
        _assistant("run-current", "c1", ["call-2"]),
        _result("run-current", "c2", "call-2"),
    ]
    assert not summary_is_stale_for_run(
        current,
        current_run_id="run-current",
        summary_covered_through_message_id="compacted-away-message-id",
    )


async def test_summary_inlines_resolved_tool_result_content():
    # The dead ``tool-result://`` URI is replaced by a bounded excerpt of the
    # stored tool result content (head / gap marker / tail).
    long_content = "HEAD-" * 600 + "\n" + "TAIL-" * 300
    resolver = _fake_resolver({_TOOL_RESULT_REF: long_content})
    prior = [
        _marker("run-prior", "重新编译项目"),
        _assistant("run-prior", "p1", ["call-compile"]),
        _result("run-prior", "p2", "call-compile", result_ref=_TOOL_RESULT_REF),
    ]
    messages = [*prior, _marker("run-current", "优化")]

    summary, _ = await bound_current_run_window(
        messages, current_run_id="run-current", resolve_tool_result=resolver
    )

    content = summary["content"]
    assert _TOOL_RESULT_REF not in content
    assert "HEAD-" in content
    assert "TAIL-" in content
    assert content.count("省略") == 1
    assert content.startswith("历史上下文（非当前任务）：上一轮已完成")
    assert "重新编译项目" in content


async def test_summary_keeps_short_tool_result_content_verbatim():
    short_content = "BUILD SUCCESSFUL in 42s\nartifact: app.apk"
    resolver = _fake_resolver({_TOOL_RESULT_REF: short_content})
    prior = [
        _marker("run-prior", "重新编译项目"),
        _assistant("run-prior", "p1", ["call-compile"]),
        _result("run-prior", "p2", "call-compile", result_ref=_TOOL_RESULT_REF),
    ]
    messages = [*prior, _marker("run-current", "优化")]

    summary, _ = await bound_current_run_window(
        messages, current_run_id="run-current", resolve_tool_result=resolver
    )

    content = summary["content"]
    assert _TOOL_RESULT_REF not in content
    assert short_content in content
    assert "省略" not in content


async def test_summary_inlines_multiple_tool_results_in_order():
    ref_a = "tool-result://" + str(uuid.UUID(int=1))
    ref_b = "tool-result://" + str(uuid.UUID(int=2))
    resolver = _fake_resolver({ref_a: "AAA-output", ref_b: "BBB-output"})
    prior = [
        _marker("run-prior", "重新编译项目"),
        _assistant("run-prior", "p1", ["call-a", "call-b"]),
        _result("run-prior", "p2", "call-a", result_ref=ref_a),
        _result("run-prior", "p3", "call-b", result_ref=ref_b),
    ]
    messages = [*prior, _marker("run-current", "优化")]

    summary, _ = await bound_current_run_window(
        messages, current_run_id="run-current", resolve_tool_result=resolver
    )

    content = summary["content"]
    assert "[1] AAA-output" in content
    assert "[2] BBB-output" in content
    assert ref_a not in content
    assert ref_b not in content


async def test_summary_falls_back_to_legacy_ref_when_resolution_returns_none():
    # Missing / unsettled results must fail open: the raw ref stays in the
    # summary (legacy format) and Run startup is never blocked.
    resolver = _fake_resolver({})
    prior = [
        _marker("run-prior", "重新编译项目"),
        _assistant("run-prior", "p1", ["call-compile"]),
        _result("run-prior", "p2", "call-compile", result_ref=_TOOL_RESULT_REF),
    ]
    messages = [*prior, _marker("run-current", "优化")]

    summary, _ = await bound_current_run_window(
        messages, current_run_id="run-current", resolve_tool_result=resolver
    )

    content = summary["content"]
    assert _TOOL_RESULT_REF in content
    assert "重新编译项目" in content
    assert content.startswith("历史上下文（非当前任务）：上一轮已完成")


async def test_summary_falls_back_to_legacy_ref_when_resolver_raises():
    # Even a broken resolver (storage outage, unexpected errors) must not
    # block the Run from starting: the legacy raw ref is kept instead.
    resolver = _fake_resolver(fail_with=RuntimeError("storage backend down"))
    prior = [
        _marker("run-prior", "重新编译项目"),
        _assistant("run-prior", "p1", ["call-compile"]),
        _result("run-prior", "p2", "call-compile", result_ref=_TOOL_RESULT_REF),
    ]
    messages = [*prior, _marker("run-current", "优化")]

    summary, _ = await bound_current_run_window(
        messages, current_run_id="run-current", resolve_tool_result=resolver
    )

    content = summary["content"]
    assert _TOOL_RESULT_REF in content
    assert "重新编译项目" in content
    assert content.startswith("历史上下文（非当前任务）：上一轮已完成")


async def test_summary_inline_wording_stays_past_tense_history():
    # The inlined excerpt must not turn the summary into a new directive: the
    # past-tense "非当前任务" frame is unchanged even with resolved content.
    resolver = _fake_resolver({_TOOL_RESULT_REF: "BUILD OK"})
    prior = [
        _marker("run-prior", "重新编译项目"),
        _assistant("run-prior", "p1", ["call-compile"]),
        _result("run-prior", "p2", "call-compile", result_ref=_TOOL_RESULT_REF),
    ]
    messages = [*prior, _marker("run-current", "优化现在的app项目")]

    summary, _ = await bound_current_run_window(
        messages, current_run_id="run-current", resolve_tool_result=resolver
    )

    content = summary["content"]
    assert "目标：" not in content
    assert "非当前" in content
    assert content.startswith("历史上下文（非当前任务）：上一轮已完成")
    assert "重新编译项目" in content
    assert "BUILD OK" in content


async def test_summary_keeps_non_tool_result_refs_verbatim():
    # Only opaque ``tool-result://`` refs go through the resolver; other ref
    # schemes (artifact://, ...) stay verbatim and are never resolved.
    consulted: list[str] = []

    async def spy_resolver(ref: str) -> str | None:
        consulted.append(ref)
        return "should-not-be-used"

    prior = [
        _marker("run-prior", "重新编译项目"),
        _assistant("run-prior", "p1", ["call-compile"]),
        _result("run-prior", "p2", "call-compile", result_ref="artifact://apk"),
    ]
    messages = [*prior, _marker("run-current", "优化")]

    summary, _ = await bound_current_run_window(
        messages, current_run_id="run-current", resolve_tool_result=spy_resolver
    )

    assert "artifact://apk" in summary["content"]
    assert consulted == []

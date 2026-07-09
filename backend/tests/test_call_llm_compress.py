"""call_llm 压缩接线的 mock 集成测试.

覆盖 68 单元测试之外的「接线」验收点（评审要求）：
- Layer 0：_process_tool_call 区分 Tier1 exclude 与可压工具结果
- Blocker #4：vision list 结果保留结构，不做字符串压缩
- Blocker #6：已含 marker（执行期已压）则跳过，避免双压
- 会话级 dedup 隔离：不同 ContextCompressor 实例互不污染
- repair：_ctx_compress 清理孤儿 tool 链
"""
import sys
sys.path.insert(0, '/Users/shubinzhang/Documents/agent/Clawith/backend')

import json
import pytest

from app.services.llm.client import LLMMessage
from app.services.llm import caller
from app.services.llm.caller import (
    _apply_tool_result,
    _process_tool_call,
    _ctx_compress,
    _dedup_file_tool_results,
    _repair_truncated_messages,
    ContextCompressor,
)
from app.services.llm.tool_trim import _COMPRESS_MARKER, _TOOL_HARD_CEIL_CHARS


async def _run_process_tool_call(result, tool_name="read_file", *, compress_enabled=True,
                                 supports_vision=False, model_name="gpt-4o"):
    """驱动 _process_tool_call，用 monkeypatch 替换 execute_tool 返回给定 result。"""
    api_messages: list = []
    # 传合法参数，避开 TOOLS_REQUIRING_ARGS guard 的提前返回
    tc = {"id": "t1", "function": {
        "name": tool_name,
        "arguments": json.dumps({"file_path": "a.py", "query": "q"}),
    }}

    async def _fake_execute_tool(name, args, **kwargs):
        return result

    orig = caller.execute_tool
    caller.execute_tool = _fake_execute_tool
    try:
        result = await _process_tool_call(
            tc=tc,
            api_messages=api_messages,
            agent_id="agent-1",
            user_id="user-1",
            session_id="sess-1",
            supports_vision=supports_vision,
            on_tool_call=None,
            full_reasoning_content="",
            allowed_tool_names={tool_name},
            model_name=model_name,
            ctx_window=100000,
            compress_enabled=compress_enabled,
        )
        _apply_tool_result(result, api_messages)
    finally:
        caller.execute_tool = orig
    return api_messages


class TestLayer0ProcessToolCall:
    async def test_excluded_search_file_kept_verbatim(self):
        """Tier1 search_file 对齐 headroom Grep，超大结果也原样保留。"""
        big = json.dumps([{"id": i, "name": f"item_{i}", "d": "x" * 40} for i in range(400)])
        msgs = await _run_process_tool_call(big, tool_name="search_file")
        content = msgs[0].content
        assert isinstance(content, str)
        assert content == big
        assert _COMPRESS_MARKER not in content

    async def test_compressible_search_clawhub_result_gets_marker(self, monkeypatch):
        """Tier2 search_clawhub 仍可走压缩 + CCR marker。"""
        async def _fake_store(**kwargs):
            return "e" * 64

        monkeypatch.setattr("app.services.llm.ccr_store.store_entry", _fake_store)
        big = json.dumps([{"id": i, "name": f"item_{i}", "d": "x" * 40} for i in range(400)])
        msgs = await _run_process_tool_call(big, tool_name="search_clawhub")
        content = msgs[0].content
        assert isinstance(content, str)
        assert content.startswith("<!-- ccr:" + "e" * 64 + " -->")
        assert len(content) < len(big)

    async def test_small_tool_result_untouched(self):
        """小工具结果不触发压缩，无 marker."""
        small = "OK build successful"
        msgs = await _run_process_tool_call(small)
        assert msgs[0].content == small
        assert _COMPRESS_MARKER not in msgs[0].content

    async def test_flag_off_keeps_hard_ceiling(self, monkeypatch):
        """flag 关闭时 Layer1 透传；hard_ceil 需 CCR store 成功才截断（P0 可逆性）。"""
        async def _fake_store(**kwargs):
            return "c" * 64

        monkeypatch.setattr("app.services.llm.ccr_store.store_entry", _fake_store)
        huge = "a" * (_TOOL_HARD_CEIL_CHARS + 10000)
        msgs = await _run_process_tool_call(huge, compress_enabled=False)
        content = msgs[0].content
        assert len(content) < len(huge)
        assert "<!-- ccr:" in content

    async def test_already_marked_skipped(self):
        """已含 marker（执行期已压）→ 跳过再压缩（防双压）."""
        pre = _COMPRESS_MARKER + "\n" + ("x" * 9000)
        msgs = await _run_process_tool_call(pre, tool_name="read_file")
        assert msgs[0].content == pre           # 原样透传，未二次处理


class TestVisionListBypass:
    async def test_vision_list_not_string_compressed(self, monkeypatch):
        """screenshot vision 注入返回 list → 保留结构，不字符串压缩（Blocker #4）."""
        vision_blocks = [
            {"type": "text", "text": "screenshot captured"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]

        def _fake_inject(tool_name, result_str, ws_path):
            return vision_blocks

        monkeypatch.setattr(
            "app.services.vision_inject.try_inject_screenshot_vision", _fake_inject
        )
        msgs = await _run_process_tool_call(
            "raw screenshot path", tool_name="take_screenshot", supports_vision=True
        )
        assert isinstance(msgs[0].content, list)   # list 结构保留
        assert msgs[0].content == vision_blocks


class TestSessionDedupIsolation:
    def test_dedup_store_isolated_between_sessions(self):
        """两个会话的 ContextCompressor 各自 dedup，不跨污染."""
        def _make_msgs():
            return [
                LLMMessage(role="assistant", content="ok", tool_calls=[{
                    "id": "1",
                    "function": {"name": "read_file", "arguments": '{"file_path":"a.py"}'},
                }]),
                LLMMessage(role="tool", content="X" * 2000, tool_call_id="1"),
            ]

        c1 = ContextCompressor(session_id="s1")
        c2 = ContextCompressor(session_id="s2")

        m1 = _make_msgs()
        _dedup_file_tool_results(m1, round_i=0, dedup_store=c1._dedup_seen)
        # c1 记录了该内容哈希
        assert len(c1._dedup_seen) == 1
        # c2 未受影响
        assert len(c2._dedup_seen) == 0

        # 同一内容在 c2 首次出现，不应被当作重复
        m2 = _make_msgs()
        _dedup_file_tool_results(m2, round_i=0, dedup_store=c2._dedup_seen)
        assert "DUPLICATE-READ" not in (m2[1].content or "")


class TestRepairIntegration:
    def test_repair_removes_orphan_tool(self):
        """call_llm 首轮前 _repair_truncated_messages 清理孤儿 tool 消息（防 LLM 400）."""
        msgs = [
            LLMMessage(role="user", content="hi"),
            LLMMessage(role="tool", content="orphan", tool_call_id="nonexistent"),
        ]
        result = _repair_truncated_messages(msgs)
        assert all(getattr(m, "role", None) != "tool" for m in result)
        assert len(result) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

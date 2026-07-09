"""CTX 计划验收 — E1 跨 prompt / E3 cache / D0 高压（不依赖 IDE）。"""
import json
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.plugins.clawith_acp.acp_session import AcpSessionManager
from app.plugins.clawith_acp.history_cache import invalidate_history_cache
from app.services.llm.client import LLMMessage
from app.services.llm import caller
from app.services.llm.caller import ContextCompressor, _apply_pre_round_context
from app.services.llm.tool_trim import _TOOL_HARD_CEIL_CHARS
from app.services.llm.utils import convert_chat_messages_to_llm_format, format_history_tool_content


def _msg(role: str, content: str):
    return SimpleNamespace(role=role, content=content, id=uuid.uuid4(), thinking=None)


def _build_pressure_messages(rounds: int = 6) -> list[LLMMessage]:
    """造压：多轮 read_file + git_status 大结果。"""
    msgs: list[LLMMessage] = [LLMMessage(role="system", content="sys")]
    for r in range(rounds):
        for tool, blob in (("read_file", "R" * 12000), ("git_status", "G" * 8000)):
            tc_id = f"tc{r}_{tool}"
            msgs.append(
                LLMMessage(
                    role="assistant",
                    content="",
                    tool_calls=[{"id": tc_id, "function": {"name": tool, "arguments": "{}"}}],
                )
            )
            msgs.append(LLMMessage(role="tool", content=blob, tool_call_id=tc_id))
        msgs.append(LLMMessage(role="assistant", content=f"round {r} done"))
        msgs.append(LLMMessage(role="user", content=f"continue {r}"))
    return msgs


class TestE1CrossPrompt:
    def test_execute_command_1417_chars_survives_convert(self):
        long_out = "o" * 1417
        payload = {
            "name": "execute_command",
            "args": {"command": "gradle test"},
            "status": "done",
            "result": long_out,
        }
        rows = [_msg("tool_call", json.dumps(payload))]
        out = convert_chat_messages_to_llm_format(rows, ctx_window=100_000, path="acp")
        tool = [m for m in out if m["role"] == "tool"][0]
        assert len(tool["content"]) > 500
        assert len(tool["content"]) == 1417

    def test_format_history_tool_content_verbatim_1417(self):
        body, strategy = format_history_tool_content(
            "execute_command", "z" * 1417, ctx_window=100_000, path="acp"
        )
        assert strategy == "verbatim"
        assert len(body) == 1417


class TestE3CacheInvalidation:
    def test_invalidate_clears_acp_session_cache(self):
        mgr = AcpSessionManager()
        sid = "sess-cache-test"
        mgr._history_cache[sid] = (time.monotonic(), [{"role": "user", "content": "hi"}])
        invalidate_history_cache(sid)
        assert sid not in mgr._history_cache


@pytest.mark.asyncio
class TestD0Pressure:
    async def test_pressure_reaches_compress_threshold(self, monkeypatch):
        """D0：6 轮大 tool 结果应使 session pressure >= 0.55。"""
        from app.services.llm.context_compressor import _est_tokens

        async def _noop_offload(*a, **k):
            return a[0], 0

        monkeypatch.setattr("app.services.llm.ccr_offload.offload_old_tool_messages", _noop_offload)
        monkeypatch.setattr(
            "app.services.llm.caller._multi_role_compress",
            lambda msgs, **kw: msgs,
        )

        api = _build_pressure_messages(6)
        ctx_window = 100_000
        pressure = _est_tokens(api, "gpt-4o") / ctx_window
        assert pressure >= 0.55, f"pressure={pressure:.2f}"

        compressor = ContextCompressor()
        before_len = len(api)
        await _apply_pre_round_context(
            api,
            ctx_window=ctx_window,
            model_name="gpt-4o",
            session_id="sess-pressure",
            agent_id="agent-1",
            ctx_path="acp",
            tools_for_llm=[{"type": "function", "function": {"name": "retrieve_context"}}],
            round_i=5,
            compressor=compressor,
            compress_ratio=0.55,
        )
        assert _TOOL_HARD_CEIL_CHARS <= 16384
        assert len(api) == before_len


@pytest.mark.asyncio
class TestE2PersistMarker:
    async def test_long_result_gets_ccr_hash_in_payload(self, monkeypatch):
        fake_hash = "f" * 64
        saved = {}

        async def _fake_store(**kwargs):
            saved.update(kwargs)
            return fake_hash

        monkeypatch.setattr("app.services.llm.ccr_store.store_entry", _fake_store)

        class _FakeDB:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            def add(self, obj):
                saved["msg"] = obj
            async def commit(self):
                pass

        monkeypatch.setattr("app.database.async_session", lambda: _FakeDB())
        monkeypatch.setattr(
            "app.plugins.clawith_acp.history_cache.invalidate_history_cache",
            lambda sid: saved.setdefault("invalidated", sid),
        )

        from app.services.chat_session_service import save_tool_call_log

        await save_tool_call_log(
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            conversation_id=str(uuid.uuid4()),
            tool_name="execute_command",
            arguments={"command": "gradle test"},
            result="x" * 3000,
        )
        payload = json.loads(saved["msg"].content)
        assert payload.get("ccr_hash") == fake_hash
        assert saved.get("invalidated")

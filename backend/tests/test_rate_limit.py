"""速率限制模块单元测试 — 验证 WebSocket 连接和工具执行的限流逻辑。

覆盖:
- IP 粒度速率限制（WebSocket 连接保护）
- Session 粒度速率限制（工具执行保护）
- 限流超限异常抛出
- 滑动窗口正确性
"""

import asyncio
import time

import pytest

from app.core.rate_limit import (
    check_ip_rate_limit,
    check_session_rate_limit,
    is_write_tool,
    _DEFAULT_IP_LIMITS,
    _DEFAULT_SESSION_LIMITS,
)


class TestIpRateLimit:
    """IP 粒度速率限制 — 保护 WebSocket 连接端点（P1-3 修复验证）"""

    @pytest.mark.asyncio
    async def test_localhost_exempt(self):
        """localhost/127.0.0.1/::1 免于速率限制（IDE 插件本地连接）。"""
        for ip in ("127.0.0.1", "::1", "0:0:0:0:0:0:0:1", "localhost"):
            for _ in range(50):  # 远超 10/60s 限制
                await check_ip_rate_limit(ip, "ws_connect")

    @pytest.mark.asyncio
    async def test_allows_requests_within_limit(self):
        """窗口内不超限的请求正常通过。"""
        ip = f"192.168.1.{int(time.time() * 1000) % 255}"
        for _ in range(5):
            await check_ip_rate_limit(ip, "ws_connect")  # limit: 10/60s, 5 should pass

    @pytest.mark.asyncio
    async def test_blocks_requests_over_limit(self):
        """超限时抛出 HTTPException 429。"""
        ip = f"10.0.0.{int(time.time() * 1000) % 255}"
        with pytest.raises(Exception) as exc_info:
            for _ in range(15):
                await check_ip_rate_limit(ip, "ws_connect")
        assert "429" in str(exc_info.value.status_code) or "rate" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_separate_ips_independent(self):
        """不同 IP 的限流计数独立。"""
        ip1 = f"172.16.0.{int(time.time() * 1000) % 255}"
        ip2 = f"172.16.1.{int(time.time() * 1000) % 255}"
        for _ in range(5):
            await check_ip_rate_limit(ip1, "ws_connect")
        # ip2 计数应独立，5 次仍可通过
        for _ in range(5):
            await check_ip_rate_limit(ip2, "ws_connect")

    @pytest.mark.asyncio
    async def test_different_endpoints_independent(self):
        """不同端点的限流计数独立。"""
        ip = f"192.168.2.{int(time.time() * 1000) % 255}"
        for _ in range(5):
            await check_ip_rate_limit(ip, "ws_connect")
        # ws_connect_acp 计数独立
        for _ in range(5):
            await check_ip_rate_limit(ip, "ws_connect_acp")


class TestSessionRateLimit:
    """Session 粒度速率限制 — 保护工具执行端点（P1-4 修复验证）"""

    @pytest.mark.asyncio
    async def test_tool_execute_rate_limit(self):
        """工具执行在窗口内不超限时正常通过。"""
        sid = str(int(time.time() * 1000))
        for _ in range(5):
            await check_session_rate_limit(sid, "tool_execute")  # limit: 30/10s

    @pytest.mark.asyncio
    async def test_tool_execute_over_limit(self):
        """工具执行超限时抛出 RuntimeError。"""
        sid = f"over-{int(time.time() * 1000)}"
        with pytest.raises(RuntimeError, match="rate limit"):
            for _ in range(35):
                await check_session_rate_limit(sid, "tool_execute")

    @pytest.mark.asyncio
    async def test_write_tool_stricter_limit(self):
        """写工具使用更严格的频率限制。"""
        sid = f"write-{int(time.time() * 1000)}"
        with pytest.raises(RuntimeError, match="rate limit"):
            for _ in range(15):
                await check_session_rate_limit(sid, "tool_write")  # limit: 10/60s

    @pytest.mark.asyncio
    async def test_separate_sessions_independent(self):
        """不同 session 的限流计数独立。"""
        sid1 = f"s1-{int(time.time() * 1000)}"
        sid2 = f"s2-{int(time.time() * 1000)}"
        for _ in range(20):
            await check_session_rate_limit(sid1, "tool_execute")
        # sid2 计数独立
        for _ in range(5):
            await check_session_rate_limit(sid2, "tool_execute")


class TestWriteToolDetection:
    """写工具检测 — 用于分级限流"""

    def test_write_tools_detected(self):
        """已知写工具被正确识别。"""
        assert is_write_tool("write_file")
        assert is_write_tool("delete_file_by_path")
        assert is_write_tool("execute_code")
        assert is_write_tool("run_in_terminal")

    def test_read_tools_not_detected(self):
        """读工具不被识别为写工具。"""
        assert not is_write_tool("read_file")
        assert not is_write_tool("search_file")
        assert not is_write_tool("list_dir")
        assert not is_write_tool("grep_code")


class TestDefaultLimits:
    """默认限流配置一致性"""

    def test_ws_connect_limits(self):
        """WebSocket 连接限流配置存在且合理。"""
        assert "ws_connect" in _DEFAULT_IP_LIMITS
        assert "ws_connect_lsp4j" in _DEFAULT_IP_LIMITS
        assert "ws_connect_acp" in _DEFAULT_IP_LIMITS
        max_calls, window = _DEFAULT_IP_LIMITS["ws_connect"]
        assert max_calls == 10
        assert window == 60

    def test_tool_execute_limits(self):
        """工具执行限流配置存在且合理。"""
        assert "tool_execute" in _DEFAULT_SESSION_LIMITS
        assert "tool_write" in _DEFAULT_SESSION_LIMITS
        tool_max, tool_window = _DEFAULT_SESSION_LIMITS["tool_execute"]
        write_max, write_window = _DEFAULT_SESSION_LIMITS["tool_write"]
        assert tool_max > write_max, "普通工具限制应 > 写工具限制"
        assert write_window > tool_window, "写工具窗口应 > 普通工具窗口"

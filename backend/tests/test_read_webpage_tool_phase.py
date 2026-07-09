"""read_webpage 工具阶段日志回归测试。"""

import pytest

from app.services.agent_tools import _log_tool_phase, _read_webpage


def test_log_tool_phase_accepts_url_host_only_in_fields() -> None:
    """_finish_read_webpage 修复后：url_host 仅经 **fields 传入，不与显式 kwarg 冲突。"""
    fields = {"url_host": "example.com", "status": 200, "bytes": 0, "result_len": 0}
    _log_tool_phase("read_webpage", "total", 1.0, total_ms=1.0, outcome="ok", **fields)


@pytest.mark.asyncio
async def test_read_webpage_validation_error_does_not_raise_type_error(monkeypatch) -> None:
    """校验失败路径会调用 _finish_read_webpage(url_host=...)；不得再抛 duplicate url_host。"""
    async def _fake_validate(_url: str, *, _perf_tool: str | None = None):
        return None, "❌ invalid url"

    monkeypatch.setattr(
        "app.services.agent_tools._validate_public_http_url",
        _fake_validate,
    )

    result = await _read_webpage({"url": "not-a-valid-url"})

    assert isinstance(result, str)
    assert "invalid url" in result

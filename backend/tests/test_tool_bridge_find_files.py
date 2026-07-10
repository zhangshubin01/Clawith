"""find_files ACP 路由单元测试。"""
import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

import pytest

from app.plugins.clawith_acp import tool_bridge


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("**/*", True),
        ("*", True),
        ("", True),
        ("**/src/**", True),
        ("**/*.kt", False),
        ("HomeViewModel.kt", False),
    ],
)
def test_is_broad_glob_pattern(pattern, expected):
    assert tool_bridge._is_broad_glob_pattern(pattern) is expected


@pytest.mark.asyncio
async def test_try_acp_find_files_routes_to_list_files(monkeypatch):
    calls = []

    async def fake_execute(name, args, handler):
        calls.append((name, dict(args)))
        return "📂 listed"

    monkeypatch.setattr(tool_bridge, "_try_acp_execute", fake_execute)

    class Handler:
        _cwd = "/proj"

    result = await tool_bridge._try_acp_find_files(
        {"pattern": "**/*", "path": "/proj"},
        Handler(),
    )
    assert result == "📂 listed"
    assert calls[0][0] == "list_files"
    assert calls[0][1]["path"] in ("/proj", ".")


@pytest.mark.asyncio
async def test_try_acp_execute_intercepts_find_files(monkeypatch):
    async def fake_find_files(args, handler):
        return "routed"

    monkeypatch.setattr(tool_bridge, "_try_acp_find_files", fake_find_files)

    class Handler:
        _cwd = "/proj"

    result = await tool_bridge._try_acp_execute(
        "find_files",
        {"pattern": "**/*", "path": "/proj"},
        Handler(),
    )
    assert result == "routed"

"""search_files ACP 路由单元测试。"""
import json
import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

import pytest

from app.plugins.clawith_acp import tool_bridge


@pytest.mark.parametrize(
    "dir_path,file_pattern,expected",
    [
        (".", "*", None),
        ("src/main", "*.kt", "src/main/**/*.kt"),
        ("src", "*", "src/**/*"),
    ],
)
def test_normalize_search_files_file_pattern(dir_path, file_pattern, expected):
    assert tool_bridge._normalize_search_files_file_pattern(dir_path, file_pattern) == expected


@pytest.mark.asyncio
async def test_try_acp_search_files_routes_to_search_text(monkeypatch):
    calls = []

    async def fake_execute(name, args, handler):
        calls.append((name, dict(args)))
        return "matches"

    monkeypatch.setattr(tool_bridge, "_try_acp_execute", fake_execute)

    class Handler:
        _cwd = "/proj"

    result = await tool_bridge._try_acp_search_files(
        {"pattern": "class Foo", "path": "/proj", "file_pattern": "*.kt"},
        Handler(),
    )
    assert result == "matches"
    assert calls[0][0] == "search_text"
    assert calls[0][1]["query"] == "class Foo"
    assert calls[0][1]["regex"] is True
    assert calls[0][1]["caseSensitive"] is True


@pytest.mark.asyncio
async def test_try_acp_search_files_missing_pattern():
    class Handler:
        _cwd = "/proj"

    result = await tool_bridge._try_acp_search_files({"path": "/proj"}, Handler())
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_try_acp_execute_intercepts_search_files(monkeypatch):
    async def fake_search(args, handler):
        return "routed"

    monkeypatch.setattr(tool_bridge, "_try_acp_search_files", fake_search)

    class Handler:
        _cwd = "/proj"

    result = await tool_bridge._try_acp_execute(
        "search_files",
        {"pattern": "foo", "path": "/proj"},
        Handler(),
    )
    assert result == "routed"

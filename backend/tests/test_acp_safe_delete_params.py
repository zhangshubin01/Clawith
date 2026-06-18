"""ACP delete_file → fs/safe_delete 参数构建与路由映射回归测试。"""

import pytest

from app.plugins.clawith_acp.tool_bridge import (
    _ACP_METHOD_MAP,
    _build_safe_delete_params,
)


@pytest.mark.asyncio
async def test_delete_file_maps_to_fs_safe_delete():
    assert _ACP_METHOD_MAP["delete_file"] == "fs/safe_delete"


@pytest.mark.asyncio
async def test_build_safe_delete_params_delete_file_forces_target_type_file():
    result = await _build_safe_delete_params(
        "delete_file",
        {"path": "src/Foo.kt"},
        object(),
        "session-1",
        "src/Foo.kt",
    )
    assert isinstance(result, dict)
    assert result["targetType"] == "file"
    assert result["path"] == "src/Foo.kt"
    assert result["file"] == "src/Foo.kt"
    assert result["sessionId"] == "session-1"


@pytest.mark.asyncio
async def test_build_safe_delete_params_accepts_file_key():
    result = await _build_safe_delete_params(
        "safe_delete",
        {"file": "lib/Bar.java"},
        object(),
        "session-2",
        "",
    )
    assert isinstance(result, dict)
    assert result["path"] == "lib/Bar.java"
    assert result["targetType"] == "symbol"


@pytest.mark.asyncio
async def test_build_safe_delete_params_camel_target_type():
    result = await _build_safe_delete_params(
        "safe_delete",
        {"path": "x.txt", "targetType": "file"},
        object(),
        "session-3",
        "x.txt",
    )
    assert result["targetType"] == "file"


@pytest.mark.asyncio
async def test_build_safe_delete_params_empty_path_returns_error():
    result = await _build_safe_delete_params(
        "delete_file",
        {},
        object(),
        "session-4",
        "",
    )
    assert isinstance(result, str)
    assert "不能为空" in result

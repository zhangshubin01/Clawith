"""ACP delete_file → fs/safe_delete 参数构建与路由映射回归测试。"""

import pytest

from app.plugins.clawith_acp.tool_bridge import (
    _ACP_METHOD_MAP,
    _build_find_super_methods_params,
    _build_project_params,
    _build_safe_delete_params,
    _guard_acp_command_paths,
    _normalize_acp_project_path,
    _guard_acp_dangerous_command,
    _timeout_for_acp_method,
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


def test_normalize_project_path_strips_workspace_prefix():
    path, error, internal = _normalize_acp_project_path(
        "workspace/app/src/main/java/Foo.kt",
        "/Users/dev/project",
    )
    assert error is None
    assert internal is False
    assert path == "app/src/main/java/Foo.kt"


def test_normalize_project_path_converts_absolute_inside_project():
    path, error, internal = _normalize_acp_project_path(
        "/Users/dev/project/app/src/main/java/Foo.kt",
        "/Users/dev/project",
    )
    assert error is None
    assert internal is False
    assert path == "app/src/main/java/Foo.kt"


def test_normalize_project_path_rejects_absolute_outside_project():
    path, error, internal = _normalize_acp_project_path(
        "/Users/dev/other/app/src/main/java/Foo.kt",
        "/Users/dev/project",
    )
    assert path == "/Users/dev/other/app/src/main/java/Foo.kt"
    assert internal is False
    assert error is not None
    assert "路径越界" in error


def test_normalize_project_path_keeps_agent_internal_local():
    path, error, internal = _normalize_acp_project_path("memory/memory.md", "/Users/dev/project")
    assert error is None
    assert internal is True
    assert path == "memory/memory.md"


def test_guard_command_rewrites_cd_to_project_root():
    command, error, debug = _guard_acp_command_paths(
        "cd /Users/dev/project && git status",
        "/Users/dev/project",
    )
    assert error is None
    assert command == "git status"
    assert debug["rewritten"] is True


def test_guard_command_rewrites_cd_to_project_subdir():
    command, error, debug = _guard_acp_command_paths(
        "cd /Users/dev/project/app && ./gradlew test",
        "/Users/dev/project",
    )
    assert error is None
    assert command == "cd app && ./gradlew test"
    assert debug["rewritten"] is True


def test_guard_command_rejects_cd_outside_project():
    command, error, _debug = _guard_acp_command_paths(
        "cd /Users/dev/other && git status",
        "/Users/dev/project",
    )
    assert command == "cd /Users/dev/other && git status"
    assert error is not None
    assert "命令路径越界" in error


def test_guard_command_rejects_absolute_destructive_operand_outside_project():
    _command, error, _debug = _guard_acp_command_paths(
        "rm -rf /Users/dev/other/app/src/main/java/com/demo/_test",
        "/Users/dev/project",
    )
    assert error is not None
    assert "绝对路径不在 IDE 项目根内" in error


def test_guard_command_keeps_relative_project_command():
    command, error, debug = _guard_acp_command_paths("./gradlew test", "/Users/dev/project")
    assert error is None
    assert command == "./gradlew test"
    assert debug["rewritten"] is False


def test_normalize_project_path_rejects_absolute_agent_internal_name_outside_project():
    path, error, internal = _normalize_acp_project_path(
        "/Users/dev/other/memory.md",
        "/Users/dev/project",
    )
    assert internal is False
    assert error is not None
    assert "路径越界" in error


def test_guard_command_rejects_home_expansion_paths():
    command, error, _debug = _guard_acp_command_paths("rm -rf ~/other", "/Users/dev/project")
    assert command == "rm -rf ~/other"
    assert error is not None
    assert "shell 展开路径" in error


def test_guard_command_rejects_home_env_paths():
    command, error, _debug = _guard_acp_command_paths("cat $HOME/secret.txt", "/Users/dev/project")
    assert command == "cat $HOME/secret.txt"
    assert error is not None
    assert "shell 展开路径" in error


def test_guard_command_rewrites_absolute_operand_inside_project():
    command, error, debug = _guard_acp_command_paths(
        "rm -rf /Users/dev/project/app/build/tmp",
        "/Users/dev/project",
    )
    assert error is None
    assert command == "rm -rf app/build/tmp"
    assert debug["rewritten"] is True


@pytest.mark.asyncio
async def test_find_super_methods_rejects_class_symbol_before_ide_call():
    result = await _build_find_super_methods_params(
        "find_super_methods",
        {"symbol": "com.example.LoginViewModel"},
        object(),
        "session-5",
        "",
    )
    assert isinstance(result, str)
    assert "方法符号" in result


@pytest.mark.asyncio
async def test_find_super_methods_accepts_method_symbol():
    result = await _build_find_super_methods_params(
        "find_super_methods",
        {"symbol": "com.example.LoginViewModel#onLogin"},
        object(),
        "session-6",
        "",
    )
    assert isinstance(result, dict)
    assert result["symbol"] == "com.example.LoginViewModel#onLogin"


@pytest.mark.asyncio
async def test_find_super_methods_requires_position_or_symbol():
    result = await _build_find_super_methods_params(
        "find_super_methods",
        {"file": "src/Foo.kt"},
        object(),
        "session-7",
        "src/Foo.kt",
    )
    assert isinstance(result, str)
    assert "参数不足" in result


@pytest.mark.asyncio
async def test_build_project_timeout_uses_request_timeout_seconds():
    params = await _build_project_params("build_project", {"timeoutSeconds": 240}, object(), "session-8", "")
    assert isinstance(params, dict)
    assert _timeout_for_acp_method("ide/build_project", params) == 240.0


def test_build_project_timeout_has_long_default():
    assert _timeout_for_acp_method("ide/build_project", {}) == 180.0


@pytest.mark.asyncio
async def test_build_project_params_omits_timeout_when_not_requested():
    params = await _build_project_params("build_project", {}, object(), "session-9", "")
    assert isinstance(params, dict)
    assert "timeoutSeconds" not in params
    assert _timeout_for_acp_method("ide/build_project", params) == 180.0


def test_guard_command_rejects_relative_cd_outside_project():
    command, error, _debug = _guard_acp_command_paths("cd .. && git status", "/Users/dev/project")
    assert command == "cd .. && git status"
    assert error is not None
    assert "路径越界" in error


def test_guard_command_rejects_relative_operand_outside_project():
    _command, error, _debug = _guard_acp_command_paths("cat ../secret.txt", "/Users/dev/project")
    assert error is not None
    assert "路径不在 IDE 项目根内" in error


def test_guard_command_rejects_absolute_path_inside_option_value_outside_project():
    _command, error, _debug = _guard_acp_command_paths(
        "tool --config=/Users/dev/other/config.yml",
        "/Users/dev/project",
    )
    assert error is not None
    assert "路径不在 IDE 项目根内" in error


def test_guard_command_rewrites_absolute_path_inside_option_value_inside_project():
    command, error, debug = _guard_acp_command_paths(
        "tool --config=/Users/dev/project/config.yml",
        "/Users/dev/project",
    )
    assert error is None
    assert command == "tool --config=config.yml"
    assert debug["rewritten"] is True


@pytest.mark.asyncio
async def test_build_project_params_clamps_large_timeout():
    params = await _build_project_params("build_project", {"timeoutSeconds": 999999}, object(), "session-10", "")
    assert isinstance(params, dict)
    assert params["timeoutSeconds"] == 600
    assert _timeout_for_acp_method("ide/build_project", params) == 600.0


def test_guard_command_rewrite_preserves_shell_control_operators():
    command, error, debug = _guard_acp_command_paths(
        "cp /Users/dev/project/a /Users/dev/project/b && echo done",
        "/Users/dev/project",
    )
    assert error is None
    assert command == "cp a b && echo done"
    assert debug["rewritten"] is True


def test_dangerous_command_guard_blocks_network_command():
    result = _guard_acp_dangerous_command("curl https://example.com")
    assert result is not None
    assert "网络命令" in result or "危险命令" in result


def test_safe_delete_timeout_matches_permission_window():
    assert _timeout_for_acp_method("fs/safe_delete", {}) == 120.0


def test_safe_delete_timeout_respects_env(monkeypatch):
    monkeypatch.setenv("ACP_PERMISSION_TIMEOUT", "90")
    assert _timeout_for_acp_method("fs/safe_delete", {}) == 90.0

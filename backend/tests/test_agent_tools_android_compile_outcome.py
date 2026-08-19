"""D-026 typed execution boundary for _android_compile_outcome."""

from __future__ import annotations

from pathlib import Path
import uuid

import pytest

from app.services import agent_tools
from app.services.agent_runtime.tool_execution import ToolExecutionOutcome
from app.services.sandbox.base import ExecutionResult


class FakeAndroidBuildBackend:
    name = "android-build"
    client = object()

    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def install_backend(monkeypatch, backend: FakeAndroidBuildBackend) -> None:
    from app.services.sandbox import registry

    def fake_backend(_config):
        return backend

    monkeypatch.setattr(registry, "get_sandbox_backend", fake_backend)


async def _make_outcome(
    monkeypatch,
    tmp_path: Path,
    project_subdir: str | None = "app",
    task: str = "assembleDebug",
    java_version: str = "17",
    backend: FakeAndroidBuildBackend | None = None,
) -> ToolExecutionOutcome:
    """Helper: configure mocks and call _android_compile_outcome."""
    agent_id = uuid.uuid4()

    # 1. 创建 project_path 目录（模拟 agent workspace 下的项目）
    ws_root = tmp_path / "workspace" / str(agent_id)
    ws_root.mkdir(parents=True)
    project_dir = ws_root
    if project_subdir is not None:
        project_dir = ws_root / project_subdir
        project_dir.mkdir(parents=True, exist_ok=True)
        # 创建 gradlew 标记
        (project_dir / "gradlew").write_text("#!/bin/bash\necho fake gradle\nexit 0\n")
        (project_dir / "gradlew").chmod(0o755)

    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _aid: ws_root)

    async def fake_config(_agent_id, _name):
        return {"sandbox_type": "android-build", "max_timeout": 600}

    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_config)

    if backend is None:
        backend = FakeAndroidBuildBackend(
            result=ExecutionResult(success=True, stdout="BUILD SUCCESSFUL\n", stderr="", exit_code=0, duration_ms=50000)
        )
    install_backend(monkeypatch, backend)

    arguments = {"project_path": project_subdir, "task": task}
    if java_version:
        arguments["java_version"] = java_version
    return await agent_tools._android_compile_outcome(agent_id, arguments)


# ─── 1. 签名未变 ───


def test_android_compile_outcome_is_callable() -> None:
    """_android_compile_outcome 是可导入的 async callable，签名包含 agent_id / arguments / on_output。"""
    import inspect

    sig = inspect.signature(agent_tools._android_compile_outcome)
    params = list(sig.parameters)
    assert params == ["agent_id", "arguments", "on_output"]
    # 返回类型是 ToolExecutionOutcome（也可能是 Coroutine，但注解已标记）
    ret = sig.return_annotation
    assert ret is ToolExecutionOutcome or "ToolExecutionOutcome" in str(ret)


# ─── 2. 成功路径 ───


@pytest.mark.asyncio
async def test_apk_found_returns_typed_success_with_apk_list(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """构建成功 + APK 产物 → status=succeeded, summary 包含产物的路径列表."""
    backend = FakeAndroidBuildBackend(
        result=ExecutionResult(success=True, stdout="BUILD SUCCESSFUL\n", stderr="", exit_code=0, duration_ms=45000)
    )
    outcome = await _make_outcome(monkeypatch, tmp_path, project_subdir="app", backend=backend)

    assert isinstance(outcome, ToolExecutionOutcome)
    assert outcome.status == "succeeded"
    assert outcome.error_code is None
    # 没有真实的产物文件，所以走无 APK 分支
    assert "no APK/AAB found" in outcome.result_summary


@pytest.mark.asyncio
async def test_apk_artifacts_listed_in_summary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """构建成功后，扫描到的 APK/AAB 产物路径会出现在 result_summary 中."""
    agent_id = uuid.uuid4()
    ws_root = tmp_path / "workspace" / str(agent_id)
    ws_root.mkdir(parents=True)
    project_dir = ws_root / "app"
    project_dir.mkdir(parents=True)

    (project_dir / "gradlew").write_text("#!/bin/bash\necho ok\n")
    (project_dir / "gradlew").chmod(0o755)

    # 创建模拟 APK 产物
    apk_dir = project_dir / "app/build/outputs/apk/debug"
    apk_dir.mkdir(parents=True)
    (apk_dir / "app-debug.apk").write_text("fake apk content")

    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _aid: ws_root)

    async def fake_config(_agent_id, _name):
        return {"sandbox_type": "android-build", "max_timeout": 600}

    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_config)

    backend = FakeAndroidBuildBackend(
        result=ExecutionResult(success=True, stdout="BUILD SUCCESSFUL\n", stderr="", exit_code=0, duration_ms=30000)
    )
    install_backend(monkeypatch, backend)

    outcome = await agent_tools._android_compile_outcome(
        agent_id, {"project_path": "app", "task": "assembleDebug"}
    )

    assert outcome.status == "succeeded"
    assert outcome.result_summary is not None
    assert "app-debug.apk" in outcome.result_summary
    assert "assembleDebug" in outcome.result_summary


@pytest.mark.asyncio
async def test_success_no_apk_returns_no_apk_message(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """构建成功但产物目录为空 → summary 显示 '(no APK/AAB found)'."""
    backend = FakeAndroidBuildBackend(
        result=ExecutionResult(success=True, stdout="BUILD SUCCESSFUL\n", stderr="", exit_code=0, duration_ms=20000)
    )
    outcome = await _make_outcome(monkeypatch, tmp_path, project_subdir="app", backend=backend)

    assert outcome.status == "succeeded"
    assert outcome.result_summary is not None
    assert "no APK/AAB found" in outcome.result_summary
    assert "assembleDebug" in outcome.result_summary


# ─── 3. 失败路径 ───


@pytest.mark.asyncio
async def test_failure_no_output_returns_no_output_captured(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """构建失败但 stdout+stderr 均为空 → result_summary 包含 '(no output captured)'."""
    backend = FakeAndroidBuildBackend(
        result=ExecutionResult(success=False, stdout="", stderr="", exit_code=1, duration_ms=5000)
    )
    outcome = await _make_outcome(monkeypatch, tmp_path, project_subdir="app", backend=backend)

    assert outcome.status == "failed"
    assert outcome.error_code == "sandbox_execution_failed"
    assert outcome.result_summary is not None
    assert "no output captured" in outcome.result_summary


@pytest.mark.asyncio
async def test_failure_structured_errors_are_present(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """构建失败 + 正常 stderr → result_summary 包含错误详情, error_code 为 sandbox_execution_failed."""
    stderr = """e: /app/src/main.kt: (1, 1): Unresolved reference: foo
/app/src/main.java:5: error: cannot find symbol
  symbol:   method bar
  location: class Main
FAILURE: Build failed with 2 errors and 0 warnings
"""
    backend = FakeAndroidBuildBackend(
        result=ExecutionResult(success=False, stdout="", stderr=stderr, exit_code=1, duration_ms=12000)
    )
    outcome = await _make_outcome(monkeypatch, tmp_path, project_subdir="app", backend=backend)

    assert outcome.status == "failed"
    assert outcome.error_code == "sandbox_execution_failed"
    assert outcome.metadata is not None
    assert outcome.metadata.get("exit_code") == 1
    assert outcome.metadata.get("error_count", 0) >= 2
    assert outcome.metadata.get("duration_ms") == 12000


# ─── 4. error_code 不变 ───


@pytest.mark.asyncio
async def test_error_code_sandbox_execution_failed_is_unchanged(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """非零退出的 sandbox error_code 必须是 'sandbox_execution_failed'."""
    backend = FakeAndroidBuildBackend(
        result=ExecutionResult(success=False, stdout="error: something broke", stderr="", exit_code=1, duration_ms=3000)
    )
    outcome = await _make_outcome(monkeypatch, tmp_path, project_subdir="app", backend=backend)

    assert outcome.status == "failed"
    assert outcome.error_code == "sandbox_execution_failed"


@pytest.mark.asyncio
async def test_sandbox_exception_code_is_sandbox_execution_failed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """后端抛出异常 → error_code = 'sandbox_execution_failed'."""
    backend = FakeAndroidBuildBackend(error=RuntimeError("connection lost"))
    outcome = await _make_outcome(monkeypatch, tmp_path, project_subdir="app", backend=backend)

    assert outcome.status == "failed"
    assert outcome.error_code == "sandbox_execution_failed"


# ─── 5. 早期验证错误 (不再进沙箱) ───


@pytest.mark.asyncio
async def test_missing_project_path_returns_early_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """project_path 为空 → _typed_failure('project_path required', 'invalid_tool_arguments')."""
    agent_id = uuid.uuid4()
    outcome = await agent_tools._android_compile_outcome(agent_id, {})

    assert outcome.status == "failed"
    assert outcome.error_code == "invalid_tool_arguments"
    assert outcome.result_summary is not None
    assert "project_path required" in outcome.result_summary


@pytest.mark.asyncio
async def test_invalid_project_path_type_returns_early_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """project_path 不是字符串 → _typed_failure('project_path required', 'invalid_tool_arguments')."""
    agent_id = uuid.uuid4()
    outcome = await agent_tools._android_compile_outcome(agent_id, {"project_path": 123})

    assert outcome.status == "failed"
    assert outcome.error_code == "invalid_tool_arguments"
    assert "project_path required" in outcome.result_summary


@pytest.mark.asyncio
async def test_gradlew_not_found_returns_early_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """项目目录存在但没有 gradlew → 独立文案 + 目录内容列表（L3 边界区分）."""
    agent_id = uuid.uuid4()
    ws_root = tmp_path / "workspace" / str(agent_id)
    ws_root.mkdir(parents=True)
    (ws_root / "my_project").mkdir(parents=True)
    (ws_root / "my_project" / "settings.gradle.kts").write_text("// no wrapper\n")

    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _aid: ws_root)

    async def fake_config(_agent_id, _name):
        return {"sandbox_type": "android-build", "max_timeout": 600}

    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_config)

    outcome = await agent_tools._android_compile_outcome(
        agent_id, {"project_path": "my_project"}
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "android_project_not_found"
    assert outcome.result_summary is not None
    assert "gradlew not found" in outcome.result_summary
    assert "缺少 Gradle wrapper 脚本" in outcome.result_summary
    assert "settings.gradle.kts" in outcome.result_summary


@pytest.mark.asyncio
async def test_project_dir_missing_returns_path_diagnostics(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """项目目录不存在 → 不再是裸 'gradlew not found'，附结构化路径诊断."""
    agent_id = uuid.uuid4()
    ws_root = tmp_path / "workspace" / str(agent_id)
    ws_root.mkdir(parents=True)
    (ws_root / "workspace").mkdir(parents=True)
    (ws_root / "workspace" / "other-app").mkdir(parents=True)

    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _aid: ws_root)

    outcome = await agent_tools._android_compile_outcome(
        agent_id, {"project_path": "ghost"}
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "android_project_not_found"
    assert outcome.result_summary is not None
    assert "Android project not found: 'ghost'" in outcome.result_summary
    assert "Resolved against agent workspace root" in outcome.result_summary
    assert "Entries under it:" in outcome.result_summary
    assert "workspace" in outcome.result_summary


@pytest.mark.asyncio
async def test_unprefixed_path_falls_back_to_workspace_prefix(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """模型少传 workspace/ 前缀 → 自动回退命中 workspace/<p> 并注明（L3 核心场景）."""
    agent_id = uuid.uuid4()
    ws_root = tmp_path / "workspace" / str(agent_id)
    ws_root.mkdir(parents=True)
    project_dir = ws_root / "workspace" / "indonesia-loan-app"
    project_dir.mkdir(parents=True)
    (project_dir / "gradlew").write_text("#!/bin/bash\necho fake\n")
    (project_dir / "gradlew").chmod(0o755)

    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _aid: ws_root)

    async def fake_config(_agent_id, _name):
        return {"sandbox_type": "android-build", "max_timeout": 600}

    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_config)

    backend = FakeAndroidBuildBackend(
        result=ExecutionResult(success=True, stdout="BUILD SUCCESSFUL\n", stderr="", exit_code=0, duration_ms=10000)
    )
    install_backend(monkeypatch, backend)

    outcome = await agent_tools._android_compile_outcome(
        agent_id, {"project_path": "indonesia-loan-app"}
    )

    assert outcome.status == "succeeded"
    assert "[路径回退]" in outcome.result_summary
    assert backend.calls, "backend 应被执行"
    assert backend.calls[0]["project_path"] == str(project_dir)
    assert backend.calls[0]["work_dir"] == str(project_dir)


@pytest.mark.asyncio
async def test_fallback_requires_gradlew_in_workspace_candidate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """workspace/<p> 存在但没有 gradlew → 不回退，报路径诊断并建议正确目录."""
    agent_id = uuid.uuid4()
    ws_root = tmp_path / "workspace" / str(agent_id)
    ws_root.mkdir(parents=True)
    (ws_root / "workspace" / "foo").mkdir(parents=True)

    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _aid: ws_root)

    async def fake_config(_agent_id, _name):
        return {"sandbox_type": "android-build", "max_timeout": 600}

    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_config)

    backend = FakeAndroidBuildBackend(
        result=ExecutionResult(success=True, stdout="BUILD SUCCESSFUL\n", stderr="", exit_code=0, duration_ms=10000)
    )
    install_backend(monkeypatch, backend)

    outcome = await agent_tools._android_compile_outcome(
        agent_id, {"project_path": "foo"}
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "android_project_not_found"
    assert "Did you mean: 'workspace/foo'?" in outcome.result_summary
    assert not backend.calls, "不应触发构建"


@pytest.mark.asyncio
async def test_wrapper_jar_missing_hint_on_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """gradlew 在但 wrapper jar 缺失 → 构建失败时附带补齐提示."""
    agent_id = uuid.uuid4()
    ws_root = tmp_path / "workspace" / str(agent_id)
    ws_root.mkdir(parents=True)
    project_dir = ws_root / "app"
    project_dir.mkdir(parents=True)
    (project_dir / "gradlew").write_text("#!/bin/bash\necho fake\n")
    (project_dir / "gradlew").chmod(0o755)

    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _aid: ws_root)

    async def fake_config(_agent_id, _name):
        return {"sandbox_type": "android-build", "max_timeout": 600}

    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_config)

    backend = FakeAndroidBuildBackend(
        result=ExecutionResult(success=False, stdout="", stderr="FAILURE: Build failed\n", exit_code=1, duration_ms=10000)
    )
    install_backend(monkeypatch, backend)

    outcome = await agent_tools._android_compile_outcome(
        agent_id, {"project_path": "app"}
    )

    assert outcome.status == "failed"
    assert "[wrapper 提示]" in outcome.result_summary
    assert "gradle-wrapper.jar" in outcome.result_summary


@pytest.mark.asyncio
async def test_gradlew_bat_is_also_accepted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Windows 系统下 gradlew.bat 也能通过验证."""
    agent_id = uuid.uuid4()
    ws_root = tmp_path / "workspace" / str(agent_id)
    ws_root.mkdir(parents=True)
    project_dir = ws_root / "android_app"
    project_dir.mkdir(parents=True)
    # 只有 gradlew.bat，没有 gradlew
    (project_dir / "gradlew.bat").write_text("@echo off\necho fake gradle\n")

    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _aid: ws_root)

    async def fake_config(_agent_id, _name):
        return {"sandbox_type": "android-build", "max_timeout": 600}

    monkeypatch.setattr(agent_tools, "_get_tool_config", fake_config)

    backend = FakeAndroidBuildBackend(
        result=ExecutionResult(success=True, stdout="BUILD SUCCESSFUL\n", stderr="", exit_code=0, duration_ms=10000)
    )
    install_backend(monkeypatch, backend)

    outcome = await agent_tools._android_compile_outcome(
        agent_id, {"project_path": "android_app"}
    )

    assert outcome.status == "succeeded"


# ─── 6. metadata 序列化 ───


@pytest.mark.asyncio
async def test_failure_metadata_contains_expected_keys(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """失败路径的 metadata 包含 exit_code / error_count / warning_count / duration_ms 等字段."""
    stderr = """e: /src/Main.kt: (5, 3): 'foo' is not a function
警告: /src/res/values/strings.xml:2: String 'app_name' is not translated
BUILD FAILED in 10s
"""
    backend = FakeAndroidBuildBackend(
        result=ExecutionResult(success=False, stdout="", stderr=stderr, exit_code=1, duration_ms=15000)
    )
    outcome = await _make_outcome(monkeypatch, tmp_path, project_subdir="app", backend=backend)

    assert outcome.status == "failed"
    metadata_keys = set(outcome.metadata.keys()) if outcome.metadata else set()
    assert "exit_code" in metadata_keys
    assert "error_count" in metadata_keys
    assert "warning_count" in metadata_keys
    assert "duration_ms" in metadata_keys
    assert "total_output_chars" in metadata_keys
    assert outcome.metadata["exit_code"] == 1


@pytest.mark.asyncio
async def test_success_metadata_is_empty(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """成功路径的 metadata 应为空 dict."""
    backend = FakeAndroidBuildBackend(
        result=ExecutionResult(success=True, stdout="BUILD SUCCESSFUL\n", stderr="", exit_code=0, duration_ms=5000)
    )
    outcome = await _make_outcome(monkeypatch, tmp_path, project_subdir="app", backend=backend)

    assert outcome.status == "succeeded"
    assert outcome.metadata is not None
    # success 不传 metadata, _typed_success 默认 {}
    assert outcome.metadata == {}


# ─── 7. 循环导入检查 ───


def test_android_compile_outcome_import_does_not_cause_cyclic_import() -> None:
    """直接 import agent_tools 不会导致循环导入 (模块级 import 已经通过文件加载验证)。"""
    # 模块顶层的 from app.services import agent_tools 已在上方执行成功。
    # 调用该函数不应引发 ImportError。
    import inspect
    assert inspect.iscoroutinefunction(agent_tools._android_compile_outcome)

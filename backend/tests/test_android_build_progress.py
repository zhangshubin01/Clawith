"""验证 Gradle 构建进度（方案 B：init script 文件侧信道 + 方案 C：静默期心跳）。

背景：Gradle daemon 把任务输出经 socket 转发周期性批量送达（不提供逐行实时），
「无进度」观感的根治是：
- 方案 B：init script 把任务边界写进 /workspace/.gradle-progress（文件侧信道绕过 socket 缓冲），
  后端 tail 该文件实时转发给 on_output；
- 方案 C：静默期（无 docker 日志流且无进度侧信道输出）超阈值发「构建进行中…」心跳。

关键正确性约束：init script 必须用 beforeProject + configureEach + doFirst/doLast 注入，
而非 Gradle.addListener(TaskExecutionListener)/addBuildListener——后者与 configuration-cache 不兼容
（会令缓存条目「存储但永不复用」），而 doFirst/doLast 随任务图序列化、缓存命中仍触发。

测试策略（复用 test_android_build_backend_fixes.py 的 mock 基建，不依赖真实 Docker）。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.local.android_build_backend import AndroidBuildBackend

# 复用 test_android_build_backend_fixes.py 的 mock 基建（不依赖真实 Docker）
from test_android_build_backend_fixes import _MockDockerClient


@pytest.fixture
def mock_docker_client():
    return _MockDockerClient()


@pytest.fixture
def backend(mock_docker_client, monkeypatch):
    config = SandboxConfig(type="android-build", max_timeout=1800)
    monkeypatch.setattr(
        "app.services.sandbox.local.android_build_backend.get_docker_client",
        lambda: mock_docker_client,
    )
    monkeypatch.setattr(
        "app.services.sandbox.local.android_build_backend._detect_host_agent_data_root",
        lambda: "/host/data/agents",
    )
    return AndroidBuildBackend(config)


class TestInitScriptContract:
    """验证进度 init script 内容契约（config-cache 兼容性关键）。"""

    def test_uses_configcache_compatible_hooks(self):
        """必须用 beforeProject + configureEach，禁用 addListener/addBuildListener。"""
        s = AndroidBuildBackend._GRADLE_PROGRESS_INIT_SCRIPT
        assert "beforeProject" in s
        assert "configureEach" in s
        assert "doFirst" in s and "doLast" in s
        # 反模式：这两者在 config-cache 下报「unsupported」并令缓存条目永不复用
        assert "addListener" not in s, "不应使用 Gradle.addListener（config-cache 不兼容）"
        assert "addBuildListener" not in s, "不应使用 Gradle.addBuildListener（config-cache 不兼容）"

    def test_emits_task_boundaries(self):
        """必须产出 TASK_START / TASK_END 标记。"""
        s = AndroidBuildBackend._GRADLE_PROGRESS_INIT_SCRIPT
        assert "TASK_START|${task.path}" in s
        assert "TASK_END|${task.path}" in s

    def test_writes_to_file_not_stdout(self):
        """进度走文件侧信道，不应 print/println 到 stdout（会被 daemon socket 缓冲）。"""
        s = AndroidBuildBackend._GRADLE_PROGRESS_INIT_SCRIPT
        assert "println(" not in s
        assert "print(" not in s
        assert "progressFile.append" in s


class TestCommandInjection:
    """验证构建命令注入了 init script 文件 + -I 显式加载。"""

    @pytest.mark.asyncio
    async def test_command_injects_init_script_and_flag(self, backend, mock_docker_client):
        await backend.execute(
            code="", language="java",
            timeout=30, work_dir="/workspace",
            project_path="/workspace/app",
            gradle_task="assembleDebug",
        )
        cmd = mock_docker_client.containers.last_run_kwargs["command"]
        assert cmd[0] == "bash" and cmd[1] == "-c"
        full = cmd[2]
        # heredoc 写入 + -I 加载
        assert "-I /tmp/gradle-progress.gradle" in full
        assert "GRADLE_PROGRESS_EOF" in full
        assert "TASK_START|${task.path}" in full
        # 保证仍在 --no-daemon --console=plain 下运行（不改变既有构建语义）
        assert "--no-daemon --console=plain" in full


class TestProgressStreaming:
    """验证进度 tail 实时转发 + 静默期心跳（方案 B + C 运行时行为）。"""

    class _SlowContainer:
        """wait() 阻塞 delay 秒的容器 mock（复用其 logs 生成器）。"""

        def __init__(self, delay: float):
            self.id = "slow123"
            self._delay = delay
            self.kill_called = False
            self.remove_called = False

        def exec_run(self, *a, **k):
            class _R:
                exit_code = 0
                output = b""
            return _R()

        def logs(self, *, stream=False, follow=False, stdout=True, stderr=True):
            def _gen():
                yield b"BUILD SUCCESSFUL\n"
            return _gen()

        def wait(self):
            import time as _t
            _t.sleep(self._delay)
            return {"StatusCode": 0}

        def kill(self, **k):
            self.kill_called = True

        def remove(self, *, force=False):
            self.remove_called = True

    @pytest.mark.asyncio
    async def test_tail_forwards_task_boundaries_and_heartbeat_fires(
        self, backend, mock_docker_client, monkeypatch, tmp_path
    ):
        """慢构建（1.5s）期间：进度文件新行被 tail 实时转发，静默期心跳也触发。"""
        # 缩短心跳阈值，让心跳在测试时间窗内触发
        monkeypatch.setattr(AndroidBuildBackend, "_HEARTBEAT_INTERVAL", 0.3)
        monkeypatch.setattr(AndroidBuildBackend, "_HEARTBEAT_POLL", 0.1)

        project = str(tmp_path)
        progress_file = os.path.join(project, ".clawith-gradle-progress")
        outputs: list[str] = []

        async def on_output(text: str):
            outputs.append(text)

        mock_docker_client.containers.run_result = self._SlowContainer(delay=1.5)

        task = asyncio.create_task(backend.execute(
            code="", language="java",
            timeout=30, work_dir="/workspace",
            project_path=project,
            gradle_task="assembleDebug",
            on_output=on_output,
        ))

        # 等 tail 启动后，模拟 init script 写任务边界
        await asyncio.sleep(0.4)
        with open(progress_file, "a", encoding="utf-8") as f:
            f.write("TASK_START|:app:compileDebugKotlin\n")
        await asyncio.sleep(0.2)
        with open(progress_file, "a", encoding="utf-8") as f:
            f.write("TASK_END|:app:compileDebugKotlin\n")

        result = await task
        assert result.success

        joined = "".join(outputs)
        # 方案 B：任务边界被实时转发，且渲染为可读文案（而非原始 TASK_START| 协议 token）
        assert "▶ 正在执行 :app:compileDebugKotlin" in joined
        assert "✓ 完成 :app:compileDebugKotlin" in joined
        assert "TASK_START|" not in joined, "原始协议 token 不应透传给用户"
        # 方案 C：静默期心跳触发
        assert "构建进行中" in joined

    @pytest.mark.asyncio
    async def test_progress_file_cleaned_up(self, backend, mock_docker_client, tmp_path):
        """构建结束（含成功路径）后，进度侧信道文件应被清理，不污染用户工作区。"""
        project = str(tmp_path)
        progress_file = os.path.join(project, ".clawith-gradle-progress")
        with open(progress_file, "w", encoding="utf-8") as f:
            f.write("TASK_START|:x\n")

        await backend.execute(
            code="", language="java",
            timeout=30, work_dir="/workspace",
            project_path=project,
            gradle_task="assembleDebug",
        )
        assert not os.path.exists(progress_file), "构建后 .gradle-progress 应被清理"

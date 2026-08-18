"""验证 AndroidBuildBackend P0 修复方案的测试。

覆盖 5 个修复：Semaphore 并发控制、生命周期日志、日志级别修正、
超时路径异常隔离、/dev/shm 扩容。

测试策略（不依赖真实 Docker — 全部用 mock）：
- Fix 1 (Semaphore): 注入可控延迟，3 并发请求验证时间线
- Fix 2 (日志): monkeypatch logger, 追踪调用计数
- Fix 3 (日志级别): 触发各错误场景, 断言日志级别
- Fix 4 (超时): mock TimeoutError + kill 失败, 断言仍返回超时
- Fix 5 (/dev/shm): 捕获 containers.run 参数, 断言 tmpfs 配置
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import docker.errors
import pytest

from app.services.sandbox.base import ExecutionResult
from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.local.android_build_backend import (
    AndroidBuildBackend,
    _detect_host_agent_data_root,
)
from app.services.sandbox.docker_client import get_docker_client

# ─────────────────────────────────────────────────────────
# 通用 Mock 工厂
# ─────────────────────────────────────────────────────────


class _MockExecResult:
    """模拟 docker SDK ExecResult。"""

    def __init__(self, *, exit_code: int = 0, output: bytes = b""):
        self.exit_code = exit_code
        self.output = output


class _MockContainer:
    """模拟 Docker container 对象。"""

    def __init__(self, *, id: str = "abc123def456", wait_result: dict | None = None):
        self.id = id
        self._wait_result = wait_result or {"StatusCode": 0}
        self.kill_called = False
        self.remove_called = False
        self.exec_run_calls: list[dict] = []

    def exec_run(self, cmd, *, user=None, **kwargs):
        """记录调用并返回成功的 ExecResult（模拟 root chown 预热）。"""
        self.exec_run_calls.append({"cmd": cmd, "user": user, "kwargs": kwargs})
        return _MockExecResult()

    def logs(self, *, stream=False, follow=False, stdout=True, stderr=True):
        """返回一个生成器，模仿 container.logs(stream=True)。"""
        def _gen():
            yield b"BUILD SUCCESSFUL\n"
        return _gen()

    def wait(self):
        return self._wait_result

    def kill(self, **kwargs):
        self.kill_called = True

    def remove(self, *, force=False):
        self.remove_called = True


class _MockVolume:
    """模拟 Docker volume 对象。"""
    name = "gradle_cache_global"


class _MockImage:
    """模拟 Docker image 对象。"""
    pass


class _MockDockerClient:
    """模拟 Docker SDK 客户端，允许调用方注入异常。

    默认成功路径。调用方可覆写各属性的返回值或设为异常。
    """

    def __init__(self):
        self.containers = _MockContainers()
        self.images = _MockImages()
        self.volumes = _MockVolumes()
        self.ping_ok = True

    def ping(self):
        if not self.ping_ok:
            raise RuntimeError("Docker ping failed")
        return True


class _MockContainers:
    def __init__(self):
        self.run_result = _MockContainer()
        self.get_result = _MockContainersGetOk()

    def run(self, *args, **kwargs):
        if isinstance(self.run_result, Exception):
            raise self.run_result
        self.last_run_args = args
        self.last_run_kwargs = kwargs
        return self.run_result

    def get(self, name):
        if isinstance(self.get_result, Exception):
            raise self.get_result
        return self.get_result


class _MockContainersGetOk:
    """模拟 containers.get() 返回的容器对象（用于 _detect_host_agent_data_root）。"""
    def __init__(self, mounts: list[dict] | None = None):
        self.attrs = {
            "Mounts": mounts or [
                {
                    "Destination": "/data/agents",
                    "Source": "/host/data/agents",
                    "Mode": "rw",
                    "RW": True,
                }
            ]
        }


class _MockImages:
    def __init__(self):
        self.get_result = _MockImage()

    def get(self, name):
        if isinstance(self.get_result, Exception):
            raise self.get_result
        return self.get_result


class _MockVolumes:
    def __init__(self):
        self.get_result = _MockVolume()

    def get(self, name):
        if isinstance(self.get_result, Exception):
            raise self.get_result
        self.last_get_name = name
        return self.get_result

    def create(self, name):
        self.last_create_name = name


class _LoggerSpy:
    """用于 monkeypatch 模块级 logger 的间谍。

    记录所有调用并**透传**到真实 logger（含 exc_info 等 kwargs）。
    这样测试可以断言收到了哪些日志，同时真实 logger 的行为不变。
    """

    def __init__(self, real_logger) -> None:
        self._real = real_logger
        self.messages: list[tuple[str, str, str]] = []  # [(level, template, formatted)]

    def _record(self, level: str, template: str, *args, **kwargs):
        formatted = str(args[0]) if args else template
        self.messages.append((level, template, formatted))
        # 透传真实 logger，含 exc_info 等关键字参数
        getattr(self._real, level)(template, *args, **kwargs)

    def info(self, template, *args, **kwargs):
        self._record("info", template, *args, **kwargs)

    def warning(self, template, *args, **kwargs):
        self._record("warning", template, *args, **kwargs)

    def error(self, template, *args, **kwargs):
        self._record("error", template, *args, **kwargs)

    def exception(self, template, *args, **kwargs):
        self._record("exception", template, *args, **kwargs)

    def debug(self, template, *args, **kwargs):
        self._record("debug", template, *args, **kwargs)

    def remove(self, *args, **kwargs):
        self._real.remove(*args, **kwargs)

    def add(self, *args, **kwargs):
        return self._real.add(*args, **kwargs)


# ─────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_docker_client():
    """返回一个默认成功的模拟 Docker 客户端。"""
    return _MockDockerClient()


@pytest.fixture
def backend(mock_docker_client, monkeypatch):
    """创建 AndroidBuildBackend 实例，注入 mock Docker 客户端。

    默认成功路径：_detect_host_agent_data_root 返回 /host/data/agents，
    镜像存在，卷存在，容器运行成功。
    """
    config = SandboxConfig(type="android-build", max_timeout=1800)

    # 注入 mock Docker 客户端 — 通过 monkeypatch get_docker_client
    monkeypatch.setattr(
        "app.services.sandbox.local.android_build_backend.get_docker_client",
        lambda: mock_docker_client,
    )
    # 同时设置 _host_agent_data_root 避免 __init__ 走真实 Docker
    # 但 __init__ 会调用 _detect_host_agent_data_root → 需 mock
    # 最简单：mock 模块级函数
    monkeypatch.setattr(
        "app.services.sandbox.local.android_build_backend._detect_host_agent_data_root",
        lambda: "/host/data/agents",
    )

    inst = AndroidBuildBackend(config)
    return inst


@pytest.fixture
def logger_spy(monkeypatch):
    """monkeypatch AndroidBuildBackend 模块的 logger 为 _LoggerSpy。"""
    import app.services.sandbox.local.android_build_backend as abm
    spy = _LoggerSpy(abm.logger)
    monkeypatch.setattr(abm, "logger", spy)
    return spy


# ─────────────────────────────────────────────────────────
# Fix 5: /dev/shm 扩容测试（最先测，因为它只需要构造函数）
# ─────────────────────────────────────────────────────────


class TestDevShmSize:
    """验证 /dev/shm tmpfs 扩容至 1GB。"""

    def test_dev_shm_tmpfs_has_1g(self, backend, mock_docker_client):
        """tmpfs 配置中 /dev/shm 的 size 必须是 1g。"""
        # 触发 execute 走 mock 容器路径
        # 先让 execute 能通过预检
        result = asyncio.run(backend.execute(
            code="", language="java",
            timeout=30, work_dir="/workspace",
            project_path="/workspace/app",
            gradle_task="assembleDebug",
        ))
        # 无论成功与否，检查最后一次 containers.run 的参数
        last_kwargs = mock_docker_client.containers.last_run_kwargs
        assert last_kwargs is not None, "containers.run 应该已被调用"
        tmpfs = last_kwargs.get("tmpfs", {})
        assert "/dev/shm" in tmpfs
        assert "size=1g" in tmpfs["/dev/shm"], (
            f"预期 /dev/shm size=1g, 实际: {tmpfs['/dev/shm']}"
        )
        assert "size=256m" not in tmpfs["/dev/shm"], "旧值 256m 不应存在"


# ─────────────────────────────────────────────────────────
# Fix 6: /home/builduser/.android tmpfs 权限 + 代理透传
# ─────────────────────────────────────────────────────────


class TestAndroidTmpfsPermissions:
    """验证 /home/builduser/.android tmpfs 带 uid/gid（P5 Fix 2 遗漏项）。

    容器以 read_only rootfs + user=builduser(uid=1000) 运行，.android 由 tmpfs
    覆盖。若挂载选项缺 uid=1000,gid=1000，tmpfs 默认 root 所有，
    sdkmanager 写 $ANDROID_USER_HOME 报 Permission denied。
    """

    def test_android_tmpfs_has_uid_gid(self, backend, mock_docker_client):
        asyncio.run(backend.execute(
            code="", language="java",
            timeout=30, work_dir="/workspace",
            project_path="/workspace/app",
            gradle_task="assembleDebug",
        ))
        last_kwargs = mock_docker_client.containers.last_run_kwargs
        assert last_kwargs is not None, "containers.run 应该已被调用"
        tmpfs = last_kwargs.get("tmpfs", {})
        android_tmpfs = tmpfs.get("/home/builduser/.android", "")
        assert "uid=1000" in android_tmpfs and "gid=1000" in android_tmpfs, (
            f"预期 .android tmpfs 带 uid=1000,gid=1000, 实际: {android_tmpfs!r}"
        )


class TestProxyPassthrough:
    """验证容器 env 透传代理（与 docker_backend/subprocess_backend 对齐）。

    sdkmanager/AGP 在容器内联网下载缺失 SDK 组件时依赖代理出口，
    否则 dl.google.com 不可达导致自动下载失败。
    """

    def test_proxy_env_passed_into_container(self, backend, mock_docker_client, monkeypatch):
        monkeypatch.setenv("http_proxy", "http://proxy.local:3128")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
        monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")

        asyncio.run(backend.execute(
            code="", language="java",
            timeout=30, work_dir="/workspace",
            project_path="/workspace/app",
            gradle_task="assembleDebug",
        ))
        last_kwargs = mock_docker_client.containers.last_run_kwargs
        assert last_kwargs is not None, "containers.run 应该已被调用"
        env = last_kwargs.get("environment", {})
        assert env.get("http_proxy") == "http://proxy.local:3128"
        assert env.get("HTTPS_PROXY") == "http://proxy.local:3128"
        assert env.get("no_proxy") == "localhost,127.0.0.1"


class TestMirrorSwitchPassthrough:
    """验证 ANDROID_GRADLE_MIRRORS 开关透传进构建容器 env。

    entrypoint 默认开启国内镜像注入；部署方可通过宿主环境
    ANDROID_GRADLE_MIRRORS=off 关闭（fake-ip 代理挂死根治方案的开关）。
    """

    def test_mirror_switch_env_passed_into_container(
        self, backend, mock_docker_client, monkeypatch
    ):
        monkeypatch.setenv("ANDROID_GRADLE_MIRRORS", "off")

        asyncio.run(backend.execute(
            code="", language="java",
            timeout=30, work_dir="/workspace",
            project_path="/workspace/app",
            gradle_task="assembleDebug",
        ))
        last_kwargs = mock_docker_client.containers.last_run_kwargs
        assert last_kwargs is not None, "containers.run 应该已被调用"
        env = last_kwargs.get("environment", {})
        assert env.get("ANDROID_GRADLE_MIRRORS") == "off"


# ─────────────────────────────────────────────────────────
# Fix 1: Semaphore 并发控制
# ─────────────────────────────────────────────────────────


class TestSemaphoreConcurrency:
    """验证 _build_semaphore 限制并发为 _BUILD_MAX_CONCURRENT=2。"""

    def test_semaphore_initialized_correctly(self, backend):
        """_build_semaphore 必须是 asyncio.Semaphore(2)。"""
        sem = backend._build_semaphore
        assert isinstance(sem, asyncio.Semaphore)
        assert sem._value == 2, "Semaphore 初始值应为 2"

    class _SlowContainer(_MockContainer):
        """wait() 会阻塞 delay 秒的容器 mock，用于模拟真实构建耗时。"""

        def __init__(self, delay: float = 0.3):
            super().__init__()
            self._delay = delay

        def wait(self):
            """在 asyncio.to_thread 中运行，阻塞 delay 秒后返回成功。

            因为 asyncio.to_thread 在独立线程中运行，不会阻塞事件循环，
            其他 async task 可以正常调度。
            """
            time.sleep(self._delay)
            return {"StatusCode": 0}

    @pytest.mark.asyncio
    async def test_concurrency_limited_to_two(self, backend, mock_docker_client):
        """3 并发请求 → 最多 2 同时执行，第 3 个等前一个完成后才能开始。

        测试设计（经典并发令牌桶验证）：
        1. 容器 wait() 模拟构建耗时 0.3s（在独立线程中运行）
        2. 同时启动 3 个 execute() 协程
        3. 度量总耗时：3 × 0.3s / 2 并发 = ~0.6s
           如果并发控制失效（Fix 未应用）→ ~0.3s（3 个全并行）
           如果串行执行 → ~0.9s（1 个接 1 个）
        """
        mock_docker_client.containers.run_result = self._SlowContainer(delay=0.3)

        start = time.monotonic()
        results = await asyncio.gather(
            backend.execute(
                code="", language="java",
                timeout=600, work_dir="/workspace",
                project_path="/workspace/proj_a",
                gradle_task="assembleDebug",
            ),
            backend.execute(
                code="", language="java",
                timeout=600, work_dir="/workspace",
                project_path="/workspace/proj_b",
                gradle_task="assembleRelease",
            ),
            backend.execute(
                code="", language="java",
                timeout=600, work_dir="/workspace",
                project_path="/workspace/proj_c",
                gradle_task="assembleDebug",
            ),
            return_exceptions=True,
        )
        elapsed = time.monotonic() - start

        # 所有执行应成功
        all_success = all(
            isinstance(r, ExecutionResult) and r.success for r in results
        )
        assert all_success, f"所有执行应成功: {results}"

        # 3 tasks × 0.3s / 2 并发 → ~0.6s
        # 无并发控制 → ~0.3s（3 个全并行）
        # 串行 → ~0.9s
        # 当前代码（Fix 未应用）= ~0.3s（无并发控制）
        # 修复后 = 0.45-0.75s
        assert elapsed > 0.4, (
            f"Semaphore 应限制并发，如果全并行则 ~0.3s, 实际 {elapsed:.3f}s. "
            "这可能意味着 Fix 1（async with self._build_semaphore）未应用。"
        )


# ─────────────────────────────────────────────────────────
# Fix 2: 构建生命周期日志
# ─────────────────────────────────────────────────────────


class TestLifecycleLogs:
    """验证构建生命周期 3 个关键节点日志的存在性。"""

    @pytest.mark.asyncio
    async def test_logs_start_container_done_on_success(
        self, backend, mock_docker_client, logger_spy, monkeypatch
    ):
        """成功构建 → 产生 3 条生命周期日志：[start, container_start, done]。"""
        await backend.execute(
            code="", language="java",
            timeout=30, work_dir="/workspace",
            project_path="/workspace/app",
            gradle_task="assembleDebug",
        )

        start_logs = [
            m for m in logger_spy.messages
            if m[0] == "info" and "[AndroidBuild] start" in m[2]
        ]
        container_logs = [
            m for m in logger_spy.messages
            if m[0] == "info" and "[AndroidBuild] container_start" in m[2]
        ]
        done_logs = [
            m for m in logger_spy.messages
            if m[0] == "info" and "[AndroidBuild] done" in m[2]
        ]

        assert len(start_logs) == 1, f"应有 1 条 start 日志, 实际: {len(start_logs)}"
        assert len(container_logs) == 1, f"应有 1 条 container_start 日志, 实际: {len(container_logs)}"
        assert len(done_logs) == 1, f"应有 1 条 done 日志, 实际: {len(done_logs)}"

        # 验证日志包含关键字段
        assert "project=" in start_logs[0][2], f"start 日志应含 project=: {start_logs[0][2]}"
        assert "task=" in start_logs[0][2], f"start 日志应含 task=: {start_logs[0][2]}"
        assert "container_start" in container_logs[0][2]
        assert "exit=" in done_logs[0][2], f"done 日志应含 exit=: {done_logs[0][2]}"
        assert "duration=" in done_logs[0][2], f"done 日志应含 duration=: {done_logs[0][2]}"
        assert "exit=0" in done_logs[0][2], f"成功构建 exit 应为 0: {done_logs[0][2]}"


# ─────────────────────────────────────────────────────────
# Fix 3: 日志级别修正
# ─────────────────────────────────────────────────────────


class TestLogLevelCorrection:
    """验证日志级别从 WARNING 修正为 ERROR/INFO。"""

    @pytest.mark.asyncio
    async def test_docker_sock_unavailable_logs_error(self, monkeypatch):
        """docker.sock 不可用时 → logger.error（不是 warning）。

        模拟步骤：mock get_docker_client 抛异常 → _detect_host_agent_data_root
        捕获后应记录 ERROR。
        """
        from app.services.sandbox.local import android_build_backend as abm
        spy = _LoggerSpy(abm.logger)
        monkeypatch.setattr(abm, "logger", spy)

        # get_docker_client 抛异常 → 触发 except Exception 分支
        def raise_exc():
            raise RuntimeError("docker.sock unavailable")

        monkeypatch.setattr(
            "app.services.sandbox.local.android_build_backend.get_docker_client",
            raise_exc,
        )

        abm._detect_host_agent_data_root()

        error_msgs = [m for m in spy.messages if m[0] == "error"]
        warning_msgs = [m for m in spy.messages if m[0] == "warning"]

        # docker.sock unavailable 应记录为 ERROR
        docker_errors = [
            m for m in error_msgs
            if "docker.sock" in m[2] or "docker.sock" in m[1]
        ]
        assert len(docker_errors) >= 1, (
            f"docker.sock 不可达应记录 ERROR, "
            f"ERROR 消息: {[m[2] for m in error_msgs]}"
        )
        # 不应有 WARNING 级别
        docker_warnings = [
            m for m in warning_msgs
            if "docker.sock" in m[2] or "docker.sock" in m[1]
        ]
        assert len(docker_warnings) == 0, (
            f"不应有 docker.sock 的 WARNING: {[m[2] for m in docker_warnings]}"
        )

    @pytest.mark.asyncio
    async def test_docker_sock_not_found_logs_error(self, monkeypatch):
        """docker.sock 返回 NotFound → logger.error（不是 warning）。"""
        from app.services.sandbox.local import android_build_backend as abm
        spy = _LoggerSpy(abm.logger)
        monkeypatch.setattr(abm, "logger", spy)

        # containers.get 抛 NotFound
        class MockNotFoundContainers:
            def get(self, name):
                raise docker.errors.NotFound("container not found")

        client = _MockDockerClient()
        client.containers = MockNotFoundContainers()

        monkeypatch.setattr(
            "app.services.sandbox.local.android_build_backend.get_docker_client",
            lambda: client,
        )

        abm._detect_host_agent_data_root()

        error_msgs = [m for m in spy.messages if m[0] == "error"]

        # 容器未找到应记录为 ERROR
        not_found_errors = [
            m for m in error_msgs if "not found via docker.sock" in m[2]
        ]
        assert len(not_found_errors) >= 1, (
            f"容器未找到应记录 ERROR: {[m[2] for m in error_msgs]}"
        )

    @pytest.mark.asyncio
    async def test_path_not_under_data_agents_is_info_not_warning(
        self, backend, mock_docker_client, logger_spy
    ):
        """路径不在 /data/agents 下 → logger.info（不是 warning）。"""
        # _resolve_host_path 在 _host_agent_data_root 非空时比较路径前缀
        # 传入一个不在 /data/agents 下的容器路径
        result = backend._resolve_host_path("/tmp/custom/path")

        # 此时应有一条 logger.info 消息（原为 logger.warning）
        info_msgs = [m for m in logger_spy.messages if m[0] == "info"]
        warning_msgs = [m for m in logger_spy.messages if m[0] == "warning"]

        path_info = [
            m for m in info_msgs
            if "path not under /data/agents" in m[2]
        ]
        path_warnings = [
            m for m in warning_msgs
            if "path not under /data/agents" in m[2]
        ]

        assert len(path_info) >= 1, (
            f"路径回退应记录为 INFO, INFO 消息: {[m[2] for m in info_msgs]}"
        )
        assert len(path_warnings) == 0, (
            f"路径回退不应有 WARNING: {[m[2] for m in warning_msgs]}"
        )


# ─────────────────────────────────────────────────────────
# Fix 4: 超时路径异常隔离
# ─────────────────────────────────────────────────────────


class TestTimeoutErrorIsolation:
    """验证超时路径中 kill/remove 失败不覆盖原始超时错误。

    策略：monkeypatch asyncio.wait_for 使第一次调用抛 TimeoutError。
    在我们的 mock 流程中，第一个 wait_for 就是 container.wait 的那个。
    """

    async def _run_with_timeout(
        self, backend, monkeypatch, *,
        container_kill, container_remove,
    ) -> ExecutionResult | Exception:
        """辅助方法：注入 TimeoutError + 自定义 kill/remove 行为后执行。

        如果 kill/remove 的异常未被嵌套 try/except 捕获，会传播到此处。
        调用方应检查返回值类型以判断 Fix 4 是否已应用。
        """
        async def _raise_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "wait_for", _raise_timeout)

        mock_client = _MockDockerClient()
        mock_container = _MockContainer(wait_result={"StatusCode": 0})
        mock_container.kill = container_kill
        mock_container.remove = container_remove
        mock_client.containers.run_result = mock_container
        monkeypatch.setattr(
            "app.services.sandbox.local.android_build_backend.get_docker_client",
            lambda: mock_client,
        )

        try:
            return await backend.execute(
                code="", language="java",
                timeout=1, work_dir="/workspace",
                project_path="/workspace/app",
                gradle_task="assembleDebug",
            )
        except Exception as exc:
            return exc

    @pytest.mark.asyncio
    async def test_timeout_error_returned_when_kill_fails(self, backend, monkeypatch, logger_spy):
        """Fix 4 验证：超时发生后 kill 抛异常 → 返回值仍是超时信息（exit_code=124）。

        RED：当前代码缺少嵌套 try/except，kill 异常会传播到外层 → 返回异常。
        GREEN：Fix 4 应用后，kill 异常被嵌套 try/except 吞掉 → 返回 exit_code=124。
        """
        def failing_kill(**kw):
            raise RuntimeError("Docker daemon not responding")

        def failing_remove(**kw):
            raise RuntimeError("Container removal failed")

        result = await self._run_with_timeout(
            backend, monkeypatch,
            container_kill=failing_kill,
            container_remove=failing_remove,
        )

        if isinstance(result, Exception):
            # RED: Fix 4 未应用，kill 异常传播到外层
            pytest.fail(
                f"Fix 4 未应用：kill/remove 异常传播到了外层。\n"
                f"预期：exit_code=124 的超时结果\n"
                f"实际异常：{type(result).__name__}: {result}\n"
                f"修复：在 timeout 清理路径加嵌套 try/except 隔离 kill/remove 异常"
            )

        # GREEN: Fix 4 已应用
        assert result.exit_code == 124, f"超时应返回 exit_code=124, 实际: {result.exit_code}"
        assert "编译超时" in (result.error or ""), (
            f"错误信息应包含'编译超时': {result.error}"
        )
        assert "kill" not in (result.error or ""), (
            f"错误信息不应包含 kill 异常: {result.error}"
        )

    @pytest.mark.asyncio
    async def test_timeout_error_returned_when_remove_fails(self, backend, monkeypatch):
        """Fix 4 验证：超时后 remove 失败 → 返回值仍是超时信息。"""
        def failing_remove(**kw):
            raise RuntimeError("Permission denied")

        result = await self._run_with_timeout(
            backend, monkeypatch,
            container_kill=lambda **kw: None,
            container_remove=failing_remove,
        )

        if isinstance(result, Exception):
            pytest.fail(
                f"Fix 4 未应用：remove 异常传播到了外层。\n"
                f"预期：exit_code=124 的超时结果\n"
                f"实际异常：{type(result).__name__}: {result}"
            )

        assert result.exit_code == 124, f"超时应返回 exit_code=124"
        assert "编译超时" in (result.error or "")


# ─────────────────────────────────────────────────────────
# 附：Fix 4 外层 finally 清理不重复抛出
# ─────────────────────────────────────────────────────────


class TestFinallyCleanupSafety:
    """验证外层 finally 不会在超时路径的清理已执行后二次抛异常。"""

    @pytest.mark.asyncio
    async def test_finally_does_not_crash_when_variables_unbound(self, backend, monkeypatch):
        """超时路径已处理 kill/remove → finally 块不重复引起异常。"""
        async def _raise_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "wait_for", _raise_timeout)

        mock_docker_client = _MockDockerClient()
        mock_container = _MockContainer(wait_result={"StatusCode": 0})

        mock_container.kill = lambda **kw: None
        mock_container.remove = lambda **kw: None
        mock_docker_client.containers.run_result = mock_container
        monkeypatch.setattr(
            "app.services.sandbox.local.android_build_backend.get_docker_client",
            lambda: mock_docker_client,
        )

        try:
            result = await backend.execute(
                code="", language="java",
                timeout=1, work_dir="/workspace",
                project_path="/workspace/app",
            )
        except Exception as exc:
            pytest.fail(
                f"Fix 4 未应用：finally 块二次清理引发了异常。\n"
                f"预期：exit_code=124\n"
                f"实际异常：{type(exc).__name__}: {exc}"
            )
        assert result.exit_code == 124


# ─────────────────────────────────────────────────────────
# 卷属主预热（防复发保险丝）
# ─────────────────────────────────────────────────────────


class TestGradleCacheOwnershipPreheat:
    """验证构建容器启动后以 root exec 幂等 chown gradle 缓存卷。

    背景：gradle_cache_global 卷在镜像历史版本间迁移/重建时可能残留
    root 属主内容，而构建容器以 builduser (uid=1000) 运行且根文件系统
    只读，无法自行修复，会以 Permission denied 失败。预热是运行时保险丝：
    属主正确时是 no-op，失败仅告警不阻塞构建。
    """

    def test_preheat_runs_as_root_with_chown(self, backend, mock_docker_client):
        result = asyncio.run(backend.execute(
            code="", language="java",
            timeout=30, work_dir="/workspace",
            project_path="/workspace/app",
            gradle_task="assembleDebug",
        ))
        assert result.success is True, f"预热不应影响构建结果: {result.error}"

        container = mock_docker_client.containers.run_result
        assert container.exec_run_calls, "构建前应执行卷属主预热 exec_run"
        call = container.exec_run_calls[0]
        assert call["user"] == "root", f"预热必须以 root 执行, 实际: {call['user']!r}"
        cmd_text = " ".join(call["cmd"])
        assert "chown -R 1000:1000 /home/builduser/.gradle" in cmd_text, (
            f"预热命令应含幂等 chown, 实际: {cmd_text!r}"
        )

    def test_preheat_failure_only_warns(self, backend, mock_docker_client, logger_spy):
        """exec_run 失败 → 仅告警，不阻塞构建。"""
        def _failing_exec_run(*args, **kwargs):
            raise RuntimeError("docker exec failed")

        mock_docker_client.containers.run_result.exec_run = _failing_exec_run

        result = asyncio.run(backend.execute(
            code="", language="java",
            timeout=30, work_dir="/workspace",
            project_path="/workspace/app",
            gradle_task="assembleDebug",
        ))
        assert result.success is True, f"预热失败不应阻塞构建: {result.error}"
        warnings = [
            m for m in logger_spy.messages
            if m[0] == "warning" and "属主预热" in m[2]
        ]
        assert warnings, "预热失败应产生 warning 日志"

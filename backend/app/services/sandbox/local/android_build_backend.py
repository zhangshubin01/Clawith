"""Android 项目编译沙箱后端。

基于预构建的 clawith-android-builder 镜像 + 共享卷。
Gradle 缓存按项目隔离，避免并发锁冲突。

关键：通过 docker.sock 启动构建容器时，project_path 必须是宿主机路径。
使用 _resolve_host_path() 将容器内路径翻译为宿主机可访问的路径。
"""

import asyncio
import os
import queue as thread_queue
import socket
import time
from pathlib import Path

import docker
from docker import errors

from app.services.sandbox.base import BaseSandboxBackend, ExecutionResult, SandboxCapabilities
from app.services.sandbox.config import SandboxConfig
from loguru import logger


def _detect_host_agent_data_root() -> str:
    """通过 docker.sock 查询本容器，返回 /data/agents 在宿主机上的实际路径。

    Docker-on-Docker (DooD): 后端容器通过 docker.sock 启动构建容器，
    bind mount 的源路径由宿主机 Docker daemon 解析，必须是宿主机路径。
    本函数无需任何配置，换电脑自动适配。

    在 macOS (Docker Desktop/OrbStack/Colima) 上, Mounts[].Source 返回
    macOS 宿主机路径 (如 /Users/.../agent_data)，Docker 的文件共享层
    会正确处理后续容器的 bind mount 路径翻译。
    """
    hostname = os.environ.get("HOSTNAME") or socket.gethostname()
    try:
        client = docker.from_env()
        info = client.containers.get(hostname)
    except errors.NotFound:
        logger.warning(f"[AndroidBuild] container {hostname} not found via docker.sock")
        return ""
    except Exception as e:
        logger.warning(f"[AndroidBuild] docker.sock unavailable: {e}")
        return ""

    for m in info.attrs.get("Mounts", []):
        dest = (m.get("Destination") or "").rstrip("/")
        if dest == "/data/agents":
            host_path = m["Source"]
            logger.info(f"[AndroidBuild] detected host path: {host_path}")
            return host_path

    logger.warning("[AndroidBuild] /data/agents mount not found in container info")
    return ""


class AndroidBuildBackend(BaseSandboxBackend):
    """Android 项目编译沙箱后端。

    与 DockerBackend 的关键差异：
    - 使用定制镜像（而非官方 language 镜像）
    - 挂载项目源码 + 全局 JDK/SDK 缓存 + 项目独立 Gradle 缓存
    - 最长 30 分钟超时（适配 assembleRelease）
    """

    name = "android-build"
    DEFAULT_IMAGE = "clawith-android-builder:latest"

    # 全局共享卷
    VOLUME_JDK = "global_jdk_cache"
    VOLUME_SDK = "global_android_sdk"

    # 全局共享 Gradle 缓存卷（SERIAL_ALWAYS 无并发锁冲突）
    GRADLE_CACHE_VOLUME = "gradle_cache_global"

    def __init__(self, config: SandboxConfig):
        self.config = config
        self._client = None
        # 运行时自动检测宿主机路径（零配置，换电脑自动适配）
        self._host_agent_data_root = _detect_host_agent_data_root()

    @property
    def client(self):
        """延迟加载 docker SDK。"""
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _resolve_host_path(self, container_path: str) -> str:
        """将容器内 /data/agents/... 翻译为宿主机绝对路径。

        Docker-in-Docker 场景：后端容器通过 docker.sock 启动构建容器，
        绑定挂载的源路径由宿主机 Docker 守护进程解析，必须是宿主机路径。
        """
        if not self._host_agent_data_root:
            return container_path  # 本地开发，路径直接可用
        prefix = "/data/agents"
        if container_path.startswith(prefix):
            return self._host_agent_data_root + container_path[len(prefix):]
        logger.warning(
            f"[AndroidBuild] path not under /data/agents, may fail: {container_path}"
        )
        return container_path

    def get_capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            supported_languages=["java", "kotlin", "android-build"],
            max_timeout=self.config.max_timeout,  # 从配置读取，默认 1800s
            max_memory_mb=8192,
            network_available=True,
            filesystem_available=True,
        )

    async def health_check(self) -> bool:
        """检查 Docker 是否可用。"""
        try:
            self.client.ping()
            return True
        except Exception:
            return False

    async def execute(
        self,
        code: str,
        language: str,
        timeout: int = 1800,
        work_dir: str | None = None,
        **kwargs
    ) -> ExecutionResult:
        """在 Docker 容器中执行 Android 项目编译。"""
        start_time = time.time()

        # 提取参数
        project_path = kwargs.get("project_path", work_dir or "/workspace")
        java_version = str(kwargs.get("java_version", "17"))
        git_username = kwargs.get("git_username", "")
        git_token = kwargs.get("git_token", "")
        gradle_task = kwargs.get("gradle_task", "assembleDebug")

        # 将容器内路径翻译为宿主机路径
        host_project_path = self._resolve_host_path(str(project_path))

        # 全局共享 Gradle 缓存卷（SERIAL_ALWAYS 保证无并发）
        gradle_volume = self.GRADLE_CACHE_VOLUME

        # 确保 Gradle 卷存在（只捕获 NotFound，避免掩盖连接异常）
        try:
            self.client.volumes.get(gradle_volume)
        except errors.NotFound:
            logger.info(f"[AndroidBuild] 创建项目 Gradle 缓存卷: {gradle_volume}")
            self.client.volumes.create(name=gradle_volume)

        # 环境变量
        env = {
            "JAVA_VERSION": java_version,
            "GRADLE_OPTS": (
                "-Dorg.gradle.daemon=false "
                "-Dorg.gradle.jvmargs=-Xmx4096m "
                "-XX:MaxMetaspaceSize=1g "
                "-XX:+HeapDumpOnOutOfMemoryError "
                "-XX:+ExitOnOutOfMemoryError "
                "-Dorg.gradle.configuration-cache=true "
                "-Dorg.gradle.configuration-cache.problems=warn "
                "-Dorg.gradle.configuration-cache.max-problems=512 "
                "-Dorg.gradle.caching=true "
                "-Dorg.gradle.parallel=true "
                "-Dkotlin.compiler.execution.strategy=in-process"
            ),
        }
        if git_username and git_token:
            env["GIT_USERNAME"] = git_username
            env["GIT_TOKEN"] = git_token
        # 签名密钥密码从 kwargs 透传
        for key in ("KEY_STORE_PASSWORD", "KEY_ALIAS", "KEY_PASSWORD"):
            if key.lower() in kwargs:
                env[key] = str(kwargs[key.lower()])

        # 卷挂载（SDK 卷只读，避免并发写入损坏）
        volumes = {
            host_project_path: {"bind": "/workspace", "mode": "rw"},
            self.VOLUME_JDK: {"bind": "/opt/jdks", "mode": "rw"},
            self.VOLUME_SDK: {"bind": "/opt/android-sdk", "mode": "rw"},  # SERIAL_ALWAYS 保证无并发写，rw 允许 AGP 自动补全缺失 SDK
            gradle_volume: {"bind": "/root/.gradle", "mode": "rw"},
        }

        # 堆内存按任务类型调整
        mem_limit = "8g" if "Release" in str(gradle_task) else "6g"

        try:
            container = self.client.containers.run(
                image=self.DEFAULT_IMAGE,
                command=[
                    "bash", "-c",
                    f'echo "sdk.dir=/opt/android-sdk" > local.properties '
                    f"&& chmod +x ./gradlew "
                    f"&& ./gradlew {gradle_task} "
                    f"&& cp -r app/build/outputs/apk /workspace/apk-output 2>/dev/null || true "
                    f"&& sleep 5",
                ],
                detach=True,
                volumes=volumes,
                environment=env,
                working_dir="/workspace",
                mem_limit=mem_limit,
                cpu_quota=400000,
                cpu_period=100000,
                # build 产物直接写入 workspace（agent workspace），
                # 利用 Docker bind mount 持久化到宿主机，保留增量编译缓存。
                network_mode="bridge",
                remove=False,
            )

            on_output = kwargs.get("on_output")
            stdout_buf = bytearray()
            # threading.Queue 天然线程安全，用于流式线程→主协程通信
            output_queue: thread_queue.Queue[bytes | None] = thread_queue.Queue()

            def _stream_logs():
                """在后台线程中流式读取容器日志，写入线程安全队列。"""
                try:
                    for chunk in container.logs(stream=True, follow=True, stdout=True, stderr=True):
                        output_queue.put(chunk)
                finally:
                    output_queue.put(None)  # 停止信号

            stream_task = asyncio.ensure_future(asyncio.to_thread(_stream_logs))

            async def _drain_queue():
                """主协程消费队列：收集日志 + 推送 WebSocket。"""
                loop = asyncio.get_running_loop()
                while True:
                    chunk = await loop.run_in_executor(None, output_queue.get)
                    if chunk is None:
                        break
                    stdout_buf.extend(chunk)
                    if on_output:
                        try:
                            await on_output(chunk.decode("utf-8", errors="replace"))
                        except Exception:
                            pass

            drain_task = asyncio.create_task(_drain_queue())

            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(container.wait, timeout=timeout),
                    timeout=timeout + 10,
                )
            except asyncio.TimeoutError:
                drain_task.cancel()
                stream_task.cancel()
                container.kill()
                container.remove(force=True)
                return ExecutionResult(
                    success=False, stdout="", stderr="",
                    exit_code=124,
                    duration_ms=int((time.time() - start_time) * 1000),
                    error=f"编译超时（{timeout}s），任务: {gradle_task}",
                )

            # 等待队列消费完毕
            await drain_task

            stdout = stdout_buf.decode("utf-8", errors="replace")

            # 保留末尾输出（Gradle 编译错误关键信息在末尾）
            if len(stdout) > 50000:
                stdout = "...(前段省略)..." + stdout[-50000:]

            exit_code_val = result.get("StatusCode", 1)
            duration_ms = int((time.time() - start_time) * 1000)

            return ExecutionResult(
                success=exit_code_val == 0,
                stdout=stdout,
                stderr="",
                exit_code=exit_code_val,
                duration_ms=duration_ms,
                error=None if exit_code_val == 0 else f"构建失败 (exit={exit_code_val})",
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.exception("[AndroidBuild] 编译异常")
            return ExecutionResult(
                success=False, stdout="", stderr="",
                exit_code=1, duration_ms=duration_ms,
                error=f"构建错误: {str(e)[:200]}",
            )
        finally:
            # 确保清理：所有路径（成功/超时/异常）统一收尾
            try:
                if 'drain_task' in locals():
                    drain_task.cancel()
            except Exception:
                pass
            try:
                if 'stream_task' in locals():
                    stream_task.cancel()
            except Exception:
                pass
            try:
                if 'container' in locals():
                    container.remove(force=True)
            except Exception:
                pass

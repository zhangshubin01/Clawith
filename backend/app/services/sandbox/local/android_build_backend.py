"""Android 项目编译沙箱后端。

基于预构建的 clawith-android-builder 镜像 + 共享卷。
Gradle 缓存按项目隔离，避免并发锁冲突。

关键：通过 docker.sock 启动构建容器时，project_path 必须是宿主机路径。
使用 _resolve_host_path() 将容器内路径翻译为宿主机可访问的路径。
"""

import asyncio
import os
import queue as thread_queue
import shlex
import socket
import time
from pathlib import Path

from docker import errors

from app.services.sandbox.base import BaseSandboxBackend, ExecutionResult, SandboxCapabilities
from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.docker_client import get_docker_client
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
        client = get_docker_client()
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
    DEFAULT_IMAGE = os.getenv("DEVBOX_ANDROID_IMAGE", "clawith-android-builder:latest")

    # 全局共享卷
    VOLUME_JDK = "global_jdk_cache"
    VOLUME_SDK = "global_android_sdk"

    # 全局共享 Gradle 缓存卷（SERIAL_ALWAYS 无并发锁冲突）
    GRADLE_CACHE_VOLUME = "gradle_cache_global"

    # 单 worker 级并发限制。多 worker (uvicorn --workers N) 下全局并发 = N × 2。
    # 如需全局上限，改为 Redis 分布式信号量 (aioredlock)。
    _BUILD_MAX_CONCURRENT = 2

    # 模块级信号量 — get_sandbox_backend() 每次创建新实例，实例级 Semaphore 无效
    _build_semaphore = asyncio.Semaphore(_BUILD_MAX_CONCURRENT)

    def __init__(self, config: SandboxConfig):
        self.config = config
        self._client = None
        # 运行时自动检测宿主机路径（零配置，换电脑自动适配）
        self._host_agent_data_root = _detect_host_agent_data_root()

    @property
    def client(self):
        """延迟加载 docker SDK。"""
        if self._client is None:
            self._client = get_docker_client()
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
        timeout: int = 600,
        work_dir: str | None = None,
        **kwargs
    ) -> ExecutionResult:
        """在 Docker 容器中执行 Android 项目编译（并发控制入口）。"""
        async with AndroidBuildBackend._build_semaphore:
            start_time = time.time()

            # 提取参数
            project_path = kwargs.get("project_path", work_dir or "/workspace")
            java_version = str(kwargs.get("java_version", "17"))
            git_username = kwargs.get("git_username", "")
            git_token = kwargs.get("git_token", "")
            gradle_task = kwargs.get("gradle_task", "assembleDebug")

            logger.info(
                f"[AndroidBuild] start project={Path(project_path).name} task={gradle_task} jdk={java_version} timeout={timeout}s"
            )

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
                "TERM": "dumb",  # 强制 JVM 行缓冲输出（非 TTY 环境）
                "HOME": "/home/builduser",
                "GRADLE_USER_HOME": "/home/builduser/.gradle",
                "GRADLE_OPTS": (
                    "-Dorg.gradle.daemon=false "
                    "-Dorg.gradle.jvmargs=-Xmx4096m "
                    "-XX:MaxMetaspaceSize=768m "
                    "-XX:+HeapDumpOnOutOfMemoryError "
                    "-XX:+ExitOnOutOfMemoryError "
                    "-Dorg.gradle.configuration-cache=true "
                    "-Dorg.gradle.configuration-cache.problems=warn "
                    "-Dorg.gradle.configuration-cache.max-problems=512 "
                    "-Dorg.gradle.caching=true "
                    "-Dorg.gradle.parallel=true "
                    "-Dorg.gradle.workers.max=4 "
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
                gradle_volume: {"bind": "/home/builduser/.gradle", "mode": "rw"},
            }

            # 堆内存按任务类型调整
            mem_limit = "8g"

            try:
                container = self.client.containers.run(
                    image=self.DEFAULT_IMAGE,
                    command=[
                        "bash", "-c",
                        # 接受 Android SDK 许可协议（CI/CD 标准做法）
                        f"yes | sdkmanager --licenses >/dev/null 2>&1 || true; "
                        f'echo "sdk.dir=/opt/android-sdk" > local.properties '
                        f"&& chmod +x ./gradlew "
                        f"&& ./gradlew --no-daemon --console=plain {shlex.quote(str(gradle_task))} ",
                    ],
                    detach=True,
                    volumes=volumes,
                    environment=env,
                    working_dir="/workspace",
                    mem_limit=mem_limit,
                    cpu_quota=400000,
                    cpu_period=100000,
                    # tmpfs 内存盘：仅项目级 build/ 写入内存加速（Gradle 中间产物）
                    # app/build 和 .gradle 不挂 tmpfs — APK 产物 + 配置缓存落在 bind mount 持久化
                    # /dev/shm 和 /tmp 也挂 tmpfs 避免容器默认的 64M shm 溢出
                    tmpfs={
                        "/workspace/build": "rw,exec,noatime,size=2g",
                        "/dev/shm": "rw,noexec,nosuid,size=1g",
                        "/tmp": "rw,noexec,nosuid,size=1g",
                        "/home/builduser/.android": "rw,noexec,nosuid,size=128m",
                    },
                    network_mode="bridge",
                    remove=False,
                    security_opt=["no-new-privileges:true"],
                    user="builduser",
                    read_only=True,
                )

                logger.info(
                    f"[AndroidBuild] container_start id={container.id[:12]}"
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
                    """主协程消费队列：批量收集日志（100ms 窗口），合并后推送 WebSocket。

                    Docker 的 json-file log driver 按行分帧，container.logs(stream=True)
                    每帧一个 chunk。逐帧推 WebSocket 会导致前端消息间产生空行。
                    100ms 批量缓冲合并相邻 chunk，消除消息边界产生的视觉空行。
                    """
                    loop = asyncio.get_running_loop()
                    batch: list[bytes] = []
                    last_flush = time.time()
                    BATCH_INTERVAL = 0.1  # 100ms 批处理窗口

                    async def _flush_batch():
                        nonlocal last_flush
                        if not batch:
                            return
                        merged = b"".join(batch)
                        batch.clear()
                        last_flush = time.time()
                        if on_output:
                            try:
                                await on_output(merged.decode("utf-8", errors="replace"))
                            except Exception as e:
                                logger.warning(f"[AndroidBuild] on_output 回调异常: {e}")

                    while True:
                        chunk = await loop.run_in_executor(None, output_queue.get)
                        if chunk is None:
                            await _flush_batch()
                            break
                        stdout_buf.extend(chunk)
                        if on_output:
                            batch.append(chunk)
                            if time.time() - last_flush >= BATCH_INTERVAL:
                                await _flush_batch()

                drain_task = asyncio.create_task(_drain_queue())

                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(container.wait),
                        timeout=timeout + 10,
                    )
                except asyncio.TimeoutError:
                    drain_task.cancel()
                    stream_task.cancel()
                    try:
                        container.kill()
                    except Exception:
                        logger.warning("[AndroidBuild] 超时后容器 kill 失败", exc_info=True)
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

                logger.info(
                    f"[AndroidBuild] done exit={exit_code_val} duration={duration_ms}ms"
                )

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
                except Exception as e:
                    logger.warning(f"[AndroidBuild] drain_task 取消失败: {e}")
                try:
                    if 'stream_task' in locals():
                        stream_task.cancel()
                except Exception as e:
                    logger.warning(f"[AndroidBuild] stream_task 取消失败: {e}")
                try:
                    if 'container' in locals():
                        container.remove(force=True)
                except Exception as e:
                    logger.warning(f"[AndroidBuild] 容器清理失败: {e}")

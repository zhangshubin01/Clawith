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
        logger.error(f"[AndroidBuild] container {hostname} not found via docker.sock")
        return ""
    except Exception as e:
        logger.error(f"[AndroidBuild] docker.sock unavailable: {e}")
        return ""

    for m in info.attrs.get("Mounts", []):
        dest = (m.get("Destination") or "").rstrip("/")
        if dest == "/data/agents":
            host_path = m["Source"]
            logger.info(f"[AndroidBuild] detected host path: {host_path}")
            return host_path

    logger.error("[AndroidBuild] /data/agents mount not found in container info")
    return ""


class AndroidBuildBackend(BaseSandboxBackend):
    """Android 项目编译沙箱后端。

    与 DockerBackend 的关键差异：
    - 使用定制镜像（而非官方 language 镜像）
    - 挂载项目源码 + 全局 JDK/SDK 缓存 + 项目独立 Gradle 缓存
    - 最长 30 分钟超时（适配 assembleRelease）
    """

    name = "android-build"
    DEFAULT_IMAGE = os.getenv("DEVBOX_ANDROID_IMAGE", "clawith-devbox-android:latest")

    # 全局共享卷
    VOLUME_JDK = "global_jdk_cache"
    VOLUME_SDK = "global_android_sdk"

    # 全局共享 Gradle 缓存卷（进程内 Semaphore(2)，多副本部署需分布式协调）
    GRADLE_CACHE_VOLUME = "gradle_cache_global"

    # 单 worker 级并发限制。多 worker (uvicorn --workers N) 下全局并发 = N × 2。
    # 如需全局上限，改为 Redis 分布式信号量 (aioredlock)。
    _BUILD_MAX_CONCURRENT = 2
    # stdout 缓冲上限，防止异常构建（如无限循环日志）撑爆内存
    _MAX_STDOUT_CAPTURE = 5_000_000  # 5MB
    _GRADLE_CACHE_MODULE_DIRS_MAX = 500        # modules-2 目录数阈值

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
        """将容器内路径翻译为宿主机绝对路径，防止符号链接穿越和 .. 组件绕过。

        安全要求：解析真实路径后验证仍在 agent_data_root 内。
        """
        if not self._host_agent_data_root:
            return container_path
        prefix = "/data/agents"
        if not container_path.startswith(prefix):
            logger.info(
                f"[AndroidBuild] path not under /data/agents, may fail: {container_path}"
            )
            return container_path

        try:
            resolved = os.path.realpath(
                self._host_agent_data_root + container_path[len(prefix):]
            )
            agent_data_real = os.path.realpath(self._host_agent_data_root)
        except OSError as e:
            logger.error(f"[AndroidBuild] 路径解析失败: {container_path} error={e}")
            raise ValueError(f"路径解析失败: {container_path}") from e
        if not resolved.startswith(agent_data_real + os.sep) and resolved != agent_data_real:
            logger.error(
                f"[AndroidBuild] 路径穿越拒绝: {container_path} resolved={resolved}"
            )
            raise ValueError(f"路径穿越检测: {container_path}")

        return resolved

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
            logger.opt(exception=True).error("[AndroidBuild] health_check 失败：Docker daemon 不可用")
            return False

    async def _enforce_gradle_cache_quota(self):
        """Gradle 依赖缓存目录数超过阈值时告警（清理由 Gradle 内置 30 天 GC 处理）。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "run", "--rm",
                "-v", f"{self.GRADLE_CACHE_VOLUME}:/cache",
                "alpine:latest", "sh", "-c",
                "find /cache/caches/modules-2 -mindepth 3 -maxdepth 3 -type d | wc -l",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            dir_count = int(stdout.decode().strip() or "0")
            if dir_count > self._GRADLE_CACHE_MODULE_DIRS_MAX:
                logger.warning(
                    f"[AndroidBuild] Gradle 依赖缓存目录数 {dir_count} > {self._GRADLE_CACHE_MODULE_DIRS_MAX}"
                    f"（Gradle 内置 GC 将驱逐 30 天未访问缓存）"
                )
            else:
                logger.debug(f"[AndroidBuild] gradle module dirs={dir_count}")
        except (asyncio.TimeoutError, ValueError, ProcessLookupError, FileNotFoundError):
            pass  # 超时/解析失败/容器不存在/docker CLI 不可用 — 不影响构建

    async def _check_sdk_version_drift(self, container) -> bool:
        """比较镜像 SDK 版本与卷中版本，检测漂移。"""
        try:
            exec_result = await asyncio.to_thread(
                container.exec_run,
                ["sh", "-c", "test -f /opt/android-sdk/.image_version && cat /opt/android-sdk/.image_version || echo unknown"],
            )
            volume_version = exec_result.output.decode().strip()
            image = await asyncio.to_thread(
                self.client.images.get, self.DEFAULT_IMAGE
            )
            image_version = image.labels.get("clawith.sdk-version", "unknown")
            if volume_version != image_version and volume_version != "unknown":
                logger.warning(
                    f"[AndroidBuild] SDK 版本漂移: 卷={volume_version} 镜像={image_version}"
                    f" 建议: docker volume rm {self.VOLUME_SDK}"
                )
                return False
            return True
        except Exception:
            return True  # 检测失败不阻塞构建

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
            from app.services.sandbox.local import android_build_metrics
            android_build_metrics.record_build_start()
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
                self.client.volumes.create(
                    name=gradle_volume,
                    labels={"managed-by": "clawith", "role": "gradle-cache"},
                )

            # 确保 SDK/JDK 卷存在（SERIAL_ALWAYS 保证无并发创建）
            for vol in (self.VOLUME_SDK, self.VOLUME_JDK):
                try:
                    self.client.volumes.get(vol)
                except errors.NotFound:
                    logger.info(f"[AndroidBuild] 创建全局缓存卷: {vol}")
                    self.client.volumes.create(
                        name=vol,
                        labels={"managed-by": "clawith", "role": "android-sdk" if vol == self.VOLUME_SDK else "jdk"},
                    )

            # 环境变量
            env = {
                "JAVA_VERSION": java_version,
                "TERM": "dumb",  # 强制 JVM 行缓冲输出（非 TTY 环境）
                "HOME": "/home/builduser",
                "GRADLE_USER_HOME": "/home/builduser/.gradle",
                # sqlite-jdbc aarch64 兼容: 强制使用纯 Java 模式（KSP worker classpath 不经项目依赖解析）
                "JAVA_TOOL_OPTIONS": "-Dsqlite.purejava=true",
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
                    "-Dorg.gradle.kotlin.daemon.jvmargs=-Xmx2048m "
                    "-Dkotlin.compiler.execution.strategy=in-process "
                    "-Dorg.gradle.warning.mode=all"
                ),
            }
            # 签名密钥密码从 kwargs 透传
            for key in ("KEY_STORE_PASSWORD", "KEY_ALIAS", "KEY_PASSWORD"):
                if key.lower() in kwargs:
                    env[key] = str(kwargs[key.lower()])

            # 代理透传（与 docker_backend/subprocess_backend 对齐）：
            # 容器内 sdkmanager/AGP 联网下载缺失 SDK 组件依赖代理出口，
            # 否则 dl.google.com 不可达导致自动下载失败
            http_proxy = self.config.http_proxy or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
            https_proxy = self.config.https_proxy or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
            no_proxy = self.config.no_proxy or os.environ.get("no_proxy") or os.environ.get("NO_PROXY")
            if http_proxy:
                env["http_proxy"] = http_proxy
                env["HTTP_PROXY"] = http_proxy
            if https_proxy:
                env["https_proxy"] = https_proxy
                env["HTTPS_PROXY"] = https_proxy
            if no_proxy:
                env["no_proxy"] = no_proxy
                env["NO_PROXY"] = no_proxy

            # 卷挂载（SDK 卷只读，避免并发写入损坏）
            volumes = {
                host_project_path: {"bind": "/workspace", "mode": "rw"},
                self.VOLUME_JDK: {"bind": "/opt/jdks", "mode": "rw"},
                self.VOLUME_SDK: {"bind": "/opt/android-sdk", "mode": "rw"},  # SERIAL_ALWAYS 保证无并发写，rw 允许 AGP 自动补全缺失 SDK
                gradle_volume: {"bind": "/home/builduser/.gradle", "mode": "rw"},
            }

            # 堆内存按任务类型动态调整（从 kwargs 透传，默认 8g）
            import re
            mem_limit = str(kwargs.get("mem_limit", "8g"))
            if not re.match(r'^\d+[bBkKmMgG]$', mem_limit):
                logger.warning(f"[AndroidBuild] 无效 mem_limit 格式: {mem_limit}，使用默认值 8g")
                mem_limit = "8g"
            # OOM 重试时自动扩容
            if kwargs.get("retry_after_oom"):
                mem_limit = "12g"
                logger.info(f"[AndroidBuild] OOM 重试: mem_limit -> {mem_limit}")

            # Git 凭据通过 tmpfs 文件注入（避免环境变量在 docker inspect 中泄漏）
            if git_username and git_token:
                git_credential_cmd = (
                    f'(umask 077 && echo "https://{shlex.quote(git_username)}:{shlex.quote(git_token)}@github.com"'
                    f' > /tmp/.git-credentials) && '
                    f'git config --global credential.helper "store --file /tmp/.git-credentials" && '
                )
            else:
                git_credential_cmd = ""

            try:
                # 确保构建镜像存在，缺失时自动拉取
                try:
                    self.client.images.get(self.DEFAULT_IMAGE)
                except errors.ImageNotFound:
                    logger.info(f"[AndroidBuild] 拉取镜像: {self.DEFAULT_IMAGE}")
                    try:
                        await asyncio.to_thread(
                            self.client.images.pull,
                            self.DEFAULT_IMAGE.split(":")[0],
                            tag=self.DEFAULT_IMAGE.split(":")[1] if ":" in self.DEFAULT_IMAGE else "latest",
                        )
                    except Exception as e:
                        logger.error(f"[AndroidBuild] 镜像拉取失败: {e}")
                        hint = (
                            f"该镜像通常是宿主机本地构建的（仓库内无同名可拉取镜像），"
                            f"请在宿主机重建: docker build -t {self.DEFAULT_IMAGE} "
                            f"-f backend/Dockerfile.android-builder backend/"
                        )
                        return ExecutionResult(
                            success=False, stdout="", stderr="",
                            exit_code=1, duration_ms=0,
                            error=f"构建镜像不可用: {self.DEFAULT_IMAGE}，拉取失败原因: {e}。{hint}",
                        )

                container = self.client.containers.run(
                    image=self.DEFAULT_IMAGE,
                    command=[
                        "bash", "-c",
                        # Git 凭据注入（tmpfs 文件，容器退出自动销毁）
                        f"{git_credential_cmd}"
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
                        "/workspace/build": "rw,exec,noatime,size=2g,uid=1000,gid=1000",
                        "/dev/shm": "rw,noexec,nosuid,size=1g",
                        "/tmp": "rw,noexec,nosuid,size=1g",
                        # uid/gid 必填: tmpfs 默认 root 所有, builduser (uid=1000) 写失败 →
                        # sdkmanager 写 $ANDROID_USER_HOME 报 Permission denied (P5 Fix 2 遗漏项)
                        "/home/builduser/.android": "rw,noexec,nosuid,size=128m,uid=1000,gid=1000",
                        # P5 Fix 2: tmpfs 覆盖冲突缓存子目录（优先级高于 volume）
                        # modules-2/ (依赖 JAR) 和 wrapper/dists/ (Gradle 发行版) 由全局卷持久化
                        "/home/builduser/.gradle/caches/build-cache-1": "rw,exec,noatime,size=1g,uid=1000,gid=1000",
                        "/home/builduser/.gradle/caches/journal-1": "rw,noexec,nosuid,size=128m,uid=1000,gid=1000",
                        "/home/builduser/.gradle/kotlin-daemon": "rw,noexec,nosuid,size=256m,uid=1000,gid=1000",
                    },
                    network_mode="bridge",
                    remove=False,
                    # auto_remove: Docker daemon 在容器退出后自动删除，即使 Python 进程崩溃也不残留
                    auto_remove=True,
                    security_opt=["no-new-privileges:true"],
                    user="builduser",
                    read_only=True,
                )

                logger.info(
                    f"[AndroidBuild] container_start id={container.id[:12]}"
                )

                # 检测 SDK 版本漂移（容器启动后立即检查）
                await self._check_sdk_version_drift(container)

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
                            except Exception:
                                logger.opt(exception=True).warning("[AndroidBuild] on_output 回调异常")

                    while True:
                        chunk = await loop.run_in_executor(None, output_queue.get)
                        if chunk is None:
                            await _flush_batch()
                            break
                        # stdout 缓冲上限保护 — on_output 始终透传，不受上限影响
                        if len(stdout_buf) < self._MAX_STDOUT_CAPTURE:
                            allowed = self._MAX_STDOUT_CAPTURE - len(stdout_buf)
                            stdout_buf.extend(chunk[:allowed])
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
                    duration_ms = int((time.time() - start_time) * 1000)
                    logger.warning(
                        f"[AndroidBuild] done timeout duration={duration_ms}ms limit={timeout}s"
                    )
                    # 保留部分输出 — bytes-first 截取优化（P5 Fix 6）
                    if stdout_buf:
                        tail = stdout_buf[-self._MAX_STDOUT_CAPTURE:]
                        partial = tail.decode("utf-8", errors="replace")[-50000:]
                    else:
                        partial = ""
                    return ExecutionResult(
                        success=False, stdout=partial, stderr="",
                        exit_code=124,
                        duration_ms=duration_ms,
                        error=f"编译超时（{timeout}s），任务: {gradle_task}",
                    )

                # 等待队列消费完毕
                await drain_task

                # 异步检查 Gradle 缓存大小（不阻塞返回）
                asyncio.ensure_future(self._enforce_gradle_cache_quota())

                # 先截取 bytes 再 decode，避免对大 buffer 做全量 UTF-8 解码
                _MAX_RESULT_BYTES = self._MAX_STDOUT_CAPTURE
                _MAX_RESULT_CHARS = 50000
                if len(stdout_buf) > _MAX_RESULT_BYTES:
                    tail = stdout_buf[-_MAX_RESULT_BYTES:]
                    stdout = tail.decode("utf-8", errors="replace")[-_MAX_RESULT_CHARS:]
                    stdout = "...(前段省略)..." + stdout
                else:
                    stdout = stdout_buf.decode("utf-8", errors="replace")

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
                android_build_metrics.record_build_end()
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
                except errors.NotFound:
                    pass  # auto_remove 已清理
                except errors.APIError as e:
                    if e.status_code not in (409,):  # 409: removal already in progress
                        logger.warning(f"[AndroidBuild] 容器清理失败: {e}")
                except Exception as e:
                    logger.warning(f"[AndroidBuild] 容器清理失败: {e}")

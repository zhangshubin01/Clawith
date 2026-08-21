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
            logger.debug(f"[AndroidBuild] detected host path: {host_path}")
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

    # ── Gradle 构建进度（方案 B + C）──
    # 进度侧信道文件名：init script 在构建容器内写 /workspace/.clawith-gradle-progress，
    # 后端经同一 bind-mount 存储（容器内 project_path）tail 该文件实时拿到任务边界。
    # 用 Clawith 命名空间前缀，避免与用户项目同名文件冲突（否则会被构建前截断 + 构建后删除）。
    _PROGRESS_FILE = ".clawith-gradle-progress"
    # 静默期心跳阈值（秒）：超过该时长既无 docker 日志流、也无进度侧信道输出，
    # 则发「构建进行中…」心跳，兜底配置阶段 / 单任务长静默段的「像卡死」观感。
    _HEARTBEAT_INTERVAL = 15.0
    _HEARTBEAT_POLL = 2.0
    # 任务边界进度 init script。用 beforeProject + configureEach + doFirst/doLast 注入，
    # 而非 Gradle.addListener(TaskExecutionListener)：后者与 configuration-cache 不兼容
    # （会令缓存条目「存储但永不复用」），而 doFirst/doLast 随任务图序列化、缓存命中仍触发。
    # 注意：进度走文件侧信道（绕过 daemon socket 转发缓冲），不写 stdout。
    _GRADLE_PROGRESS_INIT_SCRIPT = """\
def progressFile = new File('/workspace/.clawith-gradle-progress')

gradle.beforeProject { project ->
    project.tasks.configureEach { task ->
        task.doFirst {
            progressFile.append("TASK_START|${task.path}\\n")
        }
        task.doLast {
            progressFile.append("TASK_END|${task.path}\\n")
        }
    }
}
"""

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
        proc: asyncio.subprocess.Process | None = None
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
        except asyncio.TimeoutError:
            # communicate() was cancelled: kill and reap the docker CLI child
            # so it cannot linger as a zombie under uvicorn.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        except (ValueError, ProcessLookupError, FileNotFoundError):
            pass  # 解析失败/容器不存在/docker CLI 不可用 — 不影响构建

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

    async def _preheat_gradle_cache_ownership(self, container) -> None:
        """卷属主预热（幂等保险丝）。

        gradle_cache_global 卷在镜像历史版本间迁移/重建时可能残留 root 属主内容
        （旧镜像无 chown、或空卷首次挂载时 daemon 创建 root 目录），而构建容器以
        builduser (uid=1000) 运行且根文件系统只读，无法自行修复，会以
        Permission denied 失败。此处以 root exec 幂等 chown：属主正确时是 no-op，
        毫秒级完成；失败仅告警不阻塞（预热是保险丝，非构建前提）。
        """
        try:
            result = await asyncio.to_thread(
                container.exec_run,
                [
                    "sh", "-c",
                    "chown -R 1000:1000 /home/builduser/.gradle 2>/dev/null || true",
                ],
                user="root",
            )
            if result.exit_code != 0:
                logger.warning(
                    f"[AndroidBuild] gradle cache 属主预热失败: "
                    f"{(result.output or b'').decode(errors='replace')[:200]}"
                )
        except Exception as e:
            logger.warning(f"[AndroidBuild] gradle cache 属主预热异常: {e}")

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

            # 镜像仓库开关透传（entrypoint 默认开启国内镜像注入）：
            # 部署方可通过宿主环境 ANDROID_GRADLE_MIRRORS=off 关闭
            mirror_switch = os.environ.get("ANDROID_GRADLE_MIRRORS")
            if mirror_switch:
                env["ANDROID_GRADLE_MIRRORS"] = mirror_switch

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

                # 进度侧信道文件路径（容器内 project_path，即后端 /data/agents/<rel> 挂载，
                # 与构建容器 /workspace 同一 bind-mount 存储）。构建前截断：init script 用
                # append 写入，跨构建残留会污染本次进度。
                progress_path = os.path.join(str(project_path), self._PROGRESS_FILE)
                try:
                    with open(progress_path, "w", encoding="utf-8"):
                        pass
                except OSError:
                    logger.debug(f"[AndroidBuild] 进度侧信道不可写，跳过: {progress_path}")

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
                        # 注入任务边界进度 init script（方案 B）：写到 /tmp（tmpfs），
                        # 经 -I 显式加载，避免污染共享 gradle 卷的 init.d。
                        f"&& cat > /tmp/gradle-progress.gradle << 'GRADLE_PROGRESS_EOF'\n"
                        f"{self._GRADLE_PROGRESS_INIT_SCRIPT}\n"
                        f"GRADLE_PROGRESS_EOF\n"
                        # heredoc 体结束后，下一行不能以 `&&` 开头——那是 bash 语法错误
                        # （`syntax error near unexpected token '&&'`，bash -c 解析期即 exit 2，
                        # 导致 gradle 从未被执行）。gradle 作为独立语句执行即可。
                        # 2>&1 必须保留：entrypoint 以 `"$@" &` 后台执行此命令，此环境下
                        # 容器 stderr 管道（OrbStack）对后台 job 的写入会整体丢失——实测
                        # docker CLI logs / python SDK / attach 三通道都收不到 stderr，
                        # 而 Gradle 的 Kotlin `e:` 错误与 FAILURE 段全部走 stderr，
                        # 不合并会得到「compileDebugKotlin FAILED 但零错误」的盲修循环。
                        # 详见 docs/technical-plans/20260821-android-stderr-loss-analysis.md
                        f"./gradlew --no-daemon --console=plain -I /tmp/gradle-progress.gradle {shlex.quote(str(gradle_task))} 2>&1 ",
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

                # 卷属主预热：卷内容可能残留 root 属主（历史镜像/重建卷），
                # builduser 只读根文件系统无法自行修复 → root exec 幂等 chown
                await self._preheat_gradle_cache_ownership(container)

                # 检测 SDK 版本漂移（容器启动后立即检查）
                await self._check_sdk_version_drift(container)

                on_output = kwargs.get("on_output")
                stdout_buf = bytearray()
                # threading.Queue 天然线程安全，用于流式线程→主协程通信
                output_queue: thread_queue.Queue[bytes | None] = thread_queue.Queue()
                # 共享活动时间戳：drain 流与进度侧信道都会更新 last_output，
                # 心跳协程据此在静默期发「构建进行中…」（方案 C）。
                activity = {"last_output": time.time(), "last_heartbeat": time.time()}

                async def _safe_on_output(text: str, tag: str) -> None:
                    """统一 on_output 回调：未注入回调时跳过，回调异常仅告警不中断。"""
                    if not on_output:
                        return
                    try:
                        await on_output(text)
                    except Exception:
                        logger.opt(exception=True).warning(f"[AndroidBuild] on_output {tag}回调异常")

                def _stream_logs():
                    """在后台线程中流式读取容器日志，写入线程安全队列。"""
                    try:
                        for chunk in container.logs(stream=True, follow=True, stdout=True, stderr=True):
                            output_queue.put(chunk)
                    finally:
                        output_queue.put(None)  # 停止信号

                stream_task = asyncio.ensure_future(asyncio.to_thread(_stream_logs))

                async def _drain_queue():
                    """主协程消费队列：按完整行切分 + 过滤冗余任务行，独立定时器冲刷。

                    两个根治点（android_compile 输出「任务名重复 + 换行丢失」）：
                    1. 行对齐：docker logs 的 chunk 边界与 100ms 定时器可能在行中间切分，
                       直接 join 会让 stdout 块末尾缺换行符，与进度侧信道的 on_output 在
                       下游 "".join 拼接时粘连。这里维护半行缓冲，只在换行边界切分，
                       每次只转发完整行（含结尾换行符）。
                    2. 去重：Gradle --console=plain 的 "> Task :x" 行（无附加标记的纯成功
                       执行）与进度侧信道的 ▶/✓ 重复，过滤掉；保留带 SKIPPED/NO-SOURCE/
                       FAILED/UP-TO-DATE/FROM-CACHE 标记的行——这些任务的 doFirst/doLast
                       不触发，▶/✓ 里没有，是唯一信息来源。

                    定时器仍每 100ms 冲刷一次：Gradle stdout 经 JVM 块缓冲，构建安静期无
                    新帧，旧实现只在收到新 chunk 时检查窗口会让尾批滞留到构建结束。
                    """
                    loop = asyncio.get_running_loop()
                    pending_line = bytearray()   # 半行缓冲（跨 chunk 的未完整行）
                    line_batch: list[str] = []   # 待冲刷的完整行
                    BATCH_INTERVAL = 0.1  # 100ms 批处理窗口
                    # 附加标记（前面带空格，避免误匹配任务名子串）
                    _TASK_MARKS = (" SKIPPED", " NO-SOURCE", " FAILED", " UP-TO-DATE", " FROM-CACHE")

                    def _keep_line(line: str) -> bool:
                        """纯成功任务行已被 ▶/✓ 覆盖，过滤；其余（含标记行）保留。"""
                        s = line.strip()
                        if not s.startswith("> Task "):
                            return True
                        return any(m in s for m in _TASK_MARKS)

                    async def _flush_lines():
                        if not line_batch:
                            return
                        merged = "".join(line_batch)  # 每行都以换行符结尾，下游 join 不粘连
                        line_batch.clear()
                        if on_output:
                            activity["last_output"] = time.time()
                            await _safe_on_output(merged, "")

                    async def _flush_timer():
                        while True:
                            await asyncio.sleep(BATCH_INTERVAL)
                            await _flush_lines()

                    flush_timer = asyncio.create_task(_flush_timer())
                    normal_end = False
                    try:
                        while True:
                            chunk = await loop.run_in_executor(None, output_queue.get)
                            if chunk is None:
                                normal_end = True
                                break
                            # stdout 缓冲上限保护 — 结果 stdout 保留完整内容（不过滤），
                            # 供工具最终返回；on_output 实时流才做行对齐 + 去重。
                            if len(stdout_buf) < self._MAX_STDOUT_CAPTURE:
                                allowed = self._MAX_STDOUT_CAPTURE - len(stdout_buf)
                                stdout_buf.extend(chunk[:allowed])
                            if on_output:
                                pending_line.extend(chunk)
                                while True:
                                    nl = pending_line.find(b"\n")
                                    if nl < 0:
                                        break
                                    line = bytes(pending_line[: nl + 1]).decode("utf-8", errors="replace")
                                    del pending_line[: nl + 1]
                                    if _keep_line(line):
                                        line_batch.append(line)
                    finally:
                        flush_timer.cancel()
                        try:
                            await flush_timer
                        except asyncio.CancelledError:
                            pass
                        # 取消/超时路径不冲刷残留批次：尾部内容已随结果
                        # stdout 返回，且取消后回调 on_output 会与调用方的
                        # 最终 flush 并发（重复/交错事件）。
                        if normal_end:
                            # 冲刷最后的半行（无换行符结尾的尾部）——补换行符保证下游 join 不粘连
                            if pending_line:
                                tail = pending_line.decode("utf-8", errors="replace")
                                if _keep_line(tail):
                                    line_batch.append(tail if tail.endswith("\n") else tail + "\n")
                            await _flush_lines()

                drain_task = asyncio.create_task(_drain_queue())

                # ── 方案 B：进度侧信道 tail ──
                # init script 把任务边界写进 /workspace/.gradle-progress（绕过 daemon
                # socket 转发缓冲），后端轮询同一文件把新行实时转发给 on_output。
                async def _tail_progress():
                    offset = 0
                    pending = ""  # 未完整行缓冲（跨轮询的撕裂行）
                    try:
                        while True:
                            await asyncio.sleep(0.2)
                            try:
                                if not os.path.exists(progress_path):
                                    continue
                                with open(progress_path, "r", encoding="utf-8", errors="replace") as f:
                                    f.seek(offset)
                                    data = f.read()
                                    offset = f.tell()
                            except OSError:
                                continue
                            if not data:
                                continue
                            # 按行切分：末段可能是半行（Groovy append 与轮询竞态），留下次拼接
                            pending += data
                            lines = pending.split("\n")
                            pending = lines.pop()
                            rendered = []
                            for line in lines:
                                line = line.strip()
                                if line.startswith("TASK_START|"):
                                    rendered.append("▶ 正在执行 " + line[len("TASK_START|"):])
                                elif line.startswith("TASK_END|"):
                                    rendered.append("✓ 完成 " + line[len("TASK_END|"):])
                            if rendered:
                                activity["last_output"] = time.time()
                                await _safe_on_output("\n".join(rendered) + "\n", "进度")
                    except asyncio.CancelledError:
                        pass

                # ── 方案 C：静默期心跳 ──
                async def _heartbeat():
                    try:
                        while True:
                            await asyncio.sleep(self._HEARTBEAT_POLL)
                            now = time.time()
                            silent = now - activity["last_output"]
                            since_hb = now - activity["last_heartbeat"]
                            if silent >= self._HEARTBEAT_INTERVAL and since_hb >= self._HEARTBEAT_INTERVAL and on_output:
                                activity["last_heartbeat"] = now
                                await _safe_on_output(f"构建进行中… 已 {int(silent)}s，暂无新输出\n", "心跳")
                    except asyncio.CancelledError:
                        pass

                progress_task = asyncio.create_task(_tail_progress())
                heartbeat_task = asyncio.create_task(_heartbeat())

                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(container.wait),
                        timeout=timeout + 10,
                    )
                except asyncio.TimeoutError:
                    drain_task.cancel()
                    stream_task.cancel()
                    progress_task.cancel()
                    heartbeat_task.cancel()
                    # 回收被取消的任务：drain 的 finally 在取消路径跳过
                    # 残留批次冲刷（见 _drain_queue），不再有晚到的
                    # on_output 回调；生产者的后台线程随容器 kill 结束。
                    await asyncio.gather(
                        drain_task,
                        stream_task,
                        progress_task,
                        heartbeat_task,
                        return_exceptions=True,
                    )
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

                # 构建正常结束：停掉进度 tail 与心跳（二者为无限轮询循环）。
                # 停掉前 sleep 250ms 让 tail 再跑一轮轮询（200ms），追上进度文件尾部行。
                await asyncio.sleep(0.25)
                progress_task.cancel()
                heartbeat_task.cancel()
                await asyncio.gather(progress_task, heartbeat_task, return_exceptions=True)

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
                # 停掉进度 tail 与心跳（成功/异常路径可能在 return 前未停）
                for _name in ('progress_task', 'heartbeat_task'):
                    _t = locals().get(_name)
                    if _t is not None:
                        try:
                            _t.cancel()
                        except Exception as e:
                            logger.warning(f"[AndroidBuild] {_name} 取消失败: {e}")
                # 清理进度侧信道文件，避免污染用户工作区（dotfile，尽力而为）
                try:
                    if 'progress_path' in locals() and os.path.exists(progress_path):
                        os.remove(progress_path)
                except OSError as e:
                    logger.debug(f"[AndroidBuild] 进度侧信道清理失败: {e}")
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

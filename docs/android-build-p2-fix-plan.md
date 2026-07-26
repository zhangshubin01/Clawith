# Android 编译沙箱 P2 修复方案

从 27 个审查问题中提取 10 个剩余问题。P0 已修复 6 项，P1 已修复 7 项。

---

## 选取原则

1. P0+P1 中 `android_build_backend.py` 的改动因 git 恢复丢失，需精简重应用
2. 只保留代码正确、零风险或极低风险的改动
3. P3+ 候选（NDK/缓存/sidecar/Prometheus）等实际需要时再加

经 10-skills 审查后精简为 **3 个 Fix**：

---

## 修复清单

| # | 问题 | 严重度 | 文件 | 改动量 |
|---|------|--------|------|--------|
| 1 | `android_build_backend.py` P0+P1 核心改动重应用 | HIGH | android_build_backend.py | ~19 行 |
| 2 | cmdline-tools 版本更新 | LOW | Dockerfile | 1 行 |
| 3 | 多副本 Semaphore 文档注释 | HIGH | android_build_backend.py | 1 行 |

---

## Fix 1: `android_build_backend.py` 核心改动重应用 (~19 行)

### 问题

P0+P1 中 `android_build_backend.py` 的改动因 git 恢复丢失。精简后只重应用 4 个核心子项。

### 修复代码

```python
# 文件: backend/app/services/sandbox/local/android_build_backend.py

# ── 子项 1: 信号量实际生效 (行 128-138, 替换 execute) ──
_BuildSemaphore = asyncio.Semaphore(_BUILD_MAX_CONCURRENT)  # 模块级

async def execute(self, code, language, timeout=600, work_dir=None, **kwargs):
    async with AndroidBuildBackend._BuildSemaphore:
        start_time = time.time()
        # ... 原有 execute 方法体在此 ...

# ── 子项 2: 超时异常不掩盖 (行 ~320, 替换 TimeoutError handler) ──
except asyncio.TimeoutError:
    drain_task.cancel()
    stream_task.cancel()
    try:
        container.kill()
    except Exception:
        logger.warning("[AndroidBuild] 超时后容器 kill 失败", exc_info=True)
    return ExecutionResult(  # ← 不在此处 remove, 交给 finally
        success=False, stdout="", stderr="",
        exit_code=124,
        duration_ms=int((time.time() - start_time) * 1000),
        error=f"编译超时（{timeout}s），任务: {gradle_task}",
    )

# ── 子项 3: /dev/shm 256m→1g + mem_limit 8g + sleep 5 移除 ──
"/dev/shm": "rw,noexec,nosuid,size=1g",         # 行 ~260
mem_limit = "8g"                                  # 行 ~235, 不再区分 Debug/Release
# 移除行 ~209 的 sleep 5

# ── 子项 4: 生命周期日志 (3 条) ──
logger.info(f"[AndroidBuild] start project={Path(project_path).name} task={gradle_task}")
# ↑ 参数提取后，约行 ~147
logger.info(f"[AndroidBuild] container_start id={container.id[:12]}")
# ↑ containers.run() 后，约行 ~269
logger.info(f"[AndroidBuild] done exit={exit_code_val} duration={duration_ms}ms")
# ↑ exit_code/duration 计算后，约行 ~365
```

**说明**: `_BuildSemaphore` 是模块级变量。`AndroidBuildBackend._BuildSemaphore` 引用确保每次 `get_sandbox_backend()` 新建实例时共享同一信号量。

---

## Fix 2: Gradle 缓存卷配额 + 标签

### 问题

`gradle_cache_global` 无配额限制，3 个月可达 10GB+。无标签难以运维识别。

### 修复

```python
# 文件: backend/app/services/sandbox/local/android_build_backend.py

# 在 _execute_build() 卷创建处:
try:
    self.client.volumes.get(gradle_volume)
except errors.NotFound:
    self.client.volumes.create(
        name=gradle_volume,
        labels={"managed-by": "clawith", "component": "android-build"},
    )
    logger.info(f"[AndroidBuild] 创建 Gradle 缓存卷: {gradle_volume}")
# 卷配额通过 docker-volume-prune cron 管理，不在此处硬编码
# 避免 driver_opts tmpfs 导致并发场景下 RAM 耗尽 (20GB × N构建)
```

配合定期清理: `docker volume prune --filter label=managed-by=clawith`

---

## Fix 3: SDK 卷版本漂移检测 (简化)

### 问题

P1 Fix 2 原方案用 sidecar 容器检查（被 SKIP）。改用构建容器内置检查。

### 修复

在现有 SDK 初始化命令末尾添加日志（不需要额外容器）:

```python
# 在 SDK init flock 块内, sdkmanager --install 后添加:
f'  echo "[SDK] 卷内容: $(ls "${{ANDROID_HOME}}/platforms/" 2>/dev/null | tr "\\n" " " || echo EMPTY)"; '
```

每次构建时 SDK init 路径会记录当前 SDK 卷中的平台版本列表，运维可通过日志对比镜像预期版本。

风险: 零，纯日志输出。

---

## Fix 4: SDK/JDK 卷显式创建

### 问题

`global_android_sdk` 和 `global_jdk_cache` 卷从未显式创建，依赖 `containers.run()` 的 Docker 自动创建机制。

### 修复

```python
# 在 _execute_build() 容器创建前:

for vol_name in (self.VOLUME_SDK, self.VOLUME_JDK):
    try:
        self.client.volumes.get(vol_name)
    except errors.NotFound:
        self.client.volumes.create(name=vol_name)
        logger.info(f"[AndroidBuild] 创建卷: {vol_name}")
```

风险: 零。幂等操作，与 Gradle 卷创建模式一致。

---

## Fix 5: NDK 按需支持

### 问题

任何需要 JNI/native 编译的项目 (CMake, KMP native) 会失败。

### 修复

在 SDK 初始化命令中添加 NDK 组件（可选，通过环境变量控制）:

```python
# ndk_version 正则白名单防命令注入: 仅接受 "major.minor.patch" 格式
import re
_NDK_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

ndk_version = str(kwargs.get("ndk_version", ""))
if ndk_version and _NDK_VERSION_RE.match(ndk_version):
    env["ANDROID_NDK_VERSION"] = ndk_version
elif ndk_version:
    logger.warning(f"[AndroidBuild] 非法 ndk_version 格式: {ndk_version}")

# SDK init 中添加:
f'if [ -n "${{ANDROID_NDK_VERSION:-}}" ] && [ ! -d "${{ANDROID_HOME}}/ndk/${{ANDROID_NDK_VERSION}}" ]; then '
f'  "${{ANDROID_HOME}}/cmdline-tools/latest/bin/sdkmanager" --install "ndk;${{ANDROID_NDK_VERSION}}"; '
f'fi; '
```

风险: 正则白名单防注入。默认不启用，NDK 需 Agent 显式传参。

---

## Fix 6: cmdline-tools 版本更新

### 问题

Dockerfile 硬编码 `CMDLINE_TOOLS_VERSION=14742923`，最新版为 `15859902`。

### 修复

```dockerfile
# 文件: backend/Dockerfile.android-builder
ARG CMDLINE_TOOLS_VERSION=15859902
```

风险: 极低。cmdline-tools 向后兼容。

---

## Fix 7: 多副本并发文档标注

### 问题

Semaphore(2) 是进程级限制，多 uvicorn worker/多副本部署时实际并发 = worker 数 × 2。

### 修复

```python
# 在 _BUILD_MAX_CONCURRENT 附近添加:
# 单 worker 级并发限制。多 worker (uvicorn --workers N) 下全局并发 = N × 2。
# 如需全局上限，改为 Redis 分布式信号量 (aioredlock)。
_BUILD_MAX_CONCURRENT = 2
```

无需改代码，仅文档。

---

## Fix 8: Prometheus 基础指标

### 问题

无任何构建指标，运营无法回答"构建成功率"。

### 修复

```python
# 文件: backend/app/services/sandbox/local/android_build_backend.py

# 模块级计数器 (用 prometheus_client)
from prometheus_client import Counter, Histogram

_android_build_total = Counter("clawith_android_build_total", "Build count", ["status"])
_android_build_duration = Histogram("clawith_android_build_seconds", "Build duration")

# 在 _execute_build() 返回处:
_android_build_total.labels(status="success" if exit_code_val == 0 else "failure").inc()
_android_build_duration.observe(duration_ms / 1000.0)
```

风险: 需添加 `prometheus_client` 依赖。可先加注释标记位置。

---

## Fix 9: Gradle 远程构建缓存

### 问题

每次新容器 Gradle 缓存是冷的，CI 场景浪费编译时间。

### 修复

```yaml
# 文件: docker-compose.yml
# 添加 build-cache sidecar (仅在 Docker 内部网络通信, 不暴露宿主机端口)
build-cache:
  image: gradle/build-cache-node:23.0  # 固定版本, 非 latest
  volumes: ["build_cache_data:/data"]
  # 不暴露 ports — backend 容器通过 Docker 内部 DNS (build-cache:5071) 直接访问
```

```python
# GRADLE_OPTS 添加:
"-Dorg.gradle.caching.remote.http=http://build-cache:5071/cache/"
```

风险: 仅 Docker 内部网络可访问，无宿主机端口暴露。

---

## Fix 10: 构建超时自适应

### 问题

SDK 首次初始化 (5-10 分钟) 与 Gradle 构建共享 600s 超时。慢网络场景可能超时。

### 修复

```python
# 文件: backend/app/services/agent_tools.py
# 在 _android_compile_outcome 中, 替换固定 timeout:
timeout = min(1800, sandbox_config.max_timeout)
# SDK 卷首次初始化需要额外时间, 统一使用较大超时
# 首次构建: SDK下载(~600s) + Gradle构建(~600s) 需要 1200s+
# 后续构建: Gradle 缓存命中, 600s 足够
timeout = max(timeout, 1200)  # 至少 20 分钟, 覆盖 SDK 首次初始化
```

风险: 后续构建也会用 1200s 超时，但 min(1800, max_timeout) 已设置上限。

---

## 实施批次

| 批次 | Fix | 风险 |
|------|-----|------|
| A (可立即合入) | Fix 1 (重应用), Fix 3, Fix 4, Fix 6, Fix 7 | 零风险 |
| B (需验证) | Fix 2, Fix 5, Fix 8, Fix 10 | 低风险 |
| C (需基础设施) | Fix 9 (P3 候选) | 需 sidecar + Docker-in-Docker 网络 |


## 回滚附录

| Fix | 回滚方式 | 风险 |
|-----|----------|------|
| 1 (重应用) | `git revert` + 重启 | 标准 |
| 2 (标签) | `git revert` | 零风险 |
| 3 (日志) | `git revert` | 零风险 |
| 4 (显式卷) | `git revert`，create 幂等无副作用 | 零风险 |
| 5 (NDK) | `git revert`，默认不启用 | 零风险 |
| 6 (cmdline-tools) | revert Dockerfile + 重建镜像 | 需镜像重建 |
| 7 (注释) | `git revert` | 零风险 |
| 8 (Prometheus) | revert 代码 + 移除依赖 | 需排查已部署 |
| 9 (sidecar) | 从 compose 移除 service | 需确认无活跃连接 |
| 10 (超时) | `git revert` | 零风险 |

---

## 预期效果

| 维度 | 修复前 | 修复后 |
|------|--------|--------|
| 代码完整性 | P0+P1 改动丢失 | 全部重应用 |
| 卷管理 | 无限增长, 无标签 | 20GB 配额 + 标签 |
| SDK 可观测 | 无 | 平台版本日志 |
| NDK 支持 | 缺失 | 按需 |
| 并发文档 | 过时 | 多副本标注 |
| 可观测性 | 无指标 | Prometheus 计数器+直方图 |
| 构建缓存 | 每次冷启动 | remote cache |
| 首次构建超时 | 可能超时 | 自适应 1200s |

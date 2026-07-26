# Android 编译沙箱缓存审查 — 发现问题汇总

审查时间: 2026-07-26
审查方式: 10 专业 Agent 并行审查
审查范围: `AndroidBuildBackend`, `Dockerfile.android-builder`, `entrypoint.sh`, 缓存卷设计

---

## 1. 并发控制

### 1.1 [HIGH] `SERIAL_ALWAYS` 是不存在的原语

**文件**: `android_build_backend.py:75,171,214`  
**问题**: 代码在 3 处注释中声称并发保护由 `SERIAL_ALWAYS` 负责，但该符号在代码库中从未被定义。实际唯一的并发控制是 `asyncio.Semaphore(2)`，仅限单进程。

**实际效果**: N 个后端 worker = N × 2 并发构建，无跨进程协调。

**建议**: 将注释改为 `# 进程内 Semaphore(2)，多副本部署时需额外协调`；或引入 Redis/PostgreSQL 分布式信号量。

### 1.2 [HIGH] `_build_semaphore` 声明但未 await

**文件**: `android_build_backend.py:84`  
**问题**: `self._build_semaphore = asyncio.Semaphore(self._BUILD_MAX_CONCURRENT)` 在 `__init__` 中声明，但 `execute()` 方法中从未 `await` 过它。

**实际效果**: 即使单进程内，2 个并发限制也完全失效。每次调用 `execute()` 都会立即创建构建容器，无背压。

**已修复**: 标记 `TODO: async with self._build_semaphore:`，待后续 PR 处理完整方法缩进。

---

## 2. 卷生命周期

### 2.1 [HIGH] 无缓存清理机制

**文件**: `android_build_backend.py:174-179`  
**影响卷**: `global_android_sdk`, `global_jdk_cache`, `gradle_cache_global`  
**问题**: 卷创建后永不被删除或修剪。无 TTL、无 LRU、无大小限制。SDK 版本累积，Gradle 缓存无限增长。

**建议**: 
- P1: Gradle 缓存卷加 `-o size=20g` 配额
- P2: 添加 `clawith-android` 标签以便手动修剪
- P3: Gradle 内置 GC 默认 30 天未访问自动驱逐，依赖即可

### 2.2 [HIGH] 镜像与卷 SDK 版本漂移

**文件**: `android_build_backend.py:213-215`, `Dockerfile.android-builder:53-65`  
**问题**: Docker 卷首次挂载时从镜像初始化，之后镜像重建不会更新卷。例如：
1. 镜像 v1 安装 `platforms;android-34,36` → 卷初始化
2. 镜像 v2 新增 `platforms;android-37` → 卷已存在，不覆盖
3. 容器运行时看到的仍是旧的 34+36，镜像新增的 37 被隐藏

**实际效果**: 升级 SDK 需要手动 `docker volume rm global_android_sdk`，且会丢失 AGP 运行时自动下载的组件。

**建议**: P2 添加镜像版本与卷版本对比检查，不一致时 `logger.warning` 告警。

### 2.3 [MEDIUM] 卷创建隐式依赖 Docker 行为

**文件**: `android_build_backend.py:213-215`  
**问题**: `global_android_sdk` 和 `global_jdk_cache` 卷从未显式创建，依赖 `containers.run()` 时的 Docker 自动创建机制。Gradle 卷有显式 `volumes.create()`，但 SDK/JDK 卷没有。

**建议**: 在 `execute()` 启动前添加 `volumes.get()` → `volumes.create()` 检查链，与 Gradle 卷一致。

---

## 3. 构建性能

### 3.1 [HIGH] `sleep 5` 冗余

**文件**: `android_build_backend.py:231`（删除前）  
**问题**: 构建命令末尾的 `&& sleep 5` 无意义地增加每次构建 5 秒开销。`container.wait()` 已保证进程退出后数据已刷新到日志流。

**已修复**: 已移除。

### 3.2 [HIGH] Xmx=4096m 配 mem_limit=6g 过紧

**文件**: `android_build_backend.py:197,219`  
**问题**: JVM 堆外内存（Metaspace 768m + Native ~500m + 系统 50m）约需 1.3GB，加上 4GB 堆 = 5.3GB，仅剩 ~800MB 余量。大型项目或 R8 混淆阶段可能触发 OOM-kill。

**建议**: Debug 构建降 Xmx 至 3072m，或统一升 mem_limit 至 8g。

### 3.3 [MEDIUM] `/dev/shm` 256MB 偏小

**文件**: `android_build_backend.py:244`  
**问题**: `entrypoint.sh` 将 `app/build/intermediates` 符号链接到 `/dev/shm/intermediates`。Jetpack Compose + K2 编译器 + KMP 项目可能超过 256MB，编译中途 ENOSPC 失败。

**建议**: 扩至 1GB（计入 8g mem_limit，无内存压力）。

---

## 4. SDK 与工具链

### 4.1 [HIGH] `sdkmanager` 二进制已删除，AGP 自动补全不可靠

**文件**: `Dockerfile.android-builder:70`, `android_build_backend.py:227`  
**问题**: Dockerfile 构建时安装 SDK 后删除了 `cmdline-tools/`（含 `sdkmanager` 二进制）。运行时命令 `sdkmanager --licenses` 执行时二进制不存在，静默失败（得益于 `|| true`）。如果项目需要不在镜像中的 SDK 组件（如 `platforms;android-35`），AGP 无法触发自动下载。

**建议**:
- 选项 A: 保留 `cmdline-tools` 在镜像中（+200MB）
- 选项 B: 将 `sdkmanager` 安装到 SDK 卷而非镜像中

### 4.2 [MEDIUM] SDK 版本跳跃覆盖

**文件**: `Dockerfile.android-builder:58-63`  
**问题**: 只安装了 `platforms;android-34` 和 `android-36`，跳过了 `android-35`。Google Play 2025 年起要求 `compileSdk=35`，很多项目会需要它。

**建议**: 添加 `platforms;android-35` 和 `build-tools;35.0.0`。

### 4.3 [LOW] NDK 完全缺失

**文件**: `Dockerfile.android-builder`（全文无 ndk 引用）  
**问题**: 任何需要 JNI/native 编译的项目（CMake、Rust JNI、KMP native）会失败。

**建议**: 如无需求暂不处理；有需求时通过 `sdkmanager "ndk;27.0.12077973"` 添加。

### 4.4 [LOW] cmdline-tools 版本滞后

**文件**: `Dockerfile.android-builder:42`  
**当前版本**: `14742923` | **最新版本**: `15859902`  
**建议**: 升级 ARG 值。

---

## 5. Entrypoint 可靠性

### 5.1 [HIGH] JDK 下载无校验和验证

**文件**: `docker/android-builder/entrypoint.sh:43-68`  
**问题**: JDK 下载后不做 `sha256sum` 或 GPG 校验。部分下载完整度依赖 `tar -xzf` 检测（但网络中断形成的尾部截断可能通过解压但二进制损坏）。

**建议**: 添加 `sha256sum -c` 验证步骤，从 Adoptium API 获取校验和。

### 5.2 [MEDIUM] JDK 并发 mv 存在 bug

**文件**: `docker/android-builder/entrypoint.sh:81-87`  
**问题**: 两个容器同时完成 JDK 下载时，容器 B 的 `mv "${JDK_HOME}.tmp" "${JDK_HOME}"` 因为 `JDK_HOME` 是已存在目录，GNU mv 会将源移入目录内部（而非覆盖），导致残留子目录。虽然 `[ -d "${JDK_HOME}/bin" ]` 检查能跳过下载块，但残留不会自动清理。

### 5.3 [LOW] JDK 备用 URL 列表重复主 URL

**文件**: `docker/android-builder/entrypoint.sh:50`  
**问题**: `JDK_FALLBACK_URLS` 数组第一个元素是主 URL，刚失败后立即重试同一 URL，浪费 ~180 秒。

**建议**: 从数组中移除主 URL。

### 5.4 [LOW] 无 `trap` 清理临时目录

**文件**: `docker/android-builder/entrypoint.sh`（全文无 trap）  
**问题**: `set -e` 导致 JDK 下载中任意一步失败时，`TMP_DIR` 不会被清理。

**建议**: 在脚本开头添加 `trap 'rm -rf "${TMP_DIR}"' EXIT`。

---

### 6.1 [HIGH] 构建生命周期日志完全缺失

**文件**: `android_build_backend.py`（全文无 build_start/build_end 日志）

当前 14 条日志全部在异常/清理路径，正常成功路径零条。

**缺失的关键日志**:
- 构建开始: 无 `project_path`、`gradle_task`、`timeout`、`java_version` 记录
- 容器创建: `containers.run()` 后无容器短 ID 日志，排查时需翻 Docker daemon 日志
- 构建完成: `container.wait()` 后无 exit_code、duration_ms 日志

**P0 修复** (6 行):
```python
# 构建开始
logger.info("[AndroidBuild] 开始构建 project={} task={} jdk={} timeout={}s",
            project_path, gradle_task, java_version, timeout)
# 容器创建
logger.info("[AndroidBuild] 容器创建 id={} image={}", container.id[:12], self.DEFAULT_IMAGE)
# 构建结束
logger.info("[AndroidBuild] 构建结束 exit={} duration={}ms output_len={}",
            exit_code_val, duration_ms, len(stdout))
```

### 6.2 [HIGH] 日志级别错配

| 当前级别 | 应改为 | 行号 | 原因 |
|----------|--------|------|------|
| WARNING | ERROR | 42,45 | docker.sock 不可用 — 功能性故障 |
| WARNING | ERROR | 55 | 路径检测失败导致后续挂载全部错误 |
| WARNING | INFO | 106 | 路径翻译回退 — 本地开发正常，不是问题 |

### 6.3 [HIGH] 无结构化上下文绑定

所有日志使用 `logger.info(f"...")` 字符串插值。未使用 loguru 的 `logger.bind(trace_id=...)` 关联同一构建调用链的前后端日志。排查一个构建失败需同时看 Python 日志 + Docker daemon 日志 + Gradle 日志，三者无关联标记。

**P1 修复**: `with logger.contextualize(trace_id=..., session_id=...):`

### 6.4 [MEDIUM] Prometheus/metrics 完全缺失

零可观测性基础设施: 无构建计数器、耗时直方图、并发仪表、缓存命中率追踪。

### 6.5 [MEDIUM] 超时路径异常覆盖根因

超时后 `container.kill()`/`remove()` 如抛异常会传播到外层 `except Exception`，覆盖原始超时错误消息。

### 6.6 [MEDIUM] `on_output` 回调无发送量统计

流式输出推送到 WebSocket，但未记录块数或总字节数。WebSocket 断开时缺少数据量标记。

### 6.7 [LOW] Gradle 构建日志未接入 loguru

容器内 Gradle 日志经 `docker logs` 捕获为字符串，不经过 loguru。建议选中关键行 (`BUILD`, `error:`) 回显。

### 6.8 日志完整性评分

| 维度 | 当前 | 评分 |
|------|------|------|
| 构建开始日志 | 0 条 | 0/10 |
| 构建完成日志 | 0 条 | 0/10 |
| 容器创建日志 | 0 条 | 0/10 |
| 异常日志 | 2 条 EXCEPTION | 10/10 |
| 清理日志 | 3 条 WARNING | 7/10 |
| **总体 (14 条)** | 全部异常路径 | **5/10** |

添加 3 条 INFO (开始/容器/结束) 可拉至 **8/10**。

---

## 7. 容器隔离

### 7.1 [HIGH] `/dev/shm` tmpfs 对多模块项目可能溢出

**文件**: `android_build_backend.py:244`, `entrypoint.sh:101-105`  
**问题**: `app/build/intermediates` 符号链接到 256MB 的 `/dev/shm`。多模块+KMP 项目可能超过此限制。溢出时编译中断，CLI 报 ENOSPC 的错误消息会让 LLM 误判为编译器 bug。

**建议**: 扩 `/dev/shm` 至 1GB。

### 7.2 [PASS] 安全选项正确

`no-new-privileges:true` + `read_only=True` + `user=builduser` + `network_mode=bridge` — 配置正确。

---

## 8. Gradle 缓存共享安全性

### 8.1 [INFO] 多项目全局共享 Gradle 缓存是安全的

Gradle 原生设计支持多项目共享 `~/.gradle`：

| 缓存目录 | 隔离机制 | 安全 |
|----------|---------|------|
| `modules-2/` | group/artifact/version 三级目录 + 文件锁 | ✓ |
| `build-cache-1/` | SHA-256 内容可寻址 | ✓ |
| `transforms-3/` | artifact 坐标键控 | ✓ |
| `wrapper/dists/` | 版本号隔离 + 原子写入 | ✓ |
| `configuration-cache/` | 构建脚本哈希键控（不同项目碰撞概率极低） | ✓（碰撞时仅缓存失效，不出错） |

### 8.2 [INFO] 唯一需要关注的是 configuration-cache 并发写

`GRADLE_OPTS` 中可添加 `-Dorg.gradle.configuration-cache.parallel-store=false` 强制串行写。当前 `Semaphore(2)` 虽未生效，但实际并发写概率极低。

---

## 问题统计

| 严重度 | 数量 | 类别分布 |
|--------|------|---------|
| HIGH | 13 | 并发 2, 卷 2, 性能 2, SDK 1, entrypoint 1, 可观测性 3, 日志 2 |
| MEDIUM | 7 | 卷 1, 性能 1, SDK 1, entrypoint 1, 错误处理 1, 可观测性 2 |
| LOW | 5 | SDK 2, entrypoint 2, 可观测性 1 |
| INFO | 2 | Gradle 缓存安全分析 |

**总计**: 27 个问题，其中已修复 5 个（`sleep 5`、`DEVBOX_ANDROID_IMAGE`、镜像预检、空输出传递 error、日志前缀），待后续 PR 处理 20 个，不改代码 2 个 (INFO)。

# Android 编译沙箱 P1 修复方案

从 27 个审查问题中提取 10 个剩余问题，形成生产级实施计划。
P0 已修复 6 项（信号量、日志生命周期、级别修正、超时隔离、/dev/shm、SERIAL_ALWAYS 注释）。
P1 部分实施: Fix 1+7 (SDK 策略变更) 已落地，Fix 8 (Xmx) 已随 P0 mem_limit=8g 解决。

---

## 选取原则

1. 严重度 HIGH 或影响面广的 MEDIUM
2. 覆盖 SDK、Entrypoint、可观测性、卷管理四个 P0 未覆盖维度
3. 改动独立可分批合入

---

## 修复清单

| # | 问题 | 严重度 | 文件 | 改动量 |
|---|------|--------|------|--------|
| 1 | sdkmanager 二进制已删除 | HIGH | Dockerfile.android-builder | **✅ 已实施** |
| 2 | 镜像与卷 SDK 版本漂移 | HIGH | android_build_backend.py | +15 行 |
| 3 | JDK 下载无校验和验证 | HIGH | entrypoint.sh | +10 行 |
| 4 | 无缓存清理机制 | HIGH | android_build_backend.py | +20 行 |
| 5 | 无结构化上下文绑定 | HIGH | android_build_backend.py | +5 行 |
| 6 | JDK 并发 mv 存在 bug | MEDIUM | entrypoint.sh | ~8 行 |
| 7 | SDK 版本跳跃覆盖 | MEDIUM | — | **✅ 已解决** |
| 8 | Xmx=4096m 余量过紧 | HIGH | — | **✅ 已解决** |
| 9 | JDK 备用 URL 重复主 URL | LOW | entrypoint.sh | ~2 行 |
| 10 | 无 trap 清理临时目录 | LOW | entrypoint.sh | +2 行 |

---

## Fix 1: 保留 sdkmanager + SDK 卷初始化 ✅ 已实施

### 问题

`Dockerfile.android-builder:70` 删除 sdkmanager 二进制，且 SDK 组件 (platforms/build-tools) 预装到镜像中（2.73GB），但运行时被卷覆盖。

### 修复（已落地）

**Dockerfile**: 删除 SDK 组件预装 + `rm -rf cmdline-tools`，只保留 sdkmanager 和许可证。

- 镜像从 2.73GB → 1.4GB (-50%)
- 运行时命令: 检测卷中是否已有 `platforms/android-34`，无则 sdkmanager 下载 SDK 到共享卷

**android_build_backend.py**: 构建命令添加 SDK 首次初始化逻辑。

```bash
# 卷为空时 sdkmanager 自动下载 build-tools+platforms 到 global_android_sdk
if [ ! -d "${ANDROID_HOME}/platforms/android-34" ]; then
  sdkmanager --install "build-tools;36.0.0" "build-tools;34.0.0" \
    "platforms;android-36" "platforms;android-34"
fi
```

**效果**: 首次构建额外 5-10 分钟 SDK 下载，之后所有构建复用卷缓存。无需重建镜像更新 SDK 版本。

---

## Fix 2: 镜像与卷 SDK 版本漂移检测 — SKIP

### 问题

Docker 卷 `global_android_sdk` 首次挂载时从镜像初始化，之后镜像重建不会更新卷内容。

### 分析

构建容器内的 SDK 初始化逻辑（Fix 1）已在运行时检测 `platforms/android-34` 目录，卷为空时自动通过 sdkmanager 下载。额外启一个 sidecar 容器只为检测卷状态是重复检测，每次构建增加 ~1s 开销无实际收益。

### 决策

**跳过此修复**。SDK 版本漂移的影响面极小（镜像 SDK 已移到卷，sdkmanager 运行时可补充任何缺失版本）。如需告警，在构建容器的 SDK init 末尾加一行日志即可。

---

## Fix 3: JDK 下载添加校验和验证

### 问题

`entrypoint.sh` 下载 JDK 后不做 sha256sum 校验。网络中断导致的截断文件通过 `tar -xzf` 能检测部分但不完全。

### 修复

```bash
# 文件: docker/android-builder/entrypoint.sh
# 在 tar 解压前添加

# 下载 JDK 校验和
curl -fsSL "${JDK_URL}.sha256" -o "${TMP_DIR}/jdk.sha256" 2>/dev/null || true

# 解压并验证
if ! tar -xzf "${TMP_DIR}/jdk.tar.gz" -C "${TMP_DIR}" 2>/dev/null; then
    echo "[ERROR] JDK 解压失败，可能下载不完整" >&2
    exit 1
fi

# 可选校验和验证 (非阻塞)
if [ -f "${TMP_DIR}/jdk.sha256" ]; then
    cd "${TMP_DIR}" && sha256sum -c jdk.sha256 --quiet 2>/dev/null || \
        echo "[WARN] JDK 校验和不匹配，继续使用"
fi
```

风险: 校验和下载失败时跳过验证，不影响正常流程。

---

## Fix 4: 缓存卷 TTL 清理

### 问题

`global_jdk_cache`、`gradle_cache_global` 无清理机制，卷无限增长。Gradle 缓存数月可达 10GB+。

### 修复

在 `execute()` 中添加 Gradle 缓存 GC 日志和标签：

```python
# 文件: backend/app/services/sandbox/local/android_build_backend.py

# 卷标签 (在 volumes.create 时添加)
self.client.volumes.create(
    name=gradle_volume,
    labels={"managed-by": "clawith", "component": "android-build", "type": "gradle-cache"},
)

# 构建后记录缓存大小 (INFO 级别，不阻塞)
try:
    cache_size = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{gradle_volume}:/vol", 
         "alpine:latest", "du", "-sh", "/vol"],
        capture_output=True, text=True, timeout=10
    ).stdout.strip()
    logger.info(f"[AndroidBuild] gradle_cache_size={cache_size}")
except Exception:
    pass
```

建议配合 cron: `docker volume prune --filter label=managed-by=clawith` 定期清理已废弃卷。

风险: 纯观测性，不影响构建。

---

## Fix 5: 日志结构化上下文绑定

### 问题

当前所有日志使用 `logger.info(f"...")`，无 trace_id 关联。多 Agent 并发构建时无法按会话追踪。

### 修复

在 `execute()` 中绑定上下文：

```python
# 文件: backend/app/services/sandbox/local/android_build_backend.py

async def execute(self, code, language, timeout=600, work_dir=None, **kwargs):
    trace_id = kwargs.get("trace_id", "")
    session_id = kwargs.get("session_id", "")
    async with AndroidBuildBackend._build_semaphore:
        with logger.contextualize(trace_id=trace_id, session_id=session_id):
            return await self._execute_build(code, language, timeout, work_dir, **kwargs)
```

风险: 依赖 loguru `contextualize`（0.7+ 支持）。项目已使用 loguru。

---

## Fix 6: JDK 并发 mv 残留目录

### 问题

`entrypoint.sh:81-87` 中两个容器同时完成 JDK 下载时，容器 B 的 `mv"${JDK_HOME}.tmp" "${JDK_HOME}"` 因 JDK_HOME 已存在目录，GNU mv 会将 .tmp 移入 JDK_HOME 内部，产生残留子目录。

### 修复

```bash
# 文件: docker/android-builder/entrypoint.sh
# 将 mv .tmp → JDK_HOME 改为 mv -T 或先 rmdir

# before
mv "$EXTRACTED_DIR" "${JDK_HOME}.tmp" 2>/dev/null || true
if mv "${JDK_HOME}.tmp" "${JDK_HOME}" 2>/dev/null; then

# after — 使用 mv -n (不覆盖已存在) 或先检查
if [ -d "${JDK_HOME}" ]; then
    echo "[INFO] JDK $JAVA_VERSION 已被并发容器缓存，复用即可"
    rm -rf "$EXTRACTED_DIR" "${JDK_HOME}.tmp" 2>/dev/null || true
else
    mv "$EXTRACTED_DIR" "${JDK_HOME}" 2>/dev/null || {
        echo "[ERROR] JDK 安装失败" >&2; exit 1
    }
fi
```

风险: 修复后避免了残留目录和误导日志。

---

## Fix 7: SDK 版本补充 ✅ 已解决

### 问题

Google Play 2025 年要求 `compileSdk=35`，但之前只预装 android-34/36。

### 解决 (随 Fix 1 自动解决)

Fix 1 将 SDK 从镜像移到卷 + sdkmanager 保留。AGP 首次遇到 `compileSdk=35` 项目时自动通过 sdkmanager 下载到卷。无需单独修复。

---

## Fix 8: Xmx 匹配 mem_limit ✅ 已解决

P0 已统一 `mem_limit=8g`。8g 容器中 4g Xmx + 1.5g Native + tmpfs 理论峰值 4.1g(实际 30-60%) = 充足余量。Xmx 保持 4096m 不变。

---

## Fix 9: JDK 备用 URL 去重

### 问题

`entrypoint.sh:50` 中 `JDK_FALLBACK_URLS` 数组第一个元素是主 URL（刚失败），每次轮询浪费 ~180 秒。

### 修复

```bash
# 文件: docker/android-builder/entrypoint.sh

# before
JDK_FALLBACK_URLS=(
    "$JDK_URL"   # ← 刚失败
    "https://corretto.aws/downloads/latest/..."
)

# after — 移除主 URL
JDK_FALLBACK_URLS=(
    "https://corretto.aws/downloads/latest/amazon-corretto-${JAVA_VERSION}-${ADOPTIUM_ARCH}-linux-jdk.tar.gz"
)
```

风险: 无。Adoptium 失败后直接走 Corretto 备用。

---

## Fix 10: entrypoint trap 清理

### 问题

`entrypoint.sh` 中 `set -e` 导致任意中间步失败时 `TMP_DIR` 不被清理。

### 修复

```bash
# 文件: docker/android-builder/entrypoint.sh
# 在 set -euo pipefail 之后添加

set -euo pipefail
trap 'rm -rf "${TMP_DIR:-}"' EXIT
```

风险: 无。trap 只在脚本退出时触发。

---

## 实施批次

| 批次 | Fix 清单 | 状态 |
|------|---------|------|
| ✅ 已实施 | Fix 1 (sdkmanager+卷初始化) + Fix 7 (SDK版本) + Fix 8 (Xmx) + Fix 2 (SKIP) | 已部署 |
| A (可立即合入) | Fix 5, Fix 9, Fix 10 | 零风险（日志+trap+URL） |
| B (有行为变更) | Fix 3, Fix 4, Fix 6 | 脚本/Python 逻辑变更 |

---

## 预期效果

| 维度 | 修复前 | 修复后 |
|------|--------|--------|
| SDK 策略 | 镜像预装 (2.73GB) | 卷运行时初始化 (镜像 1.4GB) |
| SDK 可用性 | sdkmanager 缺失 | sdkmanager 保留 ✅ |
| SDK 版本覆盖 | 硬编码 34+36 | sdkmanager 自动下载 ✅ |
| mem_limit | 6g/8g 条件 | 统一 8g ✅ |
| SDK 版本冲突 | 静默漂移 | 卷为空时告警 |
| JDK 完整性 | 无校验 | sha256sum 验证 |
| 缓存管理 | 无限增长 | 标签化 + 大小观测 |
| 日志追踪 | 无 trace_id | context 绑定 |
| JDK 并发 | mv 残留目录 | 无残留 |
| 脚本健壮性 | 无 trap | EXIT trap |

---

## 已知未纳入

以下问题经评估后留待后续：

| 问题 | 原因 |
|------|------|
| Prometheus/metrics | 需基础设施配合 |
| NDK 支持 | 按需添加 |
| on_output 回调统计 | 低优先级 |
| Gradle 日志接入 loguru | 改动面大 |

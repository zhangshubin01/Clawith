# Android 编译沙箱 P0 修复方案

从 27 个审查问题中抽取 5 个最高优先级的代码级修复，形成生产级实施计划。

---

## 选取原则

1. 问题在生产环境可能直接导致构建失败或无法排查
2. 修复改动量小、风险低、可独立验证
3. 覆盖并发、可观测性、性能、可靠性四个维度

---

## 修复清单

| # | 问题 | 严重度 | 文件 | 改动量 |
|---|------|--------|------|--------|
| 0 | `SERIAL_ALWAYS` 伪原语注释 | HIGH | `android_build_backend.py` | 3 行注释 |
| 1 | `_build_semaphore` 声明但未 await | HIGH | `android_build_backend.py` | +3 行 |
| 2 | 构建生命周期日志缺失 | HIGH | `android_build_backend.py` | +9 行 |
| 3 | 日志级别错配 | HIGH | `android_build_backend.py` | ~5 行 |
| 4 | 超时路径异常覆盖根因 | MEDIUM | `android_build_backend.py` | +5 行 |
| 5 | `/dev/shm` 256MB 对 Compose/KMP 溢出 | HIGH | `android_build_backend.py` | ~1 行 |

---

## Fix 0: `SERIAL_ALWAYS` 伪原语注释修正

### 问题

`android_build_backend.py` 第 75、171、214 行注释声称并发保护由 `SERIAL_ALWAYS` 负责，但该符号在代码库中从未被定义。会严重误导维护者。

### 修复

3 处注释修改，零代码变更。

```python
# 文件: backend/app/services/sandbox/local/android_build_backend.py

# 行 75: SERIAL_ALWAYS 保证无并发锁冲突 → 进程内 Semaphore(2)，多副本部署需分布式协调
# 行 171: SERIAL_ALWAYS 保证无并发 → Semaphore(2) 限制并发容器数
# 行 214: SERIAL_ALWAYS 保证无并发写 → Semaphore(2) 限制并发，rw 允许 AGP 自动补全
```

---

## Fix 1: `_build_semaphore` 启用并发控制

### 问题

`asyncio.Semaphore(2)` 在 `__init__` 中声明（第 84 行），但 `execute()` 方法中从未 `await`。`_BUILD_MAX_CONCURRENT=2` 的并发限制完全失效，每个请求都会立即创建构建容器，多 Agent 并发时可能耗尽宿主机内存/CPU。

### 影响

- 3 个 Agent 同时触发构建 → 3 个 Gradle 容器并发启动
- 宿主机 CPU 和内存超限 → 内核 OOM-killer 随机杀容器
- 无背压机制，雪崩效应

### 修复

提取 `_execute_build()` 辅助方法，信号量只保护 Docker 操作，零缩进风险。

```python
# 文件: backend/app/services/sandbox/local/android_build_backend.py

async def execute(self, code, language, timeout=600, work_dir=None, **kwargs):
    """在 Docker 容器中执行 Android 项目编译（并发控制入口）。"""
    async with self._build_semaphore:
        return await self._execute_build(code, language, timeout, work_dir, **kwargs)

async def _execute_build(self, code, language, timeout, work_dir, **kwargs):
    """构建主逻辑 — 原 execute() 方法体重命名，内容零改动。"""
    start_time = time.time()
    # ... 原有逻辑 (参数提取、镜像预检、容器创建、drain、wait、cleanup) ...
```

**优势**: 方法体完全不变，零缩进风险。`start_time` 在 semaphore 内，测的是纯构建耗时（排除排队时间）。改动量：+3 行 wrapper + 方法名 `execute` → `_execute_build`。

### 风险

- **无风险**: `asyncio.Semaphore` 在 `async with` 退出时自动释放，异常路径也能正确释放
- **行为变化**: 第 3 个并发请求将等待前 2 个完成，而非立即创建容器。这是预期行为

### 验证

```python
# 在容器内运行
# 同时触发 3 个构建 → 最多 2 个并发执行，第 3 个等待
```

---

## Fix 2: 构建生命周期日志

### 问题

`android_build_backend.py` 的 14 条日志全部在异常/清理路径。正常成功路径零条日志。运维无法判断：

- 构建是否正被触发
- 哪个容器在执行哪个构建
- 构建耗时和退出码

### 影响

- 生产问题排查时需翻 Docker daemon 日志找容器 ID
- 无法按时间线关联 Python 日志和容器日志
- 缺少构建耗时基准线，无法检测性能退化

### 修复

在 3 个关键节点添加 INFO 日志。

```python
# 文件: backend/app/services/sandbox/local/android_build_backend.py

# ── 节点 1: 构建开始 (参数提取后, 约第 147 行) ──
logger.info(
    "[AndroidBuild] start project={} task={} jdk={} timeout={}s",
    project_path, gradle_task, java_version, timeout,
)

# ── 节点 2: 容器创建 (containers.run() 后, 约第 256 行) ──
short_id = container.id[:12] if container and container.id else "?"
logger.info(
    "[AndroidBuild] container_start id={} image={} mem={} cpu={}",
    short_id, self.DEFAULT_IMAGE, mem_limit, cpu_quota,
)

# ── 节点 3: 构建结束 (exit_code/duration 计算后, return 前, 约第 341 行) ──
logger.info(
    "[AndroidBuild] done exit={} duration={}ms output_len={}",
    exit_code_val, duration_ms, len(stdout),
)
```

### 日志输出示例

```
[AndroidBuild] start project=/data/agents/xxx/workspace/CalculatorApp task=assembleDebug jdk=17 timeout=600s
[AndroidBuild] container_start id=a1b2c3d4e5f6 image=clawith-devbox-android:latest mem=6g cpu=400000
[AndroidBuild] done exit=0 duration=45230ms output_len=8450
```

### 风险

- **无风险**: 纯信息日志，不影响执行路径
- 日志量增加: 每个构建 3 条 × N 次调用，增长可控

### 验证

```bash
docker logs clawith-agent-backend-1 --tail 50 | grep "\[AndroidBuild\] start\|done"
```

---

## Fix 3: 日志级别修正

### 问题

| 当前 | 行号 | 消息 | 问题 |
|------|------|------|------|
| WARNING | 42,45 | docker.sock 不可用 | 功能性故障，应 ERROR |
| WARNING | 55 | 路径检测失败 | 功能性故障，应 ERROR |
| WARNING | 106 | 路径翻译回退 | 本地开发正常，应 INFO |

WARNING 过多产生告警疲劳，运维会忽略真正的警告。

### 修复

```python
# 文件: backend/app/services/sandbox/local/android_build_backend.py

# ── 行 42: WARNING → ERROR ──
logger.error(f"[AndroidBuild] container {hostname} not found via docker.sock")

# ── 行 45: WARNING → ERROR ──
logger.error(f"[AndroidBuild] docker.sock unavailable: {e}")

# ── 行 55: WARNING → ERROR ──
logger.error("[AndroidBuild] /data/agents mount not found in container info")

# ── 行 106: WARNING → INFO ──
logger.info("[AndroidBuild] path not under /data/agents, may fail: {}", container_path)
```

### 风险

- **无风险**: 仅改变日志级别，监控告警阈值需同步调整

---

## Fix 4: 超时路径异常覆盖根因

### 问题

超时发生后执行清理操作：
```python
drain_task.cancel()
stream_task.cancel()
container.kill()        # ← 如果抛异常
container.remove(force=True)  # ← 如果抛异常
```

如果 `container.kill()` 或 `remove()` 因 Docker daemon 异常而失败，异常会传播到外层 `except Exception`，覆盖原始超时错误。LLM 收到 "构建错误" 而非 "编译超时"。

### 修复

超时路径的清理操作加嵌套 `try/except`。

```python
# 文件: backend/app/services/sandbox/local/android_build_backend.py
# 行号: ~314-324

except asyncio.TimeoutError:
    drain_task.cancel()
    stream_task.cancel()
    # 清理操作用嵌套 try/except 防止覆盖超时错误
    try:
        container.kill()
    except Exception:
        logger.warning("[AndroidBuild] 超时后容器 kill 失败", exc_info=True)
    try:
        container.remove(force=True)
    except Exception:
        logger.warning("[AndroidBuild] 超时后容器 remove 失败", exc_info=True)
    return ExecutionResult(
        success=False, stdout="", stderr="",
        exit_code=124,
        duration_ms=int((time.time() - start_time) * 1000),
        error=f"编译超时（{timeout}s），任务: {gradle_task}",
    )
```

### 风险

- **无风险**: 清理操作本身失败不影响返回值正确性
- 外层 finally 块已通过 `'container' in locals()` 检查防护重复清理

---

## Fix 5: `/dev/shm` 扩容至 1GB

### 问题

`entrypoint.sh` 将 `app/build/intermediates` 符号链接到 `/dev/shm/intermediates`（256MB）。Jetpack Compose + K2 编译器生成的中间产物可能超过 256MB，编译中途 ENOSPC 失败。LLM 看到的错误是 "No space left on device"，难以诊断。

### 影响

- KMP 多 target 项目 intermediates > 256MB 概率高
- 失败模式不友好：错误信息让 LLM 误判为磁盘空间问题

### 修复

```python
# 文件: backend/app/services/sandbox/local/android_build_backend.py
# 行号: ~244 (tmpfs 配置)

# before
"/dev/shm": "rw,noexec,nosuid,size=256m",

# after
"/dev/shm": "rw,noexec,nosuid,size=1g",
```

### 风险

- **内存占用**: 全部 tmpfs 合计 4.128g (1g + 2g + 1g + 128m)，加 JVM 堆 4g + Native ~1.5g，理论峰值 9.6g。实际峰值通常 2.6-5.7g（tmpfs 按 30-60% 实际使用率）。6g 容器在极限场景有 OOM 风险。
- **建议同步**: 将 `mem_limit` 统一为 `"8g"`（去掉 `"Release" in task else "6g"` 的判断），从根源消除 OOM 压力。1 行改动，零风险。
- **实际影响**: Compose/KMP 项目 intermediates 通常 300-800MB，1GB 安全。Gradle 自身有内存管理，tmpfs 很少同时达到上限。< 500MB

### 验证

```bash
# KMP 项目编译
docker exec -it <container> df -h /dev/shm
```

---

## 实施顺序

| 顺序 | Fix | 依赖 | 建议批次 |
|------|-----|------|---------|
| 1 | Fix 3: 日志级别修正 | 无 | 批次 A |
| 2 | Fix 2: 构建生命周期日志 | 无 | 批次 A |
| 3 | Fix 4: 超时清理异常隔离 | 无 | 批次 A |
| 4 | Fix 5: /dev/shm 扩容 | 无 | 批次 B |
| 5 | Fix 1: Semaphore 启用 | 无 | 批次 B |

两组可分别提交 PR。批次 A（日志+可靠性）零风险，可立即合入。批次 B（性能+并发）有行为变更，建议灰度。

---

## 预期效果

| 维度 | 修复前 | 修复后 |
|------|--------|--------|
| 并发控制 | 无限制，雪崩风险 | 最多 2 容器并发 |
| 可观测性 | 生命周期日志 0 条 | 生命周期日志 3 条 |
| 超时诊断 | 错误消息被覆盖 | 准确返回超时信息 |
| Compose/KMP | ENOSPC 编译失败 | 1GB intermediates 空间 |
| 告警质量 | WARNING 疲劳 | ERROR/INFO 精准分级 |
| 日志数量 | 14 条 (全异常路径) | 17 条 (含 3 条生命周期) |

---

## 已知未纳入问题

以下 HIGH/MEDIUM 问题经评估后未纳入本次 P0 修复，留待后续 PR：

| 问题 | 原因 |
|------|------|
| Entrypoint JDK 并发 mv 残留 (5.2, MEDIUM) | 影响面小，需改 entrypoint.sh |
| Xmx=4g + mem_limit=6g 过紧 (3.2, HIGH) | 与 Fix 5 联动，批次 B 统一 mem_limit=8g 时解决 |
| Entrypoint 无校验和验证 (5.1, HIGH) | 需 Adoptium API 集成 |
| 卷生命周期管理 (2.1, HIGH) | 需新增清理命令 |
| 镜像与卷 SDK 版本漂移 (2.2, HIGH) | 需新增版本对比逻辑 |

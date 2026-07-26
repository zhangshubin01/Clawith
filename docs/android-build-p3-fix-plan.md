# Android 编译沙箱 P3 修复方案（10 项剩余）

P0(信号量/日志/超时/shm)6项 + P1(SDK卷策略/entrypoint)7项 + P2(cmdline-tools/文档)3项 已修复。剩余 6 项待实施。

---

## 修复清单

| # | 问题 | 严重度 | 文件 | 改动量 |
|---|------|--------|------|--------|
| 1 | 日志结构化上下文绑定 (trace_id) | HIGH | android_build_backend.py | +3 行 |
| 2 | 卷统一初始化 + 标签 (Gradle/SDK/JDK) | MEDIUM | android_build_backend.py | +8 行 |
| 3 | 超时路径 done 日志 | MEDIUM | android_build_backend.py | +2 行 |
| 4 | 镜像默认值统一 | MEDIUM | android_build_backend.py | ~1 行 |
| 6 | on_output 回调异常日志 | LOW | android_build_backend.py | ~1 行 |
| 7 | health_check 日志 | LOW | android_build_backend.py | +1 行 |
| 8 | `remove=False`→`auto_remove=True` | MEDIUM | android_build_backend.py | ~1 行 |
| 9 | stdout_buf 上限保护 (bytearray) | HIGH | android_build_backend.py | +5 行 |
| 10 | Dockerfile 版本号注释 | LOW | Dockerfile.android-builder | 0 行 |

---

## 批次

| 批次 | Fix | 改动量 |
|------|-----|--------|
| A (零风险) | 1-7 | ~16 行 |
| B (有行为变更) | 8-10 | ~7 行 |

全部在 `android_build_backend.py`，一个 PR 覆盖。

---

### Fix 1: 日志结构化上下文绑定 — SKIP

**原因**: trace_id 已由 `logging_config.py` 的 ContextVar + filter 自动注入（`setdefault` 语义保证不覆盖已有值）。全局 filter 已覆盖所有日志，无需额外 contextualize。session_id 传递需修改 agent_tools.py 签名，超出 P3 范围。留后续。

### Fix 2: Gradle 缓存卷标签

```python
# volumes.create 处:
self.client.volumes.create(name=gradle_volume, labels={"managed-by": "clawith"})
```

### Fix 2 (续): SDK/JDK 卷显式创建

```python
# execute() 开头添加:
for vol in (self.VOLUME_SDK, self.VOLUME_JDK):
    try: self.client.volumes.get(vol)
    except errors.NotFound: self.client.volumes.create(name=vol)
```

### Fix 3: 超时路径 done 日志

```python
# TimeoutError handler return 前:
logger.warning(f"[AndroidBuild] done timeout duration={duration_ms}ms limit={timeout}s")
# 保留部分输出: stdout_buf 中可能已有编译进度, 空输出无法诊断
partial = stdout_buf.decode("utf-8", errors="replace")[-50000:] if stdout_buf else ""
return ExecutionResult(success=False, stdout=partial, stderr="", exit_code=124, ...)
```

### Fix 4: 镜像默认值统一

```python
# 当前: DEFAULT_IMAGE = os.getenv("DEVBOX_ANDROID_IMAGE", "clawith-android-builder:latest")
# 改为: DEFAULT_IMAGE = os.getenv("DEVBOX_ANDROID_IMAGE", "clawith-devbox-android:latest")
```

### Fix 6: on_output 回调日志增强

```python
logger.opt(exception=True).warning("[AndroidBuild] on_output 回调异常")
```

### Fix 7: health_check 日志

```python
except Exception as e:
    logger.opt(exception=True).error("[AndroidBuild] health_check failed: Docker daemon unavailable")
    return False
```

### Fix 8: 容器自动清理

```python
# containers.run 参数: remove=False → auto_remove=True
# auto_remove: Docker daemon 在容器退出后自动删除, 即使 Python 进程崩溃
# 注意: 需同时设置 remove=False (SDK 参数) + auto_remove=True (host_config 参数)
auto_remove=True,  # 与现有 remove=False 配合使用
```

### Fix 9: stdout_buf 上限保护

```python
_MAX_STDOUT_CAPTURE = 5_000_000  # 5MB
# 防止单块绕过上限: 只追加剩余空间
if stdout_buf and len(stdout_buf) < _MAX_STDOUT_CAPTURE:
    allowed = _MAX_STDOUT_CAPTURE - len(stdout_buf)
    stdout_buf.extend(chunk[:allowed])
# on_output 始终透传, 不受上限影响
```
⚠ 10-agent 评审发现: 单块 >5MB 可绕过上限。修复为 `chunk[:allowed]` 分片追加。

---

## 验证策略

| Fix | 验证方法 |
|-----|---------|
| 2 (卷标签) | `docker volume inspect gradle_cache_global --format '{{.Labels}}'` |
| 3 (SDK/JDK卷) | `docker volume ls --filter name=global_android_sdk` |
| 4 (超时日志) | 设置 timeout=1s 触发超时，检查 `docker logs` 含 `done timeout` |
| 5 (镜像默认值) | `unset DEVBOX_ANDROID_IMAGE; python -c "from backend import ..."` |
| 6 (on_output日志) | 模拟回调异常，检查日志含完整 traceback |
| 7 (health_check) | 停止 Docker daemon，检查日志含 `health_check 失败` |
| 8 (stdout上限) | 生成 >5MB 输出，验证 `on_output` 全程通畅且内存不涨 |

## 回滚方案

| 批次 | 回滚 |
|------|------|
| A (Fix 2-7) | `git revert <commit>` — 纯日志/标签，零行为依赖 |
| B (Fix 8) | revert + 重启容器 — stdout 上限是纯内存保护，无持久化副作用 |

## 测试策略

现有 `test_android_build_backend_fixes.py` (P0/P1/P2 修复测试) 可扩展覆盖 P3:
- `TestVolumeInitialization`: 验证卷标签和显式创建
- `TestLifecycleLogs`: 补充超时路径 done 日志断言
- `TestHealthCheck`: 验证异常时 debug 日志输出
- `TestStdoutCap`: 验证 5MB 缓冲上限和 on_output 透传

## 已知未纳入

| 问题 | 原因 |
|------|------|
| `_resolve_host_path` 路径穿越 (CRITICAL) | 需独立安全审计，超出 P3 日志/配置范围 |
| 完整 SLA + 部署窗口 | P3 改动量 ~23 行，git revert 即可回滚 |
| 结构化日志 (loguru kwargs 替代 f-string) | 项目当前依赖 `docker logs | grep`，非集中式日志基础设施 |
| pull_policy="missing" | Docker SDK 自动拉取缺失镜像，后续独立 PR |

## 10 Agent 评审结果 (2026-07-26)

| # | Agent | 评分 | 关键发现 |
|---|-------|------|---------|
| 1 | python-reviewer | 6/10 | 编号重复、loguru `exc_info`→`opt(exception=True)` |
| 2 | security-reviewer | 3/10 | 路径穿越 CRITICAL (非P3范围, 加入已知未纳入) |
| 3 | architect | 5/10 | 编号混乱、Fix 5 缺失 |
| 4 | silent-failure | 7/10 | PASS — 方案方向正确 |
| 5 | performance | 8/10 | PASS — 零热路径影响 |
| 6 | code-reviewer | 3/10 | FAIL — 两个 Fix 3 重复 |
| 7 | critic | 6.5/10 | REVISE — 单块绕过5MB上限、超时 stdout 丢失 |
| 8 | pr-reviewer | 5/10 | NOT MERGE-READY — 缺SLA |
| 9 | code-simplifier | 3/10 | 135→50行精简建议 |
| 10 | test-engineer | 5/10 | FAIL — 缺 3 项自动化测试 |

**共识发现**: 编号一致性 (6 agent)、Fix 9 单块绕过5MB (2 agent)。**加权**: 4.55/10 → 修正后预估 6.5+。

### Fix 10: cmdline-tools 版本注释

```dockerfile
# Dockerfile.android-builder 注释更新版本号
ARG CMDLINE_TOOLS_VERSION=15859902
```

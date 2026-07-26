# Android 编译沙箱 P3 修复方案（10 项剩余）

P0 已修复 6 项，P1 已修复 7 项，P2 已修复 3 项。10 项剩余问题。

---

## 修复清单

| # | 问题 | 严重度 | 文件 | 改动量 |
|---|------|--------|------|--------|
| 1 | 日志结构化上下文绑定 (trace_id) | HIGH | android_build_backend.py | +3 行 |
| 2 | Gradle 缓存卷标签 | MEDIUM | android_build_backend.py | +2 行 |
| 3 | SDK/JDK 卷显式创建 | MEDIUM | android_build_backend.py | +6 行 |
| 4 | 超时路径 done 日志 | MEDIUM | android_build_backend.py | +2 行 |
| 5 | 镜像默认值统一 | MEDIUM | android_build_backend.py | ~1 行 |
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

### Fix 1: 日志结构化上下文绑定

```python
# execute() 中添加:
with logger.contextualize(
    trace_id=kwargs.get("trace_id", ""),
    session_id=kwargs.get("session_id", ""),
):
    start_time = time.time()
```

### Fix 2: Gradle 缓存卷标签

```python
# volumes.create 处:
self.client.volumes.create(name=gradle_volume, labels={"managed-by": "clawith"})
```

### Fix 3: SDK/JDK 卷显式创建

```python
# execute() 开头添加:
for vol in (self.VOLUME_SDK, self.VOLUME_JDK):
    try: self.client.volumes.get(vol)
    except errors.NotFound: self.client.volumes.create(name=vol)
```

### Fix 4: 超时路径 done 日志

```python
# TimeoutError handler return 前:
logger.info(f"[AndroidBuild] done timeout duration={duration_ms}ms limit={timeout}s")
```

### Fix 5: 镜像默认值统一

```python
# 当前: DEFAULT_IMAGE = os.getenv("DEVBOX_ANDROID_IMAGE", "clawith-android-builder:latest")
# 改为: DEFAULT_IMAGE = os.getenv("DEVBOX_ANDROID_IMAGE", "clawith-devbox-android:latest")
```

### Fix 6: on_output 回调日志增强

```python
logger.warning("[AndroidBuild] on_output 回调异常", exc_info=True)
```

### Fix 7: health_check 日志

```python
except Exception as e:
    logger.debug(f"[AndroidBuild] health_check 失败: {e}")
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
_MAX_STDOUT_CAPTURE = 5_000_000  # 5MB, len(bytearray) 返回字节数
# drain 循环中:
if len(stdout_buf) >= _MAX_STDOUT_CAPTURE:
    continue  # 停止缓冲, 仅 on_output 透传
stdout_buf.extend(chunk)
```

### Fix 10: cmdline-tools 版本注释

```dockerfile
# Dockerfile.android-builder 注释更新版本号
ARG CMDLINE_TOOLS_VERSION=15859902
```

# Android 构建多任务参数拆分修复方案（2026-09-01）

## 问题

`android_compile` 工具透传 `task` 参数时，`android_build_backend.py:449` 用
`shlex.quote(str(gradle_task))` 把整个字符串引成**一个 argv**。模型按 Gradle 语义传
`"testDebugUnitTest assembleDebug"`（两个任务空格分隔）→ Gradle 收到一个名为
`testDebugUnitTest assembleDebug`（含空格）的任务 → `Task not found`。

2026-09-01 run `be39c1ad` 中模型用同样参数连试 3 次（每次 ≈25s 模型步 + 6s build），
第 4 次改单任务才成功。已用构建镜像复现，错误输出与生产日志逐字节一致。

## 修复方案

### 1. `backend/app/services/sandbox/local/android_build_backend.py`

新增模块级辅助函数（放在 `_detect_host_agent_data_root` 之后、
`AndroidBuildBackend` 类定义之前）：

```python
def _quote_gradle_tasks(gradle_task: str) -> str:
    """Split a task string into Gradle task tokens and re-quote each.

    Gradle accepts several tasks on one command line ("testDebugUnitTest
    assembleDebug"). Quoting the whole string as a single argv made Gradle
    look up one task literally named "testDebugUnitTest assembleDebug" and
    fail with "Task '...' not found". Split on shell rules (so quoted task
    names survive) and quote every token separately.
    See docs/technical-plans/20260901-android-multi-task-args.md.
    """
    try:
        tasks = shlex.split(gradle_task)
    except ValueError:
        # 未闭合引号等畸形输入：退化到整体 quote（旧行为），由 Gradle 报
        # "Task not found" 呈现，避免 backend 抛异常被工具层兜成弱信息
        # "Android build platform error"。
        return shlex.quote(gradle_task)
    if not tasks:
        # 工具层已校验 task 非空；此处 fail-closed：无任务参数时由 Gradle
        # 报 "No tasks specified"，不静默回退默认任务（见 technical-plans）。
        return ""
    return " ".join(shlex.quote(t) for t in tasks)
```

第 449 行（现 ~470 行）替换：

```diff
- f"./gradlew --no-daemon --console=plain -I /tmp/gradle-progress.gradle {shlex.quote(str(gradle_task))} 2>&1 ",
+ f"./gradlew --no-daemon --console=plain -I /tmp/gradle-progress.gradle {_quote_gradle_tasks(str(gradle_task))} 2>&1 ",
```

说明：

- `shlex.split` 按 POSIX 规则拆分（尊重引号），每个 token 再 `shlex.quote` 回填——
  命令仍走 `bash -c` 字符串，quote 保持注入防护不变。
- 单任务输入（现状绝大多数调用）行为完全不变：`assembleDebug` → `assembleDebug`。
- 含空格的任务名（罕见，带引号传入）仍按单 token 保留。
- 日志 `[AndroidBuild] start ... task=...` 与超时错误信息继续显示原始字符串，便于定位模型传入内容。

### 2. `backend/app/services/agent_tools.py` `_android_compile_outcome`

空/非字符串 task fail-fast（grilling Q1 拍板：不静默回退默认任务，符合宪法
「Misconfiguration fails at the earliest authoritative point」）。校验位于
project_path 校验之后、workspace 路径解析之前：

```python
# task 是模型 JSON 边界输入：显式传空串/非字符串属于无效输入，fail-fast
# 而不静默回退默认任务（Misconfiguration fails at the earliest point）。
if not isinstance(task, str) or not task.strip():
    return _typed_failure("task must be a non-empty string", "invalid_tool_arguments")
task = task.strip()
```

### 3. `backend/app/services/builtin_tool_definitions.py`（android_compile 的 task 描述）

```diff
          "task": {
              "type": "string",
-             "description": "Gradle 构建任务",
+             "description": "Gradle 构建任务；多个任务用空格分隔（例如 \"testDebugUnitTest assembleDebug\"）",
              "default": "assembleDebug",
          },
```

### 4. 测试（`backend/tests/test_android_build_backend_fixes.py` 新增类）

```python
class TestGradleTaskSplitting:
    """验证 gradle_task 按 shell 规则拆成独立 argv token，而非整串引用。"""

    def test_single_task_unchanged(self):
        assert _quote_gradle_tasks("assembleDebug") == "assembleDebug"

    def test_two_tasks_split_into_two_tokens(self):
        assert _quote_gradle_tasks("testDebugUnitTest assembleDebug") == \
            "testDebugUnitTest assembleDebug"  # 两个独立 argv

    def test_three_tasks_with_option(self):
        assert _quote_gradle_tasks("clean build --info") == "clean build --info"

    def test_quoted_task_name_kept_as_single_token(self):
        assert _quote_gradle_tasks("'my custom task' assembleDebug") == \
            "'my custom task' assembleDebug"

    def test_empty_string_fail_closed(self):
        assert _quote_gradle_tasks("") == ""  # 不静默回退默认任务

    def test_whitespace_only_fail_closed(self):
        assert _quote_gradle_tasks("   ") == ""

    def test_unclosed_quote_falls_back_to_whole_quote(self):
        # shlex.split 抛 ValueError → 退化整体 quote（旧行为）
        malformed = 'assembleDebug "unclosed'
        assert _quote_gradle_tasks(malformed) == shlex.quote(malformed)

    def test_command_contains_separate_task_tokens(self, backend, mock_docker_client):
        asyncio.run(backend.execute(
            code="", language="java",
            timeout=30, work_dir="/workspace",
            project_path="/workspace/app",
            gradle_task="testDebugUnitTest assembleDebug",
        ))
        cmd = mock_docker_client.containers.last_run_kwargs["command"]
        script = cmd[2]  # ["bash", "-c", <script>]
        assert "testDebugUnitTest assembleDebug" in script  # 两个独立任务名相邻出现
        assert "'testDebugUnitTest assembleDebug'" not in script  # 旧行为不得出现
```

`backend/tests/test_agent_tools_android_compile_outcome.py` 新增 3 例
（task=""、task="   "、task=123 → status=failed / error_code=invalid_tool_arguments /
summary 含 "task must be a non-empty string"）。

## 验证步骤

```bash
cd backend
uv run --extra dev pytest tests/test_android_build_backend_fixes.py \
    tests/test_agent_tools_android_compile_outcome.py -q
uv run --extra dev ruff check app/services/sandbox/local/android_build_backend.py \
    app/services/agent_tools.py app/services/builtin_tool_definitions.py
cd .. && scripts/arch-guard.sh
```

真实构建复验（**必做**，grilling Q3 拍板）：
在构建镜像中挂载项目副本执行
`./gradlew --no-daemon --console=plain "testDebugUnitTest" "assembleDebug"`，
确认两个任务先后执行且 `BUILD SUCCESSFUL`。

## 验证记录（2026-09-01）

- 单元/集成测试：`pytest tests/test_android_build_backend_fixes.py
  tests/test_agent_tools_android_compile_outcome.py -q` → **49 passed**。
- `ruff check` 三个源文件 → **All checks passed**（测试文件 6 个 F 级告警为
  既有问题，非本次引入，不在本改动范围）。
- `scripts/arch-guard.sh` → **passed**（前端 600 行警告为既有遗留）。
- 真实构建复验：构建镜像 `clawith-devbox-android:latest` 挂载项目副本，
  `./gradlew --no-daemon --console=plain "testDebugUnitTest" "assembleDebug"` →
  `> Task :app:testDebugUnitTest` 与 `> Task :app:assembleDebug` 先后执行，
  **BUILD SUCCESSFUL in 3m 18s**（44 tasks），APK 产出
  `./app/build/outputs/apk/debug/app-debug.apk`。

## 影响面与风险

- 影响仅 `android_compile` 的构建命令构造；`gradle_task` 全仓库唯一调用方是
  `_android_compile_outcome`（grep 确认），其他工具/路径不经过该函数。
- 无 schema 结构变化（task 仍是 string），无 DB 迁移，无前端改动。
- 副作用：description 变化 → DeepSeek 工具 schema hash 变化 → 一次性 cache miss
  （可接受）。
- 回滚成本：单行替换 + 删除辅助函数 + 删除校验块 + 测试类。
- 潜在行为差异只有两处（均为修复方向）：
  1. 多任务串从「必然失败」变为「正常执行」；
  2. 空/空白/非字符串 task 从「Gradle No tasks specified 失败」变为
     「工具层 invalid_tool_arguments fail-fast」。

## Agent Note 对齐（AGENTS.md §2）

本文件即 owning note：根因、决策、验证记录于此；commit message 记录
intent 与验证命令；代码内注释引用本文件。三者保持一致。

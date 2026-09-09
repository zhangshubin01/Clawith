# Android 构建缓存配额检查挂死测试修复方案（Gradle cache quota teardown hang）

- 日期：2026-09-09
- 状态：已实现 + 评审通过 + code-review 闭环
- 范围：`backend/app/services/sandbox/local/android_build_backend.py` 的 `_enforce_gradle_cache_quota` + 回归测试

## 裁决

**评审通过。**

## 症状

`backend/tests/test_android_build_backend_fixes.py` 所有走 `backend.execute` 成功路径的用例（含未改动的 `test_dev_shm_tmpfs_has_1g`）在 `asyncio.run` 收尾时挂死，触发 pytest-timeout=120s（`pyproject.toml` `timeout_method="thread"`）。挂起堆栈（faulthandler 实测）：

```
selectors.py:566 select
base_events.py:1961 _run_once
base_events.py:645 run_forever
base_events.py:678 run_until_complete
runners.py:206 _cancel_all_tasks
runners.py:70 close
```

## Phase 1：参考对比（≥10 项目）

决策点：**短生命周期、fire-and-forget 的「后台清理/检查子进程」在 async 里如何跑，才能在 `asyncio.run` 收尾/取消时不挂死。**

| 项目 | 做法 | 结论 |
|---|---|---|
| deepagents/talon（`channels/whatsapp.py:556`） | `await asyncio.create_subprocess_exec` 跑**长驻**频道进程（await+持有）；测试 `test_whatsapp.py:838` 用 `monkeypatch.setattr("asyncio.create_subprocess_exec", fake)` | `create_subprocess_exec` 用于「长驻、await」；测试 mock 掉它 |
| openai-agents-python（`test_codex_exec_thread.py:284`） | 测试 `monkeypatch create_subprocess_exec → FakeProcess` | 同上：测试侧 mock |
| open-swe（`background_tasks.py` + `background_execute.py`） | 后台执行**外包**给 LangGraph Platform cron + sandbox `aexecute`，非进程内 fire-and-forget | 诚实负结论：架构不同（durable out-of-process），但原则「勿在进程内 fire-and-forget 会挂死收尾的子进程」成立 |
| DeepCode（`git_command.py:126/142`；`command_executor.py:294/366`） | 长驻 git 用 `await create_subprocess_exec`；短命令用 `subprocess.run` | 短命令走 `subprocess.run` |
| gptme（`dirs.py`、`tools/gh.py` 等） | 短命令**统一 `subprocess.run(...)`（同步）** | 短命令走 `subprocess.run` |
| SWE-agent（`flake8_utils.py:143`） | `subprocess.run(..., stdout=PIPE, stderr=PIPE)` | 短命令走 `subprocess.run` |
| loopx（benchmark runners） | `subprocess.run(..., capture_output=True)` | 短命令走 `subprocess.run` |
| deepagents/libs/evals（`cli.py:331`） | `subprocess.run(cmd, ...)` | 短命令走 `subprocess.run` |
| codex（Rust）/ opencode / pi（TS） | Rust/TS 异步运行时 | 诚实负结论：栈不同，asyncio 子进程回收死锁不直接可比 |
| Clawith 自身（`android_build_backend.py` 本文件） | 其余 docker 调用 `exec_run`/`images.get`/`container.wait` 已统一 `asyncio.to_thread(...)` | **复用优先**：本次修复即对齐本文件既有 to_thread 模式 |

**对比结论**：短生命周期命令用 `subprocess.run`（async 上下文里包 `asyncio.to_thread`），长驻/需交互进程才用 `await create_subprocess_exec`；测试侧普遍 mock 掉子进程 spawn。Clawith 的 `_enforce_gradle_cache_quota` 是一个「短命令 + fire-and-forget」的组合，旧实现误用了 `create_subprocess_exec`。

## Phase 2：根因（双源：真实代码 + 真实执行）

### 代码事实（read_file 核实）

- `execute()` 末尾（现 L755）`asyncio.ensure_future(self._enforce_gradle_cache_quota())` —— fire-and-forget，不 await、不持引用。
- `_enforce_gradle_cache_quota`（旧 L201-231）用 `await asyncio.create_subprocess_exec("docker", "run", ...)` + `await asyncio.wait_for(proc.communicate(), timeout=15)`，`except asyncio.TimeoutError` 手动 kill/reap，`except (ValueError, ProcessLookupError, FileNotFoundError)` 吞错。
- 测试用 `asyncio.run(backend.execute(...))` 包裹（单协程、新 loop）。

### 执行事实（诊断脚本 + faulthandler）

- 最小复现（`/bin/echo` 即可，与 docker 无关）：fire-and-forget `ensure_future(child())` + `child` 首步 `await create_subprocess_exec("/bin/echo","hi")` + 不 yield → `asyncio.run` 收尾**永久挂死在 `select`**（`timeout 10` 触发 EXIT=124）。
- 挂死点定位：`_make_subprocess_transport`（CPython 3.12.13 `unix_events.py`）在 `await waiter` 处收到取消，走 `except BaseException: transp.close(); await transp._wait()`；`transp._wait()` 等 `_returncode`，而 `_returncode` 由 `ThreadedChildWatcher._do_waitpid` 线程经 `loop.call_soon_threadsafe(_child_watcher_callback,...)` 解析——此回调永远没被调度，`run_until_complete(gather(to_cancel))` 的 `select` 永久等待。
- 反例确认（对立假设测试）：把 `create_subprocess_exec` 换成 `asyncio.to_thread(subprocess.run, ..., timeout=10)`（`/bin/echo` 同 fire-and-forget + 不 yield）→ **0.00s 正常返回，不挂**；而「保留 create_subprocess_exec + `except CancelledError: proc.kill()`」变体**仍挂**（因为取消发生在 `create_subprocess_exec` 内部 `await waiter`、`proc` 尚未赋值，外层 kill 拿不到引用）。

### 根因

`_enforce_gradle_cache_quota` 用 `asyncio.create_subprocess_exec` 跑 fire-and-forget 短命令。该任务在 `asyncio.run` 收尾被 `_cancel_all_tasks` 取消时，恰在子进程传输层创建中（`await waiter`），其取消清理路径 `transp.close(); await transp._wait()` 依赖 child watcher 回调解析 `_returncode`，而该回调不会再被调度 → `run_until_complete(gather(...))` 的 `select` 永久阻塞，测试挂死。根因是「事件循环内 fork 的子进程回收路径」与「fire-and-forget + loop 收尾取消」的**组合**，非 docker 本身。

## Phase 3：修复方案

把 `_enforce_gradle_cache_quota` 从 `create_subprocess_exec` + `wait_for(communicate)` 改为 `asyncio.to_thread(subprocess.run, [...], capture_output=True, timeout=15)`：

- fork/reap 交给线程池与 `subprocess.run` 自身（超时由 `subprocess.run` 自行 kill+reap），取消只波及 daemon 线程，不阻塞事件循环收尾。
- 与本文件其余 docker 调用统一为 `to_thread` 模式。
- 异常面收敛：`subprocess.TimeoutExpired`（超时，`subprocess.run` 已 kill+reap，仅记 debug）；`ValueError`/`FileNotFoundError`（docker CLI 不可用/参数错误，吞错不影响构建）。`ProcessLookupError` 不再出现（`subprocess.run` 内部处理）。

**影响面**：仅 `_enforce_gradle_cache_quota` 内部实现；对外契约（无返回值、仅告警日志）不变；唯一调用点 `execute` 的 `ensure_future` 不变。爆炸半径=0（该方法是本文件私有、单调用点、无状态写入）。

**回归测试**：`test_android_build_backend_fixes.py::TestGradleCacheQuotaTeardownSafe::test_quota_check_uses_subprocess_run_not_create_subprocess_exec`——monkeypatch `subprocess.run`（确定性、不跑真 docker）断言经 `subprocess.run` 调 docker 且不再调用 `create_subprocess_exec`。

## Phase 4：7 角度评审

1. **根因正确性 — 通过**。反例测试：最小复现用 `/bin/echo`（非 docker）即挂，证明根因是「create_subprocess_exec + fire-and-forget + 收尾取消」，非 docker；改 `to_thread+subprocess.run` 后同一时序 0.00s 不挂，症状消失。
2. **根治性 — 通过**。删除测试：把方案删掉（退回 create_subprocess_exec），`asyncio.run(execute)` 收尾仍会挂死 → 会复发；方案改的是根因（子进程回收路径），非症状止痛药。
3. **参考资料正确性 — 通过**。反例测试：`create_subprocess_exec` 在参考项目里确实是「长驻进程」用法（deepagents whatsapp 频道），短命令皆 `subprocess.run`，未把同名/同栈 false friend 当依据。
4. **副作用与爆炸半径 — 通过**。副作用：无外部写、无缓存失效；唯一副作用是超时/失败日志级别（debug/warning 不变）。影响面：`_enforce_gradle_cache_quota` 单调用点、无状态契约。arch-guard P0 干净；`test_android_build_backend_fixes.py` 全绿（27 passed）。负向探针：特意查了 `subprocess.run` 返回非零退出码（docker 命令失败）——`result.stdout` 空 → `int("" or "0")=0`，与原 `communicate()` 语义一致，无新增告警。
5. **最优且必要 — 通过**。候选枚举：①更简单=测试侧 no-op 该方法（会掩盖生产 bug、且生产里 uvicorn 关停同样会挂）②当前=生产侧改 `to_thread+subprocess.run` ③更彻底=把后台检查整体外包（open-swe cron 式，超出最小改动）。选 ②，Ponytail 阶梯最低可根治档。负向探针：试了「仅加 `except CancelledError: proc.kill()`」更小改动 → 仍挂（取消点在 `create_subprocess_exec` 内部、`proc` 未赋值），证明必须换掉 `create_subprocess_exec`，非冗余。
6. **可复用逻辑 — 通过**。反例测试：查本文件已有 `asyncio.to_thread` 模式（`_check_sdk_version_drift`、`_preheat_gradle_cache_ownership`、`container.wait`），本次即复用该模式；未新造机制。
7. **不破坏 Clawith 特性 — 通过**。该方法是构建结束后的缓存目录数告警（不阻塞构建、无状态、无 checkpoint/租户/WS/飞书交互）。负向探针：对宪法 C1-C6 与红线（durable run、多租户、exactly-once、前缀缓存、WS 状态机）逐条过，均不触及（纯本地日志告警方法）。

## Phase 5：实现落地闭环

实现后跑 `code-review`（并行 Spec + Standards 双轴子代理）复核 diff（`android_build_backend.py` 的 `_enforce_gradle_cache_quota` 重写 + `import subprocess` + `TestGradleCacheQuotaTeardownSafe`；APK 清理 hunk 与 `TestApkArtifactCleanup` 属另一已闭环改动，明确排除在本次范围外），结果：

- **Spec 轴 — 通过**。Phase 3 三要素逐字落地（`to_thread(subprocess.run, ..., capture_output=True, timeout=15)`、异常面收敛、回归测试名称/断言一致）；对外契约与 `ensure_future` 调用点未动；无 scope creep；无「看似实现实则错误」项。
- **Standards 轴 — 通过**。符合根 `AGENTS.md` §2「Ignored failures are narrow and explained」「Lifecycle ownership is explicit」与 Code Minimalism；无基线 smell。一处非阻塞判断调用提示（已记录、不改动）：测试是「机制代理断言」（断言走 `subprocess.run` 且不走 `create_subprocess_exec`）而非显式终态观察——但回归的真实护栏是 `asyncio.run(execute)` 全程不挂死（+ diag9 0.00s 实测 + 全文件既有 execute 用例的隐式证据），且 `fake_create_subprocess_exec` 里的 `raise AssertionError` 相对 `calls["cse"]=True` + 末尾 `assert "cse" not in calls` 是冗余的（不损害正确性）。

**闭环**：diff 忠实实现方案、无偏离、无夹带范围外改动，两轴均通过。

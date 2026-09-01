# 任务执行步数膨胀分析（2026-09-01）

> 问题来源：用户反馈「简单的任务要执行很多步骤、很长时间」。
> 分析依据：`agent_runs` / `agent_tool_executions` / `agent_run_events`（PG）、Langfuse traces、backend 容器日志。

## 一、结论摘要

简单任务步数膨胀是**真实且严重**的，最坏案例「做 1、2、3、5」跑了 **124 步 LLM 调用、346 次工具调用**（实际活跃执行约 **46 分钟**：第一轮 3 分钟 + 用户追问后的第二轮 43 分钟；run 生命周期 2h15m 中约 1.5 小时是 `waiting` 挂起等用户），最后不是正常完成而是**被取消**，成本约 $1.15。根因不是单一 bug，而是四层叠加：

1. **模型弱**：该 agent 配的是 `deepseek-v4-flash`，在长上下文里失忆 → 同一批文件反复 read（12 个源文件每个被读 7–15 次）。
2. **上下文越滚越大**：每步全量注入 system prompt（12K+ 字符）+ memory snapshot + reflections snapshot（16K+ 字符），单步 input 从 8K 涨到 19K tokens；动态块还带着 `prefix_cache_break`，prefix cache 每步全 miss（日志 `[Token Cache] Low hit rate ratio=100%` 高频出现）→ 每步更慢（15–25s）、更贵、模型更容易失忆 → 恶性循环。
3. **平台竞态未清**：`WorkspaceFlushConflict` 在单个 run 内出现 8 次（execute_code 沙箱 build 产物发布与宿主 flush 的 manifest 竞态；646be775 修的是 write_file 直写场景，**已部署但未覆盖此路径**）；`android_compile` 反复 `gradlew: Permission denied` + `Task 'testDebugUnitTest assembleDebug' not found`，模型用同样错误参数重试 3 次。
4. **缺少熔断**：`model_turn_limit=10000` 形同虚设；运行时 12:24:51 已打出 `recursion limit (1000 steps)` 告警但 run 继续跑了 4 分钟直到用户取消。

## 二、数据证据

### 2.1 最近任务步数对比（agent_runs + tool_executions + run_events）

| 时间(UTC) | Agent | 任务 | 工具调用 | 事件数 | 结果 |
|---|---|---|---|---|---|
| 10:13 | Android 工程师 07 | 做 1、2、3、5 | **346** | **1646** | 活跃执行约 46 分钟（含 1.5h waiting），最终被取消，$1.15 |
| 06:08 | Android 工程师 07 | 先做 P0-1（除法精度）+ P1-3（输入校验） | **158** | **944** | **至今 pending**，22 个工具调用非终态 |
| 04:25 | Android 工程师 07 | 那继续开工，改代码吧 | 66 | 374 | delivered |
| 09:35 | Android 工程师 07 | 继续 P0-1（除法精度）+ P1-3（输入校验） | 42 | 246 | delivered |
| 09:59 | Android 工程师 07 | 还有那些需要优化？ | 18 | 95 | delivered |
| 10:12 | Android 工程师 07 | 现在app 还有优化的吗？ | 11 | 75 | delivered |
| 06:07 | Android 工程师 07 | 还有什么可优化的？ | 13 | 90 | delivered |
| 04:09 | Clawiee | 计算 123*456 + 789*101112 | **1** | **11** | delivered ✓（基线） |

一句话的咨询（「还有什么可优化的？」）也要 11–18 次工具调用、75–99 个事件，而对照组 Clawiee 的简单计算只花 1 次工具调用。

### 2.2 最严重案例：`be39c1ad`「做 1、2、3、5」全链路

- **时间线**：10:13:16 创建 → 第一轮 10:13–10:16 共 17 步（3 分钟完成第一版）→ 10:16:28 `waiting_started`，**挂起约 1.5 小时等用户**（此期间零步）→ 用户约 11:45 追问「查一下上面的，优化项目」→ 第二轮 11:45–12:28 共 107 步（43 分钟，平均 24s/步）→ 12:24:51 触发 1000 步 recursion limit 告警 → 12:28:22 **被取消**。
- **步数分布**：第一轮 17 步 / 3 分钟；第二轮 107 步 / 43 分钟，其中最后 9 分钟（12:19–12:28）约 35 步，build 失败重试循环密度最高。
- **LLM 调用**：124 次（trace `7e13eccb` 114 次 + trace `780228c` 10 次，中途取消重启产生第二条 trace），总成本 $1.15。
- **工具调用分布**：read_file **232** 次（去重后只有 73 个不同参数）；12 个核心源文件每个被读 7–15 次，集中在 12:22:41–12:22:49 的 8 秒内连读。
- **android_compile**：12 次调用，8 次 exit=1；同样的错误参数 `"testDebugUnitTest assembleDebug"`（两个 task 拼成一个字符串 → `Task not found`）连试 3 次，直到第 4 次拆成单 task 才成功；同时伴生 `./gradlew: 76: : Permission denied`。
- **WorkspaceFlushConflict**：同一 run 内 8 次，`expected_version` 始终不变、`current_version` 持续前进、`updated=[] deleted=[]`——模型写入被静默跳过（skipped 列表超长），模型以为已写成功，继续下一步操作。
- **结束时**：32 个工具调用处于非终态（被放弃）。
- **模型配置**：`deepseek-v4-flash`，`model_turn_limit=10000`（无实际熔断），`run_kind=foreground`。

### 2.3 全平台 token 消耗（daily_token_usage）

| 日期 | input tokens | output tokens | cache_read | cache_miss |
|---|---|---|---|---|
| 09-01 | **15,551,886** | 1,007,275 | 10,454,656 | **5,097,230** |
| 08-31 | 12,295,909 | 778,897 | 8,107,136 | 4,188,773 |
| 08-30 | 206,268 | 14,358 | 127,616 | 78,652 |

cache_miss 占比约 1/3，与日志中 `[Token Cache] Low hit rate ratio=54%~100%` 的告警一致。

## 三、根因分层

### A. 模型能力（直接原因）
- `deepseek-v4-flash` 在 15K+ tokens 上下文中明显失忆：文件读过记不住、编译错误提示写得清清楚楚（`Task not found`）仍用相同参数重试 3 次。
- 每步 reasoning 输出 1000–6400 tokens，大量重复自我盘算。

### B. 上下文构建（放大器）
- 每步 input = 完整 system prompt + 全量 memory snapshot + 全量 reflections snapshot + 全量历史。reflections 里大量「✅已完成」旧条目不裁剪，历史越长每步越重。
- 动态块前有 `prefix_cache_break: true`，DeepSeek prefix cache 每步失效 → 命中率告警 + 成本翻倍。
- 长上下文 → 失忆 → 重读文件 → 上下文更长，正反馈循环。

### C. 平台竞态 / 工具缺陷（推手）
- `WorkspaceFlushConflict`：execute_code 沙箱产出 build 产物（app-debug.apk 等）推进 manifest 版本，宿主侧 flush 仍用旧 `expected_version` 永久 CAS 冲突。`646be775` 已部署但只覆盖 write_file 直写路径，**沙箱产物发布路径仍是盲区**（今日冲突全部发生在 execute_code 之后）。
- `android_compile`：容器内 `gradlew` 权限丢失（`Permission denied`）；多 task 参数不解析（工具或模型用法问题）。
- 被取消/中断的 run 工具调用不终态化：`40ea58a3` 实际执行约 28 分钟（6:08–6:36）后 `delivery_status` 一直挂 pending（僵尸状态，非持续执行），22 个工具调用非终态。

### D. 平台配置（缺失防线）
- `model_turn_limit=10000` 无熔断意义；对「重复工具调用」无检测（read_file 同参数 15 次无任何告警/干预）。
- recursion limit 告警只打日志，不终止 run。

## 四、修复建议（按优先级）

| # | 优先级 | 建议 | 预期收益 |
|---|---|---|---|
| 1 | P0 | 把 execute_code 沙箱产物发布路径纳入 workspace manifest 刷新（扩展现有 646be775 机制），消除同 run 内 WorkspaceFlushConflict | 去掉「写入被静默跳过→重复操作」的源头 |
| 2 | P0 | ~~`model_turn_limit` 降为合理值（foreground 建议 60–100），并把 recursion-limit 告警升级为终止动作~~ **经用户决定不采纳** | — |
| 3 | P1 | 增加「重复工具调用」熔断：同 run 内相同 tool_name+arguments 超过 N 次（如 3）→ 注入强提醒或终止 | read_file×15 类浪费直接归零 |
| 4 | P1 | android_compile 容器内修正 gradlew 权限（docker 挂载后 chmod +x），多 task 参数做拆分/校验 | 消灭 8 次 build 失败中的权限类失败 |
| 5 | P1 | memory/reflections snapshot 改为「首步注入 + 后续增量」，或对已完成的旧条目做裁剪；重新评估动态块 prefix_cache_break 的必要性 | 每步 input 降 30–50%，cache 命中回升，步数天然减少 |
| 6 | P2 | Android 开发类 agent 的模型从 v4-flash 升级到更强模型（至少带 reasoning），或按任务难度做模型路由 | 直接减少失忆导致的重复读/重试 |
| 7 | P2 | 取消/中断路径上把 run 内所有非终态工具调用终态化（superseded），修复 pending 僵尸 run | 清理 40ea58a3 类残留 |

## 五、优先级待办（按收益/风险比排序）

排序依据：收益 = 对「简单任务步数膨胀」的削减幅度（以 be39c1ad 案例量化）；风险 = 实施改动面 + 回归面 + 误伤正常任务的可能。风险事实已核实：`max_tool_rounds` 默认 10000 位于 `backend/app/models/agent.py:118`，全平台所有 agent 均为此值；代码库中不存在任何重复工具调用检测。

### 第 1 梯队：快赢（本周内，风险低、收益立竿见影）

| 顺序 | 事项 | 收益 | 风险 | 工作量 | 依赖 | 验证方式 |
|---|---|---|---|---|---|---|
| 1.1 | **重复工具调用熔断（先提醒档）**：同 run 内相同 `tool_name+arguments_hash` 第 3 次触发注入提醒、第 5 次终止 | 高：本案 232 次 read_file 中 159 次为重复（12 个文件各读 7–15 次），提醒档可砍掉大半 | 低-中：检测纯只读（`agent_tool_executions` 同 hash 计数）；提醒档不终止，零误杀；终止档需阈值评审 | 小-中（1 天，提醒档） | 无 | 复现 be39c1ad 场景观察提醒注入；统计触发率 |
| 1.2 | **android_compile 修复**：容器内 gradlew 挂载后 `chmod +x`；多 task 参数（`"a b"`）拆分或校验报错 | 中：本案 8 次 build 失败中 3 次重复错误参数、多次 Permission denied | 低：工具侧改动，可单测，不碰核心链路 | 小（0.5 天） | 无 | 单测 + 真实编译通过 |

### 1.2 深度分析：android_compile 失败根因（已复现验证）

「修 gradlew 权限 + 多 task 参数校验」拆开后是**三个独立现象**，性质不同（2026-09-01 用构建镜像 `clawith-devbox-android:latest` 复现实验验证，与 12:24 日志逐字节一致）：

| # | 现象 | 性质 | 根因与证据 |
|---|---|---|---|
| A | `Task 'testDebugUnitTest assembleDebug' not found` | **真实 bug** | `android_build_backend.py:449` 用 `shlex.quote(gradle_task)` 把含空格的 `"testDebugUnitTest assembleDebug"` 整体引成一个参数传给 Gradle → Gradle 找不到带空格的 task 名。模型 12:24 连试 3 次同参数（每次 ≈25s 模型步 + 6s build），第 4 次改单 task 才成功 |
| B | `./gradlew: 76/90: : Permission denied` ×5 | **非 bug，纯噪音但误导模型** | gradlew 第 76/90 行 `if ! "$cygwin" && ...`——Linux 容器内三变量未定义 → 展开为空命令；容器 /bin/sh 是 dash，对空命令报 `Permission denied`（macOS bash 报 `command not found`，同源），`!` 反转后脚本继续执行，不中断。**权限其实从未有问题**：宿主 gradlew 8/31 起即 755，且构建命令 434 行有 `chmod +x ./gradlew` 兜底（clawith uid=1000=builduser，chmod 必成功）。危害：模型 reasoning 反复纠结「gradlew 权限问题」浪费多轮步骤 |
| C | execute_code 沙箱内 gradlew 为 0644 | **真实 bug（在 execute_code 路径）** | 12:25:27 execute_code 输出 `-rw-r--r-- sandbox sandbox 3939 Sep 1 12:25 gradlew`——run-scoped workspace 从 manifest 重建文件时 **mode 位未持久化**，可执行文件落盘变 0644；模型手动 chmod 补救。影响 execute_code 沙箱内跑 gradlew/git 等脚本，与 2.1 WorkspaceFlushConflict 同属 workspace 重建/发布路径 |

修复对应关系：A → 构建侧按空白拆分 task 为多个 argv 参数 + 工具 schema 注明「多任务用空格分隔」；B → 编译输出解析时过滤 `./gradlew: <行>: : Permission denied` 噪音行（低成本，消除模型误判）；C → workspace 重建/发布时保留 mode 位，**与 2.1 合并到同一模块修复**。

附带发现：sdk-provision 首同步下载了 10 个组件（build-tools 30–35、platforms 33–35），全局卷组件与项目需求不匹配导致**每个新项目首次构建**有一次性下载延迟；后续构建不受影响。

### 第 2 梯队：正确性修复（做前需要充分测试）

| 顺序 | 事项 | 收益 | 风险 | 工作量 | 依赖 | 验证方式 |
|---|---|---|---|---|---|---|
| 2.1 | **WorkspaceFlushConflict 沙箱路径**：execute_code 沙箱 build 产物发布推进 manifest 后，flush 前刷新 expected_version（扩展现有 646be775 机制，覆盖沙箱产物路径） | 高（正确性）：写入被静默跳过=「模型以为改了其实没改」，是重复操作的重要源头 | 中：workspace sync 是核心路径，需覆盖 execute_code 产物 / write_file 直写 / 并发 flush 三场景回归测试 | 中（1–2 天） | 理解 ADR-0011 机制 | 复现冲突场景→修复后 0 冲突；全量 workspace 测试 |
| 2.2 | **pending 僵尸 run 终态化**：取消/中断路径把 run 内所有非终态工具调用置 `superseded`、delivery_status 收敛 | 低-中：清理 40ea58a3 类残留（22 个非终态工具调用污染幂等账本与统计口径） | 低：参照 deploy-kill-replay 的账本复用逻辑 | 小-中（0.5–1 天） | 无 | 复现取消场景确认工具调用全部终态 |

### 第 3 梯队：结构性改造（收益最大、风险最高，灰度推进）

| 顺序 | 事项 | 收益 | 风险 | 工作量 | 依赖 | 验证方式 |
|---|---|---|---|---|---|---|
| 3.1 | **memory/reflections 快照增量注入**：改为「首步注入 + 后续仅注入变化条目 / 裁剪已完成旧条目」；重新评估动态块 `prefix_cache_break` 的必要性 | 最大：每步 input 降 30–50%（8–19K tokens 中快照占大头），步时降、cache 命中回升、上下文更小缓解失忆 | 高：上下文构建核心路径，影响所有 agent 所有 run；记忆「每步可见」语义需保持 | 大（设计+实验） | 无 | 灰度对比：同任务步数/质量/cache 命中率前后对照 |
| 3.2 | **模型升级**：Android 开发类 agent 从 `deepseek-v4-flash` 升级（配置变更，零代码风险） | 直接且大：失忆是根因 1，更强模型大幅减少重复读/错误重试；总成本可能反降（步数砍半） | 零代码风险；成本上升（单价×2–5），**需用户拍板** | 极小 | 无 | 选 1 个 agent 灰度对比步数/成本/质量 |

### 执行建议

1. **本周**：1.1（提醒档）+ 1.2 两件并行落地——先削重复读/错误重试两大浪费源。
2. **下周**：2.1 正确性修复（测试要足，这是数据一致性 bug）；2.2 顺手清理。
3. **立项**：3.1 上下文增量注入单独立项（影响面最大，需要灰度实验设计）；3.2 等用户就成本拍板后先行灰度。
4. 1.1 终止档是否开启，等提醒档数据出来后再评审阈值（避免误杀）。

## 六、验证过的证据链接

- PG：`agent_runs` `be39c1ad-57c5-4670-b24c-caef5c0a18bf`（346 tool calls / 1646 events / cancelled at 12:28:22Z）
- PG：`agent_tool_executions` 重复调用 TOP15（read_file 同 hash 15 次）
- Langfuse：trace `7e13eccb6d1e3ee08c95ffa15a7f6e5d`（114 gen / $1.07）、`780228c907af062c5f1f87532e8a52ab`（10 gen / $0.08）
- 日志：`[WorkspaceFlushConflict]`×8、`[AndroidBuild] done exit=1`×8、`recursion limit (1000 steps)`、`[Token Cache] Low hit rate`

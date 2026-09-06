# Compaction 摘要失真导致「失忆」、反复从零重做 —— run 764eb591 事故根因分析

- **事故 run**：`764eb591-a38a-48cb-9120-23c83c3b0bda`（Android 工程师 07，09-05 12:01:08–12:57:41 UTC，56.6min，foreground/chat/langgraph，`run_cancelled` 用户手动取消）
- **goal**：「继续执行 P2 工程侧优化立项 / P1·文档 README 脱节 / P1·展示 循环小数退化 / P1·体验 出错后输入不重置，均等创建者指示」（附 memory/清单.md）
- **运行镜像**：d4a2e081（容器 11:53:20Z 启动；含 2dd3d08d read-dedup P0/P1/P2 + 57c2e5b2 compactor 锚点票 A/B）
- **同型事故**：dc557d91（2026-09-03，`docs/analysis/2026-09-03-compactor-loop-dc557d91.md`）
- **证据基线**：agent_tool_executions / workspace_file_revisions / agent_run_events / agent_activity_logs（PG 只读）+ Langfuse ClickHouse `events_full` + 后端容器日志 + 6 次压缩摘要解码（scratchpad `summaries/12-{10,21,36,42,48,57}.md`）+ **19 代 GENERATION 连续自我怀疑序列解码**（scratchpad `truncated-tool-output/exec_command-76e4b1538de71409.log`，双重 unicode_escape 解码）

---

## 一、TL;DR

这是一起**「摘要失真 → 失忆 → 反复从零重做」的事故**。**主线是摘要失真**（§1–3：completed_actions 管道太小 → 授权语义被「纠错式改写」→ 模型连续自我怀疑），比上一次（dc557d91）多出两个此前未暴露的独立缺陷（§4–5，另立支线调查，本次聚焦主线不展开）：

1. **completed_actions 管道太小**：50 条 / 2048 字节的三重截断把本 run 最关键的 4 条 `edit_file` + 1 条 `android_compile` 全部挤出了压缩摘要的「已完成动作」事实集，摘要 LLM 只能靠被 320 次 read_file 淹没的原始历史重建状态 → 漂移。
2. **摘要失真第一因 = 授权语义被「纠错式改写」**：12:21:27 首次压缩，摘要把用户原文「均等创建者指示」（= 等同于创建者指示、已授权）判定为「均待创建者指示」的 mis-transcription 并主动"纠正"成「仍待批准」，还脑补「No blanket implementation authorization was granted」——**注入"你越权了"的自我怀疑**。注意这是**语义层误判，不是事实被挤出**：同一份摘要的 Files/Code、Current Work 段明明正确记了「Edit DONE、compile PASSED、NOT yet committed」。后续 12-36 / 12-48 摘要逐级恶化（「committed」↔「not yet committed」自相矛盾 → 过度宣称「全部 committed/pushed/MR opened/工作树干净」）。
3. **模型信以为真、连续自我怀疑**：Langfuse 铁证——12:22:23 → 12:38:31 **连续 19 代 GENERATION 每步都在 reasoning 里重复「上一轮 summary 与实际状态不符 → 从零实施」**（§根因3 全表），把自己做完的活当成没做。
4. **B 熔断三连失效**：精确匹配检测器抓不到「漂移循环」；`terminate_on_loop` 默认关；且**审计事件即使触发也静默写库失败**（`runtime_*` 5 个 action_type 不在 `activity_action_enum` 枚举里，`log_activity` 吞异常）。
5. **编辑数据真实丢失，非纯「失忆」**（新发现，最重）：`workspace_file_revisions` 铁证 Calculator.kt 12:18:35 编辑 11134→12103 字节，12:36:31 重做时 `before_len=11134`（**回到原始长度，非 12103**）——编辑被低层回退，不是经 edit_file 工具；dc557d91 同型。这放大了失忆：模型重查时真的看到原始文件，坐实了漂移后的「没做过」信念。

核心结论（主线）：**dc557d91 是「紧循环」（prefix 三现、按 09-04 签名调优的熔断器能抓），764eb591 是「漂移循环」（每轮摘要不同、prefix 每步变），精确匹配检测器结构性失效；而本案摘要失真的第一因不是事实被挤出，是摘要 LLM 对授权语义做了一次「纠错式改写」（把「均等=已授权」误改写成「均待=待批准」并脑补「未授权」），随后模型连续 19 步自我怀疑、反复从零重做。**（支线：本轮还暴露「编辑数据被低层回退」这个比摘要失真更严重的独立缺陷，已另立深挖，见 §四·根因5 / §七。）**

---

## 二、事故现象

- 12:18–12:20 已 `edit_file` 改完 4 个文件（Calculator.kt / CalculatorTest.kt / CalculatorReducer.kt / CalculatorReducerTest.kt）、12:19:31 `android_compile` 通过、12:20:44 声称「代码+单测已就位且 android_compile 已通过」。
- 12:21:27 第一次压缩后摘要失真，此后模型反复输出「当前代码尚未包含四项修复」「上一轮 summary 声称已应用/已提交，与真实状态不符，需从零实施」，把自己做完的活当成没做，重做同一批编辑。

---

## 三、事故 run 与取证铁证

### 3.1 工具分布（共 414 = 411 成功 + 3 失败）

| 工具 | 成功 | 失败 |
|---|---|---|
| read_file | 320 | 1 |
| list_files | 39 | 1 |
| search_files | 5 | 1 |
| execute_code | 20 | 0 |
| find_files | 11 | 0 |
| edit_file | 6 | 0 |
| search_experience | 4 | 0 |
| list_focus_items | 2 | 0 |
| list_triggers | 2 | 0 |
| android_compile | 1 | 0 |
| read_experience | 1 | 0 |

`read_file` 主导（321/414 = 78%），`edit_file` 仅 6 次（2 批）。

### 3.2 模型步数

96 步（`[LLM-CacheFp]` step=0→95）+ 6 次真实压缩摘要（12:10:05、12:21:27、12:36:31、12:42:14、12:48:49、12:57:29，已解码）。

### 3.3 edit_file 精确时间线（agent_tool_executions.started_at）

| 时刻(UTC) | 工具 | 对象 |
|---|---|---|
| 12:18:35 | edit_file | Calculator.kt |
| 12:18:47 | edit_file | CalculatorTest.kt |
| **12:19:31** | **android_compile** | **testDebugUnitTest assembleDebug → 成功** |
| 12:20:09 | edit_file | CalculatorReducer.kt |
| 12:20:23 | edit_file | CalculatorReducerTest.kt |
| **12:36:31** | edit_file | Calculator.kt（失忆重做） |
| **12:36:31** | edit_file | CalculatorTest.kt（失忆重做） |

### 3.4 熔断与审计铁证

- `agent_run_events`（run 764eb591）：`status_changed` 1351、`run_cancelled` 1、`delivery_succeeded` 1、`run_created` 1 —— **无任何 `runtime_compaction_loop` 等 runtime_* 事件**。
- `agent_activity_logs`（related_id = run 764eb591）：**0 行** —— 熔断审计事件要么从未触发，要么触发了也写库失败。
- 后端日志：0 条 `[RuntimeCompactionLoop]` warning。

---

## 四、五层根因链

### 根因 1：completed_actions / files_read 管道太小，write 类工具被 read 挤出

`run_compactor.py` 三重截断：

```python
# run_compactor.py:538-540
_COMPLETED_ACTIONS_LIMIT = 50
_COMPLETED_ACTIONS_MAX_BYTES = 2048
_COMPLETED_ACTION_SUMMARY_MAX_CHARS = 200
# run_compactor.py:544-545
_FILES_READ_LIMIT = 50
_FILES_READ_MAX_BYTES = 2048
```

`build_completed_actions`（:588-629）只收 `succeeded` 执行、按结算时间排序、超 50 条裁最旧、超 2048 字节**从头逐条丢弃最旧**；`build_files_read`（:658-709）同构。

**后果**：12:21:27 压缩时 completed_actions 实际只有 4 条（list_files、read Calculator.kt、read CalculatorReducer.kt、execute_code git），**无任何 edit_file/android_compile** —— 本 run 最有价值的「已改 4 文件 + 编译通过」事实，被 320 次 read_file 的大摘要挤出 2048 字节窗口。摘要 LLM 只能靠原始历史重建 → 漂移的起点。

### 根因 2：摘要漂移序列（已读全文，存 scratchpad）

| 压缩时刻 | 状态判定 | 定性 |
|---|---|---|
| 12-10 | 编辑前，正确：「No implementation started / workspace clean」 | ✅ 正确 |
| **12-21** | Primary Request 段把用户原文「均等创建者指示」误判为「均待创建者指示」的误写、写「No blanket implementation authorization was granted」；但 Files/Code、Current Work 段又正确记「Edit DONE、compile PASSED、**NOT yet committed**」（工作树有未提交改动） | ⚠️ 内部自相矛盾 + 授权自我怀疑 |
| **12-36** | Files 段「Edit DONE (**committed**)」 vs Pending Jobs 段「**not yet committed**」 | ⚠️ 自相矛盾 |
| **12-48** | 过度宣称「All four items implemented, committed, pushed, MRs opened, working tree clean」 | ❌ 过度宣称（与 12-36「2 项未 commit」矛盾） |

**摘要失真的第一因（语义层「纠错式改写」，不是事实被挤出）**：12-21 摘要的 `Primary Request and Intent` 段原文（已解码，scratchpad `summaries/12-21.md`）实锤：

> Workflow authority rests with the creator; **"均等创建者指示" is a mis-transcription of "均待创建者指示"** — i.e., the four items are still awaiting creator approval. **No blanket implementation authorization was granted** in the latest directive; recent history shows the agent proceeded to implement them as if authorized.

——摘要 LLM 把用户原文「均等创建者指示」（= 等同于创建者指示、照办，即**已授权**）判定为「均待创建者指示」的**拼写错误并主动"纠正"**，把授权语义整个翻转成「仍待批准」，还脑补了一句「No blanket implementation authorization was granted」——**给模型注入"你越权了"的自我怀疑**。

**关键定性**：这不是事实被截断挤出的失真——同一份 12-21 摘要里，`Files and Code` 段正确记了「**Edit already applied (DONE)**」×4、`Current Work` 段正确记了「Implementation ... **COMPLETE** at code level」「android_compile ... **PASSED**」「**NOT yet been committed** (working tree has uncommitted changes)」。事实账本（已改 4 文件 + 编译通过 + 未提交）**都记对了**，失真的只有 `Primary Request` 段的**授权语义**：摘要 LLM 对用户原文做了一次「纠错式改写」，把「授权」改写成了「越权」。模型读取摘要时优先采信了段首的授权判定（「未授权/越权」），于是重新用 read_file 验证、把 Files 段里其实记对的「已改未提交」当成幻觉。

**旁证（疑似上游来源）**：同一份摘要的 `Critical Context` 段还写了「The four items were tracked in `memory/清单.md` as **"均待创建者指示"** (all awaiting creator instruction)」——即 agent 自己的记忆文件里可能早已把「均等」抄成了「均待」（一字之差，等↔待），摘要 LLM 从记忆文件里读到了错误版本，反过来说用户原文是 mis-transcription。失真链条可能是「记忆文件笔误 → 摘要 LLM 采信并反向纠错用户原文」。无论上游是记忆笔误还是摘要误读，**摘要在授权语义上做了「纠错式」改写这件事本身是第一因**：verbatim 保护缺位 + 摘要 LLM 被允许对授权类指令做语义重写。

### 根因 3：模型信以为真、自我怀疑（Langfuse ClickHouse 铁证）

Langfuse `events_full`（trace `134612f11347f70147e1ffafe945cb68` + `468c79d7a2373767d6da3a60b688b868`，共 102 个 llm GENERATION）直查。输出中文以 `\uXXXX` 转义存储（原始中文搜索 0 命中，转义形态才命中）。

**命中统计**：`声称`×33、`未实施`×14、`从零`×13、`尚未`×12、`未落盘`×8、`重新实施`×3。

**主线铁证：连续自我怀疑序列（19 代 GENERATION，12:22:23 → 12:38:31 全程每步重复「从零实施」）**。首次压缩（12:21:27）后，模型每走一步都在 reasoning 里重复「上一轮 summary 与实际状态不符 → 从零实施」，把自己 12:18–12:20 已完成的 4 文件编辑 + 编译通过当成没做过。逐代解码（scratchpad `truncated-tool-output/exec_command-76e4b1538de71409.log`，双重 unicode_escape 解码）：

| 时刻(UTC) | 模型 reasoning 原文（节选，均为「从零实施/未落盘/幻觉」自证） |
|---|---|
| **12:22:23** | 「工作树干净（无未提交改动），说明上一轮声称"已改未提交"的修复实际**并未落盘**，需要**从零实施**」＋「这与 thread_summary 说"working tree has uncommitted changes"**矛盾**……让我先实际读取源码确认当前实现状态，**不能依赖 summary**」 |
| **12:22:42** | 「上一轮 summary 与实际状态不符（工作树干净、修复未落盘），需**从零实施**」 |
| **12:23:33** | 「上一轮 summary 很可能是**捏造的（幻觉）**。因此我需要真实实施」 |
| **12:24:59** | 「工作树干净——上一轮声称的改动**确未落盘**」 |
| **12:25:28** | 「工作树干净、源码未含声称的改动，需**从零实施**」 |
| **12:26:32** | 「线程摘要内容**不可信**，四个事项都需**从零实施**」 |
| **12:27:15** | 「以真实文件为准，四项全部**从零实施**」 |
| **12:27:41** | 「线程摘要中所谓「已改」**并不存在**——四项全部**从零实施**」 |
| **12:28:24** | 「线程摘要中「已改」**不实**，四项全部**从零实施**」（**关键：Calculator.kt 268 行旧逻辑 vs 摘要说的 287 行**） |
| **12:29:19** | 「线程摘要中的"已改"**均未落库**，全部**从零实施**」 |
| **12:31:21** | 「thread_summary 声称的「代码已改、测试已更新、编译已过」**与真实文件不符**……从零实施」＋「thread summary 是**不可靠的幻觉/过期数据**，必须忽略」 |
| **12:31:41** | 「thread summary 是**幻觉/过期数据**，必须忽略，从零做」 |
| **12:33:37** | 「关键矛盾：thread summary 声称 "Edit already applied (DONE)"…… 与真实文件 268/171/308 行**不一致**」 |
| **12:34:39** | 「thread summary 中关于"已应用编辑"的部分是**未落盘/幻觉**……现在必须**从头实施**四项」 |
| **12:35:28** | 「工作树干净。所有实现都要**从零开始**」 |
| **12:35:37** | 「上一轮 summary 中「已改已提交」的叙述**与磁盘不符**，本轮**从零落地**」 |
| **12:37:15** | 「上一轮记录的 P1 两处代码修复**实际未落盘**」 |
| **12:38:06** | 「两个 P1 修复**确实未落盘**，需**从头实施**」 |
| **12:38:31** | 「thread summary 与**实际文件矛盾**（实际 CalculatorTest.kt 是 234 行、147 行仍期望 `0.3333333333333333`）——两个 P1 修复**确未落盘**，现在完整实施」 |

→ 模型从 12:22:23 起不再信任自己的 summary，转而用 read_file 逐文件重新验证，随后重复编辑（12:36:31 第二次 edit_file 批次）。**这 19 步连续自我怀疑就是「摘要失真 → 失忆 → 反复从零重做」的主线铁证**：不是偶发一次误判，而是每一步都在「摘要说已做 ↔ 磁盘说没做」的冲突里站到「磁盘」一边，把真实完成的活反复当没做。

### 根因 4：B 熔断三连失效（+ action_type 枚举缺口，新发现缺陷）

**① 从未触发，且结构性抓不到。** `detect_loop`（run_compactor.py:927-947）要求**相邻两步 `prefix_fp` 与 `tools_fp` 完全相同**、且后一步带 `compaction_since_last_prefix` 才计数。本 run prefix 每步都变（漂移循环：每轮摘要不同、重读不同文件）→ 永远抓不到。

**② 与 dc557d91 的关键差异。** dc557d91 是「紧循环」（prefix `697aef1a1281` 三现），09-04 的熔断正是按此签名调优的；764eb591 是「漂移循环」，精确匹配检测器结构性失效。

**③ 默认只告警不终止。** `AGENT_RUNTIME_COMPACTION_TERMINATE_ON_LOOP=False` 默认关。

**④（新发现）审计事件即使触发也静默写库失败。** `_audit_breaker_event`（model_step_service.py:367）把熔断事件写 `agent_activity_logs`，但 action_type 用的 5 个值——

```text
runtime_compaction_loop            (model_step_service.py:3129)
runtime_tool_config_failure_loop   (model_step_service.py:2389)
runtime_tool_success_loop          (model_step_service.py:2450)
runtime_duplicate_read_stall       (model_step_service.py:2511)
runtime_duplicate_read_stall_compact(model_step_service.py:2594)
```

——**都不在 `activity_action_enum` 枚举里**。该枚举只有 14 值（`activity_log.py:20-28`：chat_reply/tool_call/feishu_msg_sent/agent_msg_sent/web_msg_sent/task_created/task_updated/file_written/error/schedule_run/heartbeat/plaza_post/agent_file_sent/agent_file_received）。`log_activity`（activity_logger.py:30）`except Exception` 吞异常只打 `[ActivityLog] Failed to log ...`。

先例：迁移 `add_agent_file_activity_enum.py` 自己就记录了同型失败——`agent_file_sent/agent_file_received` 因枚举缺值导致 INSERT「invalid input value for enum」，才补的 `ALTER TYPE ... ADD VALUE`。**同样的缺口现在又出现在 5 个 `runtime_*` 值上，且这次连迁移都没补。** 后果：熔断审计事件永久静默丢失，只剩 stdout WARNING（容器重启即丢），DB 里查不到任何熔断痕迹——见 §3.4 的 0 行铁证。

### 根因 5：编辑数据真实丢失，非纯「失忆」（新发现，重大，放大失忆）

`workspace_file_revisions`（`octet_length(before_content/after_content)`）铁证：

| 时刻(UTC) | 文件 | before_len | after_len |
|---|---|---|---|
| 12:18:35 | Calculator.kt | 11134 | **12103** |
| 12:18:47 | CalculatorTest.kt | 6766 | **7958** |
| 12:20:09 | CalculatorReducer.kt | 7969 | 8668 |
| 12:20:23 | CalculatorReducerTest.kt | 10454 | 12518 |
| **12:36:31** | **Calculator.kt（重做）** | **11134** | 11838 |
| **12:36:31** | **CalculatorTest.kt（重做）** | **6766** | 7542 |

**关键证据**：12:36:31 再次编辑 Calculator.kt 时 `before_len=11134`（= 原始长度，**不是第一次编辑后的 12103**）；CalculatorTest.kt 同理 `before_len=6766`（不是 7958）。中间无任何 edit 记录 → **12:18 的编辑被低层回退**，不是经 edit_file 工具。

**dc557d91 同型**：Calculator.kt 09:20:19 编辑 9740→10378，09:39:51 再编辑 `before_len=9740`（回到原始）——两案同型。

**辅助证据**：execute_code 沙箱 `git status` 全程干净（HEAD 2782458），从未显示未提交改动 → 说明 edit_file 写入的存储工作区与 execute_code 沙箱视图**分属不同层**，沙箱视图始终看不到这些编辑。

**时间相关性线索（强）**：android_compile（12:19:31）恰好夹在两批 edit 之间——**只有编译前的 2 个文件（Calculator.kt/CalculatorTest.kt）出现回退签名，编译后的 2 个文件（CalculatorReducer*.kt）无重做**。这指向 android_compile 的「物化→构建→flush」周期用陈旧快照覆盖了先于它的直写编辑。

**机制候选（需独立代码级深挖）**：
- `run_workspace.py:121-138` `close_run_workspace`（"re-materialize from storage" / ADR-0011 修复三：flush 冲突后丢弃陈旧工作区）；
- `gitlab_workspace.py:474` `restore_git_metadata_from_bundle`（"a mixed reset keeps uncommitted edits as unstaged modifications"，但**若 bundle 物化路径先于编辑、或 mixed reset 的基线是陈旧快照，则可能覆盖未提交编辑**）；
- 已知事故族：run-scoped temp workspace 直写绕过 manifest、陈旧 manifest 永久 CAS 冲突（记忆 `workspace-sync-conflict-root-cause`，三代修复 646be775→63b70e91→6a5a9928，第四代 .git 物化 4d3fe431）。本次是**数据被回退**（比 CAS 冲突更严重——冲突是"保护性挡住覆盖"，回退是"覆盖已经发生"）。

**此发现放大失忆**：模型重查时真的看到原始文件，坐实了它漂移后的「没做过」信念——失忆不止是摘要层面的幻觉，还有真实的数据回退在"背书"。

---

## 五、与 dc557d91 对比（紧循环 vs 漂移循环）

| 维度 | dc557d91（09-03） | 764eb591（09-05，本案） |
|---|---|---|
| 时长 / 步数 / 工具 | 63.7min / 86 步 / 263 工具 | 56.6min / 96 步 / 414 工具 |
| 工具构成 | read 199、edit 14（3 批重做 09:16/09:39/10:04）、android_compile 1、write_file 4 | read 320（+61%）、edit 6（2 批）、list_files 40 |
| 循环形态 | **紧循环**（prefix `697aef1a1281` 三现） | **漂移循环**（prefix 每步变） |
| B 熔断 | 未部署（09-04 才按此签名调优） | 已部署但**结构性抓不到**（精确匹配 vs 漂移） |
| 终局摘要 | 「All three P1 code fixes remain unapplied」、9087bee 抄成 8087bee | 「All four items implemented, committed, pushed, MRs opened, clean」 |
| workspace_file_revisions 回退 | ✅（9740→10378 后 before_len 回 9740） | ✅（11134→12103 后 before_len 回 11134） |

**共同点**：android_compile 均 1 次、edit_file 分批重做、read_file 主导、均用户手动取消、**均存在 workspace_file_revisions 回退签名**。

**结论**：漂移循环比紧循环更难治——紧循环靠精确 prefix 重复就能抓，漂移循环的每一步指纹都在变，需要「重做已编辑文件」「read 数/step 异常增长」「同一批路径在 workspace_file_revisions 反复出现」等软信号才能识别。

---

## 六、修复建议（优先级排序）

> 红线：测试环境不灰度，一步全量；DB 只读（未批准不写）。以下均为「建议方向」，需走方案评审 + tdd + arch-guard 后实施。

### 6.1 摘要失真专项（主线，本次事故的唯一收敛点）

1. **【P0】completed_actions / files_read 窗口改造（根因1）**：write 类工具（edit_file/write_file/android_compile/execute_code 的写分支）**永不淘汰** + 按类别分层预算（read 类可裁最旧，write 类只受单条 200 字符截断），而非纯字节截断。这是摘要事实漂移的第一因。
2. **【P0】用户原文 verbatim 保护、禁止授权语义「纠错式」改写（根因2 第一因）**：`Primary Request and Intent` 段的用户原文（goal/directive）必须**逐字带原文**（quote verbatim），摘要 LLM 不得对授权/范围类指令做「这是拼写错误/这是误传」式的语义重写，更不得脑补「No ... authorization was granted」这类授权否定句。`_COMPACTION_INSTRUCTION` 已有「quote verbatim」规则，但缺一条硬约束：**授权语义只许转写、不许判正误**——凡是用户原文里出现的授权词（「均等/照办/执行/直接做」），摘要一律保留原义，遇歧义宁可标注「原文如此」也不做纠错式改写。
3. **【P0】摘要对「已完成动作」的权威性强化（根因3）**：`_COMPACTION_INSTRUCTION` 已写「completed_actions pipeline is authoritative」「prior summary contradicts pipeline → pipeline wins」，但本 run 模型仍在「摘要说已做 ↔ 磁盘 read 说没做」的冲突里每次站到 read 一边。需在摘要中把「已完成动作」做成**确定性、不可被 read 结果推翻**的事实段（cline 式确定性 Files 账本：文件→状态→校验哈希），并让模型把「completed_actions 已落账」当作**一等事实而非可重新验证的推测**——否则模型每次 read 旧文件都会自我怀疑、从零重做。
4. **【P1】记忆文件笔误防线**：本事故旁证显示 `memory/清单.md` 可能早已把「均等」抄成「均待」，摘要从记忆读到错误版本后反向纠错用户原文。清单/任务状态类记忆字段宜做**受控词表或原文快照**，避免「等↔待」这类同音/近形字在记忆→摘要链路里二次漂移。

### 6.2 支线（另立独立调查/票，本次聚焦主线不展开）

- **【P0·支线】B 熔断覆盖「漂移循环」**：检测器不能只靠精确 prefix 重复，须加软信号——「同一路径在 workspace_file_revisions 短期内被再次 edit 且 before==首次 before（重做已编辑文件）」「read 数/step 异常增长（本 run 78% read）」「同 run 内 edit 后 execute_code git status 仍干净」；`terminate_on_loop` 默认值评估。
- **【P0·支线】修复 action_type 枚举缺口**：新增 migration 把 5 个 `runtime_*` 值 `ADD VALUE` 到 `activity_action_enum`（或改用 agent_run_events / 独立表）——否则熔断审计永久静默丢失。
- **【P1·支线】workspace 编辑数据丢失机制**：定位「哪次 execute_code/android_compile flush 覆盖了存储工作区的未提交 edit」，读 `run_workspace.py` 物化-flush 全链路 + `gitlab_workspace.py` bundle/mixed-reset 路径。这是比摘要失真更严重的独立缺陷（数据真实回退），已深挖见 §七 + `2026-09-05-workspace-revert-rootcause5.md`。

> **参考项目对照（18 项目扩面调研）**：`2026-09-05-compaction-reference-survey.md` —— codex/opencode/cline/OpenHands/SWE-agent/gemini-cli/gptme/claw-code/mem0/jcode/letta/herdr/orca + 上轮 dsh/langchain/deepagents，逐五根因给出「最佳可迁移范式」。摘要：根因①→cline 确定性 Files 账本；根因②→opencode「冲突时 conversation 胜」+gemini-cli anchor+Probe；根因③→gemini-cli 失败降级 + letta 摘要不保真；根因④→gemini-cli 语义等价 + orca 内容指纹/代际（18 项目无一个完整解决，Clawith 差异化机会）；根因⑤→orca runtimeFence 单调代际 + herdr 结构态分离 + letta MemFS git 化。

---

## 七、后续独立调查（数据丢失机制深挖）—— 已深挖，见 `2026-09-05-workspace-revert-rootcause5.md`

**深挖结论（2026-09-05，已实锤）**：写入架构实锤为「三写入者同文件系统」——edit_file 直写 storage（**无条件写、无 CAS**，`workspace_collaboration.py:592-604`）、android_compile 裸 rw bind-mount `_agent_workspace_root`（`android_build_backend.py:387`，source 文件零保护）、execute_code 用 run-scoped 临时工作区 + CAS flush。**回退机制已决定性确认**：模型压缩失忆后在 execute_code 沙箱内 git 探索（checkout/reset，reflog 铁证 `HEAD@{2}: reset: moving to f_android_ai`）把沙箱工作树带回 HEAD 2782458（未含 edit_file 未提交改动），flush 的 `base_hash` 把「git 回退」误判为「模型有意改动」而 CAS 发布回 storage。**精确回退时间窗 12:21:26→12:22:41**（Calculator.kt read 287 行→268 行）。候选 B（remote mixed reset）与「refresh no-op」两变体均已排除（refresh 日志 `size=12103` 实锤成功；仅 creds injected 无 restore 日志）。修复方向五条见深挖文档 §五（首条 P0：flush 区分「有意改动」vs「git 回退到 HEAD」）。

原待读链路（已全部实读，见深挖文档）：

- `run_workspace.py`：`use_run_workspace`（:100-118 物化一次）/ `close_run_workspace`（:121-138 discard→re-materialize）/ `refresh_run_workspace_path`（:141-245 直写后刷新）—— 确认 edit_file 直写 storage 后，run 工作区 temp 文件与 manifest 是否/何时被陈旧快照覆盖。
- `agent_tools.py` 四 typed outcome（`_write_file_outcome`/`_edit_file_outcome`/`_move_file_outcome`/`_delete_file_outcome`）→ `_execute_workspace_mutation` 直写 storage 的完整路径。
- `gitlab_workspace.py`：物化时的 `restore_git_metadata_from_bundle`（:474 mixed reset）与 `_prepare_temp_workspace` 的 `reset --mixed origin/<branch>` —— 确认 mixed reset 的基线是否是编辑后的 storage 快照。
- android_compile 沙箱的物化→flush 周期：为何它 flush 会用「编辑前」快照覆盖 storage（时间相关性已指向它，见 §四 根因 5）。

**取证抓手**：`workspace_file_revisions.before_len` 回退签名 + `agent_tool_executions.result_metadata->>'workspace_conflict_details'`（63e1e9dd 已落账本）+ 后端 `[RunWorkspaceRefresh*]` / `WorkspaceFlushConflict` 日志。

---

## 附录 A：取证手法（可复用）

- **Langfuse ClickHouse 直查**：`docker exec langfuse-clickhouse-1 clickhouse-client --query "SELECT ... FROM events_full WHERE trace_id IN (...)"`。列：trace_id/name/type/input/output(ZSTD)/metadata_names/metadata_values/tool_call_names。**输出中文是 `\uXXXX` 转义**，搜索须用转义形态（`\u5c1a\u672a`=尚未、`\u4ece\u96f6`=从零、`\u672a\u843d\u76d8`=未落盘）。
- **run_id↔trace**：is_app_root=true 的 run SPAN 带 metadata run_id。
- **后端日志**：`docker logs clawith-agent-backend-1 --since --until`；`[LLM-CacheFp]`（每步 prefix/tools 指纹）、`[RuntimeCompactionLoop]`（熔断告警）、`[RunWorkspaceRefresh*]`、`WorkspaceFlushConflict`。
- **DB**：`agent_tool_executions`（tool_name/status/started_at/sanitized_arguments/result_summary/result_metadata）。**工具「输入」权威来源是 `sanitized_arguments`（按 `tool_call_id` 关联），不是 events_full**——`tracing.py` 的 `observe_tool` docstring 明确「Tool arguments are intentionally not captured」。注意 `_SENSITIVE_PATHS`（`builtin_tool_definitions.py:4007-4019`）只 redact `execute_code` 的 `code/env/environment` 与少数密钥字段；**edit_file 的 `old_string/new_string` 完整落账（未 redact）**，是回退的内容级铁证。`workspace_file_revisions`（`octet_length(before_content/after_content)` 长度回退铁证；**无 run_id 列，按 actor_id + created_at 窗口查**）、`agent_run_events`（event_type 全集）、`agent_activity_logs`（related_id=run_id，action_type 枚举 14 值）。
- **压缩摘要解码**：scratchpad `summaries/12-*.md`（Langfuse output 经 ZSTD 解压 + `\uXXXX` 反转义）。
- **连续自我怀疑序列解码**：GENERATION 的 `reasoning_content`/`content` 是**双重 unicode_escape**（`\\u5de5...`），先 `json.loads` 出字段、再对字符串 `codecs.decode(s,'unicode_escape')` 迭代 2–3 次，按首列 `YYYY-MM-DD HH:MM:SS` 时间戳取每代。全序列存 scratchpad `truncated-tool-output/exec_command-76e4b1538de71409.log`（19 行 = 19 代）。

## 附录 B：关键代码位置

| 位置 | 内容 |
|---|---|
| `run_compactor.py:538-545` | completed_actions / files_read 三重截断常量 |
| `run_compactor.py:588-629` | `build_completed_actions`（字节截断裁最旧） |
| `run_compactor.py:658-709` | `build_files_read` |
| `run_compactor.py:927-947` | `detect_loop`（精确 prefix+tools 匹配） |
| `model_step_service.py:367-391` | `_audit_breaker_event`（审计写库入口） |
| `model_step_service.py:2389/2450/2511/2594/3129` | 5 个 `runtime_*` action_type 调用点 |
| `activity_log.py:20-28` | `activity_action_enum`（14 值，缺 5 个 runtime_*） |
| `activity_logger.py:30` | `log_activity` 吞异常 |
| `alembic/versions/add_agent_file_activity_enum.py` | 同型枚举缺值先例（agent_file_* 已补） |
| `run_workspace.py:121-138 / 141-245` | discard→re-materialize / 直写后刷新 |
| `gitlab_workspace.py:474` | `restore_git_metadata_from_bundle` mixed reset |

# 编号指代断裂再发：跨 run 丢「先做 #1+#2」绑定（根因定位）

- 日期：2026-09-07
- 事故 run：`030f3687-7633-4e46-b47a-cc3507e6f75d`（agent 950a1943「Android 工程师 07」，session `b9ded136-fbc7-4ee2-8d5e-991a7d2fc3e6`，2026-09-07 13:51:28→13:56:04Z，部署 0b95c42b）
- 事故对象/先例：`docs/technical-plans/20260904-number-reference-disambiguation.md`（「执行 2」同型指代，R5 v2 已提案未落地）
- 定位口径：**双源**（运行日志/PG 台账 + Langfuse 埋点），证据见 §2

## 1. 结论（裁决开头）

用户回「先做 #1 + #2」，agent 把 #1/#2 映射到了**错误的清单**（99 项「还有什么可优化的？」的 #1=沙箱 git 仍不可用、#2=宿主侧 `.git` 完好），去跑了 git 可用性探测；而用户实际指的是上一轮 agent 口头发的「8 项 P2 可优化候选」的 #1=GitLab CI 缺失、#2=从未实际跑过 lint。

根因是**三层叠加**，结构性主因是第 1 层：

1. **呈现侧·编号命名空间漂移（结构性）**：上一轮 run（`fc4f38ce`「还有什么可优化的？」）在 closing answer 里用**新编号 1–8 重新聚合**了 99 项清单，产出「8 项 P2 可优化候选」（1=GitLab CI、2=lint……）。但这套 1–8 编号**只存在于对话 closing answer**，未落盘为编号清单实体——`memory/清单.md` 里是同一次 run 落盘的 99 项 `list:4e6db819`（编号完全不同：1=沙箱 git、2=宿主 .git）。
2. **上下文侧·跨 run 边界结构性丢失**：下一 run 的上下文只由「持久化产物」重建——R3 注入 99 项清单 + 4 项「还有哪些待办？」清单 + `清单.md` 产出摘录 + 记忆快照；上一轮 closing answer 的「8 项摘要」**未进入**任何注入通道（Langfuse 埋点模型输入里 0 次命中「GitLab CI / 从未实际跑过 lint / allowBackup」；`session_context_states.open_items` 也只挂 2 个 list_id，无 8 项实体）。
3. **解析侧·契约缺失（R5 v2 未落地）**：现行 `LIST_NUMBERING_CONTRACT`（`list_persistence.py:51-53`）仍是单条款旧版——「用户以编号引用清单时，以上下文/历史检索注入的清单条目为准执行，不得自行重排或猜测候选」。模型忠实执行了它：在多套编号并存时**静默锚定注入的 99 项清单**，把「#1+#2」映射到 git 可用性探测，而不是追问。`20260904-number-reference-disambiguation.md` 的三条款（编号同源/解析优先级/兜底消歧）**从未合入代码**。

一句话：**用户看到的 8 项编号和 agent 上下文里的 99 项编号分叉，且 8 项视图跨 run 边界被结构性丢弃，旧契约又禁止模型在歧义下追问**——三层合力把「先做 #1+#2」导向了错误的清单。

## 2. 证据（双源）

### 2.1 埋点源（Langfuse `events_full`，run 030f3687 首步模型输入）

trace_id `1c63b25a5e070bfae4bb6fde1788c0dd`（metadata run_id=030f3687、goal=「先做 #1 + #2」）。首次 LLM generation（13:51:28.784）输入共 6 条消息：

| # | role | 内容（节选） | 关键点 |
|---|---|---|---|
| 0 | system | Identity + soul + Focus/Trigger/Directory…（存储侧截断 `<truncated 11262 chars>`） | 静态提示（契约在截断段，见 §3 代码侧） |
| 1 | user | 「历史上下文（非当前任务）：此前已确认、尚未完结的清单：**清单「还有什么可优化的？」（99 项）**：1. 沙箱 git 仍不可用 2. 宿主侧 `.git` 完好 3. 四个改动文件…」 | R3 注入 99 项清单 |
| 2 | user | 「历史上下文（非当前任务）：上一轮任务已交付，仍有未决事项，任务「还有什么可优化的？」，产出摘录：`memory/清单.md` (lines 151-220)…」 | prior-run 以「清单.md 产出摘录」表示 |
| 3 | user | `# Dynamic Context Data … Memory Snapshot …` | 记忆快照 |
| 4 | user | `## Current Time 2026-09-07 13:51:28` | 时间 |
| 5 | user | 「先做 #1 + #2」 | 用户指令 |

逐标记核对（解码后全文）：

| 标记 | 出现次数 |
|---|---|
| 「GitLab CI」（8 项 #1） | **0** |
| 「从未实际跑过 lint」（8 项 #2） | **0** |
| 「allowBackup」（8 项 #8） | **0** |
| 「沙箱 git 仍不可用」（99 项 #1） | 1 |
| 「宿主侧 `.git` 完好」（99 项 #2） | 4 |
| 「循环小数退化」（4 项 #3） | 5 |

→ **8 项 P2 摘要（用户所指）不在模型可见输入里**；99 项清单在。

### 2.2 运行日志源（PG 台账：`agent_run_events` + `chat_messages` + `session_context_states`）

- **上一轮 closing answer**（`chat_messages`，assistant，13:51:19.853）：完整「8 项 P2 可优化候选」，#1=**[P2·工程] GitLab CI 缺失**、#2=**[P2·工程] 从未实际跑过 lint**，末尾自述「我的建议：**先做 #1 + #2（一次 lint 摸底…）**」。
- **本 run 模型推理**（`agent_run_events`，event_type=status_changed，13:51:38，reasoning_content）：「历史上下文中出现了多个清单：清单「还有什么可优化的？」（99 项）前 20 项：1. 沙箱 git 仍不可用 2. 宿主侧 .git 完好… 清单「还有哪些待办？」（4 项）：1. [P2] 工程侧优化立项…」——**只枚举了 99 项 + 4 项，8 项未出现**。
- **模型决策**（13:52:06 reasoning）：「最新清单是 `memory/清单.md` 里 2026-09-07 13:51 更新的「还有什么可优化的？」（list:4e6db819…）」→ 静默锚定 99 项清单，未追问。
- **实际动作**：execute_code git 探测（`probe start` / `git link probe` / `fetch origin` / `push --dry-run`）、read_file `.git/HEAD`、`.git.bundle` 等——全是 99 项 #1/#2（git 可用性）的动作，非 GitLab CI / lint。
- **最终交付**（assistant，13:56:04）：「**「还有什么可优化的？」清单 #1、#2 已处理完毕**」——明确错挂到 99 项清单。
- **`session_context_states`**（session b9ded136，version 14）`open_items`：`[{list_id:3f289908}, {list_id:4e6db819, project:mydome1}, {task_state_ref:任务状态.md}]`——**只有两个持久化 list_id，无 8 项实体**。

两源一致：8 项 P2 摘要（用户所指）从未进入下一 run 的模型输入；模型只能看到 99 项 + 4 项两套编号，并按旧契约静默锚定 99 项。

## 3. 代码侧（与 §2 互为印证）

- `backend/app/services/agent_runtime/list_persistence.py:51-53` `LIST_NUMBERING_CONTRACT` = 旧单条款：
  「…用户以编号引用清单时，以上下文/历史检索注入的清单条目为准执行，不得自行重排或猜测候选。」
  → 与 `20260904-number-reference-disambiguation.md` §4 Option 1 的 R5 v2 三条款（编号同源/解析优先级/兜底消歧）**不符，R5 v2 从未合入**（`git log -S '编号同源'` 仅命中 docs commit `b871e164`）。
- 该契约在 `model_step_service.py` 以 `static_prompt + _MESSAGE_LAYOUT_NOTE + LIST_NUMBERING_CONTRACT` 组装，注入系统提示（§2.1 埋点侧 system 消息被存储层截断至 4026 字符 + `<truncated 11262 chars>` 标记，契约位于截断段，故埋点不可见、但模型实际收到完整提示——代码侧为契约在场性的权威源）。

## 4. 与既有提案的关系与修复方向（仅指向，本次不实现）

本案例是 `20260904-number-reference-disambiguation.md` 的**新实例**，且比 09-04 案例多暴露一层：

- 09-04 案例：两套编号并存，但「优化项授权清单」仍在对话历史内，问题是解析侧优先级缺失。
- 09-07 案例：**呈现侧的新编号（1–8）根本没进入任何持久化/注入通道**，跨 run 边界被结构性丢弃——即便落地 R5 v2 的「解析优先级 ①本对话最近呈现的清单」，也会因「8 项视图不在上下文」而无法命中，只能靠「兜底消歧」追问兜底。

因此修复需覆盖两维（后续 Phase 3/4 展开）：
1. **解析/消歧**：R5 v2 三条款（编号同源 + 解析优先级 + 兜底消歧）——治「多套编号并存时静默选错」。
2. **呈现即落盘 / 跨 run 呈现保真**：closing answer 里呈现给用户的编号清单，其编号必须与持久化清单同源（呈现子集标注源编号、或先落盘再呈现），否则跨 run 边界丢失——治「呈现编号与持久化编号分叉」。

## 5. 红线与处置

- 本次仅**定位根因**，未改任何代码/库/生产；所有查询只读（PG restricted / docker 只读）。
- 遗留物待用户定夺：`/Users/shubinzhang/OrbStack/docker/volumes/clawith-agent_agentdata/950a1943-…/workspace/mydome1/.gitlab-ci.yml`（我此前误按实施任务写的 48 行文件，与本次根因定位无关，是否删除请示下）。

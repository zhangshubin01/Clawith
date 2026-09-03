# R3 修复方案多角度评审（2026-09-03）

> 评审对象：`docs/technical-plans/20260903-r3-open-list-injection.md`
> 依据：参考资料（本地源码查证）+ 真实任务执行日志（run 5ad111a9 trace `a00e9d86006b0886e96ad9c295c60737` + agent_tool_executions + 实测正则行为）+ 当前 HEAD 代码（**评审前对齐了并行会话新提交：`4d3fe431` .git 根治、`e3c063d0` 票 04/05/06、`1224cf77` reasoning_content 记录**）。

## 1. 根因找的是否正确？——主体正确，因果强度需降级

**确凿的部分**（有实测+日志证据）：
- 正则截断实锤：`detect_list_reference("那执行 1→2→3→4（P1）")` → `(1,)`；
- 错误注入实锤：run 5ad111a9 每步 input 含「历史上下文（非当前任务）：此前会话曾产出清单『app 有哪些需要优化的？』，条目：1. 沙箱 git 仍不可用…」；
- 开场白死循环、数字幻觉、目标漂移实锤（9 个 generation 的时间线）。

**需降级的表述**：方案写「幻觉是 R3 错误命中喂出来的」——过强。注入的条目里没有「31」「20-23」，模型是**在矛盾信号基础上脑补**的数字；且 17:08:19 模型**读了 memory/清单.md 仍在脑补**，说明 flash 的幻觉有独立成分。准确表述：**「错误注入提供矛盾信号 + flash 长上下文脑补」双因**；R3 错误注入是必要条件级别的燃料，不是充分条件。

**结论**：根因链方向正确，表述应修正为双因。

## 2. 根治方案是否正确？——方向正确，但未消化新 HEAD 的重叠机制

方案核心（删词集、全量注入、主模型对齐）与行业做法一致，正确。

**但有一个方案制定时不存在、现已提交的事实**：`e3c063d0` 已实现 `render_pending_lists_line`（session_task_state.py:572）——task_section.phase==active 时注入「未决事项：清单『X』（N 项，见 memory/清单.md）」指针行。**两个机制现在重叠**：指针行给索引（省 token、靠模型自己读文件），R3 全量给正文（确定性、每步 1-2K tokens）。方案文档「与 pending_lists_line 不冲突」的断言站不住——它们是同一信息的两份注入，需要显式定位（见第 7 题）。

另：方案文档称「注入仅开局一次」——**错误**。实测 `context_builder.build()`（原 `build_from_checkpoint`，已被 e3c063d0 改名）在 `model_step_service._prepare_messages`（2440 行）**每个 model step 调用一次**。注入是每步一次：41 步的 run = 41 次 × 1-2K tokens。成本账要按此修正（不影响方向，影响 Q5 的成本论证）。

## 3. 参考的资料是否正确？——五处查证中两处严谨性不足

- letta-code（recall 子代理）、mem0（20+ 向量后端）、deepseek-harness（零词集、verbatim 原则）：源码查证，成立；
- **Codex**：只查到 `todos` 字段存在于 session 上下文（turn.rs/turn_context.rs 的字段名），**未读到 todo 对齐的实质实现**——「主模型对齐」是行为级常识 + 字段存在性，不是源码级证据；
- **OpenHands**：未实际读 condenser 源码——引用其「LLM 压缩」属常识级。
- 结论不受影响（没有任何参考项目用正则词集解析意图这一点是稳固的），但评审文档应把这两处标为「行为级引用，未源码核实」。

## 4. 会引起其他问题吗？——两个真实风险

1. **「当前未决清单」措辞风险**（方案 Q7 定稿的措辞本身有缺陷）：「当前」二字 + 清单正文 = 模型可能把清单**当新指令执行**。这正是 `direct-chat-run-boundary-fix` 记忆里「user 角色摘要消息被当成新指令」的同一失败模式。措辞必须去「当前」：「历史上下文（非当前任务）：此前已确认、尚未完结的清单…」。
2. **每步注入的成本**（第 2 题已述）：全量 20 条 × 每步，在 flash 长上下文 run 上放大成本与失忆压力——与 3.1 待办（上下文瘦身）方向相反，两案需联动评审。

## 5. 会把其他逻辑搞坏吗？——破坏面可控，但调用点已漂移

- `detect_list_reference` 引用点仅 context_builder.py:381（已 grep 全仓）；`ListReferenceSignal` 仅 cross_session_retrieval.py 内部 + 该调用点；
- **新事实**：e3c063d0 把 `build_from_checkpoint` 改名为 `build`，且 session_task_state.py 已是已提交文件（其 docstring 引用 render_retrieval_note 措辞）。方案文档的调用点描述需按新 HEAD 重写；
- 删除词集后 `render_retrieval_note` 措辞变化影响 `session_task_state.py:596` docstring 的一致性（非功能影响）；
- 无其他破坏面。

## 6. 这是根治的最佳方案？——是候选中的最强确定性修复，但需先与指针行决出分工

三个候选：
- A. 本方案（删词集+全量注入）：确定性最强——正文在窗口内，模型无需读文件、无脑补空间（由「错误子集」事故反证）；
- B. 删除 R3、仅靠已实现的 pending_lists_line 指针行 + 模型 read_file：零代码、最省 token，但依赖模型行为（事故中模型读了文件仍脑补，虽然发生在错误注入之后，不能完全归因）；
- C. structured output 精确消解：多一次调用、多一个失败面，条目 ≤20 时无收益。

**结论**：A 是「根治」的最强形式；但 B 的「零代码」优势与「模型读文件后仍脑补」的反证需要用户拍板——A 的成本（每步注入）与 B 的不确定性之间是 trade-off，不是 A 一边倒。**推荐 A，但把「为什么不用 B」的论证补进方案**。

## 7. 修复方案是否是多余的？——部分多余：与 pending_lists_line 的定位必须显式化

不全部多余（词集错误注入是真实事故扳机，删除它是真修复），但**「全量注入」与已实现的「指针行」是同一信息的两份注入**，方案必须给出二者关系，三选一：
- A1. R3 全量注入**取代**指针行（task_section 活跃时不再注入指针行，由 R3 注入正文）——改动跨两票，风险面大；
- A2. 分工：指针行保留为「路径索引」，R3 全量只注入**条目标题列表**（不带描述）——折中成本；
- A3. 共存（现状+全量）：双重注入，成本最高，语义冗余。

**推荐 A1 或 A2**；方案文档目前是 A3 且未说明，属未决缺口。

## 8. 是否已经有可复用的逻辑？——有，方案未用

- `parse_list_file`/`ListSection`/`_collect_list_pointer_ids`（R3 自身已用）；
- `render_pending_lists_line` 的边界逻辑：标题截 30 字符、最多 3 份、`等` 标记——**R3 全量注入没有复用这套边界约定**，两份机制的边界不一致（3 份 vs 全部、标题 30 字符 vs 无截断）；
- `TaskSection.pending_lists`（票 04 已落库的结构）可以直接作为 R3 的候选来源，避免 R3 再走 open_items 指针 + 读文件的完整链路。

**结论**：R3 改造应优先复用 TaskSection/pending_lists 与 render_pending_lists_line 的边界约定，而非自建 RetrievedListSection。

## 9. 会破坏 clawith 的特性吗？——特性演进，但一个底线必须守住

- R3 是 run-context inheritance（票 03/04）的组成部分：从「引用时注入」演进为「常驻注入」是特性语义变化，不是破坏；用户消息行为不变；
- **底线**：past-tense「非当前任务」框架必须原样保留（第 4 题措辞修正也是为守住这条底线）；
- 「无条件注入」使所有有未决指针的 session 每步 +1-2K tokens——这是平台级行为变化，需要灰度评估（与红线「测试环境不灰度」冲突的部署动作要单独走流程）。

## 评审结论（对方案的修改要求）

1. 根因表述降级为双因（错误注入=燃料 + flash 脑补）；
2. 成本论证改为「每步注入」，Q5 的成本账重写；
3. 补「为什么不用 B（指针行）」，并把 R3 与 pending_lists_line 的关系定为 A1/A2 之一（推荐 A2：R3 只注入条目标题列表，正文仍靠指针行+read_file）；
4. 措辞去「当前」；
5. 复用 render_pending_lists_line 的边界约定与 TaskSection.pending_lists；
6. 调用点描述按新 HEAD（`build`、已提交的 session_task_state.py）重写；
7. Codex/OpenHands 引用标为行为级未源码核实。

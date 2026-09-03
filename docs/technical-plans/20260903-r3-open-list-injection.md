# R3 编号对齐注入：删除词集解析、条目标题列表常驻注入（2026-09-03 定稿 v2）

> 事故背景：run 5ad111a9（`docs/analysis/2026-09-02-opening-loop-number-hallucination.md`）
> 流程：两步走——①参考资料对比；②grilling Q1-Q7 收敛 + 多角度评审（`docs/technical-plans/20260903-r3-open-list-injection-review.md`）后修订本版。
> v2 修订依据：评审 7 条（根因双因降级、每步注入成本、A2 分工、去「当前」措辞、复用票 04/05 边界、新 HEAD 调用点、引用严谨性）。

## 一、定稿决策（grilling + 评审收敛）

| # | 决策 | 结论 |
|---|---|---|
| Q1 | 消解方式 | 主模型对齐——注入**条目标题列表**（编号+标题，无描述），模型对齐「1→2→3→4」 |
| Q2 | 词集 | **整体删除**（`_NUMBER_REFERENCE`/代词名词词集/detect 全部移除）——没有提取就没有截错 |
| Q3 | 多清单 | 复用 `render_pending_lists_line` 边界：**最多 3 份**（`_MAX_PENDING_LIST_NOTES`），顺序同 `TaskSection.pending_lists`，超出标「等」 |
| Q4 | 验收 | TDD：事故消息「那执行 1→2→3→4（P1）」原样入回归用例 |
| Q5 | 注入范围 | 无条件（含 heartbeat）：未决清单是 session 级常驻上下文 |
| Q6 | 超 20 条 | 显式标注「仅列出前 K 项；完整内容见 memory/清单.md」——静默截断会再喂脑补 |
| Q7 | 措辞 | past-tense 框架 + **去「当前」**：「历史上下文（非当前任务）：此前已确认、尚未完结的清单…」 |

## 二、根因（降级后的双因表述）

1. **燃料（平台缺陷，实锤）**：R3 正则把「那执行 1→2→3→4（P1）」截断为 `(1,)`（实测）→ 注入清单第 1 条「沙箱 git 仍不可用」——一条阻塞状态而非任务项，与用户意图（执行 4 项）矛盾，成为窗口内矛盾信号之一。
2. **放大器（模型缺陷，独立成分）**：flash 在 17K 长上下文里脑补「31 项」「编号 20-23」——注入条目中不存在这些数字；模型 17:08:19 读了 memory/清单.md 后仍脑补。即使注入正确，flash 仍可能脑补。
3. 修复目标：消灭燃料（平台侧零提取零截错）；放大器由 3.2 模型升级待办解决，不在本方案范围。

## 三、正确性要点（A2 分工的依据）

- **A2 分工**（评审第 6/7 题收敛）：`render_pending_lists_line`（票 05 已提交，phase==active 时注入）给**路径索引**（标题+计数+`memory/清单.md` 路径）；R3 给**编号对齐材料**（条目标题列表，模型无需读文件即可把「1→2→3→4」映射到条目）；**正文两者都不注入**，模型按需 read_file。
- 为什么 R3 不注正文（A3 否决）：每步注入全量正文 1-2K tokens，与 3.1 上下文瘦身方向相反，且与指针行构成同一信息的两份注入。
- 为什么 R3 不废除（B 否决）：事故反证——模型读了文件仍在脑补编号；标题列表把「编号→标题」映射直接放窗口内，消除脑补空间，成本仅约 20 条 × 20 字符 ≈ 400-800 tokens/步。
- **成本事实修正**（评审第 2 条）：`context_builder.build()`（原 `build_from_checkpoint`，e3c063d0 已改名）在 `model_step_service._prepare_messages`（2440 行）**每个 model step 调用一次**——注入是每步一次，非每 run 一次。A2 下每步成本 400-800 tokens；「note 改 run 内首步注入」列为后续优化（见范围外）。

## 四、实施清单（按新 HEAD）

### 1. `backend/app/services/agent_runtime/cross_session_retrieval.py`
- **删除**：`_NUMBER_REFERENCE`、`_FULLWIDTH_TO_HALFWIDTH`、`_HISTORICAL_PRONOUNS`、`_LIST_NOUNS`、`_normalize_digits`、`detect_list_reference`、`ListReferenceSignal`；`__all__` 同步。
- **`ListRetrievalResult` 改造**：
  ```python
  @dataclass(frozen=True, slots=True)
  class RetrievedListSection:
      title: str            # 复用 _bounded_pending_title 截断（30 字符）
      items: tuple[ListItem, ...]  # 条目标题（number+title），已按上限截断
      total_count: int      # 清单真实条目数（截断标注用）
  @dataclass(frozen=True, slots=True)
  class ListRetrievalResult:
      sections: tuple[RetrievedListSection, ...]  # ≤ _MAX_PENDING_LIST_NOTES 份
      total_lists: int  # 截断前已解析的清单数；> len(sections) 时渲染「等」（实施时补入，规格所需）
  ```
- **`retrieve()`**：去掉 `signal` 参数；候选收集保留（当前 session 优先 + 最近 `MAX_SESSIONS_DEFAULT=5`，`_collect_list_pointer_ids` exact→wildcard 顺序、project 过滤、去重不变）；清单排序复用 `TaskSection.pending_lists` 的顺序语义，最多取 `_MAX_PENDING_LIST_NOTES=3` 份；每份条目标题上限 `MAX_INJECTED_ITEMS=20`，超限截断；全无清单/读取失败仍 `None`（no-op 不变）。
- **`render_retrieval_note()`** 措辞（Q7 定稿）：
  ```
  历史上下文（非当前任务）：此前已确认、尚未完结的清单：
  清单「<title>」（<total_count> 项）：
  1. <item.title>
  2. <item.title>
  ...
  ```
  多条清单各一段；截断段加「（仅列出前 K 项；完整内容见 memory/清单.md）」；超 3 份标「等」。**不含「当前未决」类现时措辞（「非当前任务」框架本身保留），不含条目描述**。

### 2. `backend/app/services/agent_runtime/context_builder.py`
- `_retrieve_list_context`（381 行附近）：删除 `detect_list_reference` 调用，直接 `retrieve(...)`；docstring 改为「open-list title index injected unconditionally for the model to align number references」。

### 3. 复用（评审第 8 条）
- 从 `session_task_state` import `_bounded_pending_title` 与 `_MAX_PENDING_LIST_NOTES`（或提升为共享常量/函数，避免跨模块私有名耦合——实现时择一，倾向提升到 `workspace_collaboration` 或本模块重定义并注释同源）；`render_pending_lists_line` 与 `render_retrieval_note` 的清单范围、标题截断、`等` 标记从此同源。

### 4. 测试 `backend/tests/test_cross_session_retrieval.py`（TDD：先红后绿）
- **删除** 11 个 `test_detect_*` 用例；
- **改写** `test_render_note_is_past_tense_and_non_imperative`：断言新措辞（无「当前」、无描述、有截断标注）；
- **改写** retrieve 系列：无条件注入、多清单 ≤3 份与顺序、每份 ≤20 标题截断标注、无指针 no-op、project 过滤保留；
- **新增回归**：`goal="那执行 1→2→3→4（P1）"` → 注入清单**全部条目标题**（断言标题数=清单条目数，且不再只有单条）。

### 5. 验收
`python -m pytest tests/test_cross_session_retrieval.py -x -q` → 全绿；`scripts/arch-guard.sh` 通过；`git status` 确认不改动并行会话相关文件（`session_task_state.py` 已提交，只读复用不修改）。

## 五、范围外（明确不做）

- structured output 精确消解：标题列表已覆盖 ≤20 条场景，超限常发再评估；
- 注入正文（A3）、废除 R3（B）：已否决，见第三节；
- note 改 run 内首步注入（每步注入的成本优化）：依赖 checkpoint 检测，列入 3.1 增量化一并设计；
- 仅 chat run 注入：Q5 否决；
- flash 幻觉放大器的模型升级：归 3.2 待办。

**补丁（2026-09-03）**：上线后首验发现后台 trigger/heartbeat run 因 actor=None 结构性不注入（Q5「含 heartbeat」意图被静默架空）——补丁方案见 `docs/technical-plans/20260903-r3-background-run-scope-fix.md`（评审 `-review.md`）。

## 六、引用严谨性声明（评审第 3 条）

- 源码级查证：letta-code（recall 子代理）、mem0（20+ 向量后端）、deepseek-harness（compaction 零词集、verbatim 原则）；
- 行为级引用（未源码核实）：Codex todo 对齐（仅确认 `todos` 字段存在于 session 上下文）、OpenHands condenser（LLM 压缩为常识级引用）——均不承载本方案的关键论证。

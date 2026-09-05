# 过程无进展检测（No-Progress Detection）实施方案

> 2026-09-05 · 关联观察 `docs/analysis/2026-09-05-agent-loop-rootcause-cross-project-study.md` · ADR-0016
> 状态：设计已收敛（两轮 grill Q1–Q9 全部按推荐采纳），纯函数核心 + tdd 已交付，接线（退役 tools_fp / 异步注入）已落地。

## 一、问题

Clawith 现有熔断器全是「签名级」——判定「动作是否相同」：

- `_trailing_identical_calls`（`model_step_service.py:603`）：连续 16 条同 (tool, args) → 终止。
- `detect_loop`（`run_compactor.py:927`）：相邻同 (prefix_fp, **tools_fp**) + 压缩 flag → 压缩失忆循环。
- `build_dup_read_ratio`（`read_dedup.py:133`）：仅 read_file 滑窗重复占比。

它们的共同盲区：**「换了姿势做同一件事」**——git status/branch/checkout/fetch + read_file 混杂，每次参数不同 → 签名哈希每次不同 → 永不触发；但任务/世界状态没有前进。

17 项目跨库研究结论（含 12 个诚实负结论）：根治级项目全部放弃签名，改问「**状态前进没有**」。证据见观察文档 §一/§二。

## 二、收敛设计（Q1–Q9 决策速览）

| # | 决策 |
|---|---|
| Q1-B | 过程无进展先行（本票）；结果验收（judge `run_outcome`/`attempt_count` 接运行循环）**已由既有 `TaskCompletionGate` + `verify` 节点实现，无需新票**（更正 2026-09-05：原「随后独立成票」系未核对既有代码的错误前提） |
| Q2-A | 信号 = 证据增益 + workspace 材料变迁，确定性、零 LLM 成本（不用 gemini-cli LLM 语义复核） |
| Q3-A | 并存现有熔断器；**退役 `detect_loop` 的 `tools_fp` 维**（工具 schema 摘要恒定 = 死信号），保留 `prefix_fp` + compaction flag 本体 |
| Q4 | 阶梯语义 nudge/pivot/stop；接线复用 `_audit_breaker_event` |
| Q5-A | tdd 先行 + dc557d91 回归用例 |
| Q6 | 增益键表（下表）；streak 纯粹由「连续零增益」（连续无任何新证据）驱动，不设 look-only 上限（见 §三末更正） |
| Q7-B | 阈值 3/5/8（对齐 `_SUCCESS_LOOP_THRESHOLD=5`、DeepCode 3/5/8 口径） |
| Q8-B | stop = 注入「停止探索、交最终答案」+ 放行 finish（非硬 terminate） |
| Q9-A | 增益从 ledger 重放计算（零新 checkpoint 状态）；`streak` 单值（bounded）；per-turn 重算 |

## 三、增益键表（Q6）

判定信号落在「任务/世界状态是否前进」，非签名。每轮（一个 model turn）聚合其工具执行的增益分：

| 工具 | 判定键 | 增益 | 证据源 |
|---|---|---|---|
| read_file | (path, content_hash) 首次 | +1 | `sanitized_arguments.path` + `result_metadata.content_hash` |
| read_file | 重复 (path, content_hash) | 0 | 同左（`build_dup_read_ratio` 已是其特例） |
| execute_code | command 相同、结果哈希变化 | +1 | `sanitized_arguments` 规范化 + `result_metadata.content_hash` |
| execute_code | 曾失败的命令现在成功 | +2 | `state.command_failed` → succeeded |
| execute_code | 新命令首次观察 / 重复（同 command 同结果哈希） | 0 | 首次观察非证据——`git status/branch/checkout/fetch` 各是新命令，天然零增益（对齐分析 §四 line 82：不靠「command 首次=+1」） |
| write 族（effect=write） | **真实变更**（WorkspaceFileRevision before≠after） | +3 | `workspace_file_revisions`（空编辑无 revision 行） |
| write 族 | 空编辑 / 无 revision | 0 | 同左 |
| external_write | 外部副作用（无 workspace 证据可验） | +3 | `effect=external_write`，假定前进 |

**无 look-only 上限**（2026-09-05 双源验证后更正）：初版照搬 Reasonix `explorationRunLimit=6`，在连续 6 轮「无真实写变更」后强制归零。生产 run `5a70c8ef` 逐轮还原发现它把「正常调研探索」（每轮读新文件）锁成误杀——`look_only_run` 计数器只被真实写重置、不被新读重置，第 8-11 轮真正读新源码文件的增益被强制归零，streak 累积到 8 触发 stop。修正：**删掉 look-only 上限**，streak 纯粹由「连续零增益」（连续无任何新证据）驱动——正常探索每轮读新文件不累积；「读遍文件绕圈」的终点（读无可读后重复读）仍被 gain==0 抓住，另有 200 轮 recursion_limit + 结果验收双兜底。

## 四、`no_progress.py` 纯函数核心（本票交付）

新模块 `backend/app/services/agent_runtime/no_progress.py`，全部纯函数、无 DB/无 checkpoint 状态，可单测：

```python
GAIN_NEW_READ = 1; GAIN_NEW_RESULT = 1; GAIN_FAILURE_RECOVERY = 2; GAIN_MUTATION = 3

@dataclass(frozen=True, slots=True)
class NoProgressConfig:
    nudge_threshold: int = 3
    pivot_threshold: int = 5
    stop_threshold: int = 8

@dataclass
class RoundState:                      # run-scoped 折叠态（replay，零持久化）
    read_keys: set[tuple[str, str]]    # 已读 (path, content_hash)
    command_results: dict[str, str]    # command_key -> 最近结果哈希
    command_failed: set[str]           # 曾失败的命令

def score_round(executions, state, *, material_change_of=None) -> int
    # 对一轮的工具执行求和增益；更新 state；纯证据驱动（无 look-only 上限）

def classify_streak(streak, *, config=NoProgressConfig()) -> str
    # none / nudge / pivot / stop

@dataclass(frozen=True, slots=True)
class NoProgressSignal:
    streak: int; level: str; last_round_gain: int

def fold_rounds(rounds, *, config=NoProgressConfig()) -> NoProgressSignal
    # rounds = 有序的 [本轮工具执行序列, ...]；折叠出尾部零增益 streak + 阶梯

def build_no_progress_signal(executions, *, run_id=None, config=..., material_change_of=None) -> NoProgressSignal | None
    # 接线接缝①：把当前 run 的 settled executions 按 assistant_message_id 分组为轮再折叠；
    # run_id 过滤前 run（_load 会 extend prior run 的执行行）；无 assistant_message_id 退化为按 tool_call_id 单例轮

def no_progress_message(signal) -> str | None
    # 接线接缝②：nudge/pivot 提示、stop 提示（放行 finish，不硬终止）；level="none" → None
```

**执行对象契约**（纯函数只读 `getattr`，与 `read_dedup.py` 同风格，用 `SimpleNamespace` 测）：`tool_name`、`status`、`sanitized_arguments`、`result_metadata`、`effect`、`material_change`（由接线层把 WorkspaceFileRevision before≠after 解析成布尔）。write 判定按 `effect ∈ {write, external_write}` + `material_change`，不按工具名白名单（`execute_code` 单独按结果哈希）。

## 五、接线落点（已落地）

在 `model_step_service.py` 的 `complete_once`（`read_dedup_map`/`dup_read_signal` 计算处之后）完成：

1. **round 分组**：`build_no_progress_signal` 按 `assistant_message_id` 把当前 run 的 ledger 执行分组为轮（无需 `_current_run_messages`——纯回答轮无工具调用天然不算轮）；`run_id` 过滤前 run（`_load` 会 extend prior run 的执行行）。
2. **material_change 解析（v1）**：`_no_op_write_tool_call_ids` 把 agent 作用域 succeeded write 执行按 (path 归一化, `created_at∈[started_at-1s, completed_at+3s]`) 命中 `workspace_file_revisions` 判定「空编辑无 revision 行」→ 返回 *positive no-op* 集合；`_material_change_of` 对失败写返回 False、对未证伪写返回 True（宽松，不误杀 group 写/未解析证据）。**已知局限**：真实写后极短间隔内同路径空编辑可能继承早期 revision 被误计为进展（记录于 ADR-0016）。
3. **fold → NoProgressSignal**，阶梯注入 `no_progress_message`（复用 `_soft_loop_reminder_message` 的 absolutely-last 位置惯例）：
   - nudge/pivot：注入 user 消息提示「换策略 / 停止绕圈」。
   - stop：注入「停止探索、直接交最终答案」，**放行 finish**（不硬 terminate）。
4. `_audit_breaker_event` 留痕（action_type `runtime_no_progress_{level}`，仅等级边界 streak==3/5/8 时触发，避免逐轮刷日志）。
5. **退役 tools_fp（Slice C）**：`_advance_loop_detection` 去掉 `tools_fp` 参数与 event/alert 字段；`detect_loop` 只比对 `prefix_fp` + compaction flag；`_cache_fingerprints` 仍产出 tools_fp 仅作日志诊断。已同步 `test_agent_runtime_compaction_loop.py`。

## 六、测试计划（tdd 先行，本票）

`backend/tests/test_agent_runtime_no_progress.py`，红→绿：

- **增益分**：新读 +1 / 重复读 0；execute_code 新结果 +1、失败→成功 +2、重复 0；write 真实变更 +3、空编辑 0；external_write +3；failed 读/命令不计分。
- **正常探索不误杀**：连续读全新文件 streak 恒 0；读遍后回头重复读（零增益）才累积 → stop。
- **阶梯**：streak 2→none、3→nudge、5→pivot、8→stop；正增益即清零 streak。
- **fold**：混排 rounds 的尾部 streak 与 last_round_gain。
- **dc557d91 回归**：14 次 edit_file（真实变更）不应触发 stop；git status/branch/checkout/fetch 绕圈（零材料进度）应触发。

## 七、回归与红线

- 后端改动前运行 `scripts/arch-guard.sh`。
- 日志用 loguru `{}` 占位符。
- DB/Redis 只读；不加 checkpoint 状态（Q9-A 零新状态）。
- 不碰并行会话未提交改动（frontend + 其他 docs）。

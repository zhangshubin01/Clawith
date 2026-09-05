# ADR-0016: 循环检测信号从「签名匹配」迁移到「证据增益 / 材料变迁」

- 状态：已采纳
- 日期：2026-09-05
- 决策人：用户（两轮 grill-with-docs Q1–Q9，全部按推荐）
- 关联：观察 `docs/analysis/2026-09-05-agent-loop-rootcause-cross-project-study.md`（17 项目）；方案 `docs/technical-plans/20260905-agent-no-progress-detection-plan.md`

## 背景

Clawith 现有熔断器全是「签名级」：`_trailing_identical_calls`（连续同 (tool,args)）、`detect_loop`（同 (prefix_fp, tools_fp) + 压缩 flag）、`build_dup_read_ratio`（read_file 滑窗）。共同盲区是**「换了姿势做同一件事」**——git status/branch/checkout/fetch + read_file 混杂，每次参数不同 → 签名哈希每次不同 → 永不触发，但任务/世界状态没有前进。

17 项目跨库研究（逐仓库读真实代码，含 12 个诚实负结论）发现：根治级项目（DeepSeek-Reasonix、loopx、ouroboros、gemini-cli、crewAI）全部**放弃签名**，改问「**状态前进没有**」（证据增益 / 材料变迁 / LLM 语义复核）；deepagents 的 RubricMiddleware 补上第二正交维度「结果未达标」。签名级/硬上限级的其余 12 个（OpenHands、DeepCode、cline、opencode/kilocode、SWE-agent、codex、openai-agents、pi、Codewhale、deepagents 自身）被证明抓不住换姿势。

## 决策

1. **检测信号从签名迁移到「证据增益 + 材料变迁」**：判定「任务/世界状态是否前进」，非「动作是否相同」。增益分按工具键表（read 新 (path,content_hash)+1、execute_code 结果哈希变化 +1 / 失败→成功 +2、write 真实变更 +3、external_write +3）。streak 纯粹由「连续零增益」驱动，不设 look-only 上限（初版照搬 Reasonix `explorationRunLimit` 会误杀正常调研探索，2026-09-05 双源验证后删除）。确定性、零 LLM 成本（不采用 gemini-cli 的语义复核，留作成本敏感时的可选项）。
2. **阶梯 nudge(3) / pivot(5) / stop(8)**，对齐现有 `_SUCCESS_LOOP_THRESHOLD=5` 与 DeepCode 3/5/8 口径；stop 档注入「停止探索、交最终答案」并放行 finish，非硬 terminate。
3. **并存现有熔断器，但退役 `detect_loop` 的 `tools_fp` 维**：工具 schema 摘要恒定、是死信号；保留 `prefix_fp` + compaction flag 本体（压缩失忆循环仍由它兜底）。`tools_fp` 降级为纯日志诊断。
4. **零新 checkpoint 状态**：增益从 ledger（`agent_tool_executions` 的 effect/status/sanitized_arguments/result_metadata）+ `workspace_file_revisions`（before≠after = 真实变更，空编辑无 revision 行）重放计算；`streak` 单值、per-turn 重算。
5. **过程无进展先行**；结果验收（「要结束时验收 → needs_revision 强制继续」）**已由既有 `TaskCompletionGate` + `verify` 节点实现**（deepagents RubricMiddleware 的迁移，早于本 ADR 已建成；judge `run_outcome`/`attempt_count` 属已上线的独立离线可观测轨道，票 03/04）。*更正 2026-09-05：原稿「结果验收随后独立成票」系写稿时未核对既有代码的错误前提，本维度无需新票。*

## 后果

- 正：根治「换姿势做同一件事」——git 巡检类命令永不产生材料变迁，天然命中；「读遍文件绕圈」的终点（读无可读后重复读）被 gain==0 抓住；信号落在客观副作用证据上，比 Reasonix 的「命令首次=+1」更根本（Reasonix 自己承认该盲区）。确定性、零增量模型调用。
- 负/风险：external_write 无法客观验证「是否真前进」，只能假定 +3（外部副作用无 workspace 证据，宁可计分防误杀真实外部交付）；material_change 用 path+created_at 时间窗 join（v1，`workspace_file_revisions` 命中 `[started_at-1s, completed_at+3s]` 判定真实变更），已知局限是真实写后极短间隔内同路径空编辑可能继承早期 revision 被误计为进展（更准的 `revision_id` 入 result_metadata 留作后续）；阶梯注入可能打扰正常长任务（以 nudge 软提醒先行、stop 放行 finish 对冲）。
- 约束：不加 checkpoint 状态；日志 loguru `{}`；`tools_fp` 退役需同步 `test_agent_runtime_compaction_loop.py`；不碰 `_trailing_identical_calls`/`_soft_loop_reminder`/`build_dup_read_ratio` 的既有语义。

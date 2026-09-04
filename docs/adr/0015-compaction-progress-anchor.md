# ADR-0015: 压缩进度锚点（Compaction Progress Anchor）

- 状态：已采纳（评审完成：9 问双轴评审结论「需修改后实施」，Q7 熔断口径等 6 项修正已并入本 ADR 与方案）
- 日期：2026-09-03
- 决策人：用户（grill-with-docs 第一轮四问，全部按推荐）
- 关联：观察文档 `docs/analysis/2026-09-03-compactor-loop-dc557d91.md`；方案 `docs/technical-plans/20260903-compaction-progress-anchor.md`

## 背景

run dc557d91（86 步 / 62 分钟）实测暴露压缩失忆循环：真实压缩每 3–7 分钟一次；压缩后消息前缀完全相同（prefix=697aef1a1281 三现）；工具调用序列跨轮重复；run 结束时摘要仍自认「三项 P1 修复均未执行」，尽管期间 edit_file 14 次。根因不在摘要指令（指令已含循环防护条文），而在**摘要输入缺少「已完成动作」的事实**：tool_exchange 进摘要时被替换为 ledger 一句话摘要（edit_file 仅「Replaced N occurrence(s) in path」，无内容），摘要模型无从判断进度，只能保守认定未完成 → 重建后重做 → 上下文再膨胀 → 再压缩。

参考资料对照（deepseek-harness 首选、官方 how_to_fix_your_context、deepagents、LLMLingua、mem0）：确定性裁剪（pruner/eviction）是跨库验证的便宜路线；「进度锚点」是本代设计（含 deepagents）通病、参考系无现成机制；mem0 的 ADD-only 事实累积是流水形态的直接先例；LLMLingua 式 token 级压缩不适合必须 preserve exact identifiers 的编码线程。

## 决策

1. **D+A 先行**：确定性即时结算（已完成工具交换离开 recent 窗口即替换为 ledger 摘要，不经 LLM）+ 已完成动作流水（ledger 构造、ADD-only 只追加不重写、注入压缩 payload）。**B（内容级 result_summary）条件触发**：仅当 D+A 上线后循环仍现才做；原料已确认现成（workspace_file_revisions 的 before/after 全文、ledger sanitized_arguments 的 old/new 全文），但撞 `_short_result` 500 字符截断需连带放宽，故暂缓。
2. **完成判定=机械口径**：工具 status=succeeded 即入流水；语义判定（修复是否真完成）留给摘要模型的 Pending Jobs 表达，不引入 verify 语义。
3. **循环熔断纳入**：run 内检测「同一消息前缀相邻重现 + 间隔发生真实压缩 + 相邻出现间工具哈希序列相同」→ **逐次计数**（前缀三现=2 次循环），run 内累计 ≥1 次循环即告警；默认只告警，终止动作可开关；指纹需从日志行产物化为可调用产物（tools_fp/msg_chain），检测基建与「第 5 层开场白熔断」共用。
4. **摘要模型维持 flash**：失真根因是输入缺事实而非模型弱；换模型留作票 11（datasets-experiments）A/B 对照项。
5. **D 落点=写 checkpoint 块级原子替换**：`_tool` 尾部与结果同帧把「已 settle 且出窗」的 assistant tool_calls + 全部结果**整块**替换为单个 user 合成消息（id 复用被删块 `message_ids[-1]`，先例 `run_compactor.py:451-458` 的 `historical_tool_exchange` 与 `_compact` RemoveMessage 全量重建）；只替换结果会走 `_resolve_incomplete_exchange` 的 succeeded 分支 → `retry_model=True`（`tool_exchange.py:525`）→ 模型重发已完成调用；不做读时投影（其 `retry_model` 语义会让模型重发已完成的调用，形成新循环）。替换只取已落库 ledger，重放由账本终态兜底；窗口计算排除 `_protected_current_run_message_ids`（repair/resume）。

## 后果

- 正：压缩频率下降（燃料被确定性结算砍掉）；摘要输入含权威进度事实，循环失去「重做理由」；run 级循环可观测（告警）。
- 负/风险：确定性结算使窗口外的工具结果仅剩一句话摘要（模型可能丢失近期细节——以 recent 窗口大小对冲）；流水若无限增长需容量上限；熔断误判可能打扰正常长任务（默认只告警对冲）。
- 约束：不碰压缩阈值与模型档位；不改摘要 section 结构（结构校验 F1.5 兼容）；流水为 payload additive 字段，不破坏 `thread_running_summary_v1` 现有消费者。

# 压缩进度锚点方案 · 根治级复审（2026-09-04）

- 输入：①方案 `20260903-compaction-progress-anchor.md`（含 6 项评审修正）+ ADR-0015；②真实执行日志：62ab3538 实时循环（prefix=bbde59154952 两现、工具哈希序列重复、7 分钟压缩节奏算术链）；③参考资料代码级对比 `docs/analysis/2026-09-04-compaction-reference-implementation-comparison.md`。
- 方式：9 问逐条，关键断言全部 read_file 实核（行号见各问）。

## 1. 根因找的是否正确？——正确，现有三重独立证据

- 事实链（实核）：`agent_tools.py:4755` edit_file 成功后仅返回 `_typed_success(f"Replaced {replaced} occurrence(s) in {result.path}.")`——无内容；进摘要时 tool_exchange 被 ledger 一句话替代，摘要模型无从判断进度 → 保守认定「未完成」→ 重建后重做。
- dc557d91 历史铁证（观察文档）+ 62ab3538 实时复现（Q7 三条件全命中）+ 参考系反证（dsh/langchain/deepagents 全部让摘要模型看到真实内容，只有 Clawith 喂一句话摘要——输入缺事实是 Clawith 独有形态）。
- 精确化：事实源其实存在（`workspace_file_revisions` before/after、ledger `sanitized_arguments` old/new），只是不在摘要输入里。根因=「事实在库、输入不含事实」。
- 同时排除：摘要指令弱（指令已含循环防护条文）、模型弱（flash 失真是输入问题非能力问题）、阈值低（阈值只决定节奏，不决定循环）。

## 2. 根治方案是否正确？——正确，三层各攻击一个变量

- D：攻击「每步 +2.8K token 增长」主燃料 → 压缩节奏从 ~7 分钟推到 ~50+ 分钟（算术链见对比文档 §3），与参考系同量级；先例=dsh pruner 的确定性替换。
- A：攻击「摘要输入缺事实」——参考系靠喂全文解决，Clawith 喂全文要动摄入路径（撞 `_short_result` 500 字符，`tool_exchange.py:181-185`），改为确定性注入 completed_actions。等价效果、成本低、additive。
- 熔断：不是根治，是安全网（D+A 后摘要模型仍可能失真）。Q7 校准口径被 62ab3538 精确命中，判据有效。
- 边界诚实声明：D+A 不保证 100% 消灭循环——这正是熔断必须同批上线的理由。

## 3. 参考资料是否正确？——正确，全部实读、引用有源码出处

- 5 个参考（dsh / deepagents+langchain / 官方 04-05 / mem0 / LLMLingua）均已读真实源码；对比文档每项都有文件+行号。
- 两处校正已记录：①OpenHands 本地镜像只剩前端 monorepo（Python condenser 不可读）——本方案未引用其实现，无影响；②dsh「摘要吃前缀缓存」对 Clawith 可迁移性有限（Clawith 摘要独立 prompt 组装），对比文档已诚实标注，不构成方案依据。
- 覆盖谱系完整：确定性裁剪（dsh pruner / deepagents truncate_args）/ LLM 摘要（langchain）/ 读时投影（deepagents）/ 事实累积（mem0）/ token 级压缩（LLMLingua，正确排除：编码线程须 preserve identifiers）。

## 4. 会引起其他问题吗？——会，全部已知且有对冲

| 新问题 | 对冲 |
|---|---|
| 窗口外细节丢失（模型看不到旧结果全文） | recent 窗口内永远全文（retained 预算语义） |
| replay 分叉（合成消息与账本不一致） | 只取已落库 ledger；账本终态兜底（6f43d25b）；合成消息禁嵌时间戳/随机值 |
| D×压缩交错（结算与 compact 同时改写消息） | 与 `summary_covered_through_message_id` 水位防交错（实核：context_builder.py:739 读、node_executor.py:703 写）；Q8 加交错测试 |
| checkpoint 写放大（每步 O(n) 块重建） | 每步结算块数 ≈1（Q6 已估），结算仅在出窗时发生 |
| payload 增大 | 50 条/2KB 硬上限，超出裁最旧 |
| 熔断误报 | 默认只告警，`terminate_on_loop` 默认 false |

## 5. 会把其他逻辑搞坏吗？——接触面已逐一实核，不会（在测试覆盖内）

- `_resolve_incomplete_exchange`（`tool_exchange.py:510-525`）：只换结果会走 succeeded 分支 → `retry_model=True` → 模型重发已完成调用。**已定案整块替换规避**。
- `validate_tool_exchange_integrity`（`tool_exchange.py:716+`）：fail-closed「每 call 恰一个相邻结果」。块级替换后必须仍配平——Q8 已加断言。
- `_protected_current_run_message_ids`（`run_compactor.py:271-318`）：repair/resume 受保护，D 窗口排除。
- retry_model 消费点（`tool_exchange.py:814` 附近）：出窗块被整块替换后不再进入该分支。
- 熔断不改图结构、不加节点间依赖，只加日志+Langfuse event。
- 结论：风险集中在「消息序列配平」一个面，且已有现成断言工具兜住。

## 6. 这是根治的最佳方案？——在约束内是

备选谱系逐一排除：喂全文（langchain 式）撞 `_short_result` 摄入截断、要动摄入路径；读时投影（deepagents 式）Q8 已否决（retry_model 重放语义）；token 级压缩（LLMLingua）破坏编码线程 identifiers；换模型不解决输入缺事实。D+A+熔断是唯一同时攻击「增长速率」与「事实缺失」的组合，每项均有参考系先例。诚实补充：若 B 条件触发，deepagents truncate_args 式确定性截断（前 20 字符）比 LLM 摘要更便宜，应优先评估（对比文档增量结论 3）。

## 7. 修复方案是否多余？——不冗余

62ab3538 正在实时循环（21+ 分钟 35+ 步、prefix 两现、工具序列重复）。加强指令救不了（已含循环防护条文）；调阈值只改节奏不改循环；现有 compactor 无确定性结算、无事实注入、无检测。D=减需求（少触发压缩）、A=给事实（压缩不失忆）、熔断=检测兜底——三层各有独立存在理由，且各被真实案例证明必要。

## 8. 是否已有可复用逻辑？——大量现成件，实现成本低于表面

- `_summary_for_exchange`（`tool_exchange.py:188-235`）：D 合成内容生成器，已支持 ledger。
- `_guard_observed_results`（`261-325`）：settle 判定现成。
- `_ledger` run 级 map（`model_step_service.py:1004-1026`）+ `inspect_tool_execution`（`tool_execution.py:975-999`）：A 数据源零新读路径。
- `_cache_fingerprints`（`model_step_service.py:713-756`）：已返回 `(prefix_fp, tools_fp, msg_chain)`——熔断直接消费，只需从日志行产物化。
- 时序现成：ledger 先于消息落库（`_settle_outcome` 先 mark 后 node update），结算后立即读 ledger 无竞态。
- `_COMPACTION_SECTION_HEADINGS` 校验（`run_compactor.py:106-119`）兼容 payload 新字段。

## 9. 会破坏 Clawith 的特性吗？——不会，但有一条特性必须专测

- 多租户隔离：D/A 全部 run-scoped（ledger run 级、payload per-run），无跨租户面。
- 账本幂等/重放：只读 ledger、ADD-only、合成消息确定性——重放一致性兜底不变。
- **DeepSeek 前缀缓存（Clawith 独有特性）**：D 替换窗口外块，窗口内（prefix）不变，理论上不打断 cache-HIT 前缀——但必须加测试断言（评审 Q8 已列 D×压缩交错；建议补「结算后第一步 cache ratio 不劣化」断言）。这是 Clawith 相对参考系最独特的特性，不能靠推断。
- 卡片/活动派生（`checkpoint_side_effects.py:381+`）：核对项已列。
- thread_summary v1 消费者：payload additive 兼容。
- waiting/审批/飞书：D 落在 `_tool` 尾部（`node_executor.py:1188-1220` update 路径），与 waiting 分支正交。

## 总判定

**根因正确、方案正确、资料正确、不冗余、可复用件充足、特性安全（需补 cache 断言）——可以实施，顺序不变：票 A（D+A）→ 票 B（熔断）。**

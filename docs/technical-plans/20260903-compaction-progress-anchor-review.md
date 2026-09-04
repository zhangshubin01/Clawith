# 压缩进度锚点方案 · 评审报告（2026-09-03）

- 评审对象：`docs/technical-plans/20260903-compaction-progress-anchor.md` + `docs/adr/0015-compaction-progress-anchor.md`
- 评审方式：9 问多角度，全部代码位置 read_file 实核
- 总体结论：**需修改后实施**（1 硬伤 + 2 语义级修正 + 3 补充，修正已并入方案）

## 逐问结论

| # | 角度 | 结论 |
|---|---|---|
| Q1 | D 替换 vs 消息配对 | ⚠️ 须明确**块级原子替换**：assistant tool_calls 与全部结果同帧移除、代之以单个 user 合成消息（id 复用 `message_ids[-1]`，先例 run_compactor.py:451-458）。只替换结果会走 `_resolve_incomplete_exchange` 的 succeeded 分支→`retry_model=True`（tool_exchange.py:525）→模型重发已完成调用（重蹈投影坑）。二次摘要不会发生（normal 块原样透传）。附带：核对卡片事件派生（checkpoint_side_effects.py:381+）不因消息移除而丢失 |
| Q2 | 幂等/重放 | ✅ 过。ledger 提交早于 checkpoint 更新；D 窗口计算为 (messages, ledger, budgets) 纯函数；RemoveMessage 对不存在 id 是 no-op。纪律：合成 id 复用被删块 `message_ids[-1]`、内容禁嵌时间戳/随机值 |
| Q3 | 与压缩交错 | ✅ 过（补一条）：D 窗口计算须同样排除 `_protected_current_run_message_ids`（run_compactor.py:271-318）的 repair/resume 消息 |
| Q4 | 双重来源 | ⚠️ 指令须补 precedence：① completed_actions 是完成与否的权威、historical_tool_exchange 仅为细节佐证；② failed 条目不算已完成、进 Pending 需标注重试而非新任务；③ v1 旧 summary 中「自认未完成」失真事实以 completed_actions 为准。另：`omitted_tool_exchanges` 是主模型 prompt 字段，与 payload 无关，勿混 |
| Q5 | 兼容性 | ✅ 过。`project_multimodal_for_summary` 未知字段透传；thread_summary 只存 {format,text}；结构校验只查 8 个 section heading |
| Q6 | 成本 | ✅ 过。每工具步新增一次 ledger SELECT + O(n) 块重建；窗口估算用 `_estimate_tokens` 启发式；每步结算块数天然 ≈1 |
| Q7 | 熔断假阳 | ⚠️ **硬伤**：按原口径（三现=1 次循环、≥2 次告警）旗舰证据 run dc557d91 本身不触发告警。修正：同一 prefix 相邻重现且间隔有真实压缩**逐次计数**（三现=2 次）；告警阈值 ≥1；判据补「相邻出现间工具哈希序列相同」（与 ADR 决策 3 对齐，指纹产物 tools_fp/msg_chain 现成） |
| Q8 | 测试覆盖 | ⚠️ 补：dc557d91 轨迹重放端到端（断言 D+A 后循环签名不再现）、杀 run 重放无分叉、D×压缩交错、protected 不被结算、`validate_tool_exchange_integrity`（tool_exchange.py:716+）断言、payload 过投影+结构校验。测试文件规模实核：test_agent_runtime_run_compactor 30、thread_compact_contract 9、test_tool_exchange 11、node_executor 44 |
| Q9 | 范围/纪律 | ✅ 过（补两点）：指纹目前仅以日志行存在（model_step_service.py:2892），需抽成可调用产物并指明检测状态存储点（lifecycle 新字段候选）；引用行号全部实核命中（唯一微偏 `_guard_observed_results` 实为 261-325） |

## 修正项清单（已并入方案）

1. §3.1 块级原子替换语义 + 卡片事件核对项
2. §3.3 熔断口径校准（逐次计数、阈值 ≥1、双条件）
3. §3.2 指令 precedence 三条
4. §3.1 D 窗口排除 protected repair/resume
5. §4 测试用例扩充 + 文件规模落实
6. §3.3 指纹产物化 + 检测状态存储点

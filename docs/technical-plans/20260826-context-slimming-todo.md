# 上下文瘦身优先级待办（收益 × 风险）

> 日期：2026-08-26 ｜ 来源：`20260826-context-slimming-plan-review.md`（三源审查裁决）+ 画像 `20260826-context-token-profile.md`
> 排序原则：收益/风险性价比，其次依赖关系（测量与 A/B 基础先于大改）。每一行 = 一个可独立提交的待办，含验收口径。
> 已核实无需再做的：①run 边界硬左边界+模板摘要（`thread_visibility.bound_run_window` 已落地，run 起始窗口仅 ~4-5K）；缓存机制本身健康（步间命中 90%+）。
> 独立观察项（非性能，随时可开工）：飞书卡片 waiting_user 无呈现 → `20260827-feishu-waiting-card-observation.md`（run e2ef5629 实锤，修复方案 A/B/C 已列）。

## 全局基线（验收参照）

run 3d2b2c19（36 步，14:01-14:09 UTC）：步间 cache_read 26.6K→57.1K；每步未命中 0.4-5.3K；压缩 2 次（18.3s/30.4s 摘要 + 后续 30-67s 重建步）；验证门 1 次 8.7K；总耗时 ~7.5 分钟。

---

## P0（本周内，收益最大）

### P0-1 工具结果截断 + 恢复路径（先 A/B）

| 维度 | 内容 |
|---|---|
| 收益 | ★★★：历史增速 +3.3K→~1.7K/步 → 压缩周期 17→34 步；36 步任务 2 次压缩→0-1 次；单任务省 1-2 分钟 + 每步注意力/计费下降 |
| 风险 | 中：截断可能丢关键信息（无一手成功率对照实验）→ **A/B 守成功率** |
| 依赖 | agent-evaluation 基建（跑同任务评测集对比截断开/关） |
| 实现要点 | ① read_file 结果头部 2K 字符 + 恢复指引（"完整内容在 {path}，用 offset/limit 读取"——deepagents 文案直抄）；②错误/告警行优先保留（SWE-agent keep tags / gemini-cli `<error>` 标签）；③只截 keep 窗口外的旧结果，近期结果不动；④MCP/搜索/列表类同策略；⑤A/B 变量可加「gemini-cli 式便宜模型摘要 ≤2000 token（错误栈完整保留）」 |
| 验收 | 评测集成功率不降 + 同任务重跑压缩次数下降 + `prompt_cache_hit/miss_tokens` 占比改善 |
| 工作量 | 中（内置工具 + MCP 结果两处） |

### P0-2 动态块拆分（稳定段入缓存前缀）

| 维度 | 内容 |
|---|---|
| 收益 | ★★：每步未命中 2-5.3K→~1K（省钱 + prefill 时间），零语义损失 |
| 风险 | 低：纯布局改动；DeepSeek 无断点 → 稳定段必须逐字节一致，靠 hit_tokens 实测回归 |
| 依赖 | 无 |
| 实现要点 | 动态块内「每步稳定」段（thread_running_summary、session_context_snapshot、company/relationships/memory、当前用户）移入 `prefix_cache_break` 之前；仅「每步变化」段（当前时间、current_run 状态、pending 消息）留尾部动态块 |
| 验收 | 指纹 chain 确认稳定段不再步变 + `prompt_cache_hit/miss_tokens` 回归 + 每步未命中 input 下降 |
| 工作量 | 小（`_prepare_messages` 布局改动） |

---

## P1（P0 后）

### P1-1 压缩模型分级（compact 摘要走便宜模型）

| 维度 | 内容 |
|---|---|
| 收益 | ★★：摘要调用 18-30s→~3-5s（36 步任务直接省 ~40s）；不占主模型限流 |
| 风险 | 中：摘要质量下降污染后续上下文 → benchmark 守完成率 |
| 依赖 | 无（group 模式已有 `resolve_multi_agent_compact_model` 先例） |
| 验收 | benchmark 完成率不降 + 摘要调用耗时下降 |
| 工作量 | 小 |

### P1-2 工具描述精简

| 维度 | 内容 |
|---|---|
| 收益 | ★~★★：schema 5,961→~4.5K，省窗口预算 + 每步注意力 |
| 风险 | 低（三类冗余已定位：跨工具引导重复/冗长散文/MCP Args 散文；风险边界清单见 backlog T1#1） |
| 依赖 | 无；Phase 1 内置工具 → Phase 2 MCP 工具 |
| 验收 | schema token 数下降 + 工具选择准确率 A/B |
| 工作量 | 中 |

### P1-3 跨 run 历史微调（可选，现量级已小）

| 维度 | 内容 |
|---|---|
| 现状 | 已落地 run 边界硬左边界 + 模板化单句摘要；run 起始窗口 ~4-5K |
| 剩余可选 | 会话多 run 后 prior-run summary 若累积变长 → 递归摘要；或按 Anthropic「清旧工具结果」进一步收紧 `model_visible_thread_messages` 保留的 prior tool facts |
| 收益/风险 | ★ / 低 |
| 验收 | 新 run 第 0 步窗口 token 监控（现 ~4-5K） |
| 工作量 | 小 |

---

## P2（观察后定）

### P2-1 验证门输入瘦身

- 收益 ★ / 风险低：每 run 一次性 8.7K→~4K。轨迹 JSON 内工具结果复用 P0-1 截断机制；rubric 作缓存前缀（OpenAI judge 模式，DeepSeek 靠字节稳定）。
- 依赖 P0-1。

### P2-2 压缩阈值 A/B

- 收益待定 / 风险低：现 0.8×108K 与 deepagents 0.85 同量级、设计正确；P0-1/2 落地改变历史增速后重测才有意义。变量：触发水位、keep 量（deepagents 0.10/OpenHands 压到一半/MemGPT 50%）。
- 依赖 P0-1、P0-2。

### P2-3 基础前缀余量确证（诊断，无代码收益）

- 26.6K 地板中 ~10-14K 归因未明（疑 DeepSeek 公共前缀 unit 与更早请求匹配）。等 provider 侧可观测（或抓 api 响应前缀信息）后确证；如有「旧前缀单元残留」可再评估清缓存手段。

---

## 执行顺序图

```
P0-1 截断 A/B ──┐
                ├──→ 重测基线 → P1-1 压缩模型分级 → P1-2 描述精简 → P1-3(可选)
P0-2 动态块拆分 ─┘                                    └──→ P2-2 阈值 A/B
                                                          └──→ P2-1 验证门瘦身
```

每项落地后跑：全量测试 + arch-guard + 部署（deploy.sh）+ `prompt_cache_hit/miss_tokens` 回归，并在本文档标注完成状态与实测数据。

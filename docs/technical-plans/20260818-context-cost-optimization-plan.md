# 上下文成本优化方案（P2-b）—— 工具 Schema 瘦身与缓存守护

日期：2026-08-18 ｜ 依据：生产实测探针 + 三参考项目源码（langgraph/langchain/deepagents）+ 官方一手文档（DeepSeek/Anthropic/OpenAI）

## 1. 现状量化（生产容器实测，agent=Android 工程师 03）

每轮模型调用 **11,648 input tokens**（日志 `[Token Cache] Read: 11648`，DeepSeek 缓存全命中）构成：

| 组成 | tokens | 占比 | 备注 |
|---|---|---|---|
| **工具 schema（59 个）** | **9,509** | **81.6%** | 描述 10,873 字符 + 参数 JSON 20,231 字符 |
| static prompt | 1,992 | 17.1% | soul 8,701 字符 + base prompt + 能力策略 + 技能目录 |
| dynamic prompt | 344 | 3.0% | 时间/组织上下文 |
| 历史消息 | 已被 `thread_visibility` 控制 | — | 全量 95 条 ≈ 31.5K tokens，每轮只注入最近部分 |

成本量级（DeepSeek 官方定价：命中输入 $0.022/M off-peak，miss $0.66/M，输出 $1.98/M）：

- 每轮全命中 ≈ **$0.00026**；丢缓存一次 ≈ **$0.0077（29 倍）**
- 平台日输入 160 万 ~ 1,687 万 tokens（8-16 峰值日）；输入:输出 ≈ 50:1
- **缓存命中是平台目前吃到的最大折扣（96.7%），任何优化不得破坏前缀缓存**

## 2. 最佳实践对照（证据）

| 来源 | 关键结论 |
|---|---|
| Anthropic Advanced Tool Use（官方实测） | 工具 schema 约 600–1,900 tokens/个；**>10 个工具或 >10K tokens 建议动态加载**（省 85% 上下文、选工具准确率 49%→74%）；58 工具 ≈ 55K tokens，极端案例 134K |
| Anthropic Writing Effective Tools | 工具响应 concise 72 vs detailed 206 tokens（省 2/3）；Claude Code 响应上限 25K；合并重叠工具 |
| DeepSeek KV Cache 文档 | 前缀完全匹配才命中、best-effort（数小时失效）；响应含 `prompt_cache_hit/miss_tokens` 可直接监控；支持 `$def/$ref` 复用 schema |
| DeepSeek 计费口径 | 1 英文字符 ≈ 0.3 token；1 中文字符 ≈ 0.6 token（100 字符 ≈ 30/60 tokens） |
| OpenAI Prompt Caching 101/201 | 前缀稳定是唯一法则：工具顺序逐字节一致、动态内容放末尾、缓存破坏原因清单可作回归检查表 |
| langgraph `pre_model_hook` | 输入层裁剪与持久化状态分离（裁剪不进 checkpoint） |
| langchain `SummarizationMiddleware` | 用**模型上报的真实 usage** 触发阈值；trigger/keep 双阈值；切割点跳过 ToolMessage |
| deepagents `FilesystemMiddleware` | 大工具结果「内容换文件引用」（head+tail 预览 + 路径）；`_truncate_args` 截旧工具参数（默认 2000 字符）；subagent 上下文重置 |

## 3. 分级优化方案

### L1：缓存命中率守护（零/微改动，性价比最高）

- 在 token 记账处落库 `prompt_cache_hit_tokens`/`miss_tokens`（响应 usage 已含），加监控/告警
- 前缀稳定性审计：工具 schema 顺序、system prompt、历史消息**严禁中间插入/重排**；动态字段（时间戳等）保持在末尾
- 任何 prompt/schema 改动上线后跑一轮「缓存命中回归」（对比改动前后 hit 数）
- 量化：每次丢缓存 = 每轮多付 $0.0074，大于其他所有优化收益总和

### L2：工具 schema 瘦身（改动集中、收益直接）★ 推荐先做

- **描述精简**：59 个工具描述共 10,873 字符 → 按「新员工一眼看懂」标准压到一句话用途+关键边界，目标减半（省 ~1,700 tokens）
- **参数 JSON 精简**：20,231 字符 → 删冗余参数描述/示例废话、长 enum 收敛（省 ~2,000 tokens）
- 合并重叠工具（Anthropic 观察：重叠工具分散模型注意力、增加选错率）
- 目标：9,509 → **~5,500 tokens/轮（省 34% 总输入）**
- 注意：schema 变更会破缓存一次（下一轮 miss ≈ $0.0077），瘦身收益永久，一次性成本可忽略

### L3：分组/动态工具注入（结构优化，收益最大）

- 方案 A（低风险）：按 agent 类型挂载高频工具子集（≤20–30 个常驻），切换只在会话边界做
- 方案 B（Anthropic 官方路线）：工具索引模式——每轮只注入工具名+一句话（~500 tokens），模型需要时再取完整 schema
- 目标：9,509 → **~2,500–3,500 tokens/轮（省 60–70% 总输入）**
- 附收益：选工具准确率提升（Anthropic 实测 +25pt），任务成功率是比 token 更值钱的回报

### L4：历史压缩阈值化（已有基础，核对阈值）

- 平台已有 `thread_visibility`（每轮注入裁剪）+ `run_compactor`（摘要）——核对触发阈值是否对齐 DeepSeek 缓存窗口
- 补「tool result clearing」：历史轮次中已消费的工具结果清成占位符（langchain `ClearToolUsesEdit` / deepagents 内容换文件引用，Clawith 已有文件系统可直接落地）
- 量化：历史 40K → 20K，每轮省 $0.00044，且避免 context rot

### L5：输出与错峰（次要）

- 工具返回值统一上限（如 ≤4K tokens/响应，参考 Claude Code 25K）
- 可批处理任务错峰 off-peak（北京 09:00–12:00、14:00–18:00 为 peak，价格翻倍）

## 4. 收益量化汇总

| 优化 | 每轮省 tokens | 每轮省钱（命中价） | 附收益 |
|---|---|---|---|
| L1 守住命中 | — | 避免 ×29 惩罚 | 稳定性 |
| L2 描述+参数精简 | ~3,700 | $0.000081 | 选工具质量 |
| L3 分组/动态注入 | ~6,000（叠加 L2 后） | $0.00013 | 选工具准确率 +25pt |
| L4 历史压缩 | 视会话长度 | 长会话 $0.0004+ | 防 context rot |

按峰值日 1,687 万 input 估算，L2+L3 全量落地后日 input 可降至 ~700–800 万（省 50%+），且所有节省都在缓存命中价之上再打折。

## 5. 实施顺序建议

1. **L1**（半天）：监控接入 + 缓存回归检查表
2. **L2**（1–2 天）：59 个工具描述/参数逐个体检精简——集中在 `agent_tools.py` 的工具定义处
3. **L3-A**（1 天）：按 agent 类型工具子集挂载（配置驱动）
4. **L3-B / L4**（按需）：依赖平台演进节奏

风险控制：每步改动用「缓存命中率回归」（改动前后 hit/miss 对比）+ 全量测试 + 一次灰度部署。

## 6. L1 落地记录（2026-08-18，提交见 git log）

- **解析与记账**：`extract_token_usage` 支持 DeepSeek `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`；无显式 miss 字段时按 `prompt_tokens - cached` 推导（OpenAI/Anthropic/Gemini 全覆盖）。新增落库字段：`agents.cache_miss_tokens_{today,month,total}`、`daily_token_usage.cache_miss_tokens`（迁移 f064）。
- **告警**：单次 miss ≥1024 tokens 且占比 ≥50% 时打 `[Token Cache] Low hit rate` WARNING——前缀被破坏的即时信号。
- **前缀稳定性实修**：`get_agent_tools_for_llm` 两处无 ORDER BY 查询（工具列表顺序随机 → 前缀不定）已加确定性排序（`AgentTool.tool_id`、`Tool.name`）。注意：上线首轮工具顺序变化会破缓存一次，此后稳定。

## 7. 缓存命中回归检查表（任何 prompt/schema 改动上线后必跑）

1. 部署前记录基线：`docker exec clawith-agent-postgres-1 psql -U clawith -d clawith -c "SELECT sum(cache_read_tokens), sum(cache_miss_tokens) FROM daily_token_usage WHERE date = current_date;"`
2. 部署后观察 30 分钟：新日志无 `[Token Cache] Low hit rate` 告警；`[Token Cache] API Provider` DEBUG 行 Read 值恢复部署前水平
3. 次日对比日报 `cache_miss_tokens` 占比（应 <10%；会话首轮 miss 属正常）
4. 检查表：工具 schema 顺序、system prompt、历史消息是否发生中间插入/重排；动态字段（时间戳）是否仍在消息尾部；工具集成员是否因配置变化而增减（配置变更属预期破缓存，需知晓而非意外）

## 8. 新发现：动态上下文破坏历史前缀缓存（2026-08-18 实测 + 三方调研）

**实测现象**：L1 告警上线后，长 run 持续 `[Token Cache] Low hit rate` 71-100%。命中恒为 system+tools ≈ 11.6k；历史消息（当前 run 已涨到 33k+）每轮全量 miss，input 每轮线性增长无上限。

**机制根因**（三方官方资料一致）：`_prompt_messages` 顺序 = `system → user(动态 JSON) → 历史`。DeepSeek 缓存只匹配「从开头起算的前缀单元」，中间的动态 user 每轮变化 → 其后历史全部脱离任何前缀单元 → 永远 miss；共同前缀检测也只能持久化「从开头起算」的前缀（现状只剩 system+tools）。不存在断点魔法（Anthropic 断点=开头到标记处的区间；OpenAI 无跳过机制；DeepSeek 无标记机制）。**唯一杠杆 = 消息顺序。**

**三参考项目共识**：稳定前缀 + 动态尾部 + 边界隔离。langgraph：静态 system 恒最前、其余顺序由 pre_model_hook 决定；langchain/deepagents：动态状态注入 system 尾块并用断点隔开（字节流上与「动态放最后」同构）。

**修复设计（方案 1，采纳）**：

```
现状: [system(static + runtime_instruction 偶发)] [user(dynamic_prompt + runtime_context 每轮变)] [历史 append]
新:   [system(纯静态)] [历史 append] [user(动态块: dynamic_prompt + runtime_context + runtime_instruction)]
```

- 配套 ①：`runtime_instruction`（system 的 dynamic_content）移出 system 并入末尾动态块——现状它在 system 尾部，一旦非空则 system 前缀每轮都断（隐患）
- 配套 ②：历史严格 append、不重排不摘要（压缩会重置缓存，OpenAI 201 §4.6）
- 预期命中（有出处）：第 1 轮冷；第 2-3 轮起共同前缀检测把 `system+历史` 持久化，此后每轮 miss 仅剩「新增消息 + 末尾动态块」；用 `prompt_cache_hit_tokens` A/B 验证
- 风险：末尾多一条 user 状态快照的模型语义（system 加固定说明「最后一条是运行时状态快照，非用户输入」）；DeepSeek 对 messages 序列约束宽松，tool-call 配对在历史内部不动，风险低
- 实施步骤：重排 `_prompt_messages` → runtime_instruction 并入动态块 → 更新消息顺序断言测试 → 全量 → 部署 A/B（对比 miss 率）

**详见**：`docs/prompt-cache-prefix-research-2026-08-18.md`（三方官方资料全文调研报告）

## 9. 多智能体审核结论与设计 v2（2026-08-18，四角度审核）

四份审核报告：运行时语义 / 架构合规 / 缓存命中模型（`docs/technical-plans/20260818-context-cache-hit-model-audit.md`）/ 测试与部署（`docs/technical-plans/20260818-message-reorder-test-deploy-review.md`）。

**审核确认**：方向正确、宪法无冲突、主体收益成立（重排后每轮 $0.0224 → $0.0020-0.0026，省 88-91%）。

**审核发现的关键问题与修订**：

1. **[高] 语义矛盾**（运行时审核）：动态块压尾会让 repair 指令/当前输入失去"最后一条 user"地位——修复循环（上限 1 次）可能失效、模型可能对快照输出文本。→ **修订：动态块插在"历史最后一条 user"之前**，尾控制消息（当前输入/repair/directive）保持最后。缓存代价仅每轮多 miss 一条输入（~几百 tokens），换取协议语义不变。
2. **[高] 实现陷阱**（测试审核）：client.py 六条序列化路径只在 `role=="system"` 分支拼 dynamic_content——动态块若用 `user+dynamic_content` 表达，runtime_instruction 会被**静默丢弃**（无异常）。→ **修订：动态块直接拼进 user content，client.py 一行不动**；保留"指令 vs 数据"两个小节标题（对冲指令权重下降，架构审核）。
3. **[高] 硬上限**（缓存审核 V1）：历史受 20 条滑动窗口 + token 预算从头裁剪——涨到上限后每轮滑动，历史命中归零。→ 本次不动窗口（当前 33k 在窗口内）；与 L4 压缩联动时改为「超限一次性摘要」而非每轮滑动（记 follow-up）。
4. **[中] system 前缀隐患**：`requires_confirmation` 确认态临时拼 static_prompt（既有行为，那轮全灭）；dynamic_prompt 含 `Current Time`（每轮变）不能留在 system。→ 修订：build_agent_context 加 `include_time` 参数（默认 True，chat 路径不变），durable 路径关掉并在动态块拼时间。
5. **[中] 指令权重**（架构审核）：runtime_instruction 是可信指令，移出 system 会降权重 → 动态块内用 `# Current Runtime Instruction`（指令）+ `Relevant Runtime Context`（数据）两小节明确边界；onboarding/A2A 指令遵守度测试必跑。
6. **[中] chat 路径分叉**：本次只改 durable 路径；chat（caller.py + context_compressor）列 follow-up，LLMMessage.dynamic_content 字段保留。
7. **[中] 测试锚点**：~6 个测试必破 + ~6 个语义漂移（`test_agent_runtime_model_step_service.py`），全部更新；新增「system 前缀逐字节稳定」回归测试（缓存守卫核心）。
8. **[中] 部署**：无 DB 迁移，回滚=纯镜像切换；部署前打 pre- 标签；低峰部署；上线首轮每活跃会话 miss 一次（可忽略）；观察 ≥48h 分层命中率（第 1/2-5/6+ 轮）。

**设计 v2 定稿**：

```
S = static_prompt + dynamic_prompt(不含 Current Time) + 固定说明
D = user(runtime_context JSON + runtime_instruction + Current Time + current_user)
C = 历史最后一条 user（当前输入/repair 指令/directive 兜底）

新序列: [S] [历史 append，不含最后一条 user] [D] [C]
```

- 前缀 = S + 历史主体（稳定 append）；每轮 miss ≈ D + C + 新增回合（~1.5-2.5k）
- C 每轮位置迁移导致其首轮 miss，稳态后与纯 append 等价（已推演）
- follow-up 清单：qwen cache_control 标记改打历史尾部；确认态 system 改动纳入缓存监控；滑窗上限联动 L4；chat 路径统一布局




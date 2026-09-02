# 参考项目索引 — 按使用热度 + 问题域查参考

> 用途：做方案/设计时，按问题类型快速定位该参考哪个项目、看什么。
> 主数据源：Agent 记忆 `reference-projects`（58 个本地仓库的路径、状态、URL 复核结论，每日保鲜）。
> 本文只做「热度分层 + 问题域细分类」索引；仓库路径变更时以记忆文件为准，不在本文重复维护路径细节。

## 统计口径

热度 = 2026-09-03 统计：`docs/analysis` + `docs/technical-plans` + `docs/adr` 中**引用该项目的文档数**（不含本文档与记忆本身；名称大小写不敏感子串匹配，与真实读码深度无关，只反映方案引用频率）。带 ★ 的有整库研究报告。

## 一、使用热度分层

### T0 — 生产在用（Clawith 基础设施，勿当外部参考）
- **langgraph / langchain / deepagents**：技术栈本体（引用 28/13/11），查 API 签名与内部实现
- **langfuse**：自托管可观测（引用 24 文档 + 13 记忆，全清单最高）；trace/评分/时序见 [[langfuse-utc-and-flush-timing]]
- **codebase-memory-mcp**：代码知识图谱 MCP（守护进程见 [[codebase-memory-daemon-lifecycle]]）
- **headroom**：上下文压缩 MCP/CLI（工具在用；方案文档暂零引用）
- **ponytail**：skills 已装本 Agent（[[ponytail-skills-install]]）

### T1 — 高频引用 / 深度研究（做方案优先查这批）
| 项目 | 引用 | 主用途（据实际引用文档聚类） |
|---|---|---|
| OpenHands | 19 | 任务拆解/生命周期/沙箱/上下文瘦身对照 |
| SWE-agent | 12 | ACI 设计、命令生命周期、沙盒终端态 |
| codex | 11 | 上下文压缩/文件路径接地/CLI 交互 |
| gemini-cli | 7 | 上下文瘦身/路径接地/前端交互 |
| E2B | 7 | 云沙箱、workspace 发布同步 |
| SWE-bench | 6 | 编程能力验收口径 |
| deepseek-harness ★ | 6 | 上下文压缩/摘要/token 计量（首选对照，研究报告 20260829） |
| letta-code ★ | 6 | 记忆分层/自进化/上下文瘦身（研究报告 20260903） |
| claude-agent-acp (acp) | 6 | Agent 互操作协议、工具路径契约 |
| A2A | 6 | Agent 协议、工具 span 规划 |
| openai-agents | 5 | 流式泵调度/任务状态 |
| gptme | 5 | 最小骨架、沙盒执行 |
| Terminal-Bench | 4 | 终端能力验收口径 |
| software-agent-sdk | 4 | event-stream/工具抽象 |
| dify | 4 | 多租户、维护者权限模型 |
| AgentTeams ★ | 1 | 多 Agent 编排/资源契约/凭据隔离（研究报告 20260903，引用刚起步） |

### T2 — 定向引用（特定主题时查）
OpenViking 3（token 优化/记忆+RAG 统一）、mem0 3（记忆）、CubeSandbox 3（沙箱）、RE-Bench 2（评估）、claw-code 2（血缘上游）、rtk 1（token 经济）、litellm 1（模型网关）、codegraph 1（知识图谱对比）、openhuman 1（local-first 记忆）。

### T3 — 收录未引用（候选池，暂无方案引用）
crewAI、LLMLingua、caveman、hermes-agent、andrej-karpathy-skills、ruflo、oh-my-pi、serena、paseo、open-agents、hello-agents、MonkeyCode、new-api、casdoor、cognee、RagaAI-Catalyst、deepeval、openai/evals、microsoft/agent-framework、DeepSeek-Reasonix、omnigent、agent-browser、superagent、awesome-agent-skills、agent-device、OpenHands-CLI（上游已弃维护，只看不动）。
> T3 不意味着低价值：多为「问题域尚未动工」的预置候选（如网关选型、评估框架）。动对应问题域时先查 T3 再动手。

## 二、问题域细分类（14 域，按 T0→T3 排列）

1. **图框架/状态/checkpoint/流式**：langgraph、langchain、deepagents、openai-agents
2. **上下文压缩/摘要/token 成本**：headroom(T0)、deepseek-harness★、OpenHands、letta-code★、codex、OpenViking、rtk、caveman、LLMLingua
3. **任务/命令生命周期与幂等**：OpenHands、SWE-agent、software-agent-sdk、codex、gemini-cli、AgentTeams★（TeamHarness _safe_id）
4. **可观测/评估平台**：langfuse(T0)、RagaAI-Catalyst、deepeval、openai/evals
5. **沙箱/workspace 执行**：E2B、CubeSandbox、gptme、SWE-agent、codex、OpenHands 沙箱封装
6. **记忆/自进化**：letta-code★、mem0、OpenViking、openhuman、dify（维护者权限门控）
7. **多 Agent 编排**：AgentTeams★、crewAI、ruflo、paseo、omnigent（反面教材：Cognition《Don't Build Multi-Agents》）
8. **Agent 互操作协议**：A2A、claude-agent-acp(acp)
9. **网关/IAM/多租户**：litellm、new-api、casdoor、dify、MonkeyCode
10. **评估基准**：SWE-bench、Terminal-Bench、RE-Bench
11. **Skills 生态**：ponytail(T0)、andrej-karpathy-skills、awesome-agent-skills、caveman、hermes-agent
12. **安全护栏**：superagent（prompt 注入/数据泄露）、AgentTeams（凭据隔离/consumer token）
13. **前端/渠道/移动端**：gemini-cli、codex、gptme、agent-browser、agent-device、OpenHands-CLI
14. **代码知识图谱**：codebase-memory-mcp(T0)、codegraph

## 三、问题 → 首选参考（映射表）

| 你要解决的问题 | 首选参考 |
|---|---|
| LangGraph State/reducer/checkpoint/interrupt 怎么写 | `langgraph` 源码 + `deepagents` |
| Agent 循环设计（规划→编码→执行→纠错→迭代） | `langgraph/examples/code_assistant`（已归档仍可读）、OpenHands、SWE-agent |
| 上下文压缩/摘要/token 成本 | `deepseek-harness` compaction 家族 + 研究报告、headroom、LLMLingua、rtk、caveman |
| DeepSeek 前缀缓存稳定性 | DeepSeek-Reasonix |
| 自进化记忆/记忆整理/回忆检索 | letta-code memory-v2/reflection/recall 子代理 + memory blocks 三原则（泛化不记事件、recall 可推导不写、过大触发体检） |
| 记忆文件系统 git 化/并发写锁/跨 agent 记忆隔离 | letta-code MemFS（memory-git/worktree/config-lock、cross-agent-guard、memory-confinement fail-closed） |
| 多 Agent 编排/协作 | AgentTeams（K8s 资源契约 + reconcile 生命周期 + TeamHarness 任务分配）、crewAI、ruflo、paseo、omnigent（反面教材对照：Cognition《Don't Build Multi-Agents》） |
| agent 生命周期声明式管理/reconcile/多实例隔离 | AgentTeams CRD 五资源 + LabelController + member phase 状态机 |
| 凭据隔离/网关持密钥/worker 降权令牌 | AgentTeams（Higress + consumer token + MCP 零凭据暴露） |
| 多租户/权限/应用编排 | dify、MonkeyCode |
| 模型网关/多租户模型路由/成本/限流 | litellm、new-api |
| 认证授权/SSO/IAM（agent-first） | casdoor |
| 可观测性 trace/评分/时序 | langfuse（对照：RagaAI-Catalyst） |
| LLM-as-judge 评估框架 | deepeval、openai/evals |
| Agent 互操作协议 | A2A、claude-agent-acp（ACP） |
| 沙箱/代码执行安全 | E2B、CubeSandbox、OpenHands 沙箱封装 |
| 安全护栏（prompt 注入/数据泄露） | superagent |
| Skills 机制设计 | andrej-karpathy-skills、ponytail、hermes-agent、awesome-agent-skills（素材目录） |
| 代码知识图谱 | codebase-memory-mcp、codegraph |
| 浏览器自动化（agent 用） | agent-browser |
| CLI 交互/前端体验 | gemini-cli、codex、gptme |
| 移动端设备控制 | agent-device |
| 评测/验收口径 | SWE-bench、Terminal-Bench、RE-Bench |

## 四、注意与红线

- **OpenHands-CLI** 上游 2026-08-11 起标记「no longer actively maintained」，只看不动。
- **daytona** 2026-06 起核心开发转私有，仅剩 README 壳，不入清单。
- 做方案时**禁止**因本地仓库可即时读取就把对比范围退化成「只对比本地三库」；URL 资料（方法论文章/官方文档）与评估基准必须一并覆盖。
- 官方设计模式/cookbook 无独立落地页：`langgraph/examples/` 已冻结归档（仍可读作历史参考），新内容随 docs.langchain.com 发布。
- 热度分层每季度或有重大方案后重算一次（grep 口径见文首），T3 项目被引用后晋级。

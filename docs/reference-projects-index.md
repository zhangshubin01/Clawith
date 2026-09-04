# 参考项目索引 — 按使用热度 + 问题域查参考

> 用途：做方案/设计时，按问题类型快速定位该参考哪个项目、看什么。
> 分层明细（T0–T3 清单、引用数、统计口径）与本地仓库路径/URL 复核在 Agent 记忆 `reference-projects`——**唯一事实源**，每日保鲜同步；本文不复刻，避免两处漂移。
> 本地仓库计数：89 个（2026-09-04 +deepagents-in-action；easy-langent 收录未克隆）。

## 一、使用热度分层

分层速查（全清单与引用数见记忆 `reference-projects` 首段）：

- **T0 生产在用**（langgraph/langchain/deepagents、langfuse、codebase-memory-mcp、headroom、ponytail）：Clawith 基础设施，查 API/内部实现，勿当外部参考抄。
- **T1 高频引用 / 深度研究**：做方案优先查这批。
- **T2 定向引用**：特定主题时查。
- **T3 收录未引用**：不意味着低价值，多为「问题域尚未动工」的预置候选；动对应问题域时先查 T3 再动手。
- **★ = 有整库研究报告**，报告在 `docs/technical-plans/`，正文按「研究报告 YYYYMMDD」标注。

## 二、问题域细分类（14 域，按 T0→T3 排列）

1. **图框架/状态/checkpoint/流式**：langgraph、langchain、deepagents、openai-agents、easy-langent（入门全栈）、deepagents-in-action（harness 分层/Event Streaming 章）
2. **上下文压缩/摘要/token 成本**：headroom(T0)、deepseek-harness★、OpenHands、letta-code★、codex、OpenViking、rtk、caveman、LLMLingua、context_engineering、how_to_fix_your_context(归档)
3. **任务/命令生命周期与幂等**：OpenHands、SWE-agent、software-agent-sdk、codex、gemini-cli、AgentTeams★（TeamHarness _safe_id）、open-swe、langgraph-codeact(归档)、langgraph-bigtool
4. **可观测/评估平台**：langfuse(T0)、RagaAI-Catalyst、deepeval、openai/evals、openevals、agentevals
5. **沙箱/workspace 执行**：E2B、CubeSandbox、gptme、SWE-agent、codex、OpenHands 沙箱封装、langchain-sandbox(归档)、openshell-deepagent、deepagents-in-action（ch10-11 沙箱/文件权限）
6. **记忆/自进化**：letta-code★、mem0、OpenViking、openhuman、dify（维护者权限门控）、langmem、memory-agent、langgraph-reflection(归档)、deepagents-in-action（ch8 长期记忆）
7. **多 Agent 编排**：AgentTeams★、crewAI、ruflo、paseo、omnigent（反面教材：Cognition《Don't Build Multi-Agents》）、langgraph-supervisor-py、langgraph-swarm-py、deepagents-in-action（ch5/6/16 子 Agent）
8. **Agent 互操作协议**：A2A、claude-agent-acp(acp)、langchain-mcp-adapters、agent-protocol
9. **网关/IAM/多租户**：litellm、new-api、casdoor、dify、MonkeyCode、agent-auth-payments(归档)、open-agent-platform(归档)
10. **评估基准**：SWE-bench、Terminal-Bench、RE-Bench、skills-benchmarks
11. **Skills 生态**：ponytail(T0)、andrej-karpathy-skills、awesome-agent-skills、caveman、hermes-agent、langchain-skills
12. **安全护栏**：superagent（prompt 注入/数据泄露）、AgentTeams（凭据隔离/consumer token）、deepagents-in-action（ch9 HITL 审批机制）
13. **前端/渠道/移动端**：gemini-cli、codex、gptme、agent-browser、agent-device、OpenHands-CLI、agent-chat-ui、agent-inbox、open-canvas(归档)、deep-agents-ui(归档)、social-media-agent
14. **代码知识图谱**：codebase-memory-mcp(T0)、codegraph

## 三、问题 → 首选参考（映射表）

| 你要解决的问题 | 首选参考 |
|---|---|
| LangGraph State/reducer/checkpoint/interrupt 怎么写 | `langgraph` 源码 + `deepagents` |
| 官方文档精确页/API 一手资料（LangGraph、dcode、LangSmith API） | `docs.langchain.com` 全站 llms.txt 索引（2026-09-03：langsmith 456、oss/python 370、langgraph 43、deepagents 41、langchain 76 页）+ agent 接入说明 `use-these-docs.md` |
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
| 任务队列/异步后台执行对照（agent_runs 队列模式） | open-swe（研究报告 `20260903-open-swe-task-queue-study.md`） |
| 大工具集 schema/调用管理 | langgraph-bigtool（研究报告 `20260903-langgraph-bigtool-tool-retrieval-study.md`） |
| agent 轨迹评估（judge 平台对照） | agentevals、openevals |
| HITL/审批流前端交互 | agent-inbox（研究报告 `20260903-agent-inbox-hitl-ux-study.md`）、social-media-agent |
| 聊天流式渲染/Artifact 面板 UX | agent-chat-ui、open-canvas（归档） |
| 持久记忆/记忆 SDK | langmem、memory-agent |
| 轻量沙箱（Pyodide/Deno） | langchain-sandbox（归档） |
| supervisor/swarm 多 Agent 模式 | langgraph-supervisor-py、langgraph-swarm-py |
| deepagents 上层 harness 生产实践（沙箱/权限/HITL/评估/流式） | deepagents-in-action 课程（研究报告 `20260904-deepagents-in-action-study.md`） |
| Skills 能力基准 | skills-benchmarks |
| 多租户计费/请求额度 | agent-auth-payments（归档） |
| 上下文精简手法（官方 notebook） | context_engineering、how_to_fix_your_context（归档） |

## 四、langchain-ai 组织仓库精选（2026-09-03 GitHub API 调研，239 库中筛出）

> 已本地克隆至 `/Users/shubinzhang/Documents/UGit/<同名目录>`（2026-09-03，--depth 1 浅克隆，随每日保鲜任务 pull；langgraph/langchain/deepagents 三库此前已在本地）。open-swe/langgraph-bigtool/agent-inbox 三库已出专项研究报告（20260903）并晋级 T2（分层见记忆 `reference-projects`），其余为 T3 候选，被方案引用后晋级。
> 状态=调研当日（活跃/归档）；归档库只读设计思想，API 版本滞后于 Clawith 当前栈。
> 不收录：20+ 云厂商/向量库集成包（langchain-aws/azure/google/nvidia/mongodb/pinecone/milvus…）与课程类仓库（academy/101 系列），无参考价值。

### 核心栈（T0 已含 Python 侧，此处补 TS 侧）
| 仓库 | ★ | 状态 | 备注 |
|---|---|---|---|
| langgraph / langchain / deepagents | 41k/145k/28.9k | 活跃 | T0 技术栈本体，见热度分层 |
| langchainjs | 18.2k | 活跃 | TS 侧依赖本体 |

### Agent 循环/队列/上下文工程
| 仓库 | ★ | 状态 | 参考点 |
|---|---|---|---|
| open-swe | 10.7k | 活跃 | 异步编码 Agent：队列化任务/后台执行/状态机，对照 agent_runs 队列模式。**专项研究 20260903**（`20260903-open-swe-task-queue-study.md`）：无自建队列，生命周期外包 LangGraph Platform 四原语，自研增量=统一入队+completion webhook+reconcile 兜底；Clawith 同步预抢取消更强 |
| context_engineering | 200 | 活跃 | 官方上下文工程 notebook，对照 compactor |
| langmem | 1.6k | 活跃 | 记忆 SDK：memory store + prompt 优化 |
| memory-agent | 470 | 活跃 | 持久记忆 agent 完整模板 |
| langgraph-codeact | 734 | 归档 | codeact 工具调用循环模式 |
| how_to_fix_your_context | 551 | 归档 | 官方上下文修复 notebook |
| langgraph-reflection | 185 | 归档 | 反思式自我改进循环 |
| open_deep_research | 12.7k | 归档 | 多步研究循环经典实现 |

### 工具 schema/调用规模
| 仓库 | ★ | 状态 | 参考点 |
|---|---|---|---|
| langgraph-bigtool | 557 | 活跃 | 大工具集（数百个）的检索式 tool 选择，直击 Clawith 工具 schema 规模问题。**专项研究 20260903**（`20260903-langgraph-bigtool-tool-retrieval-study.md`）：单 ReAct 图+无 LLM 的 select_tools 检索节点、Store 语义索引只索引 description；其逐轮增量 bind_tools 破坏前缀缓存，与 Clawith「全量绑定+字节稳定」冲突，不可照搬 |

### 前端/交互
| 仓库 | ★ | 状态 | 参考点 |
|---|---|---|---|
| agent-chat-ui | 3.1k | 活跃 | 流式渲染/断线重连，对照 WS 状态机 |
| agent-inbox | 1.1k | 活跃 | HITL inbox UX，对照 approval_requests 审批流。**专项研究 20260903**（`20260903-agent-inbox-hitl-ux-study.md`）：零后端纯前端+SDK 直连，HumanInterrupt 四开关 config 纯配置驱动；无鉴权/claim/审计，多用户协作缺失 |
| social-media-agent | 2.8k | 活跃 | HITL+审批全栈 TS 模板 |
| open-canvas | 5.5k | 归档 | Artifact 面板 UX，对照 artifact_refs/freshness 账本 |
| deep-agents-ui | 1.7k | 归档 | subagent 树可视化 |

### 沙箱/代码执行
| 仓库 | ★ | 状态 | 参考点 |
|---|---|---|---|
| langchain-sandbox | 242 | 归档 | Pyodide+Deno 沙箱 |
| openshell-deepagent | 187 | 活跃 | Deep Agents + OpenShell 策略治理沙箱 |

### 评估/观测
| 仓库 | ★ | 状态 | 参考点 |
|---|---|---|---|
| openevals | 1.2k | 活跃 | 现成 evaluator 集，对照 judge 平台规则 |
| agentevals | 714 | 活跃 | agent 轨迹级 evaluator，与 judge 平台 run_outcome/attempt_count 口径最近 |
| langsmith-sdk | 1.0k | 活跃 | 观测 SDK 结构（Clawith 用 Langfuse，对照客户端设计） |
| skills-benchmarks | 116 | 活跃 | skills 能力基准手法 |

### 多 Agent 编排/协议
| 仓库 | ★ | 状态 | 参考点 |
|---|---|---|---|
| langgraph-supervisor-py | 1.7k | 活跃 | supervisor 模式 |
| langgraph-swarm-py | 1.6k | 活跃 | swarm 模式 |
| langchain-mcp-adapters | 3.6k | 活跃 | MCP 工具适配 |
| agent-protocol | 667 | 活跃 | agent 互操作协议 |

### 多租户/计费/平台化
| 仓库 | ★ | 状态 | 参考点 |
|---|---|---|---|
| agent-auth-payments | 201 | 归档 | auth + 请求额度 + 支付全栈 |
| open-agent-platform | 1.9k | 归档 | no-code 平台：租户隔离/agent 配置化 |
| langchain-skills | 1.2k | 活跃 | 官方 skills 目录格式 |
| helm | 96 | 活跃 | 官方部署图表 |
| deployment-cookbook | 15 | 活跃 | 部署示例 cookbook |

## 五、注意与红线

- **OpenHands-CLI** 上游 2026-08-11 起标记「no longer actively maintained」，只看不动。
- **daytona** 2026-06 起核心开发转私有，仅剩 README 壳，不入清单。
- **langchain-ai 组织库**多为官方模板/demo：归档库（open-canvas、open_deep_research、langchain-sandbox 等）API 版本滞后，只读设计思想勿照搬代码；集成包与课程仓库不收录（见第四节导言）。
- 做方案时**禁止**因本地仓库可即时读取就把对比范围退化成「只对比本地三库」；URL 资料（方法论文章/官方文档）与评估基准必须一并覆盖。
- 官方设计模式/cookbook 无独立落地页：`langgraph/examples/` 已冻结归档（仍可读作历史参考），新内容随 docs.langchain.com 发布。
- 热度分层每季度或有重大方案后重算一次（grep 口径见记忆 `reference-projects` 首段），T3 项目被引用后晋级。

# 参考项目索引 — 按问题类型查参考

> 用途：做方案/设计时，按问题类型快速定位该参考哪个项目、看什么。
> 主数据源：Agent 记忆 `reference-projects`（58 个本地仓库的路径、状态、URL 复核结论，每日保鲜）。
> 本文只做「问题 → 参考」索引；仓库路径变更时以记忆文件为准，不在本文重复维护路径细节。

## 一、分类总览（58 个项目，5 大类）

### A. 框架源码三库 —— 查 API 签名、内部实现、State 定义
- `langgraph`：State/reducer/checkpoint/interrupt/子图等核心实现
- `langchain`：组件与集成层
- `deepagents`：官方高层 Agent 封装（文件系统工具、子 Agent）

### B. 开源实战 Agent 项目 —— 学架构、任务拆解、状态/工具链设计
- OpenHands / OpenHands-CLI / software-agent-sdk、gemini-cli、codex、gptme、SWE-agent、openai-agents-python
- deepseek-harness（上下文压缩设计对比首选）、crewAI（多 agent 编排）、mem0（记忆）、hiclaw/AgentTeams（多智能体 OS）
- 2026-09 新增：omnigent（meta-harness 编排 + 策略强制）、DeepSeek-Reasonix（DeepSeek 前缀缓存稳定）、agent-browser（浏览器自动化 CLI）

### C. 与 Clawith 直接对标的同赛道项目
- claw-code（血缘上游 harness）、dify（多租户平台）、langfuse（自托管可观测性）
- 上下文/成本压缩：headroom、LLMLingua、rtk、caveman
- 代码知识图谱：codebase-memory-mcp、codegraph
- 记忆/RAG/Skills 统一：OpenViking；沙箱：CubeSandbox；移动端：agent-device
- 2026-09 新增：litellm / new-api / casdoor（AI 网关 + IAM）、E2B（云沙箱）、A2A（agent 互操作协议）、letta-code / cognee（记忆）、RagaAI-Catalyst（agent 可观测对照）、superagent（安全护栏）

### D. 评估基准与评估框架 —— 能力验收口径
- 数据集：SWE-bench（通用编程）、Terminal-Bench（终端级）、RE-Bench（AI 研发）
- 2026-09 新增：deepeval、openai/evals（LLM 评估框架 + 基准注册）

### E. 泛 Agent / Skills / 工具生态 —— 机制与协议参考
- ponytail、andrej-karpathy-skills、hermes-agent、openhuman、serena、oh-my-pi、paseo、ruflo、open-agents、hello-agents、claude-agent-acp、MonkeyCode
- 2026-09 新增：microsoft/agent-framework（官方多语言框架）、awesome-agent-skills（1000+ skills 目录）

## 二、问题类型 → 首选参考

| 你要解决的问题 | 首选参考 |
|---|---|
| LangGraph State/reducer/checkpoint/interrupt 怎么写 | `langgraph` 源码 + `deepagents` |
| Agent 循环设计（规划→编码→执行→纠错→迭代） | `langgraph/examples/code_assistant`（已归档仍可读）、OpenHands、SWE-agent |
| 上下文压缩/摘要/token 成本 | `deepseek-harness` compaction 家族 + 研究报告、LLMLingua、rtk、caveman |
| DeepSeek 前缀缓存稳定性 | DeepSeek-Reasonix |
| 多租户/权限/应用编排 | dify、MonkeyCode |
| 模型网关/多租户模型路由/成本/限流 | litellm、new-api |
| 认证授权/SSO/IAM（agent-first） | casdoor |
| 可观测性 trace/评分/时序 | langfuse（对照：RagaAI-Catalyst） |
| LLM-as-judge 评估框架 | deepeval、openai/evals |
| 记忆机制 | letta-code、cognee、mem0、OpenViking、openhuman |
| 多 Agent 编排/协作 | crewAI、hiclaw(AgentTeams)、ruflo、paseo、omnigent（反面教材对照：Cognition《Don't Build Multi-Agents》） |
| Agent 互操作协议 | A2A、claude-agent-acp（ACP） |
| 沙箱/代码执行安全 | E2B、CubeSandbox、OpenHands 沙箱封装 |
| 安全护栏（prompt 注入/数据泄露） | superagent |
| Skills 机制设计 | andrej-karpathy-skills、ponytail、hermes-agent、awesome-agent-skills（素材目录） |
| 代码知识图谱 | codebase-memory-mcp、codegraph |
| 浏览器自动化（agent 用） | agent-browser |
| CLI 交互/前端体验 | gemini-cli、codex、gptme |
| 移动端设备控制 | agent-device |
| 评测/验收口径 | SWE-bench、Terminal-Bench、RE-Bench |

## 三、注意与红线

- **OpenHands-CLI** 上游 2026-08-11 起标记「no longer actively maintained」，只看不动。
- **daytona** 2026-06 起核心开发转私有，仅剩 README 壳，不入清单。
- 做方案时**禁止**因本地仓库可即时读取就把对比范围退化成「只对比本地三库」；URL 资料（方法论文章/官方文档）与评估基准必须一并覆盖。
- 官方设计模式/cookbook 无独立落地页：`langgraph/examples/` 已冻结归档（仍可读作历史参考），新内容随 docs.langchain.com 发布。

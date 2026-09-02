# Letta Code 源码研究报告

日期：2026-09-03
状态：**完成**（分析基于本地仓库 `/Users/shubinzhang/Documents/UGit/letta-code` HEAD `6394c7f`，v0.31.11，已与 origin/main 同步）
定位：参考资料研究，非实现方案。对照 Clawith 自进化记忆 P0（[[p0-memory-loop-verification]] 的 A/B/G/C 通道）与 G1 缺口（`20260902-self-evolution-gap-closure-plan.md`）。

## 0. 项目概览

- **是什么**：Letta（f.k.a MemGPT）的真实代码库——「stateful agent harness」，核心理念「learning in token-space」：Agent 是跨会话持续存在的实体，通过重写自己的系统提示词/记忆文件/技能（乃至 harness 本身的 mods）自我进化。原 letta 主仓 2026 年起沦为落地页，代码迁此。
- **技术栈**：TypeScript（bun 构建，`bun.lock`/`biome.json`），src 约 48 万行（含测试）。单仓库含 CLI(TUI)、desktop、web、channels、App Server。
- **入口**：`src/agent/prompts/letta.md` 是主系统提示词，前 40 行就把整个上下文架构讲清楚了——这是全库最值得先读的文件。
- **与 Clawith 的对标关系**：同为「自进化记忆 + skills + 多 channel + 沙箱 + 审批」的 agent 平台。区别：letta-code 是**单用户 harness + 云托管 Agent 状态**（多环境多机器，但非多租户平台），Clawith 是多租户企业平台。看设计不看实现移植（TS/bun vs Python/FastAPI）。

## 1. 上下文架构（三层记忆）

`letta.md` 定义的三层，与 Clawith 的 Focus 投影 / 自进化记忆直接同构：

| 层 | letta 机制 | 特性 |
|---|---|---|
| 经验（不可变） | recall memory | 全部消息历史由 harness 自动存，**agent 不可改写**；当前会话只有最近消息 + 被逐出消息的摘要；查旧上下文靠 recall 子代理 |
| 记忆块（in-context，可编辑） | memory blocks | 系统提示词的**可编辑分段**，每块有 `name`+`description`；`.mdx` 文件带 YAML frontmatter（`src/agent/prompts/*.mdx`：persona/human/project/style 等）。「系统提示词学习」：发现纠正过的假设/用户偏好/错误模式就写进去，要求**泛化而不是记事件** |
| 外部记忆（out-of-context） | MemFS | git 追踪的记忆文件系统，见下节 |

关键原则（`letta.md`，可直抄进 Clawith 记忆规则）：
- 记忆块是「最贵的不动产」：只放塑造身份/行为的规则 + **索引**，细节放外部记忆。
- `[[path]]` 引用 = 记忆的突触，越用越强。
- **能从 recall 搜出来或重读文件推导的，一律不写进记忆块**——防 context rot。
- 绝不写 secret（记忆是 git 追踪的、可同步出机器；secret 进 harness secrets store）。
- 系统提示词过大时 harness 会标记 `/doctor`（`context-doctor` skill）提示瘦身。

## 2. MemFS：git 化记忆文件系统

- 布局（`src/agent/subagents/builtin/memory-v2.md`）：根 `MEMORY.md`（无 frontmatter 的索引，普通 md 链接指向 core 文件 + deferred 索引）→ core 文件（`persona.md`/`human.md`，**始终注入**）→ `notes/`、`archive/`（deferred 目录，各自有 MEMORY.md，**不注入、靠索引发现**）→ `skills/`（程序记忆）→ `.sync-state.json`（内部同步状态）。
- 规则：core 之外每个 md 文件必须有且仅有 `name`+`description` frontmatter；**文件路径即记忆标签**（`project/tooling/bun.md` → label `project/tooling/bun`）；文件增删在下次 CLI 启动时同步为记忆条目。
- 体积约束：`.memfs.config.json`（`src/agent/memory-constraints.ts`）默认 `maxDepth: 2`、`maxFileCharacters: 20_000`。
- git 机制全套：`memory-git.ts`（precommit/postcommit、retry、signing、auth、config-lock 防并发写）、`memory-worktree.ts`、`memory-git-sync.ts`（`src/reminders/` 下，提醒引擎联动同步）。可设 `git@github.com:...` 远端（`/memory-repository set`）。
- 上下文窗管理：`src/agent/max-context.ts` 最小窗口 `MIN_CONTEXT_WINDOW_TOKENS = 30_000`，`/max-context` 可设 agent/conversation 两级。

## 3. 记忆编辑闭环：版本化自治子代理 + fail-closed 沙箱

子代理 prompt 即 frontmatter 配置（`src/agent/subagents/builtin/*.md`）：`name/description/tools/model/launchProfile/fork`。两代并存（v1+v2），prompt 迭代走版本文件——与 Clawith compactor 的 prompt 版本管理同思路：

| 子代理 | 职责 |
|---|---|
| `memory-v2.md` | **记忆碎片整理**：拆分多主题文件、`/` 层级嵌套、一事实一规范位置、无硬性文件数目标（明确反对配额式整理） |
| `reflection-v2.md` | 后台反思最近会话，更新记忆并维护技能（`launchProfile: memory-subagent`） |
| `recall.md` | 检索过去交互（`fork: true`，可并行调用） |
| `history-analyzer-v2.md` | 历史分析 |
| `init-v2.md` / `fork.md` / `general-purpose.md` | 初始化/派生/通用 |

安全机制（`src/memory-confinement.ts` → `src/permissions/memory-confinement-launcher.ts`）：无人值守记忆子代理运行在 **fail-closed 文件系统策略**下——可广读宿主、只写 harness 状态与自己的记忆、**不能读写其他 agent 的记忆**（`cross-agent-guard.ts`）；**检测不到受支持的内核沙箱直接 throw，绝不静默降级**。沙箱后端：`src/sandbox/bwrap.ts`（bwrap）+ `seatbelt.ts`（macOS sandbox-exec）+ `availability.ts` 探测。对 Clawith 的 bwrap 0.11/0.12 教训（[[bwrap-setuid-0.12-execute-code-broken]]）是现成的对照样本。

## 4. 其余 harness 面（按 Clawith 关注度排）

- **权限/审批**：`src/permissions/` —— 权限模式（mode.ts）、shell 命令规范化（`shell-command-normalization.ts`）、workspace-sandbox、read-only-shell、sandbox-gate/sandbox-policy；审批中断恢复靠注入 `.txt` 提醒（`approval_recovery_alert.txt`/`interrupt_recovery_alert.txt`，见 `prompts/README.md`）。
- **调度**：`src/cron/scheduler.ts`（cron-file、parse-interval、run-log）+ `src/reminders/engine.ts`（catalog/state/交互提醒）+ heartbeats；`src/schedules.ts`。
- **channels**：discord/slack/telegram/signal/whatsapp/custom/transcription 各一目录，带 debounce 测试（`telegram-debounce`、`discord-debounce`）；`src/queue/` + `src/websocket/`（listener/helpers）。
- **skills**：三级作用域（global `~/.letta`、项目 `.agents/skills`、agent 级存 MemFS）；内置 24 个 skill 含 `context-doctor`（/doctor 记忆体检）、`creating-skills`、`using-mcp-tools`、`scheduling-tasks` 等。
- **hooks**：`src/hooks/executor.ts` 在 agent 执行关键点跑自定义脚本。
- **自我修改**：`src/mods/` —— agent 可改 harness 自身（含 mod 环境生成、生命周期测试）。
- **更新**：`src/updater/` startup-auto-update；**telemetry**：`src/telemetry/`。

## 5. 基准提示词库（值得抄的工程实践）

`src/agent/prompts/` 里 `source_claude.md`/`source_codex.md`/`source_gemini.md` 是**逐字级**的竞品系统提示词快照（Claude Code v2.1.50 渲染版、Codex gpt-5.5 `instructions_template`+personality 占位符替换、Gemini CLI snippets 渲染版），`/system` 命令可切换用于 **benchmarking**。每个快照带来源/版本/渲染条件说明，且有 `.github/workflows/codex-agent-watch.yml` 监控上游稳定版漂移。Clawith 若要对比自家系统提示词与竞品的行为差异，这是现成素材和流程模板。

## 6. 可迁移点 → Clawith 映射

| # | letta 机制（文件） | Clawith 对标点 | 可借鉴要点 |
|---|---|---|---|
| 1 | core/deferred 分层 + 根 MEMORY.md 索引 + [[path]] 突触 | G1 记忆整合缺口（`20260902-self-evolution-gap-closure-plan.md`：memory.md 头截 2K、只增不减 → context rot） | memory-v2 的整理原则：一事实一规范位置、拆分看可读性不看配额、core 只放规则+索引、细节下沉 deferred。**不做成 2K 截断+人工维护** |
| 2 | memory blocks「系统提示词学习」三原则 | 自进化记忆 B 通道（系统提示重写） | 泛化而非记事件；recall 可推导的不写；过大触发 /doctor 式体检 |
| 3 | MemFS git 全套（precommit/postcommit/sync-state/signing/config-lock/worktree） | workspace .git 物化 `4d3fe431` | 并发写锁、sync-state 与启动扫描对账、git 签名，可补 Clawith 物化方案的空白 |
| 4 | 子代理 prompt 版本化（v1/v2 并存） | run_compactor / loop-breaker prompt 管理 | 新版本独立文件 + 旧版保留回滚 |
| 5 | memory-confinement fail-closed（无沙箱即 throw）+ detectSandboxBackend | execute_code 沙箱（bwrap 锁 0.11 教训） | 记忆子代理降权运行的完整策略样本：读宿主广、写仅限自记忆、跨 agent 隔离 |
| 6 | 竞品基准提示词 + 上游漂移 CI | Clawith 系统提示词迭代 | 建 source_* 快照做 A/B 行为对比、workflow 监控漂移 |
| 7 | max-context（MIN 30k、agent/conversation 两级） | DeepSeek 上下文/前缀缓存管理 | 两级窗口下限的 API 形态 |
| 8 | channels debounce + headless queue lifecycle | WS 断流/断线重连（前端双状态机） | 多 channel 平台的节流与队列生命周期测试写法 |

## 7. 局限（诚实记录）

- 单用户 harness 思维：多租户、跨租户隔离、审批工作流、知识图谱不是其重点（对应部分 Clawith 已自研且更深）。
- TS/bun 技术栈与 Clawith 不通，只取设计契约与边界条件，不做代码移植。
- 本次未深入：`src/web/`（远程 web 客户端）、`src/providers/`（模型接入层）、`src/telemetry/`、letta-client SDK 依赖面。

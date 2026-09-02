# AgentTeams 源码研究报告

日期：2026-09-03
状态：**完成**（分析基于本地仓库 `/Users/shubinzhang/Documents/UGit/AgentTeams` HEAD `ac22c88`，v1.2.2 之后，已与 origin/main 同步）
定位：参考资料研究，非实现方案。对照 Clawith 的 agent 生命周期/租户凭据/任务幂等/成本控制。
⚠️ 路径勘误：记忆 `reference-projects` 此前记的是旧路径 `UGit/hiclaw`——那是同一 remote（agentscope-ai/AgentTeams）的重复旧克隆（落后 1 提交），正式路径以本文件的 `/Users/shubinzhang/Documents/UGit/AgentTeams` 为准，记忆已修正。

## 0. 项目概览

- **是什么**：阿里 AgentScope 团队的**开源协作式多智能体 OS**——「编排平面」（orchestration plane）。关键定位（`docs/design/k8s-native-orchestration.md`）：不实现 Agent 逻辑本身，而是**编排已有 Agent runtime 容器**（Manager + 多个 Worker），借 K8s 思想（声明式 API、reconcile 循环、CRD）搭控制面。演进链：OpenClaw/Lobster 生态 → hiclaw → 2026-07 更名 AgentTeams。
- **技术栈**：Go（K8s operator，约 77.5k 行 Go，不含 vendor）+ Helm + 多种 runtime 镜像（OpenClaw/Node、CoPaw/Python、Hermes/Python、OpenHuman/Rust）+ Matrix（Tuwunel 服务器 + Element Web 客户端）+ Higress 网关 + MinIO。
- **与 Clawith 的对标关系**：Clawith 是「单平台内多租户多 agent」；AgentTeams 是「多容器多 runtime 组队协作」。互补面：资源契约化、reconcile 生命周期、凭据隔离、任务分配收敛。Clawith 强于它的是：自进化记忆、compaction、多租户、飞书原生。

## 1. 资源契约（CRD，agentteams.io/v1beta1）

五种资源（`agentteams-controller/api/v1beta1/types.go`）：

| CR | 职责 |
|---|---|
| `Human` | 通过 Matrix 设定目标、全程观察、随时介入 |
| `Manager` | 理解目标、创建/挑选 Worker、拆解委派、跟踪进度、汇总结果 |
| `Worker` | 执行聚焦任务；各有 role/model/runtime/skills/MCP |
| `Team` | 多个 Worker + Team Leader 打包成可复用协作单元 |
| Team Leader | 协调成员、维护团队上下文 |

`WorkerSpec` 关键字段（types.go:178 起）：`model`/`modelProvider`（per-worker 模型路由）、`runtime`（openclaw|copaw|hermes|qwenpaw）、`soul`（=SOUL.md 人格文件）、`skills`+`remoteSkills`（Nacos 等远端注册表）、`mcpServers`（v1.1.1 起声明式 MCP，breaking）、`package`（file://、http(s)://、nacos://）、`expose`（经 Higress 暴露端口）、`channelPolicy`/`channels`、`idleTimeout`、`containerManaged`（false 则控制器跳过容器 reconcile，支持远端/边缘 Worker）。

多控制器隔离（types.go 顶部常量）：`LabelController = "agentteams.io/controller"` —— informer 按此 label 过滤 CR 事件，同一 namespace 内多个 controller 实例互不 reconcile 对方资源。Worker 另有稳定 `LabelWorkerEdgeUUID` 支撑 Edge 部署模式下的凭据签发/轮换定位同一身份。

## 2. 控制器 reconcile 分解（`agentteams-controller/internal/controller/`）

按关注点拆文件而非按资源堆一个大文件，可直接抄的 operator 工程手法：
- `human_controller.go` + `human_reconcile_{delete,infra,rooms}.go`（矩阵房间、基础设施、删除三路）
- `manager_controller.go` + `manager_reconcile_{config,container,delete,infra,welcome}.go`
- `team_controller.go` / `worker_controller.go`（含 `worker_controller_predicates`）
- `member_reconcile.go` + `member_reconcile_service.go` + `member_phase_test.go` —— 成员 phase 状态机（member phase 推进/收敛）
- `auto_sleep_controller.go` —— Worker 空闲自动休眠（成本控制，配 `idleTimeout`）
- 内部支撑包：`accessresolver`（访问解析）、`credentials`/`credprovider`（凭据签发）、`gateway`（Higress）、`matrix`、`proxy`、`executor`、`initializer`、`apiserver`、`remoteclient`。

## 3. 安全模型：凭据隔离（README「Enterprise-Grade Security」）

- Worker 只拿 **consumer token**；真实凭据（API key、GitHub PAT）只在网关里——Worker 和攻击者都看不到。
- Higress 统一网关承载 LLM/MCP 流量、身份与访问控制；v1.0.6 起「enterprise-grade MCP Server 管理、零凭据暴露」。
- 技能生态安全前提：Worker 从 skills.sh（80k+ 技能）按需拉取「安全，因为 Worker 拿不到真实凭据」。
- 对照 Clawith：模型密钥走 vault 注入（agent 侧 env），MCP 接入的凭据中转与「consumer token」降权模式值得评估。

## 4. 协作机制

- **人机协同默认开启**（Human-in-the-Loop by Default）：每个 Matrix 房间都有 Human + Manager + 相关 Worker，一切重要沟通走房间、全程可见、随时介入——无黑盒。对照 Clawith 审批转发到 PenguinHarness 会话（[[clawith-approval-forward-to-penguinharness]]）与飞书渠道。
- **TeamHarness**（`docs/design/teamharness/`：boundary-and-contracts.md、project-task-runtime-design.md、runtime-integration-tdd-plan.md）：项目任务分配协议。近期修复可见其严谨性：`fix(controller): validate replan taskId against TeamHarness _safe_id (#1191)` —— replan 的 taskId 必须通过 _safe_id 校验防注入。对照 Clawith 任务/run 幂等账本（[[deploy-kill-replay-divergence]]）。
- **多 runtime 同房间分工**：OpenClaw/QwenPaw（确定性）当 Leader 编排，Hermes 做自主代码执行——「每个 runtime 干它最擅长的」。对照 Clawith 单 runtime + 卡片模式的模型分工思路。
- **MinIO 共享文件系统**：agent 间信息交换走共享 FS 而非互发消息，「显著降低多智能体协作的 token 消耗」。对照 Clawith workspace sync/物化（`4d3fe431`）与 artifact 机制；契约见 `docs/design/member-runtime-config-contract.md`。
- **Skills 分发**：v1.2.2 新增 Manager→Worker 自定义 Skill 投递（带校验、storage 上传、`Worker.spec.skills` 分配、QwenPaw 免重启热刷）；Nacos 远端技能注册表。对照 Clawith skill-creator/技能注入。

## 5. 部署与运维

Helm chart（Higress/Tuwunel/MinIO/controller/Manager CR）、`install/` Docker Compose 一键栈（`curl | bash`）、`agt` CLI（v1.1.0 起替代 shell 脚本）、`migrate/` 升级迁移助手（v1.2.0 起保留 legacy installer 兼容旧契约）、`tests/` 集成测试、`scripts/replay-task.sh` 任务重放。

## 6. 可迁移点 → Clawith 映射

| # | AgentTeams 机制（文件） | Clawith 对标点 | 可借鉴要点 |
|---|---|---|---|
| 1 | 声明式资源契约（CRD + spec/status + subresource） | agent 生命周期管理（Agent/Worker/Team 抽象） | 把 agent 配置做成带 status 的声明式资源、变更走 reconcile 而非过程式调用 |
| 2 | per-concern reconcile 文件分解 + member phase 状态机 | run 状态机（排队/运行/取消）、agent_tool_executions 账本 | phase 显式建模 + 每 phase 独立 reconcile 函数的写法 |
| 3 | `LabelController` 多控制器隔离 + WorkerEdgeUUID 稳定身份 | 多实例部署时 run 归属、worker 凭据轮换 | 资源打 owner label 过滤事件的模式 |
| 4 | TeamHarness taskId `_safe_id` 校验 + replan | 任务分配幂等（tool_call_idempotency_mismatch、replay divergence） | 任务 ID 必须经安全编码校验防注入 |
| 5 | auto_sleep_controller + `idleTimeout` | 成本纪律（[[cost-discipline]]：空闲 agent/设备不烧钱） | Worker 空闲自动休眠的声明式字段 + 独立控制器 |
| 6 | 凭据隔离（网关持密钥、Worker 仅 consumer token、MCP 零凭据暴露） | 模型密钥/MCP 凭据注入（vault env） | 短时效消费令牌 + 网关集中代理的降权模式 |
| 7 | MinIO 共享 FS + member-runtime-config-contract | workspace sync/物化、多 agent 上下文交换 | 共享上下文契约化、文件交换替代消息轰炸减 token |
| 8 | Matrix 房间全程可见 + Human 随时介入 | 飞书渠道 + 审批转发 | 协作可见性=默认开启的治理原则 |
| 9 | Manager→Worker Skill 投递（校验+热刷） | skills 分发（skill-creator） | 技能下发带校验 + 免重启热加载 |

## 7. 局限（诚实记录）

- **不做 agent 逻辑**：无 LLM 调用循环、无记忆/compaction——这些都在被编排的各 runtime 内，AgentTeams 只看编排面。Clawith 的自进化记忆/压缩无对标价值。
- 企业 IM 选择与 Clawith 相反：README 明说 Matrix/Element 是为了「消除钉钉/飞书集成开销和企业审批流程」——Clawith 主打飞书原生，其渠道取舍不可抄。
- `v1beta1` 契约仍在演进：v1.1.x→v1.2.x 连续 breaking（声明式 MCP、Worker CR 名与 runtime 名解耦等），学其方向勿抄其具体字段。
- Go operator + 多镜像栈与 Clawith Python 栈不同：看契约与设计，不移植代码。
- 未深入：`copaw/`、`hermes/`、`openhuman/` 各 runtime 内部实现、`helm/` 细节、`plugins/` 插件平台。

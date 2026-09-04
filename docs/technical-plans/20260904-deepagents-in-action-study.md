# deepagents-in-action 课程专项研究

日期：2026-09-04
状态：**完成**（分析基于 GitHub API 与 raw 文件抽查：README 全文、仓库文件树、最近 12 次提交、`content/ch09-human-in-the-loop.md` 正文、PyPI `deepagents` 元数据；本地已克隆至 `/Users/shubinzhang/Documents/UGit/deepagents-in-action` 并经 HEAD 4097ff9 核对=上游最新）
定位：参考资料研究，非实现方案。Datawhale 名下《Deep Agents 实战》中文课程，对照 Clawith 平台功能域。

## 0. 项目概览

- **是什么**：`datawhalechina/deepagents-in-action`《Deep Agents 实战》——基于 LangChain 官方 **Deep Agents（deepagents 库）** 的生产级中文实战课程。出品人 **沧海九粟**（Haili Zhang / @webup），**LangChain 官方认证大使**，《LangChain 实战》《LangGraph 实战》作者，B 站万粉 UP 主；课程托管于 Datawhale 组织。
- **规模/活跃**：1870★ / 190 fork / 仅 3 个 open issue；2026-05-05 创建；**2026-09-04（研究当日）仍在提交**（v0.7 release guide #107）。
- **形态**：Astro 静态课程站（GitHub Pages）+ B 站视频合集 + 小红书图文合集 + 每章 PDF 幻灯片 + AgentSeek 实验模板（`agentseek-ai/agentseek-templates`：default / research / content-builder / mcp / subagents-dynamic）。
- **协议**：课程内容 CC BY-NC-SA 4.0（非商用），站点源码 MIT。
- **版本基线**：从 deepagents 0.5 开始编写，现行学习与运行基线 **0.7.x**；PyPI 实测 `deepagents` 最新 **0.7.13**，课程紧跟官方补丁节奏。

## 1. 与 easy-langent 的定位对比

| | easy-langent | deepagents-in-action |
|---|---|---|
| ★/创建 | 476★，2025-12-31 | 1870★，2026-05-05 |
| 出品 | Datawhale 教程组 | LangChain 官方大使单人核心 |
| 定位 | LangChain/LangGraph **全栈入门**，游戏实战（谁是卧底/狼人杀） | **进阶生产级**：deepagents 上层 harness 机制深挖 |
| 结论 | 入门候选（T3） | 生产实践对标（本研究后进 T1） |

两者互补不冲突。对 Clawith（自研 LangGraph 平台）而言，deepagents-in-action 的参考价值显著更高：其章节与平台功能域几乎一一映射（见 §5）。

## 2. 章节地图（content/ 下 19 个 md）

- **准备篇**：pre01 AgentSeek 生命周期工作流（`agentseek create/doctor/dev`）、pre02 `npx skills` 开发技能安装
- **认知篇**：ch01 Agent Framework vs Harness vs Runtime 三层边界；ch02 5 分钟 quickstart
- **核心篇**：ch03 虚拟文件系统（Context Engineering 核心）；ch04 任务规划与 Todo；ch05 子 Agent 与上下文隔离；ch06 异步子 Agent（AsyncSubAgent）
- **进阶篇**：ch07 Skills 能力包；ch08 长期记忆（CompositeBackend/StoreBackend）；ch09 Human-in-the-Loop；ch10 沙箱；ch11 FilesystemPermission；ch12 MCP；ch13 RubricMiddleware 评估；ch14 Event Streaming v3；ch15 Interpreters（langchain-quickjs）；ch16 Dynamic Subagents（六种编排模式）
- **release-v0-7**：版本迁移指南（不是 changelog 抄录）

每章配：AgentSeek 模板（可一键 `agentseek create <模板> --checkout main`）+ PDF 幻灯片 + 视频链接。ch6/8/9/11 需在模板基础上按正文补充本章能力，ch14 用 Streaming 专用模板，ch15/16 共用 Dynamic Subagents Pattern Lab。

## 3. 版本治理机制（「API 滞后」痛点的正面解法）

这是本课程区别于零散博客的核心工程素养，值得 Clawith 参考索引重点标记：

1. **显式版本基线声明**：README 顶部 WARNING 块写明「0.5 起步、0.7 现行基线」，新读者直接用最新 0.7.x 补丁；老读者先读 release 章再迁移。
2. **旧内容不删改、就地标注**：旧章节保留历史学习路径，紧邻处嵌「v0.7 提醒」标注现行用法——历史与现行用法共存，不做破坏性改写。
3. **release 章讲行为变化而非功能清单**：解释「默认行为为什么改、哪些应用受影响、如何用评测与 Trace 验证迁移」，且**对官方宣传口径独立核对**（明确提醒「官方 65% 基础输入 token 降幅≠每应用总成本降 65%」）。
4. **能力级版本门槛标注**：条件中断需 `langchain>=1.3.3`；FilesystemPermission 基础权限 `deepagents>=0.5.2`、interrupt 权限模式 `>=0.6.8`；RubricMiddleware 为 Beta 以 `deepagents==0.7.1` 验证；Interpreters 需 Python 3.11+ 与 `langchain-quickjs>=0.2.0`。
5. **实验环境版本化**：模板跟随课用 `--checkout main`；冻结作业环境把 main 换成完整提交 SHA。

## 4. 内容质量抽查（ch09 HITL 正文）

- 代码块可直接运行：`create_deep_agent` + `interrupt_on` 三配置值（True / False / `{"allowed_decisions":[...]}`）+ 四种决策类型（approve/edit/reject/respond）语义表 + `when` 谓词条件中断（只拦危险参数组合）。
- **超出官方文档的工程洞察**：明确「拒绝副作用工具用 `reject` 而非 `respond`」——`respond` 会被模型当作一次成功的 ToolMessage，只适合 ask_user 类工具；删除文件/发邮件/部署必须用 `reject` 让 Agent 知道工具未执行。附三条记忆规则。
- 指出 Checkpointer 是 HITL 的必要条件，并说明 `interrupt_on` 只是便捷入口、自定义 LangChain Middleware 可直接调用底层 `interrupt()` 覆盖非工具暂停点。

## 5. 与 Clawith 功能域映射

| 课程章节 | Clawith 对应域 |
|---|---|
| ch03 虚拟文件系统 / Context Engineering | 上下文构建与 compactor |
| ch09 HITL / ch11 权限边界 | `approval_requests` 审批流转（对照 agent-inbox 研究报告） |
| ch10 沙箱 | bwrap 沙箱（setuid/镜像漂移坑是同类主题） |
| ch13 Rubric 评估 | Langfuse judge 平台设计 |
| ch14 Event Streaming | WS 断流/事件语义问题域 |
| ch08 长期记忆 | 自进化记忆 |
| ch05/06/16 子 Agent 编排 | 多 Agent 协作设计 |

**价值定位**：不只是「学习资料」，更是自研平台各功能域的官方上层实践对标素材。

## 6. 风险与使用注意

1. **教的是 deepagents 上层 harness，不是裸 LangGraph StateGraph**——底层图模型心智（State/Reducer/Checkpointer 细节）仍以官方文档/DLAI 课程为准；Clawith 是自研平台，**借鉴设计模式（中间件栈、背板、权限边界、评估管线），不抄 API**。
2. 单人核心作者，质量高度依赖个人（当前内容密度与严谨度在线，但持续维护性弱于官方文档）。
3. 示例默认绑硅基流动平台（作者有返利并回馈学员配额池）；模型可换——`MODEL_NAME` 环境变量管理，作者自己推荐 DeepSeek-V4-Flash / GLM-5.2，与 Clawith 当前模型栈直接兼容。
4. 内容协议 CC BY-NC-SA：非商用参考无碍，吸收进商业产品文档需注意署名/同协议约束。

## 7. 结论

- **入参考索引 T1**（沿 AgentTeams 先例：专项研究报告 + 引用刚起步），标记 ★ 研究报告 20260904。
- **重点阅读章**：ch03、ch09、ch10-11、ch13、ch14。
- easy-langent 同批收录为 T3 候选（入门定位，与本研究课程分工互补）。

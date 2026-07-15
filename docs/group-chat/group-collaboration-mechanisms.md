# Group 自组织协作机制（已确认基线与待讨论项）

> 本文只列机制，不写 PRD，不描述页面方案。
>
> `docs/group-chat/prd.md` 是 Group 产品规则来源。本文中已经逐项确认的 Planning v2、自组织公开交接等内容，是对旧 PRD 对应段落的后续明确修订；除此之外，如果审计推断、技术方案或实现建议与 PRD 冲突，以 PRD 为准，不把技术担忧自行升级成产品限制。
>
> 本文记录当前讨论形成的目标机制，不代表现有代码已经全部实现。已确定项与待讨论项分开维护。共享 Runtime、Prompt、Tool、Verifier 与 RunView 语义以 `docs/single-agent-runtime/runtime-architecture-decisions-2026-07-15.md` 为准；本文只补充 Group 的继承边界和产品机制。

## 0. 与单 Agent Runtime ADR 的继承边界

### 0.1 直接继承的共享机制

Group Planning Run、普通 Group Agent Run、公开 `@` child Run 和 Group 场景中的私下 A2A，都使用同一套已确认 Runtime 基线：

1. LangGraph checkpoint 是执行进度真值，`AgentRunCommand` 是 start/resume/cancel 控制命令真值；产品逻辑不得依赖 `projected_*` 或第二套执行状态。
2. 不保留 `RuntimeProjector`，不增加 projection table、execution job table、Group workflow 状态表或平行的 Runtime。
3. 一次 Command invocation 从其精确 checkpoint 继续执行，直到稳定 waiting 或 terminal；Graph 稳定后再由独立、幂等的产品同步处理公开消息、Planning 调度、A2A 回传和 lane 释放。产品同步失败不得重新执行 Graph 或 Tool。
4. `RunStateReader` 必须按目标 Run、Command 和 `applied_checkpoint_id` 精确读取状态；Group 页面和后台任务都只能消费 typed `RunView`，不能把 Thread 最新 checkpoint 或产品侧镜像当成目标 Run 状态。
5. cancel 采用 interrupt-and-preserve，只取消目标 Run 并保留最后稳定 checkpoint。默认不级联取消同一 `root_run_id` 下的 Planning、公开 child 或 A2A child；Workflow 级级联取消如有产品需求，必须另行定义。
6. 普通 Group Agent Run 共享 Run 级模型决策轮次 Step budget。`agent_id = null` 的 Planning Run 不进入 Agent loop，继续使用独立、有界的 planning attempt / repair 次数；不能把 Agent `max_tool_rounds` 当成 Planner 尝试次数。当前 Planning 上限为初次调用加最多两次 repair，即总计最多三次模型调用。canonical builtin Tool definitions、有效 Tool 集解析、既有 `ToolExecutionOutcome`、Tool Ledger、私有 Result Store、确定性 Verifier、Base Prompt V1 和动态上下文装配规则由 Group Agent Run 与其他 Agent 入口共享。
7. 每个 Group Agent Run 的确定性 Verifier 只判断当前 Run 是否满足 `finish` 协议、是否存在未结算 Tool，以及当前 Run 的可信 artifact/evidence 是否可读；它不判断整个群协作是否在业务上完成，也不调用另一个 LLM 做语义裁判。
8. Group 专属产品规则只能通过可信 Group context、条件化 Tool Schema/Capability Policy 和产品同步实现，不得在 GroupContext、Base Prompt、Tool description 与 Planner prompt 中重复注入互相竞争的指令。

### 0.2 明确不从 Direct Chat 继承的机制

Direct Chat 的一个窗口只服务一个 Agent，因此可以让一个 `ChatSession` 直接对应一个持续的 LangGraph Thread。Group 不满足这个前提：同一 Group Session 内会出现 Planning Run、多个 Agent、并行入口、公开 child Run 和私下 A2A。把整个 Group Session 映射成一个 LangGraph Thread 会混合不同 Agent 的消息、状态、工具权限和执行顺序。

Group 固定采用以下映射：

```text
Group ChatSession
  = 产品层公开群对话与共享 Session Context
  != 一个共享 LangGraph Thread

Planning Run
  = 一个独立 LangGraph Thread

每个 Group Agent Run / 公开 @ child Run
  = 一个独立 LangGraph Thread

同一 Run 的 wait -> resume
  = 在该 Run 自己的 Thread 上继续
```

因此：

1. Direct Chat 的 `thread_id = ChatSession.id` 只适用于 Direct Chat；Group Run 当前继续使用独立 Run thread identity，不得为了统一映射而改成 `Group ChatSession.id`。
2. Direct Chat 的 Thread FIFO 不能决定 Group 的 lane scope。Group 继续复用同一套 durable lane 基础设施，但 `group_mention:{tenant_id}:{agent_id}` 的范围、优先级和 Workflow 穿插仍是 Group 产品决策。
3. Direct Chat 的单套 Thread Running Summary 不替代 Group Session Context compact。Group 的共享公开上下文跨越多个独立 Agent Thread，因此继续保留一套 Group Session 级 compact；每个 Group Run 自己的模型上下文仍遵守共享的 token 预算、Tool Exchange 规范化和动态装配规则。
4. Direct Web Chat 的 `waiting_user` 回复恢复协议不能直接套到 Group。Group 没有可靠的群内回复关联之前，不允许让普通 Group Agent Run 进入无法恢复且长期占 lane 的 `waiting_user`；需要用户补充时，V1 应在公开终态中提出明确问题，后续由人类新的结构化 `@` 创建新的顶层 Run。私下 A2A 的 `waiting_agent` 仍恢复原 Run。
5. Direct Session 的 singular `activeRun`、单 waiting correlation 和“当前 Thread 最新 Run”接口不适用于 Group。一个 Group Session 可以同时有多个 Agent lane 和多个 active/waiting Run；Group 查询必须按明确 `run_id` 读取 `RunView`，群级列表如需聚合应返回集合，不能任选一个 active Run 代表整个群。

### 0.3 本文仍然负责的 Group 专属机制

以下内容不由单 Agent ADR 替 Group 做产品决定，继续以本文为准：

- 人类单 `@`、人类多 `@` 与 Planning 的入口规则；
- `advisory / enforced`、`plan_prompt` 和公开轮转；
- 结构化公开 mentions、公开消息与 child Runs 的原子产品同步；
- Group lane scope、触发消息截止点与 Workflow 穿插；
- 群公开 Session Context、Group Workspace/Memory 和私下 A2A 的可见性边界；
- 单 Run 完成与整个群协作完成之间的边界。

## 1. 总体执行模式

Group 使用同一套 Runtime，支持两种约束模式。

### 1.1 默认：简单初始编排 + 自组织执行

1. 人类消息确定原始目标和首批被 `@` 的 Agent。
2. Runtime 先按稳定 participant ID 去重有效 Agent mentions，再根据发送者类型和 mention 数量选择入口；不使用 LLM 判断人类任务内容是否“复杂”。
3. 人类单 `@` 不启动 Planner，Runtime 直接创建对应 Agent 的普通 Group Run。
4. 人类多 `@` 为该消息强制启动且只启动一次 Planner；Planner 输出统一的 `mode = advisory`、完整 `plan_prompt` 和入口任务。
5. 默认自组织模式下，Agent 执行中无论公开 `@` 一个还是多个 Agent，都不因 mention 数量自动启动 Planner；Runtime 直接创建对应的 Group child Run，并沿用现有 `parent_run_id / root_run_id` 自关联规则。
6. 所有后续参与 Agent 都获得同一份完整 `plan_prompt`，但 `advisory` 只表示建议，不能覆盖人类原始消息，也不成为执行期的权威硬 DAG。
7. 执行过程中，Agent 根据当前群上下文、私下 A2A 结果和已有产物，自主判断是否继续协作、找谁协作以及何时结束自己的工作。
8. 实际 Run 路径可以偏离初始计划。

### 1.2 人类显式 Workflow

1. 在当前入口规则下，人类多 `@` 先启动同一个 Planner；不增加前置意图分类器、UI 开关或关键词匹配器。
2. Planner 直接读取人类原始 Prompt，判断人类是否明确规定了 Agent、顺序、轮数、依赖、分工或完成条件。
3. 如果这些流程约束来自人类 Prompt，Planner 使用同一输出结构设置 `mode = enforced`；如果没有，则设置 `mode = advisory`，进入默认自组织模式。
4. Planner 不能把自己建议的流程升级为强制 Workflow；语义不明确时默认 `advisory`。
5. `enforced` 模式下，`plan_prompt` 是所有参与 Agent 都会获得的完整执行协议，包含角色、顺序、条件、循环、分支、公开交接和完成规则；它不要求被压成固定 DAG。
6. Planner 只启动 Workflow 的入口 Agent 或入口并行任务，不在后续执行中充当持续协调器。之后由当前 Agent 根据完整 `plan_prompt`、群上下文和当前结果决定下一步。
7. 当前 Agent 认为 Workflow 尚未完成时，在终态公开回复中按计划 `@` 下一位 Agent；认为完成条件已经满足时，输出最终结果且不再 `@`。
8. 后续每个由 Workflow 公开 `@` 创建的新 Run，都获得同一份不可变 `plan_prompt`、人类原始 Prompt、自己的当前责任以及必要的上游公开结果。
9. Agent 可以在 Workflow 内使用普通私下 A2A；A2A 是当前 Run 的内部辅助，不替代公开 Workflow 交接。
10. Runtime 不做 Workflow 语义判定，不调用另一个 LLM 判断某次公开 `@` 是否“符合计划”；V1 通过 Prompt 优先级约束 Agent，只校验身份、权限、预算、幂等和循环保护。
11. `enforced` 不新增独立 Scheduler、Workflow Runtime、状态表或系统调度消息。它与自由模式使用同一套公开 `@`、新 Run、A2A 和终态机制。
12. 未被人类 Workflow 限定的步骤内部实现仍可由 Agent 自主决定，但不能改变人类明确规定的流程约束。
13. 约束优先级固定为：

```text
平台安全、权限和预算
  > 人类原始 Prompt（包括显式 Workflow 和后续修改）
  > Planner 从人类 Prompt 编译出的 plan_prompt
  > Agent 的现场判断
  > 初始 Planner 建议
```

### 1.3 Planner 统一输出

自由模式和 Workflow 模式不使用两套 Planner schema，只通过 `mode` 改变同一份计划 Prompt 的约束强度：

```json
{
  "version": 2,
  "mode": "advisory | enforced",
  "goal": "协作目标",
  "plan_prompt": "完整计划、角色、条件、分支和完成规则",
  "entry_steps": [
    {
      "agent_id": "入口 Agent ID",
      "instruction": "当前入口责任"
    }
  ]
}
```

固定规则：

1. `plan_prompt` 在两种模式下都完整提供给所有参与 Agent；`advisory` 可以偏离，`enforced` 必须服从人类明确规定的部分。
2. `entry_steps` 只决定第一批启动谁，可以包含一个或多个并行入口，不描述后续 DAG。
3. 后续协作统一由 Agent 终态公开 `@` 推进，不再由 Planner 持续调度。
4. 参与者的稳定 `agent_id / participant_id` 继续来自 Runtime 已有的 `candidate_agents / mention_targets`，Planner 不重新生成身份映射。
5. Runtime 只校验 schema、入口 Agent 是否属于候选集合、字段非空与去重；不校验自然语言计划的业务正确性。
6. 每个 Agent 实际获得“平台固定执行规则 + 人类原始 Prompt + 完整 `plan_prompt` + 当前责任”。Planner 只生成任务专属计划，不重复平台规则。
7. 平台固定执行规则根据 `mode` 明确计划是建议还是强约束，从而降低 Planner 和弱执行模型理解两套协议的压力。
8. Planner 生成并校验合法 v2 plan 后，Planning Run 直接进入 `completed`，checkpoint 保存不可变 `mode + plan_prompt + entry_steps`。稳定 checkpoint 之后由独立产品同步幂等创建入口 child Runs；Planning Run 不再进入 `waiting_agent`，child 完成后也不 resume Planning Run。

### 1.4 当前代码的计划与进度拼接方式

以下是现有静态 Planning 实现，不是 1.1～1.3 已确定的目标机制：

1. 完整 `version = 1` Plan 只保存在 Planning Run 的 `lifecycle.planning` checkpoint 中，包含静态 `steps + depends_on_step_ids` 以及每一步的状态和结果。
2. Planning child 的正式 `goal` 是当前 `step.instruction`。child 不获得完整 Plan、`execution_strategy`、其他步骤、全局完成进度或剩余任务。
3. child payload 只额外携带 `planning_root_run_id`、当前 `planning_step_id / planning_instruction`，以及当前步骤所有已完成直接依赖的 `related_run_summaries`；间接依赖和无依赖关系的兄弟步骤结果不会从 Planning checkpoint 显式传入。
4. `related_run_summaries` 中的结果来自依赖 child 的终态 `lifecycle.result_summary`。默认 finalizer 的 summary 基本等于该 child 的最终公开答案，不是单独生成的 Workflow 进度报告。
5. child 启动时，Runtime 读取当时最新的群 Session Context、pending messages 和 recent messages，并与 Planning 字段一起冻结为该 Run 的输入快照；Run 启动后不再刷新。
6. 模型调用时，Session Context、当前 Run、`related_run_summaries` 和完整 `initial_input` 被序列化进 system message 的 Durable Runtime context；recent 群消息则按普通 user/assistant messages 继续拼接。
7. 正常终态处理顺序是：先把 child 最终答案投递成公开群消息，再合并 Session Context，然后把 child 结果回送 Planning Run。所以下一个依赖 child 通常既能看到公开答案，又可能在 Session Context 中看到其摘要，同时还收到直接依赖的 `related_run_summaries`。
8. 当前存在确定的重复：`planning_instruction` 会出现在 `current_run.goal`、`initial_input` 和 `group_context.planning_hint`；`related_run_summaries` 同时存在于顶层 Runtime section 和 `initial_input`；原始群消息和前置结果还可能再次出现在 recent messages 与 Session Context 中。
9. Session Context 和 recent messages 当前按 start command 真正执行时的最新状态读取，没有按触发消息位置截断。因此并行或排队 child 可能得到不同版本、并混入触发消息之后的无关群消息；只有 `related_run_summaries` 是 Planning 按直接依赖关系确定传递的进度。

现有实现本质上是“Planning Run 内部保存全局状态，child 只拿当前步骤和直接依赖摘要，再由群 Session Context 补充背景”，并不是“所有 Agent 获得完整计划和统一进度”。目标实现需要重新确定不可变 `plan_prompt`、当前公开交接和累计公开进度的拼接边界，不能直接沿用当前重复字段。

当前阶段不定义步骤级进度、完成百分比、剩余步骤、步骤状态表或独立 `progress_summary`，也不要求 Runtime 从自然语言 `plan_prompt` 推导这些信息。协作进展只通过当前公开交接消息和已有群 Session Context 表达。以后如果正式引入 DAG，再连同节点状态、依赖完成条件、分支汇合和进度计算方式一起设计；不能在没有 DAG 的情况下先造一套伪精确的步骤进度。

### 1.5 目标 Prompt 与 Tool 装配边界

Group Agent Run 继承 Base Prompt V1，但 Planning 根节点例外：Planning 根节点继续使用独立、无业务工具、只输出 Planning JSON 的专用 Prompt，不套数字员工 Base Prompt，也不暴露 `finish / wait`。

普通 Group Agent Run 的目标装配顺序固定为：

```text
稳定前缀：Name + Soul + Base Prompt V1
+ 条件化的 Group Capability Policy（仅一份）
+ 动态后缀：当前 Group scope、触发消息、公开上下文、plan_prompt、当前责任
+ 规范化的当前 Thread messages / Tool Exchanges
+ 本轮有效 Tool Schema
```

具体约束：

1. 当前 Agent 的 `role_description` 不再作为身份或指令注入 Group self context；其他成员的 role 可以作为通讯录/成员发现数据保留。
2. `GroupContextBuilder` 不再重复注入 Base Prompt 已有的 `scope_rules`、`tool_permissions` 或同一 Planning instruction。`group_context.planning_hint` 只保存必要的结构化 `mode + plan_prompt + current_responsibility`，不复制整份 `initial_input`。
3. 人类原始消息、当前责任、`plan_prompt`、直接上游公开结果和 recent messages 必须各有唯一来源；不得同时在 `current_run.goal`、`initial_input`、Group context 和普通 messages 中重复成多份可执行指令。
4. Group scope、只能访问当前群成员/Workspace/Memory、只能写自身 Group Memory 等授权边界仍保留，但只在可信 Group Capability Policy 中出现一次；这类边界不能为了缩短 Prompt 删除。
5. 本轮有效 Tool 集必须先解析并追加 Group tools，再据此生成 `allowed_tool_names` 和 Capability Policy，避免 Prompt 宣传实际不可调用的能力。
6. Group tools 必须进入 canonical builtin definition 数据模块，并且只有经过校验的 `group_context` 存在时才可见；不增加 Registry class 或第三层 adapter，自定义工具不得覆盖 `group_*`、`finish` 或 `wait`。
7. `group_query_members` 对 Agent participant 必须显式返回稳定 `agent_id`，同时保留兼容字段 `participant_id / participant_ref_id`；后续 `group_read_memory(agent_id)` 和终态公开 mention 不得依赖显示名猜测身份。
8. Group tool handler 与普通/A2A tool handler 统一返回扩展后的既有 `ToolExecutionOutcome`。Tool 结果、effect、retry policy、artifact/evidence 和大结果引用都遵守共享 Tool Ledger 契约，不返回只靠字符串前缀判断成败的旁路结果。
9. 模型正常通过当前公开上下文和 `group_query_members` 得到协作对象；本版不增加 Tool Search、向量检索、Graph Retriever 或复杂语义工作集优化。
10. 按 Group PRD，Agent 自身上下文和普通工具能力继续复用单 Agent 机制，包括 Agent 自身 Memory、Skills、Tools 和 Workspace；不因为进入 Group 就改成一套只允许 Group tools 的封闭 allowlist。Group scope 只限制 Group member、Group Memory、Group Workspace 与群消息等群资源，不能把 Agent 私有内容自动视为群共享内容。
11. Agent Workspace、私信或其他空间的内容复制到 Group，或把 Group 内容发送到私信/外部渠道，必须遵守 PRD 2.10.1：由人显式触发、只处理用户明确指定内容、校验触发人权限、必要时预览/确认、记录来源与审计。`send_channel_message / send_channel_file / send_platform_message` 等语义等价动作必须归一到同一 canonical action，不能通过工具别名绕过授权。
12. 群公告、成员 role、Group Memory、Workspace 正文、Planner 生成的 `plan_prompt` 和用户消息都是低信任数据。稳定 system prefix 只放平台边界；这些正文必须进入明确的 dynamic data / user-context 通道，不能因为包在 `group_context` JSON 中就获得 system instruction 权限。
13. Group read tool 的大正文不能只被 8 KiB inline 上限截成不可继续的摘要。文件读取应提供有界 chunk/offset 或 continuation；已有 Workspace revision/path 作为稳定 artifact ref，私有 Tool Result Store 只保存执行归档，不替代 Group Workspace 本身。
14. Group write Tool 的模型回执只返回有界 summary、path、content hash、version/revision 和必要 ref，不回显刚写入的完整正文。storage 成功而 revision/ledger 未 settle 时必须靠稳定 operation ID 对账或补偿，绝不自动重做一次写入。

## 2. 群内公开 `@` 与唤醒

### 2.1 统一心智

群聊中的 Agent 唤醒与公开 `@` 使用同一个用户心智：

```text
业务 Agent 开始一个公开协作 Run
  => 群成员一定能看到与该次协作入口对应的结构化 @Agent
```

硬规则：

1. 群内没有不可见的业务 Agent 唤醒。
2. 只要业务 Agent 被群消息唤醒，群成员就必须看到对应的公开 `@`。
3. 人类单 `@` 和 Agent 终态公开 `@` 直接创建目标 Agent Run；人类多 `@` 先创建唯一 Planning Run，mentions 是 Planner 的候选参与者集合，不要求所有候选立即并发启动。
4. 如果只想在文字中提到某个 Agent、但不想唤醒，应直接写名字，不使用 `@`。
5. `@` 必须保存为结构化 mention，不能只依赖模型输出文本或正则解析。
6. 同一条消息重复 `@` 同一个 Agent，只产生一次唤醒。
7. 一条消息可以 `@` 多个 Agent，对应多个公开协作入口。
8. 人类多 `@` 是 Planning 的公开入口：所有 mentions 都成为 Planner 的候选参与者，但 Planner 只启动计划要求的入口 Agent。非入口 Agent 真正轮到执行时，由上一位 Agent 再次公开 `@`，不生成系统调度 `@`。

### 2.2 原子性

一次公开 `@` 必须原子形成：

```text
公开 ChatMessage
+ 结构化 mentions[]
+ 对应的 Planning Run 或 Agent Run
+ Runtime dispatch command
```

不得出现“看见 `@` 但没有唤醒”，也不得出现“Agent 已被群聊唤醒但群里没有可见 `@`”。公开 `@` 只创建新 Run，不恢复已经结束的旧 Run。

### 2.3 Agent 之间互相 `@`

以下规则同时适用于默认自组织模式和 `enforced` Workflow；区别只是前者可以现场偏离 `plan_prompt`，后者必须遵守其中来自人类的硬约束。

1. Agent 认为公开协作还没有结束时，可以在当前 Run 的终态公开群回复中继续 `@` 其他 Agent。
2. 公开 `@` 是一次 Run 回合结束后的公开交接；Run 执行到一半时，正文里出现的 `@` 不产生唤醒。
3. 当前 Agent 发布带 `@` 的终态公开回复后，当前 Run 结束；每个被 `@` Agent 都创建新的 Group child Run。
4. 新 child Run 按现有 AgentRun 自关联规则记录 `parent_run_id / root_run_id`；不增加 `workflow_execution_id`。如果来源 Run 来自 Planner 协作，同时把不可变 `mode + plan_prompt` 传给新 Run。
5. 被 `@` 的 Agent 获得当前群 Session 的公开上下文，并在群里公开回应；它如果需要把公开责任交回先前 Agent，必须在自己的终态回复中再次公开 `@` 对方，由 Runtime 创建一个新的 Run，不恢复已完成的旧 Run。
6. 默认模式下，Agent 认为不再需要新的公开协作者时不再创建新的公开 `@`；Workflow 模式下，Agent 在完成条件满足时停止 `@`。
7. Agent 在同一条终态消息中公开 `@` 多个 Agent 时，所有目标 child Run 使用同一条不可变的触发消息和相同的上下文截止点；每个 child Run 只额外获得自己的 `target_participant_id`。
8. Runtime 不调用 LLM 为多个目标重新拆写任务；各目标的责任由发起 Agent 在同一条公开消息中用自然语言说明。

### 2.4 Agent 发起公开 `@` 时的任务说明

Agent 自己发起公开 `@`，说明它已经掌握当前上下文并决定继续协作，因此应由发起 Agent 在公开消息中说明希望目标 Agent 做什么，不再自动启动 Planner 补写意图。

Prompt 行为要求：

1. 公开 `@` 其他 Agent 时，应说明希望对方完成、判断或回应什么。
2. 同时 `@` 多个 Agent 时，应尽可能分别说明每个 Agent 的责任。
3. 如果希望多个 Agent 围绕同一问题公开讨论，应明确说明这是共同讨论，而不是不同子任务。
4. 任务说明使用自然语言，不强制弱模型生成复杂的 instruction、dependency 或 completion schema。
5. `enforced` Workflow 中，公开消息还应说明当前判断、满足了哪条转换条件，以及希望下一位 Agent 执行的责任。

Runtime 只强制机器可判断的结构：

- mention 指向当前群内有效、可唤醒的 Agent participant；
- 公共消息非空，mention 使用稳定 ID；
- message、mentions、child Run 和 dispatch command 满足原子性与幂等；
- 权限、预算和当前来源 Run 的状态允许继续协作。

Runtime 不判断“任务是否真的说清楚”，也不因为语义模糊自动启动 Planner。合法 JSON 或非空字段不能证明任务语义明确，因此不设置语义清晰度硬门槛。

Agent 终态消息同时 `@` 多个目标时采用全有或全无校验：Runtime 必须在来源 Run 提交终态前验证全部 participant ID、群成员资格、可用状态、权限和预算；任一目标无效时，不发布群消息、不创建任何 child Run，而是把可修复错误返回当前模型。全部有效后，产品侧在同一事务中提交公开消息、mentions、child Runs 和 dispatch commands。来源 Run 的 terminal checkpoint 属于 Runtime checkpoint 事务，不宣称与产品侧投递是同一数据库事务。Agent 不知道稳定 participant ID 时，应先通过现有群成员查询工具获取，不能根据显示名猜测。

目标 Agent 如果无法根据公开消息和群 Session Context 理解请求，应公开 `@` 发起 Agent 追问；该追问在同一 `root_run_id` 下创建新的 child Run。结构化 mention 无效时，Runtime 可以向当前模型返回简单错误并允许有限重试，但不得先发布一个无法兑现唤醒的公开 `@`。

#### 2.4.1 Group 终态的结构化协议

共享 Base `finish` 仍然只有必填 `content`。只有在已校验的 Group Agent Run 中，Runtime 才把同一个 `finish` Tool Schema 条件化扩展为：

```json
{
  "content": "最终公开群回复",
  "mention_participant_ids": ["稳定 participant UUID"]
}
```

固定规则：

1. `mention_participant_ids` 可选、去重、有界，只接受当前群内可唤醒 Agent participant；空数组与未提供都表示当前 Run 只公开回复、不继续交接。
2. 这不是第二个完成协议，也不是 artifact/evidence 申报入口。artifact/evidence 仍只能由当前 Run 已成功的 typed `ToolExecutionOutcome` 派生。
3. 非 Group Run 的 `finish` Schema 不出现该字段，parser 也拒绝旁路字段；Group Planning 根节点不使用 `finish`。
4. mention 是产品交付动作，不进入通用语义 Verifier。Group delivery preflight 在 terminal checkpoint 前校验全部目标的 scope、成员资格、Agent 状态、权限、数量、深度、预算和循环保护；可修复错误进入现有 repair loop。
5. preflight 通过后，terminal checkpoint 只保存不可变 delivery intent。随后产品同步以该 intent 为输入，原子创建公开 `ChatMessage + mentions + child Runs + start commands`，并写幂等回执。
6. 如果 preflight 后成员状态发生竞态变化，产品同步必须 fail closed，并以同一 intent 重试或形成可观察的 delivery failure；不得部分发布、猜测替代目标、回滚 checkpoint 或重新执行来源 Run。
7. 不从 `content` 文本中解析 `@名字`，也不新增平行的 `group_finish` 或 `group_handoff` Tool。

### 2.5 `enforced` Workflow 中的 Agent 公开 `@`

1. Agent 公开 `@` 是 Workflow 的正常轮转方式，不是计划外行为；仍然只能发生在当前 Run 的终态公开回复中。
2. 当前 Agent 根据 `plan_prompt` 和当前结果选择下一位 Agent：未满足完成条件时公开 `@` 下一位，满足完成条件时返回最终结果且不再 `@`。
3. “直到达成共识”“审核不通过返回重做”和结果分支都由当前 Agent 按照 Workflow Prompt 判断，并通过公开 `@` 形成下一 Run。
4. 每个新 Run 继承同一份不可变 `mode + plan_prompt`。Agent 不得改写人类流程、增加计划外目标或把自己的建议提升为新硬约束。
5. Runtime V1 不判断公开 `@` 的业务语义是否符合计划；它只验证结构化 mention、成员身份、权限、预算、幂等、深度和循环保护。模型偏离 Workflow 属于行为可靠性风险，不伪装成 Runtime 已经形式化保证。
6. 人类公开 `@` 不受 Agent Workflow 约束。人类新消息会创建独立执行，当前实现不会自动把它并入正在运行的 Workflow。

### 2.6 同一 Agent 连续被人类 `@`：当前实现

当前代码已经存在串行机制，但文档此前没有说明。现状如下：

1. 不同人类群消息每次有效 `@` 都立即保存自己的 `ChatMessage`，并创建独立的新 `AgentRun + start command`；不会合并、覆盖或恢复该 Agent 已有的 Run。
2. 人类直接 `@` 创建的 Run 没有 `parent_run_id / root_run_id`，因此即使 Agent 此时正在执行另一个 Planning/Workflow child，它仍然是独立顶层 Run，不会自动归入旧 Planning Run。
3. 普通人类单 `@` Run 和 Planning child Run 使用同一个 `scheduling_lane_key = group_mention:{tenant_id}:{agent_id}`。
4. 同一 Agent 的这些公开 Group Run 不会同时执行。当前 Run 持有 lane 时，后续消息、Run 和 command 已经落库，但 start command 保持 pending；只有前一个 Run 进入 `completed / failed / cancelled` 后才释放 lane。
5. `waiting_user / waiting_agent / waiting_external` 都不会释放 lane。因此当前 Run 私下 A2A 并处于 `waiting_agent` 时，新的人类公开 `@` 会继续排队，而不是并发启动。
6. lane key 不包含 `group_id / session_id`。因此当前串行范围实际是“同一 tenant 内的同一 Agent”，不同群和不同群 Session 也会互相排队。
7. ACK“收到，我开始处理。”不是入队时发送，而是在该 start command 真正取得 lane、准备执行时才发送。排队期间用户看不到独立的“已排队”提示。
8. 每个 Run 结束后独立向原群 Session 写一条普通 Agent `ChatMessage`。正常路径中，前一个 Run 的终态回复先投递，然后释放 lane，之后下一个 Run 才 ACK 并开始，因此同一 Agent 通常表现为：

```text
第一条 @ 消息
Agent ACK 1
第二条 @ 消息（此时只入队）
Agent 最终回复 1
Agent ACK 2
Agent 最终回复 2
```

9. 不同 Agent 拥有不同 lane，回复仍可能在群里交错出现。
10. 当前 `ChatMessage` 没有 `reply_to_message_id` 或 `source_run_id`。内部可以通过 `AgentRun.source_id -> 触发消息` 以及 delivery event `run_id -> 最终 message_id` 追溯，但当前群消息 API/UI 只显示普通时间线，用户不能直接看出某条回复对应哪一次 `@`。
11. 当前输入快照在 start command 真正执行时捕获，而不是在公开消息入队时捕获。查询读取当时最新的 Session Context 和消息窗口，没有按触发消息位置截断。因此排队后的 Run 通常会看到前一个 Run 的 ACK、最终回复以及排队期间新增的其他群消息。

Workflow 执行期间再次被人类 `@` 时，当前实现还有一个明确边界：

1. 新的人类 Run 不会打断正在执行的 Workflow child，只会等待同一 Agent lane。
2. 当前 child 完成后，Planning Run 需要 resume 并创建后续步骤；与此同时，已排队的人类 Run 也在竞争刚释放的 Agent lane。
3. 单 worker 和多 worker 下，Planning resume、后续 child 创建和人类 pending start 的先后可能不同。因此当前代码不能保证新的人类 Run 一定在整个 Workflow 之后执行，也不能保证它一定先于下一 Workflow 步骤执行；它可能插入两个 Workflow 步骤之间。
4. 新的人类 Run 不会取消、修改或完成原 Workflow；它只是独立执行，但可能延迟同一 Agent 的后续 Workflow 步骤。

以上是现有实现，不代表边界已经全部确定。后续需要分别决定：

- lane 是否继续按 `tenant + agent` 全局串行，还是缩小到 Group/Session；
- 人类新 `@` 与 `enforced` Workflow 后续步骤谁优先，是否允许穿插；
- 群消息是否需要显式展示回复所对应的触发消息；
- 严格触发消息截止与“排队 Run 看见前一个 Run 结果”之间如何取舍；

`waiting_user` 已按 0.2 和 7.2 收敛：在群内没有稳定回复关联前，普通 Group Agent Run 不进入该状态，而是公开提出问题并结束；后续人类 `@` 创建新 Run。

queued Group Run 的 cancel 也按共享 cancel 语义固定：后续 start 因同 Agent lane 尚未取得而阻塞时，cancel 必须能越过该 Run 自己尚未 applied 的 start，直接把它结算为 `cancelled_before_start`；不发送 ACK、不创建 checkpoint、不影响当前 lane holder，也不得被 earlier pending start 永久阻塞。

此外，当前 Planning 代码仍以静态 `steps + depends_on_step_ids` 创建全部计划 child Runs，尚未实现本方案已经确定的统一 `mode + plan_prompt + entry_steps + Agent 公开 @ 轮转`。这是后续代码改动，不应把当前静态 Planning 行为误写成最终机制。

## 3. 私下 A2A

### 3.1 直接复用全局 A2A

Group 不新增一套“群内私信协议”。按 Group PRD 2.7.4，群内发起 A2A 与普通 A2A 相同，直接复用现有：

- `send_message_to_agent` 工具；
- `notify`；
- `consult`；
- `task_delegate`；
- 独立 `session_type = a2a` 的 ChatSession；
- delegated Run、等待、结果返回和原 Run 恢复机制。

全局 A2A 的产品语义已经确定：同一 tenant 内同一 Agent pair 可以复用现有 pair-global A2A `ChatSession`、私下消息历史和 Session Context；不按来源 Group、Group Session、来源用户或来源 Run 再切分上下文。Agent 之间的长期私下协作连续性是允许的，不作为跨群泄漏缺陷修复，也不新增 source-scoped A2A 表或上下文容器。

Group 在这里新增的只有公开边界：

1. A2A 请求、过程和原始结果保存在全局私下 A2A 会话中，不写入当前 Group `chat_session_id` 的公开消息、Group Session Context、Group Memory 或 Group Workspace。
2. A2A Tool contract、delegated `AgentRun`、Tool Ledger、`parent_run_id / root_run_id`、correlation 和 `waiting_agent -> resume` 全部沿用普通 A2A。
3. Group 上下文可以记录来源 Run 用于 trace、权限和回传关联，但这些字段不参与 A2A Session 隔离，也不改变 pair-global 消息历史。
4. 只有调用 Agent 后续整理并公开表达的内容才进入 Group ChatMessage 和 Group Session Context。

### 3.2 可见性边界

1. A2A 是 Agent 的内部工具调用和私下执行过程。
2. 人类和群成员看不到 A2A 请求、过程消息和原始结果。
3. A2A 不在群里渲染 `@`，也不写入当前群 `chat_messages`。
4. A2A 内容不自动进入群 Session Context、群 summary、群 workspace 或群 memory。
5. `consult` 和 `task_delegate` 的结果返回调用 Agent，并恢复原调用 Run。
6. 调用 Agent 认为结果对群协作有价值时，必须自行整理成公开群回复；只有整理后公开表达的部分才进入群上下文。
7. A2A 结果没有群价值时，可以完全不在群里出现。
8. `enforced` Workflow 内的 A2A 仍遵守以上规则：它是当前 Run 的内部协助，不替代 `plan_prompt` 要求的公开发言或公开 `@` 交接。

因此，可见性规则固定为：

```text
公开 @ 和公开回复 -> 群成员可见，进入群 Session Context
私下 A2A          -> 群成员不可见，只返回调用 Agent
```

### 3.3 公开 `@` 与私下 A2A 的选择

Agent 使用以下固定判断：

```text
没有目标 Agent 的返回结果，当前 Run 就不能完成
  -> 使用私下 A2A；当前 Run 可以 waiting_agent，并在结果返回后恢复

当前 Run 已经可以结束，接下来由其他 Agent 在群里公开继续
  -> 在终态公开回复中 @ 对方；当前 Run 结束，对方创建新 Run

只需要让对方知道，不需要返回结果
  -> 使用 A2A notify
```

因此，群聊公开协作层不使用“公开等待并恢复旧 Run”的机制。`waiting_agent` 只属于私下 A2A 的内部执行过程，群成员不可见。

### 3.4 A2A 对共享 Runtime 变更的继承

1. Group 场景里的 `send_message_to_agent` 与 A2A delegated Run 继续走既有 A2A 入口和同一 `ToolExecutionOutcome`，不得返回一套 Group 专属字符串结果。
2. `notify` 完成后当前 Run 可以继续；`consult / task_delegate` 创建 delegated Run 后，来源 Run 进入 `waiting_agent`，目标完成时以精确 correlation 恢复同一来源 Run 和同一 Thread。
3. A2A 的 reservation、effect、retry policy、started/succeeded/failed/unknown、结果引用和幂等全部写入共享 Tool Ledger；产品同步失败不得导致重复发送或重复创建 delegated Run。
4. cancel 当前 Group Run 时，只中断该 Run。已经产生的外部副作用不回滚；是否取消仍在执行的 A2A child 不是默认行为，需要独立产品策略。

## 4. Runtime 必需的执行关联

本方案不再引入 Collaboration Graph，也不引入独立的 Execution Root 对象。Runtime 只复用现有 AgentRun、ChatMessage、Planning checkpoint 和 A2A 记录。

### 4.1 AgentRun 现有父子字段

`parent_run_id / root_run_id` 是 `AgentRun` 上已有的可空自关联字段，不是另一张 Root 表，也没有独立 Root 状态或生命周期。

固定规则：

1. 人类单 `@` 创建的顶层 Agent Run：`parent_run_id = null`，`root_run_id = null`。
2. 人类多 `@` 创建的顶层 Planning Run：`parent_run_id = null`，`root_run_id = null`。
3. Planning 创建的入口 child：`parent_run_id = Planning Run.id`，`root_run_id = Planning Run.id`。
4. A2A delegated Run：`parent_run_id = 调用 Run.id`，`root_run_id = 调用 Run.root_run_id or 调用 Run.id`。
5. 默认自组织模式和 `enforced` Workflow 由 Agent 公开 `@` 创建 child Run 时，都沿用同一规则：`parent_run_id = 来源 Run.id`，`root_run_id = 来源 Run.root_run_id or 来源 Run.id`。Planner 协作 child 额外继承来源 Run 输入快照中的不可变 `mode + plan_prompt`。
6. 人类在任何时候发出的新公开 `@` 都创建新的顶层 Run，不继承当前正在运行的 Planning/Workflow lineage，也不恢复已经结束的旧 Run。
7. 这些字段只记录执行来源，不用于判断群消息是不是“同一个长期任务”，也不作为额外业务上下文注入普通 Agent。
8. 不做 Root 级完成聚合、Root 状态机或全局静止检测；Planning Run 只负责生成计划并启动入口 children，后续公开协作由 Agent 自己推进。

### 4.2 使用现有业务记录

需要保留的事实分别由已有业务对象记录：

| 事实 | 记录位置 |
|---|---|
| 群公开内容和公开 `@` | `ChatMessage + mentions` |
| Agent 执行及父子关系 | `AgentRun` |
| 私下 A2A 调用、delegated Run 和返回 | A2A 工具调用及 Runtime 关联记录 |
| Planner 建议或人类强制 Workflow | Planning Run checkpoint 中的 `mode + plan_prompt + entry_steps` |
| Workflow 入口任务 | Planning 创建的入口 child `AgentRun` |
| Workflow 后续公开轮转 | 来源 AgentRun、公开 ChatMessage mentions 和新 child AgentRun |

Run 至少需要以下字段或等价信息：

```text
run_id
agent_id
root_run_id / parent_run_id
source_id（Group Run 中指向触发消息 ID）
payload.message_id
scheduling_position_created_at / scheduling_position_id
runtime_thread_id（Group 中每个 Planning/Agent Run 独立）
graph_name / graph_version（仅作观测元数据，不参与 checkpoint 恢复路由）
start/resume/cancel AgentRunCommand
command.applied_checkpoint_id
LangGraph config 中的 thread_id
checkpoint metadata 中的 clawith_run_id / clawith_command_id
delivery_target / delivery_status
mode + plan_prompt（Planner 协作 Run 输入快照内；不是新表或 AgentRun 列）
```

执行状态、waiting、结果、错误和验证事实由 `RunStateReader` 从目标 Run 的精确 checkpoint 读取并转换为 `RunView`，不再存储或读取 `projected_execution_status / projected_result_summary / projection_checkpoint_id / projection_updated_at`。

tenant、Run 与 Thread scope 先由 `AgentRun` 校验；Thread identity 来自 LangGraph config。恢复和对账只使用 namespaced `clawith_run_id / clawith_command_id` metadata，不能把 LangGraph invocation 的通用 `run_id` 当成 Clawith `AgentRun.id`。`graph_name / graph_version` 仅保留为观测元数据，部署后的当前 Graph 代码用于恢复，不按旧 version 路由历史代码。

不为了“协作关系”新增 Graph、节点表、边表、Workflow execution 表或独立事件日志。`mode + plan_prompt` 保存在现有 Planning checkpoint，并复制进相关 Run 的不可变输入快照；现有记录不足以完成 Runtime 路由、隔离、幂等或审计时，优先复用已有字段、Command 和 checkpoint payload。

### 4.3 群上下文快照

目标机制是：每个由公开 `@` 创建的新 Run，都把触发消息作为群消息上下文截止点：

```text
source_id / payload.message_id
+ scheduling_position = (trigger_message.created_at, trigger_message.id)
+ parent_run_id（Agent 公开交接时）
-> new_run_id
```

这里不要求新增 `trigger_message_id` 或 `context_cutoff_*` 列；现有 `source_id`、payload 和 scheduling position 已经携带触发位置。

**实现差距：** 当前 Context snapshot 虽然会保存固定的 `group_context.trigger`，但 Session Context 和 recent messages 是在 start command 真正执行时读取的，没有按 scheduling position 过滤。因此以下截止规则尚未实现，连续 `@` 的实际行为以 2.6 为准。

固定规则：

1. 新 Run 的基础群消息上下文由现有 Session Context 和压缩机制生成，但只读取截止位置及以前的消息，不按 Worker 真正开始执行的时间读取最新消息。
2. 同一条公开消息同时创建的多个 Run 使用相同的群消息截止点；排队和实际执行先后不能改变它们的语义输入。
3. 触发消息之后产生的普通群消息或其他 Run 结果，不自动注入已经创建的 Run。
4. 后续信息如果需要某个 Agent 处理，必须通过新的公开 `@` 创建新的消息、上下文截止点和 Run。
5. Run 状态、队列状态和其他 Runtime 控制面变化不得反向改写已启动 Run 的语义输入快照。
6. 后续新 Run 按自己的新截止点读取群 Session Context；历史 Run 的输入保持可重放。
7. Run 父子树、A2A trace 和 Runtime 控制记录不默认注入普通 Agent 上下文。
8. `mode + plan_prompt` 例外：它们会直接影响 Agent 行为，因此属于 Planner 协作 Run 的必要业务输入，必须提供给入口 Run 和后续公开 child Run；它们不是协作图或 lineage 摘要。
9. Group Session Context compact 必须使用同一消息截止点生成或选择可用版本；不能在 recent messages 截断到触发消息的同时，注入包含触发消息之后内容的最新 compact。
10. Group Session 原始消息和产品记录不因 compact 被删除；compact 只是多个独立 Group Run 共享公开历史的有界派生上下文，不是 LangGraph 执行真值。

### 4.4 LLM 与 Runtime 的固定分工

| 动作 | 谁决定 | 谁验证并保存记录 |
|---|---|---|
| 人类公开 `@Agent` | 人类 | Runtime |
| Agent 公开 `@Agent` | Agent LLM | Runtime |
| `send_message_to_agent` | Agent LLM | Runtime |
| A2A 结果返回 | A2A 终态回调 | Runtime |
| 人类 Workflow 编译为完整 Prompt | Planner LLM | Runtime 保存到 Planning checkpoint 和入口 Run snapshot |
| Workflow 中下一位公开 `@` | 当前 Agent LLM 按 `plan_prompt` 判断 | Runtime 校验并创建新 Run |
| `parent_run_id / root_run_id` | Runtime | Runtime |
| 人类多 `@` 的初始建议 | Planner LLM | Runtime 保存到 Planning Run |

硬规则：

1. LLM 只提交结构化 mention、工具调用、Planner 的 `mode + plan_prompt + entry_steps` 或终态动作。
2. Runtime 校验目标身份、群或 A2A scope、权限、预算、幂等和当前 Run 状态，再写入对应的现有业务记录。
3. 不增加 Graph Retriever、Graph Resolver、Embedding、`continue/unrelated` 分类或任何 Graph 相关 LLM 输出。
4. 不把 Run 父子关系或其摘要作为额外业务上下文提供给 Agent。
5. 不因为判断“是否继续旧任务”而阻塞 Agent 执行；Agent 直接根据当前消息和压缩后的群 Session Context 工作。

### 4.5 明确删除的机制

当前方案不包含：

- `collaboration_graph_id`；
- `goal_version / continues_from_root_id`；
- `suggested_graph / actual_graph`；
- Graph 节点、边、归属、候选检索和语义合并；
- Graph 相关可视化、持久化、上下文注入和模型调用；
- 将历史 Graph 提升为 Workflow 的机制。

如果以后需要执行路径分析或可视化，应作为独立需求，从已有消息、Run、A2A 和 Planning checkpoint 离线查询，不回到当前 Agent 执行关键路径。

### 4.6 Group Run 状态与产品同步

Group 的每个 Run 都按自己的 Command/checkpoint identity 独立结算：

```text
Command claim
-> 读取该 Run 的精确 checkpoint
-> 执行到 stable waiting / terminal
-> Command applied_checkpoint_id settle
-> Group 产品同步读取 RunView / delivery intent
-> 幂等写 ACK、公开回复、mentions、child Runs、Planning 回传和 lane release
```

产品同步需要拆成可独立重试、带稳定幂等键的窄处理器。ACK、普通 delivery、带 mention 的 handoff delivery、Planning 入口 child 创建、A2A completion、Session Context merge 和 lane release 可以由不同 handler 负责，但任何 handler 都不得决定 Graph 下一节点，也不得因自身失败重新运行模型或工具。

同一 Group Run 的 `completed` 只代表该 Run 已通过确定性验证并形成稳定 delivery intent。公开消息已经可见、child Run 已创建等产品事实必须分别以同步回执为准；不得把 terminal checkpoint 当成所有产品副作用已经完成的证明。

## 5. Run 结果与公开群回复

当前实现不增加 `internal_result`、`public_reply` 或 `GroupPublicReply` 领域对象：

1. 普通 Group Run 完成后，现有 checkpoint 中的 `final_answer / delivery_request` 经过 delivery 写成群 `ChatMessage`。
2. delivery event 内部保存 `run_id -> message_id` 回执，用于幂等和追溯；`ChatMessage` 自身目前不保存 `source_run_id` 或 `reply_to_message_id`。
3. 私下 A2A Run 的结果返回调用 Agent，不直接写入群消息；调用 Agent 决定如何整理成自己的公开答案。
4. 默认自组织模式或 `enforced` Workflow 需要 Agent 终态公开 `@` 时，最小改动是在现有 `finish -> lifecycle.delivery_request -> delivery` 链路中增加可选的结构化 mention participant IDs，并复用群消息 intake 创建 mentions 和 child Runs；不新增平行交付契约。
5. Planner 来源 Run 创建公开 child Run 时，必须把不可变 `mode + plan_prompt` 继续传入 child 的输入快照；自由模式与 Workflow 模式使用同一传递机制。
6. 当前来源 Run 的 terminal checkpoint 与产品侧群消息投递不是同一个数据库事务；实现时可以保证“公开消息 + mentions + child Runs + commands”原子落库，但不能把来源 terminal checkpoint 也宣称为同一事务。
7. terminal checkpoint 中的 delivery intent 必须包含公开 `content`、已预检的 `mention_participant_ids`、来源 `run_id`、`parent/root` 传播输入、Group/Session scope、上下文截止点和稳定幂等键；不得依赖同步时重新解析模型文本。
8. 不带 mentions 的普通 Group 回复继续复用现有 delivery；带 mentions 的 handoff delivery 复用同一交付契约，只在一个产品事务中额外创建 mentions、child Runs 和 start commands。
9. delivery 成功回执必须能从 `run_id` 定位最终 `message_id`；同一 delivery intent 的重试只能返回已有结果，不能重复消息或重复唤醒。
10. delivery 失败不会把已完成 Run 改回 running。可重试错误由产品 reconciler 重试；不可恢复的 scope/权限竞态以明确 delivery failure 暴露，不伪装成 Run 未完成。

## 6. 公开多轮讨论示例

人类要求 A、B 公开讨论直到达成共识，属于人类 Workflow：

```text
人类：@A @B 围绕方案讨论，直到达成共识

Planner（内部 plan_prompt，mode = enforced）：
- A 首先提出观点
- 尚未达成共识时，当前发言者公开说明分歧并 @另一方
- 达成共识时，当前发言者输出共同结论且不再 @

A：第一轮观点…… @B
B：仍有分歧…… @A
A：修订观点…… @B
B：已达成共识，最终结论是……
...
```

规则：

1. 每轮有用内容都作为普通群消息公开，并进入群 Session Context。
2. 最初的人类结构化 `@A @B` 是 Planning 入口；Planner 启动入口 Agent A，后续每次业务 Agent 被唤醒，群里都有上一位 Agent 的公开结构化 `@`。
3. A、B 每次获得同一份完整 `plan_prompt`，根据当前公开上下文判断继续还是结束。
4. Agent 必须按 Workflow 选择公开交接目标，不得添加计划外参与者或擅自改变人类规则。
5. 公开辩论轮次不是 A2A。
6. 如果辩论 Agent 私下调用第三个 Agent 辅助研究，该支线才是普通 A2A；A2A 内容不可见，调用 Agent只能在后续公开发言中整理引用。

## 7. 完成、等待与协作停止

### 7.1 当前 Run 的确定性完成下限

普通 Group Agent Run 与其他入口共用同一完成协议：

1. 模型只有在当前责任已经完成、必要验证已经执行后才能调用 `finish`；`finish` 必须非空且是该响应唯一 Tool Call。
2. 当前 Run 不得存在 pending Tool Call，Tool Ledger 中也不得存在未结算的 `started / unknown` 执行；reservation 落库后即为 `started`，没有独立 `reserved` 状态。
3. 最终 artifact/evidence 只从当前 Run 已 `succeeded` 的 typed `ToolExecutionOutcome` 收集，并校验 tenant/run scope、真实存在与可读性。
4. Group 结构化 mentions 由 2.4.1 的 delivery preflight 校验，不由自然语言语义 Verifier判断。
5. 可修复协议错误最多进入两次 repair；权限、配置或不可恢复错误形成 typed failure。Verifier 只返回 pass/repair/fail，不增加第二个等待路由。

### 7.2 协作停止边界

1. 默认自组织模式下，Agent 可以结合现场情况偏离 `plan_prompt` 并决定是否发出新的公开 `@`；`enforced` Workflow 下，Agent 根据完整 `plan_prompt` 的转换条件决定是否公开 `@` 下一位。
2. Agent 需要私下协助时，显式调用普通 A2A 工具；没有目标 Agent 的结果就无法完成时，当前 Run 可以 `waiting_agent` 并在精确回传后恢复。
3. Group Agent 需要用户补充时，不使用当前缺少群回复关联的 `waiting_user`。它以终态公开回复提出问题且不继续 `@`；用户后续新的结构化 `@` 创建新顶层 Run。
4. Agent 认为自己的当前责任完成时，不再发起新的协作，并返回结果。它结束的是自己的 Run 或责任分支，不会自动关闭其他 Agent 分支。
5. 不新增 execution root 级静止检测器、Root 状态机或语义完成裁判。Planning Run 只生成计划并启动入口 children，不承担后续全局协调和完成判定。
6. 当前 root 下没有新的公开 `@`、没有新的 A2A，且既有 Runs 均已稳定终态，只能说明协作执行已经静止；不等于业务答案客观正确、完整或满足了人类 Workflow 的所有自然语言条件。
7. 本版没有“重新打开旧终态 Run”。后续用户输入或 Agent 公开交接都创建新 Run；只有已经稳定 waiting 的同一 Run 才能通过合法 correlation resume。

## 8. Session、Workspace 与 Memory 边界

已经确定：

- 群 Session 保存公开群消息，是群内共享对话上下文。
- A2A Session 保存私下 Agent 协作，不进入群上下文。
- `root_run_id / parent_run_id` 和现有触发消息引用只用于 Runtime 执行关联，不替代消息正文或产物正文，也不默认进入 Agent 上下文。
- 新的人类 Run 通过压缩后的群 Session、Workspace 和 Memory 获得语义连续性，不依赖旧 Run 归属判断。
- Group Session Context compact 是公开群历史的唯一共享 compact。它独立于每个 Group Run 的 LangGraph Thread，不与 Direct Chat Thread Running Summary 合并，也不再额外叠一份 Group Run summary 作为第二份公开历史真相。
- Workspace 文件、Group Memory、成员资料和公告属于动态数据，不进入 Base Prompt 稳定内核；模型通过有界常态快照和真实可用的 Group tools 渐进读取。
- 当前指令和最新的人类消息高于历史 Session compact、Workspace 与 Memory；这些数据不能覆盖平台规则、人类当前目标或 `enforced` 中来自人类的流程约束。

仍需讨论：

1. 哪些内容只保留在 Session，哪些内容应沉淀到 Workspace 或 Memory。
2. Workspace 产物如何记录 `group_id / session_id` 归属以及可选的 `root_run_id / run_id` 来源，并处理并发写入和版本冲突；Run 关联只能作为 provenance，不能限制后续新 Run 读取共享 Workspace。
3. 群 Memory 是事实、决策、协作经验还是摘要，谁有权写入，何时验证后生效。
4. 当前消息、压缩后的 Session、Workspace 和 Memory 同时存在时，Agent 应按什么优先级读取。
5. 用户修改目标后，旧上下文和旧产物如何降级为历史证据，而不是继续成为当前指令。

## 9. Runtime 控制面

Runtime 不参与业务判断，但必须集中维护：

- tenant、group、session、user 和 Planning scope；
- Run 身份、`parent_run_id / root_run_id`、触发消息引用、scheduling position 和幂等；
- Planning、普通 Group Run、公开 child Run 和 A2A delegated Run 的创建与状态；
- 每个 Run/Command 的 checkpoint metadata、`applied_checkpoint_id`、typed `RunView` 和独立产品同步回执；
- 并发、深度、Run 数、token、时间和成本预算；
- 循环检测、取消、超时、失败和恢复；
- A2A 与群公开通道的权限隔离；
- Workspace/Memory 的版本和写入约束。

Runtime 控制面不维护 `projected_*`、Group workflow 全局状态或语义完成结论。Group lane 只负责产品级执行互斥，LangGraph checkpoint 只负责单 Run 执行进度，两者不能互相替代。

## 10. 实施顺序与仍待产品决定的事项

### 10.1 已确认实施顺序

1. 先落地共享 Runtime 正确性：checkpoint/Command 真值、删除 projector、精确 `RunStateReader`、稳定 checkpoint 后的幂等产品同步和 interrupt-and-preserve cancel；Group 作为共享入口做代表性回归，不在这一阶段改变 Planning 产品语义。
2. 落地 canonical Tool definitions、扩展既有 `ToolExecutionOutcome`、Tool Ledger/Result Store、确定性 Verifier和有效 Tool 解析；同步修复 Group member `agent_id`、Group tool typed outcome 与条件化可见性。
3. 落地 Base Prompt V1 和 Context assembly，删除 Group self Role、重复 scope/tool permission、Planning instruction 与相关摘要，保证 Group Agent Run 只有一个当前责任和一份 Group Capability Policy。
4. 把 Planning `version = 1` 静态 DAG 改为已确认的 `version = 2 / mode + plan_prompt + entry_steps`，只启动入口 children，并把不可变计划传给后续公开 child Run。
5. 实现 Group 条件化 `finish.mention_participant_ids`、terminal 前全有或全无 preflight，以及公开消息、mentions、child Runs、commands 的原子幂等产品同步。
6. 实现严格触发消息截止，包括 recent messages 与 Group Session Context compact 的同一 cutoff 语义。
7. 最后做共享入口回归和 Group 专项回归；表结构修改按与 `main` 的最终 schema 差异一次生成一份合并 migration，不在当前开发分支按实现阶段叠加多份迁移脚本。

### 10.2 Group 专属回归门禁

至少覆盖：

1. 人类单 `@` 只创建一个 Group Agent Run；人类多 `@` 只创建一个 Planning Run。
2. Planning v2 只启动 `entry_steps`，入口和后续公开 child 都获得同一不可变 `mode + plan_prompt`。
3. Group/Planning Run 保持独立 thread identity；Direct Chat 的 Session Thread 改动不能把多个 Group Agent 写入一个 Thread。
4. 同一 Group Agent lane 串行，不同 Agent 可按既有策略并行；waiting_agent、cancel、terminal 与 lane release 行为明确。
5. Group Context 不含 self `role_description`、重复 scope/tool policy、重复当前指令或重复 related summaries；Group tools 追加后再计算有效工具名。
6. 非 Group Run 看不到 Group tools 或 Group finish 扩展；合法 Group Run 的 `group_query_members` 返回稳定 `agent_id`。
7. 多目标 handoff 全有或全无；失败不发布消息、不创建部分 child；同步重试不重复消息或唤醒。
8. 每个 child 使用相同触发消息 cutoff；排队延迟不能混入 cutoff 之后消息或更新后的 compact。
9. 私下 A2A 不写群消息，consult/task_delegate 恢复原 Run；公开 `@` 创建新 child Run，不恢复终态 Run。
10. `RunView` 精确返回目标 Group Run，而不是同 Session、同 root 或同 Agent 的另一个 Run checkpoint；产品同步失败不重跑模型/Tool。

### 10.3 仍待后续产品决定

以下事项不阻塞上述正确性修复：

1. Group lane 最终继续按 `tenant + agent` 全局串行，还是缩小到 Group/Session。
2. 人类新 `@` 与 `enforced` Workflow 后续步骤谁优先，是否允许穿插。
3. 群消息是否增加 `reply_to_message_id / source_run_id` 的显式 UI 关联。
4. Workflow 偏离、弱模型不交接和无界循环的进一步产品策略；V1 先使用现有深度、Run 数、token、时间与成本预算兜底。
5. Workspace 并发写入、版本冲突、产物所有权，以及 Group Memory 的长期治理。
6. 是否提供 Workflow/root 级观察页面；即使提供，也只能聚合既有事实，不能变成新的执行真值或语义完成裁判。

# Clawith 单 Agent Runtime、Run State 与 Context Summarization PRD

## 1. 背景

当前 Clawith 单 Agent 已经具备基础的对话、工具调用、文件操作、触发器唤醒和 A2A 协作能力，但它的运行过程仍主要依赖最近聊天历史和函数内的 tool-call 循环来维持上下文。Claude Code、Codex、OpenCode 等主流 Agent 产品已经把 context summarization、Agent Run、tool-call loop、trace、恢复点和验证结果视为统一的 runtime 能力；普通聊天记录和单个 LLM 调用函数只承载其中一部分能力。Clawith 要逐步向这类 Agent Runtime 设计看齐，但需要分阶段推进。

本 PRD 是第一阶段，只做两件事：

1. Context Summarization：把长对话、工具结果和关键上下文总结成可注入的结构化摘要。
2. Runtime DB：用 Agent Run 表达当前任务做到哪里、如何恢复、有哪些证据和阻塞，同时为后续升级 agent loop runtime 打基础。

## 2. 目标

### 2.1 产品目标

本阶段的产品目标是让 Clawith 单 Agent 在 Context Summarization、Agent Run 状态和任务恢复能力上，逐步向 Claude Code、Codex、OpenCode 等主流 Agent 的设计看齐。

具体表现是：让单 Agent 在长对话、跨天任务、A2A 委托、等待外部回复或工具结果较多的场景中，能够知道：

- 当前要完成什么。
- 已经做了哪些步骤。
- 哪些结论已经确认。
- 关键证据在哪里。
- 是否正在等人、等 Agent、等外部系统或等时间。
- 被 trigger / A2A / 用户继续消息唤醒后应该从哪一步接着做。

### 2.2 工程目标

本阶段的工程目标是建立单 Agent 的最小持久化 Run State 和 Runtime Context 注入路径。

第一阶段定义三张 runtime 表：

```text
Session Summary 表：session_context_states
Agent Run 状态表：agent_runs
Run Events 表：agent_run_events
```

三张表都需要持久化保存，但保存方式不同：

- `session_context_states` 保存当前 session 的最新滚动 Session Summary，每次压缩覆盖更新同一条 current state 数据。
- `agent_runs` 保存每次 Agent Run 的当前状态和恢复信息。
- `agent_run_events` 追加保存 Agent Run 过程中的 Run Events。

完成后，单 Agent 的 LLM 调用由 Runtime Context 统一组装，最近消息作为局部上下文参与组装：

```text
soul.md
+ Session Summary
+ 当前 Agent Run 状态
+ 最近少量消息
```

其中 `agent_runs` 和 `agent_run_events` 同时承担任务状态呈现、执行恢复和 agent loop runtime 升级的基础能力。

## 3. 核心概念

### 3.1 Session Summary

Session Summary 是当前会话历史的结构化摘要，用于在长对话后恢复会话上下文。

它回答的问题是：这个 session 到目前为止发生了什么、哪些结论已经确认、还有哪些事项未完成、关键证据和相关 workspace 文件在哪里。

它是 LLM 调用前用于恢复上下文的滚动 Session Summary 记录。同一个 session 默认只有一条当前 summary，每次压缩基于旧摘要和新增上下文生成新摘要，并覆盖更新当前记录。

### 3.2 Agent Run

Agent Run 是一次可恢复的 Agent 执行单元，用于表达 Agent 当前正在处理的一件事。

它可以由这些入口创建或续接：

- 用户在 chat 中发起一个明确任务。
- trigger 唤醒 Agent。
- `task_executor` 执行后台任务。
- A2A `task_delegate` 发起或返回。
- heartbeat 等系统机制唤醒。

它回答的问题是：这次执行的目标是什么、当前状态是什么、做到哪一步、有哪些阻塞、下一次唤醒后应该如何继续。

Agent Run 保存执行状态和恢复信息。大段消息正文和工具结果正文继续由消息、工具日志或 workspace 文件承载。

### 3.3 Run Events

Run Events 是 Agent Run 过程中的追加式事件日志，用于记录执行过程中的关键节点。

它回答的问题是：一次 run 中发生过哪些模型调用相关事件、tool call、tool result、等待、错误、验证和状态变化。

Run Events 是后续升级 agent loop runtime 的基础。它让 tool-call loop 从函数内部临时过程升级为可以被观察、恢复、压缩和排查的 trace。

### 3.4 Runtime Context

Runtime Context 是每次调用 LLM 前构造的上下文输入，用于把 Session Summary、Agent Run 和最近消息组合成一次模型调用可理解的输入。

它来自：

- 当前 `ChatSession` 的 Session Summary。
- 当前 Agent Run。
- 最近少量消息，v1 默认 Web chat 最近 20 条。
- 现有 Agent system prompt，例如 `soul.md`、角色、工具规则。

Runtime Context 的目标是让 Agent 在长任务中优先看到“当前状态”，减少对大量聊天历史和最近几条消息的依赖。

## 4. Runtime DB 设计

Runtime DB 是第一阶段的核心工程改造，用于把单 Agent 的上下文恢复、Run State 和 trace 沉淀为三类可持久化对象。

这些表由 Clawith 后端运行时服务写入，LLM 不直接操作数据库。后端负责创建 run、推进 status、记录 tool call / tool result、关联消息和文件引用，并在合适时机触发 Context Summarization。LLM 可以生成计划、步骤描述、摘要、恢复说明和验证结论等文本内容，但这些内容需要由后端按当前运行过程、工具结果和权限边界校验后再落库。

写入责任可以按三类区分：

1. 系统事实字段由后端自动写入，例如 `tenant_id`、`agent_id`、`session_id`、`source_type`、`message_id`、`tool_call_id`、`tool_name`、`created_at`、`updated_at`。
2. 状态推进字段由后端根据运行流程写入，例如 `status`、`current_step`、`completed_steps`、`pending_steps`、`last_error`、`completed_at`。LLM 不直接写 `status`、`current_step`、`completed_steps`、`pending_steps` 等字段。LLM 只生成候选语义内容；后端 runtime 根据实际运行过程、工具调用结果、等待条件、错误和验证结果推进状态并落库。
3. 语义摘要字段可以由 LLM 或 Context Summarization 生成候选内容，再由后端写入，例如 `goal`、`plan`、`summary`、`decisions`、`open_items`、`resume_instruction`、`verification_result`。

三张表的职责如下：

| 表 | 职责 | 保存方式 |
|-|-|-|
| `session_context_states` | 保存一个 session 当前最新的 Session Summary，供下一次 LLM 调用恢复上下文。 | 持久化保存，滚动覆盖更新；v1 不保留每次压缩的历史版本。 |
| `agent_runs` | 保存一次 Agent Run 的当前状态、目标、步骤、阻塞和恢复说明。 | 持久化保存，每次 run 一条主记录。 |
| `agent_run_events` | 保存 Agent Run 过程中发生的关键事件，例如模型调用相关事件、tool call、tool result、等待、错误和验证。 | 持久化追加保存。 |

三者关系：

```text
agent_run_events 记录过程
agent_runs 保存当前状态
session_context_states 保存下次 LLM 可注入的 Session Summary
```

Runtime DB 只保存摘要和引用。大段工具结果、大文档正文或大网页正文继续保存在现有消息、工具日志、workspace 文件或外部系统中。

### 4.1 agent_runs

`agent_runs` 表表示一次可恢复的 Agent Run。

它解决的问题是：Agent 当前正在处理哪件事、这件事处于什么状态、已经完成哪些步骤、还在等待什么、下一次被唤醒后应该如何继续。

一个 Agent Run 可以由 chat、trigger、task、A2A、heartbeat 等系统机制创建。普通短问答保持原有 chat 路径；一旦任务需要多步执行、工具调用、等待外部结果或跨轮恢复，就应该创建或续接 run。

`agent_runs` 保存当前状态。完整执行过程由 `agent_run_events` 追加记录。

建议字段：

| 字段 | 说明 |
|-|-|
| `id` | run ID。 |
| `tenant_id` | 所属租户。 |
| `agent_id` | 执行 Agent。 |
| `session_id` | 关联 `ChatSession.id`，可为空。 |
| `source_type` | `chat` / `trigger` / `task` / `a2a` / `heartbeat`等。 |
| `source_id` | 来源对象 ID，如 message_id、trigger_id、task_id、A2A session id。 |
| `origin_user_id` | 原始请求人，可为空。 |
| `origin_agent_id` | 原始请求 Agent，A2A 场景使用，可为空。 |
| `goal` | 本次 run 的目标。 |
| `status` | 当前运行状态。 |
| `plan` | 结构化步骤列表。 |
| `current_step` | 当前步骤。 |
| `completed_steps` | 已完成步骤。 |
| `pending_steps` | 未完成步骤。 |
| `blockers` | 阻塞项。 |
| `evidence_refs` | 关键证据引用。 |
| `workspace_refs` | 相关文件引用。 |
| `resume_instruction` | 下次唤醒时直接给 Agent 的恢复说明。 |
| `verification_result` | 验证结果。 |
| `last_error` | 最近错误。 |
| `created_at` | 创建时间。 |
| `updated_at` | 更新时间。 |
| `completed_at` | 完成时间。 |

状态枚举：

```text
planning
running
waiting_user
waiting_external
waiting_agent
verifying
completed
failed
cancelled
```

状态规则：

1. `planning`：Agent 正在拆解目标和计划。
2. `running`：Agent 正在执行当前步骤。
3. `waiting_user`：需要用户补充信息或确认。
4. `waiting_external`：等待外部系统、webhook、时间或第三方回复。
5. `waiting_agent`：等待另一个 Agent 的 A2A 返回。
6. `verifying`：主要执行已完成，正在校验和收尾。
7. `completed`：已完成并已向目标接收方交付结果。
8. `failed`：无法继续执行，且已记录原因。
9. `cancelled`：被用户或系统取消。

### 4.2 agent_run_events

`agent_run_events` 表是 Agent Run 的追加式事件流。

它解决的问题是：一次 run 的执行过程需要从函数调用栈沉淀为可查询的事件记录。每次模型调用、工具调用、工具结果、等待、错误、恢复、验证和状态变化，都应该形成对应事件。

`agent_run_events` 从 run 视角记录关键事件摘要、结构化小字段和产物引用，用于恢复、压缩、排查和后续升级 agent loop runtime。

建议字段：

| 字段 | 说明 |
|-|-|
| `id` | event ID。 |
| `run_id` | 所属 run。 |
| `tenant_id` | 所属租户。 |
| `agent_id` | 执行 Agent。 |
| `event_type` | 事件类型。 |
| `message_id` | 关联 ChatMessage，可为空。 |
| `tool_call_id` | 工具调用 ID，可为空。 |
| `tool_name` | 工具名，可为空。 |
| `summary` | 事件短摘要。 |
| `payload` | 结构化小字段。 |
| `artifact_refs` | 产物引用，包括文件、消息、工具结果和外部链接。 |
| `created_at` | 创建时间。 |

事件类型：

```text
run_created
status_changed
plan_created
step_started
step_completed
model_call_started
model_call_completed
assistant_message
tool_call
tool_result
evidence_added
workspace_ref_added
blocker_added
waiting_started
resume_instruction_updated
verification_updated
compression_updated
run_completed
run_failed
run_cancelled
```

大文本规则：

1. `payload` 保存结构化小字段和短摘要。
2. 大工具结果保存在现有工具日志、ChatMessage 或 workspace 文件中，`artifact_refs` 保存引用。
3. `summary` 用于 LLM 快速恢复，完整审计继续依赖原始消息、工具日志和产物。

### 4.3 session_context_states

`session_context_states` 表表示一个 session 当前最新的 Session Summary。

它解决的问题是：长对话需要减少对最近消息和完整历史回读的依赖。系统需要把已经发生过的对话、工具结果、关键结论和当前 run 摘要整理成下一次 LLM 调用可直接注入的 Session Summary。

同一个 session 默认只有一个当前 Session Summary 记录。每次压缩基于旧摘要和新增上下文生成新摘要，并覆盖更新同一条 current state。生成失败时保留旧 Session Summary 记录，并继续使用最近消息作为兜底上下文。

建议字段：

| 字段 | 说明 |
|-|-|
| `id` | context state ID。 |
| `tenant_id` | 所属租户。 |
| `agent_id` | 当前 Agent。 |
| `session_id` | 当前 ChatSession。 |
| `summary` | session 滚动摘要。 |
| `decisions` | 已确认结论和决策。 |
| `open_items` | 未完成事项。 |
| `evidence_refs` | 关键证据引用。 |
| `workspace_refs` | 相关 workspace 文件引用。 |
| `active_run_ids` | 当前 session 关联的未完成 runs。 |
| `last_message_id` | 已压缩到的最后一条消息。 |
| `last_compacted_at` | 最近压缩时间。 |
| `version` | Session Summary 版本。 |
| `created_at` | 创建时间。 |
| `updated_at` | 更新时间。 |

规则：

1. 同一个 `session_id` 默认只有一个当前 Session Summary 记录。
2. 压缩结果覆盖更新，默认保留当前最新版本。
3. 如果压缩失败，仍保留旧 state。
4. `active_run_ids` 保存未完成或近期完成且仍可能被引用的 run。

## 5. Runtime Context 构造

Runtime Context 是 Runtime DB 进入 LLM 的统一入口，是每次模型调用前临时构造的上下文输入。

v1 Runtime Context 采用组合式压缩策略，而不是纯摘要策略：

```text
Session Summary
+ Active Agent Run State
+ Recent Messages
+ Tool Result Handling
```

v1 的原则：

1. 先注入稳定状态，再注入最近消息。
2. 优先保证当前任务状态完整，最近消息作为局部上下文补充。
3. 大正文只通过摘要和引用进入 prompt。
4. 保留现有 `ctx_size` 作为兜底策略，分阶段接入 Runtime Context。

### 5.1 构造输入

每次调用 LLM 前，构造 Runtime Context：

```json
{
  "session_state": {
    "summary": "...",
    "decisions": [],
    "open_items": [],
    "evidence_refs": [],
    "workspace_refs": []
  },
  "active_runs": [
    {
      "run_id": "...",
      "goal": "...",
      "status": "running",
      "current_step": "...",
      "pending_steps": [],
      "blockers": [],
      "resume_instruction": "..."
    }
  ],
  "recent_messages_policy": {
    "max_messages": 20,
    "preserve_tool_pairs": true
  }
}
```

### 5.2 注入顺序

推荐注入顺序：

1. Agent static context：身份、角色、工具规则。
2. Runtime Context：session summary、decisions、open items、evidence refs。
3. Active Agent Run：当前任务状态和 resume instruction。
4. 最近少量消息。

### 5.3 最近消息策略

当前 `ctx_size` 仍保留，但语义从“主要上下文”降级为“短期局部上下文”。

v1 建议：

- Web chat 最近消息默认保留 20 条，并优先保留最近用户原话。
- tool call / tool result 必须继续保持 pair integrity。
- 大工具结果只保留摘要和产物引用。
- 如果有当前 run，优先保证 run 状态注入，最近消息数量按剩余上下文预算控制。

Tool Pair Integrity 规则：

1. Runtime Context 只允许注入完整的 `assistant(tool_calls)` + `tool result` pair。
2. 对持久化的 tool call 记录，应先重建为 `assistant(tool_calls)` + `tool result` 的合法消息序列。
3. 压缩或截断后出现孤立 tool result，直接丢弃。
4. `assistant(tool_calls)` 缺少对应 tool result 时，移除缺失结果的 tool calls；如果 assistant 无正文且无有效 tool calls，则整条丢弃。
5. 大 tool result 可以被摘要化或替换为 `artifact_refs`，但不得破坏 tool call / tool result 的合法配对。

## 6. Context Summarization 策略

Context Summarization 由后端触发，复用当前 Agent 的 LLM 能力，把长 session 历史、工具结果摘要和 run events 转成 `session_context_states`。它的职责是更新可注入的 Session Summary，并保留原始消息作为来源记录。

### 6.1 触发时机

满足任一条件时触发压缩：

1. 当前 session 新增消息超过配置阈值。
2. 工具结果累计长度超过配置阈值。
3. LLM 调用前估算 Runtime Context token 数达到当前模型可用输入窗口的 85%。
4. run 状态从 `running` 进入 `waiting_*`。
5. run 状态进入 `completed` / `failed`。
6. 用户显式要求总结当前会话或当前任务进展。

其中第 3 条是默认自动压缩兜底阈值。可用输入窗口按当前模型总 context window 扣除输出预算和系统保留预算后计算：

```text
available_input_budget =
  model_context_window
  - max_output_tokens
  - reserved_system_and_tools_budget
```

`max_output_tokens` 只表示单次模型回复的最大输出长度，不等同于模型上下文窗口。它不改变压缩策略，只影响触发阈值计算：输出预算越大，可用输入窗口越小，压缩越早触发。

### 6.2 摘要输出

Context Summarization 输出结构：

```json
{
  "summary": "...",
  "decisions": [],
  "open_items": [],
  "evidence_refs": [],
  "workspace_refs": [],
  "active_run_updates": [
    {
      "run_id": "...",
      "resume_instruction": "...",
      "evidence_refs": [],
      "workspace_refs": []
    }
  ]
}
```

规则：

1. Context Summarization 只更新 session state 和 run state。
2. Context Summarization 保留原始 ChatMessage，并更新可注入的 Session Summary。
3. 生成失败时，当前对话继续使用旧 Session Summary 记录或最近消息作为兜底上下文。

### 6.3 摘要写入

压缩结果写入：

1. `session_context_states`。
2. 必要时更新 `agent_runs` 的 `resume_instruction`、`evidence_refs`、`workspace_refs`。
3. `agent_run_events` 追加 `compression_updated`。

## 7. Agent Run 接入流程

Agent Run 流程的目标是在第一阶段沿用现有 tool-call loop，并在现有 chat / trigger / task / A2A 路径上补齐 run 状态和事件记录。

v1 接入原则：

1. 现有 LLM 调用和工具执行路径继续工作。
2. 在入口处创建或续接 `agent_runs`。
3. 在关键节点追加 `agent_run_events`。
4. 在 LLM 调用前通过 Runtime Context 注入当前 session 和 run 状态。
5. 在中断、等待、失败或完成时更新 `agent_runs.resume_instruction` 和 `status`。

### 7.1 用户消息入口

用户发消息后：

```text
1. 解析 ChatSession。
2. 找或创建 session context state。
3. 判断是否续接当前未完成的 run：
   - 用户明确继续之前任务：续接。
   - 用户发起新任务：创建新 run。
   - 用户只是普通问答：保持原有 chat 路径。
4. 构造 Runtime Context。
5. 调用 LLM。
6. 将模型调用相关事件、tool call、tool result、最终回复写入 agent_run_events。
7. 根据执行结果更新 agent_runs。
8. 必要时异步压缩。
```

### 7.2 Trigger 入口

trigger 唤醒时：

```text
1. 根据 trigger.focus_ref / config / origin_session_id 查找关联 run。
2. 如果找到当前未完成的 run，注入 run.resume_instruction。
3. 如果找不到 run，按现有 trigger reason 创建临时 run。
4. LLM 执行后更新 run 状态。
5. 如需等待下一次触发，状态进入 waiting_external / waiting_user / waiting_agent。
```

### 7.3 A2A task_delegate

source Agent 发起 `task_delegate` 时：

```text
1. source run 记录 waiting_agent。
2. 创建或关联 target run。
3. source run 写入 waiting reason 和 target_agent_id。
4. target 完成后返回结果。
5. source run 被唤醒，进入 verifying 或 running。
6. source 决定汇总给用户、继续追问 target，或完成 run。
```

### 7.4 task_executor

现有 `task_executor` 从一次性执行改为 run 驱动：

```text
1. Task 创建或执行时创建 agent_run。
2. status: planning -> running。
3. 每个主要步骤写 event。
4. 若等待外部，Task 保持执行态或等待态，run 进入 waiting_*。
5. 验证完成后 Task 才进入 done。
```

## 8. 权限和数据边界

1. Runtime DB 中的摘要、证据引用和 run 状态必须按现有 session / tenant / agent 权限过滤。
2. A2A 场景中，source run 和 target run 按完成任务所需的 origin context 共享上下文。
3. Runtime DB 保存摘要和引用，大段工具结果和外部文档正文继续保存在原始来源。
4. `agent_run_events` 是 append-only 事件流，业务修正通过追加新事件表达，旧事件语义保持稳定。

### 8.1 Session 删除后的 Runtime Context 生命周期

用户侧删除 session 属于 soft delete。

系统不立即物理删除该 session 关联的 `session_context_states`、`agent_runs`、`agent_run_events` 等 runtime 派生产物，但必须将其从 Runtime Context 查询和注入路径中排除。

被删除 session 的压缩摘要、run state 和 run events 仅可用于后台恢复、审计、排查或延迟清理，不得继续影响 Agent 后续回复。

v1 可优先只依赖 `ChatSession.deleted_at` 判断；Runtime Context 查询时过滤 `deleted_at is null` 的 session。后续如需要更清晰的生命周期管理，再为 runtime 表补充 `archived_at` 或 `deleted_at` 字段。

## 9. 实施顺序

### Step 1：Runtime DB 和 Runtime Context

目标：先让 Agent 能从结构化 state 中恢复当前会话和当前任务。

范围：

- 新增 `session_context_states`。
- 新增 `agent_runs`。
- 新增 `agent_run_events`。
- 实现 Runtime Context 构造。
- 在 web chat 调用 LLM 前注入 Runtime Context。
- 保留现有 `ctx_size` 作为兜底策略。

### Step 2：Agent Run 基础状态机

目标：让 chat / trigger / task_executor 至少能创建和续接 run。

范围：

- chat 任务创建当前 run。
- trigger 唤醒优先查找当前未完成的 run。
- task_executor 改为创建 run 并记录状态。
- A2A `task_delegate` 写入 waiting_agent 状态。

### Step 3：Context Summarization

目标：把长 session 历史和 run events 总结成结构化 Session Summary。

范围：

- 实现 `compact_session_context`。
- 压缩消息、工具结果摘要和 run events。
- 写入 `session_context_states`。
- 必要时更新当前 run 的 `resume_instruction`。

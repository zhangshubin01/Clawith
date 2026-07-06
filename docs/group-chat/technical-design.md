# Clawith 群聊技术设计草案

本文基于线上 PRD `Clawith 群聊 v1 PRD v3`（revision 755）生成，用于承接 PRD 中标记为“放到技术文档中”的建模、上下文、执行和存储细节。

PRD 是产品规则来源；本文只定义技术实现边界。PRD 中仍保留删除线的历史内容，技术设计默认以未删除的当前正文为准。

## 1. 设计原则

1. 群聊首先是消息系统，可以触发 Agent 执行，但群消息不等同于 Agent 执行日志。
2. 表存业务对象、关系、状态和生命周期；文件存正文、产物和可由 ID 推导的内容。
3. 群是原生业务对象，使用 `groups` 和 `group_sessions` 作为主模型。
4. 人和 Agent 统一通过 `participants` 表进入群成员、消息发送者、创建者等关系。
5. 群公告、群 workspace、群 memory 都按固定路径由 `group_id` 推导，不在业务表里保存正文或 storage key。
6. 群 session 隔离消息上下文，不隔离群 workspace。
7. Agent 被 @ 后的群内回复写回当前群 session；Agent 内部过程、工具日志、trace 不写成普通群消息。

原生群聊的中心是：

```text
group_id -> group_sessions -> group_messages
```

## 2. 数据模型

### 2.1 groups

`groups` 表表示一个长期群聊对象。

| 字段 | 说明 |
|-|-|
| `id` | 群 ID，主键。 |
| `tenant_id` | 所属租户，指向 `tenants.id`。 |
| `name` | 群名称。 |
| `description` | 群介绍。 |
| `created_by_participant_id` | 创建群的参与者，指向 `participants.id`。 |
| `deleted_at` | 解散或删除时间。 |
| `created_at` | 创建时间。 |
| `updated_at` | 更新时间。 |

建表规则：

1. `groups` 只保存群元信息、租户归属、创建者和生命周期状态。
2. v1 产品入口只允许人类成员创建群，但 schema 使用 `created_by_participant_id`，为后续 Agent 主动创建群预留空间。
3. 群名不做唯一约束，同名群通过 `id` 区分。
4. 群公告正文不进表。
5. 群 workspace key 不进表。
6. 群 memory 正文不进表。

固定路径：

```text
groups/{group_id}/system/announcement.md
groups/{group_id}/workspace
groups/{group_id}/agents/{agent_id}/memory/memory.md
```

### 2.2 group_members

`group_members` 表表示“某个 participant 在某个 group 里的成员身份”。

| 字段 | 说明 |
|-|-|
| `id` | 成员关系 ID。 |
| `group_id` | 所属群。 |
| `participant_id` | 指向 `participants.id`，可以是人或 Agent。 |
| `role` | `manager` / `member`。 |
| `joined_at` | 加入时间。 |
| `removed_at` | 移出时间。 |
| `session_read_state` | JSON，记录该成员在各群 session 的已读位置。 |

规则：

1. 所有群成员关系存在同一张 `group_members` 表，用 `group_id` 区分不同群。
2. 不直接存 `user_id` 或 `agent_id`，通过 `participants.type + participants.ref_id` 找到真实用户或 Agent。
3. 保留独立的 `id` 作为成员关系 ID，不使用 `(group_id, participant_id)` 作为复合主键。
4. 建唯一约束 `unique(group_id, participant_id)`，防止同一参与者重复加入同一个群。
5. `group_members` 不保存 `status` 字段，使用 `removed_at IS NULL` 表示当前仍在群内。
6. 移出成员时不删除对应 `group_members` 记录，写入 `removed_at`。
7. 再次邀请时复用原 membership 记录，更新 `joined_at`，并清空 `removed_at`。
8. `removed_at` 只表达当前 membership 是否已被移出；完整移出历史仍以审计日志为准。
9. 邀请来源不放在成员表。谁邀请了谁、从什么入口邀请，进入审计日志。
10. `joined_at` 记录当前这次成员关系的加入时间。
11. 建群人默认是 `manager`。
12. 群内至少保留一个 `manager`。
13. v1 不提供将其他成员设为 `manager`、取消 `manager` 或转让群管理的产品入口。
14. `member` 不能移出成员。
15. “至少一个 manager”约束只适用于群继续存在时的成员变更操作，不适用于解散群。
16. 解散群只校验操作者是当前群的 `manager`；校验通过后进入群删除流程，不再检查删除后是否仍有 manager。
17. 用户账号或 Agent 自身是否可用由主体自身状态判断，不进入 `group_members`。
18. v1 不单独建立 read state 表，群 session 未读状态先保存在 `session_read_state` JSON 中。
19. 邀请候选可以来自用户、Agent 或第三方组织成员；执行入群时必须解析为有效 `participant_id`，只有已解析为 `participants` 记录的对象才能写入 `group_members`。

保留独立 `id` 的原因是：API 操作、审计记录以及后续成员级设置可以稳定引用这条 membership 关系。

`session_read_state` 示例：

```json
{
  "group_session_id": {
    "last_read_message_id": "group_message_id",
    "last_read_at": "2026-07-03T10:30:00Z"
  }
}
```

该 JSON 只用于 v1 简化实现：当前用户查看群 session 列表、进入 session 标记已读、计算自己的未读。后续如果需要复杂通知推送、全员未读统计、按 session 查询未读成员或更强并发控制，再拆成独立 read state 表。

### 2.3 group_sessions

`group_sessions` 表表示群里的一个独立消息流。它用于隔离消息上下文，但不隔离群 workspace。

| 字段 | 说明 |
|-|-|
| `id` | 群 session ID。 |
| `group_id` | 所属群。 |
| `title` | session 名称。 |
| `is_primary` | 是否为该群的 primary session。 |
| `created_by_participant_id` | 创建 session 的参与者。 |
| `deleted_at` | 软删除时间。 |
| `last_message_at` | 最近群消息时间。 |
| `created_at` | 创建时间。 |
| `updated_at` | 更新时间。 |

规则：

1. 一个群可以有多个群 session。
2. 每个群的第一个群 session 自动成为 primary session。
3. primary session 用作群级任务或其他没有明确 session 指向时的默认落点。
4. 同一个群内最多只有一个未删除的 primary session。
5. 建议增加部分唯一约束：`unique(group_id) where is_primary = true and deleted_at is null`。
6. 删除 primary session 前，需要先明确新的 primary session，或者阻止删除；具体交互后续由产品确认。
7. v1 中 `created_by_participant_id` 必须指向 `type = user` 的 participant，Agent 不允许创建群 session。
8. 删除群 session 时写入 `deleted_at`，默认 session 列表、消息查询和 Agent 上下文构造都过滤已删除 session。
9. 删除群 session 不删除群本身、群成员、群公告或群 workspace。
10. `last_message_at` 在该 session 写入新的公开群消息时更新，用于 session 列表排序和最近活跃时间展示。
11. `created_at` 记录 session 创建时间。
12. `updated_at` 在 session 元信息变化时更新，例如标题修改、primary 变更或删除标记变化。

明确不加的字段：

| 不加字段 | 原因 |
|-|-|
| `agent_id` | 群 session 不属于某个 Agent。 |
| `user_id` | 群 session 不属于某个用户。 |
| `source_channel` | 原生群 session 是 Clawith 内部对象，不混外部渠道入口。 |
| `external_conv_id` | 外部渠道 adapter 后续单独处理。 |
| `is_group` | 已经有 `groups` 表表达群。 |
| `group_name` | 群名在 `groups.name`。 |
| `participant_id` | session 本身不是参与者，只需要 `created_by_participant_id`。 |
| `peer_agent_id` | 群内 A2A 不在 `group_sessions` 中保存 Agent-to-Agent 消息链。 |
| `last_read_at_by_user` | 群未读是成员/session 维度，不放在 session 主表；v1 使用 `group_members.session_read_state`。 |
| `title_source` | v1 不记录标题来源，只保存最终 `title`。 |
| `topic_state` | topic 已从 PRD 正文移到技术设计，后续作为上下文压缩机制单独设计。 |
| `summary` | 摘要不放主表，后续作为上下文压缩机制单独设计。 |
| `workspace_key` | workspace 属于 group，路径由 `group_id` 推导。 |

### 2.4 group_messages

`group_messages` 表表示群 session 中公开可见的消息。

| 字段 | 说明 |
|-|-|
| `id` | 消息 ID。 |
| `group_id` | 所属群，冗余用于权限过滤和查询。 |
| `group_session_id` | 所属群 session。 |
| `sender_participant_id` | 发送者，可以是人或 Agent。 |
| `content` | 消息正文。 |
| `mentions` | 当前消息解析出的 @ 对象列表。 |
| `created_at` | 创建时间。 |

规则：

1. 群消息只保存用户可见的公开消息。
2. Agent 的中间工作过程不写入 `group_messages`。
3. 用户消息可以在任意位置 @ 一个或多个 Agent。
4. Agent 回复中可以 @ 群内成员；如果触发 A2A，按 4.3 的规则处理。
5. `group_id` 保留在消息表中，用于权限过滤、按群搜索和删除清理。
6. 写入消息时，`group_id` 由服务端根据 `group_session_id` 推导，不信任客户端传入的 group ID。
7. v1 不新增 `content_json`。群消息展示先使用 `content` 文本/Markdown 模式；后续需要富文本 block、文件卡片或结果卡片时再扩展结构化内容字段。
8. `mentions` 保留为解析后的 @ 对象列表，包含稳定 `participant_id`、participant 类型和展示名。
9. `mentions` 用于前端展示、人类 @ 提醒、Agent 唤醒输入和后续上下文中的身份引用。
10. Agent 唤醒由消息写入时的 @ 解析和现有 Agent 执行流程处理，不依赖 `mentions` 承担执行状态。
11. v1 不新增 `message_type`。系统状态、文件说明和 Agent 最终回复先使用 `content` 文本模式。
12. v1 不在消息表保存 `source_run_id`。Agent 最终回复就是普通群消息；如果后续需要群级执行追踪，再单独设计执行表。

### 2.5 v1 不新增群执行表

v1 不新增 `group_agent_runs` 或 `group_agent_run_events`。

规则：

1. Agent 被 @ 后，执行过程由现有 Agent runtime / job / 日志体系承载，不进入群聊 v1 业务表。
2. 群聊数据结构只保存用户可见的公开消息和群相关元数据。
3. Agent 执行完成后，只有它决定公开发送到群里的最终回复写入 `group_messages`。
4. 有用的中间产物通过群 workspace 文件持久化，不通过群消息表或群执行表保存。
5. 群聊 v1 不为每次 Agent 回复新增独立 run 表。
6. 现有触发器队列、工具调用日志、Agent activity log、audit log 继续按原有表处理；群聊 v1 不新增 group-scoped log 表。
7. v1 不建群执行表。如果后续需要群级排队、取消、重试、执行状态详情或用户可见执行历史，只新增一张群执行表，不拆分 run event 附表。

### 2.6 v1 不新增群审计表

v1 不新增 `group_audit_logs` 或其他 group-scoped 审计表。

规则：

1. 群创建、群信息修改、成员邀请、成员移除、session 创建、session 删除、解散群等操作，复用现有 `audit_logs`。
2. `audit_logs.details` 中记录 `group_id`、`group_session_id`、`group_member_id`、目标 `participant_id` 等群上下文。
3. 群成员关系、群 session、群消息等业务当前状态仍写入对应业务表。
4. 操作来源、操作者、变更原因和审计上下文不进入 `groups` / `group_members` / `group_sessions` / `group_messages` 主表。
5. 如果后续有独立群审计查询、合规留存或权限隔离要求，再单独设计群审计表。

## 3. 文件和固定路径

### 3.1 群公告

群公告是固定路径文件：

```text
groups/{group_id}/system/announcement.md
```

规则：

1. `groups` 表不保存 `announcement_md`。
2. Agent 在群内被 @ 时，context builder 按固定路径读取群公告并注入。
3. 群公告全文可以很长，但注入上下文时有长度上限。
4. 超出注入上限的部分不自动进入本轮上下文。
5. Agent 需要更多公告内容时，通过群公告读取工具按需读取。

### 3.2 群 workspace

群 workspace 是固定路径目录：

```text
groups/{group_id}/workspace
```

规则：

1. `groups` 表不保存 `workspace_key`。
2. 群 workspace 属于 group，不属于 group session。
3. 同一个群下的多个 session 共享同一个群 workspace。
4. 群 workspace 作为新增的 group scope 复用现有 workspace revision / lock 能力，不迁移现有 Agent workspace 数据。

workspace scope 规则：

| 字段 | 说明 |
|-|-|
| `scope_type` | `agent` / `group`。 |
| `scope_id` | `agent_id` 或 `group_id`。 |
| `path` | scope 内的文件路径。 |
| `session_id` | 触发文件变更的 `group_session_id`。 |

新增 group scope 后，`workspace_file_revisions` 和 `workspace_edit_locks` 支持两类 scope：

1. 现有 Agent workspace 继续使用 agent scope，历史数据、路径、API 和锁语义保持不变。
2. 新增群 workspace 使用 group scope，按 `scope_type = group`、`scope_id = group_id`、`path` 记录版本和编辑锁。
3. 新增 group scope 不要求迁移现有 Agent workspace 数据。

### 3.3 群 memory

群 memory 是 Agent 针对某个群维护的固定路径文件：

```text
groups/{group_id}/agents/{agent_id}/memory/memory.md
```

规则：

1. v1 不建 `group_agent_memories` 正文表。
2. 群 memory 正文以文件为唯一真实来源。
3. 产品语义上，群 memory 属于群 workspace；技术实现上，它是 group scope 下的系统文件，不作为普通 workspace 文件直接管理。
4. Agent 在群内被 @ 时，加载该 Agent 对应这个群的 memory。
5. Agent 在非当前群上下文中被唤醒时，不加载当前群 memory。
6. Agent 在其他群中被唤醒时，不加载当前群 memory。
7. Agent 可以读取群内其他 Agent 针对该群的 memory。
8. Agent 只能写自己的群 memory，不能修改其他 Agent 的群 memory。
9. 人类用户可以读、写、删除群内所有 Agent 的群 memory。
10. Agent 自身 memory 不会自动从群 memory 中学习内容。

### 3.4 文件 API 边界

群公告、群 memory、session summary 和群 workspace 都文件化，但对前端暴露的 API 需要保持 group 语义边界。

规则：

1. 前端和业务层不直接把 `groups/{group_id}/...` 物理路径当作业务 contract。
2. 群公告、群 memory、session summary 走 group-scoped wrapper API，由后端映射到固定路径。
3. 普通群 workspace 文件走 group workspace 文件 API，底层复用现有 storage、revision、lock 能力。
4. group API 负责校验群成员、群管理、Agent memory 读写权限等群语义。
5. 底层 file/storage API 只负责文件读写、版本、锁、下载，不承载群成员权限。
6. 固定系统文件不通过普通 workspace path 随意写入，避免绕过业务权限和注入规则。

API 形态：

```text
GET /groups/{group_id}/announcement
PUT /groups/{group_id}/announcement

GET /groups/{group_id}/agents/{agent_id}/memory
PUT /groups/{group_id}/agents/{agent_id}/memory
DELETE /groups/{group_id}/agents/{agent_id}/memory

GET /groups/{group_id}/sessions/{session_id}/summary

GET    /groups/{group_id}/workspace?path=...
GET    /groups/{group_id}/workspace/file?path=...
PUT    /groups/{group_id}/workspace/file?path=...
DELETE /groups/{group_id}/workspace/file?path=...
```

## 4. 唤醒、任务规划和 A2A

### 4.1 @ 唤醒

发送群消息的处理流程：

1. 保存用户群消息。
2. 解析当前消息中的 @。
3. 过滤出当前群内 `removed_at IS NULL` 且 Agent 自身可用的 Agent 成员。
4. 如果只 @ 一个 Agent，直接触发该 Agent 执行。
5. 如果同时 @ 多个 Agent，先进入任务规划阶段。
6. 被唤醒 Agent 执行完成后，最终回复写回当前群 session。

历史消息中的 @ 只是上下文文本，不会重新触发 Agent。

写入顺序：

1. 校验 `group_id` 和 `group_session_id` 匹配，且群和 session 未删除。
2. 校验发送者是当前群成员，且 `removed_at IS NULL`。
3. 服务端解析消息中的 mention token，生成 `mentions`。
4. 写入 `group_messages`，并更新 `group_sessions.last_message_at`。
5. 更新除发送者外的人类成员未读状态。
6. 对解析出的 Agent mention 执行唤醒流程。

@ 解析规则：

1. 只以结构化 mention token 为准，不靠纯文本名字猜测成员。
2. mention 必须解析到当前群成员；不在群内、已移出、账号不可用的对象不触发。
3. @ 人类成员只用于展示提醒和未读中的 @ 标记，不触发 Agent。
4. @ Agent 才进入 Agent 唤醒。
5. 同一条消息重复 @ 同一个 Agent，只触发一次。
6. Agent 回复中的 @ 按同一套规则处理。
7. 被唤醒状态属于本次请求/实时事件，不写入 `group_messages`，也不新增状态表。

非法 mention 处理：

1. 已移出成员：不触发，前端可展示 mention 失效。
2. 不存在成员：不触发，保留原文。
3. 不可用 Agent：不触发，可返回轻量错误状态给当前发送者。
4. 混合合法和非法 mention 时，合法对象继续处理。

### 4.2 多 Agent 任务规划

同一条消息同时 @ 多个 Agent 时，系统进入任务规划阶段。

任务规划 Agent：

1. 是系统内置 Agent。
2. 不作为普通群成员展示。
3. 默认不在群里发言。
4. 只生成轻量分工计划，不替代业务 Agent 工作。

轻量分工计划包含：

1. 本次协作目标。
2. 被 @ 的 Agent 列表。
3. 每个被 @ Agent 的建议分工。

如果用户已经明确给出分工、顺序或协作方式，以用户规划为准。

执行规则：

1. 任务规划 Agent 的输出是内部结构，不作为群消息写入。
2. 规划结果可以作为本次消息发送后的状态返回给前端展示，但不进入 `group_messages`。
3. 未写入 `group_messages` 的规划结果不作为公开群消息进入后续群上下文。
4. 规划结果只影响被 @ Agent 的初始上下文，不创建业务表。
5. 被 @ Agent 默认并发执行。
6. 每个 Agent 独立构造自己的群上下文包。
7. Agent 最终公开回复分别写回当前 `group_session_id`。
8. 如果某个 Agent 执行失败，只影响该 Agent 的回复，不阻塞其他 Agent。
9. 失败是否写一条公开群消息由产品交互决定；v1 可以先只向触发者返回失败状态。

同一个 Agent 同时被多条消息 @ 时，按群消息写入顺序串行执行：

1. 顺序以服务端成功写入 `group_messages` 的顺序为准，建议使用 `created_at, id` 作为稳定排序。
2. 如果该 Agent 当前没有正在处理的消息，系统立即调用该 Agent。
3. 如果该 Agent 正在处理上一条 @，新的 @ 等待上一条处理完成后再执行。
4. v1 不并发执行同一个 Agent 的多个 @ 请求。

### 4.3 群内 A2A

根据线上 PRD v3，群内 A2A 的发起方式和普通 A2A 相同，消息和结论都不放到群内过程中。

技术含义：

1. 群内 Agent 如果需要另一个 Agent 协助，可以复用现有普通 A2A 能力。
2. 该 A2A 的过程消息不写入 `group_messages`。
3. 该 A2A 的结论默认不写入当前群 session。
4. 未公开的 A2A 结论默认不更新 session summary、群 memory 或群 workspace。
5. 群消息流只保留发起 Agent 最终决定公开发送的群回复。
6. 如果发起 Agent 判断 A2A 结果对群内协作有价值，可以在自己的最终群回复中引用、总结或转述；只有这部分公开表达出来的内容才作为普通群消息进入当前 `group_session_id`。
7. 如果发起 Agent 判断 A2A 结果没有价值，可以完全不在群里提及。

因此，原生群聊不需要把群内 A2A 持久化成 `group_sessions` 内的 Agent-to-Agent 消息链。

### 4.4 任务触发和回调落点

人类成员在群 session 中为 Agent 创建触发器、回调任务或其他异步任务时，需要把群来源写入任务配置或 job metadata。

来源 metadata：

| 字段 | 说明 |
|-|-|
| `_origin_source` | 固定为 `group`。 |
| `_origin_group_id` | 任务创建时所在群。 |
| `_origin_group_session_id` | 任务创建时所在群 session。 |
| `_origin_message_id` | 触发任务创建的群消息，可为空。 |
| `_origin_sender_participant_id` | 创建任务的人类成员。 |

回写规则：

1. 如果存在 `_origin_group_session_id`，任务触发或完成后的公开群消息优先写回该群 session。
2. 回写前校验群、群 session、触发者和目标 Agent 仍然有效，且目标 Agent 仍在群内。
3. 如果原群 session 已删除，不静默改写到其他 session；本次回写失败或只向触发者返回失败状态。
4. 如果只有 `_origin_group_id`，没有明确 session 指向，则写回该群当前未删除的 primary session。
5. 如果群没有可用 primary session，不自动创建新 session；本次回写失败或只向触发者返回失败状态。
6. 任务执行过程、工具日志和 trace 不写入 `group_messages`。
7. 只有任务触发或完成后需要公开给群内成员看的最终消息，才写入 `group_messages`。
8. 回写消息中如果包含结构化 mention token，按 4.1 的 @ 解析规则继续处理。

## 5. 群上下文构造

Agent 被 @ 后，不直接加载完整群聊记录。系统通过 group context builder 组装本轮上下文。

### 5.1 拼装入口

context builder 输入：

| 输入 | 说明 |
|-|-|
| `group_id` | 当前群。 |
| `group_session_id` | 当前群 session。 |
| `trigger_message_id` | 当前触发消息。 |
| `sender_participant_id` | 当前发言人。 |
| `target_agent_participant_id` | 本次被唤醒的 Agent。 |
| `mention_targets` | 当前消息解析出的 @ 对象。 |
| `planning_hint` | 多 Agent 任务规划结果，可为空。 |

拼装前置校验：

1. 群未删除。
2. 群 session 未删除。
3. 当前发言人仍是群成员。
4. 被唤醒 Agent 仍是群成员。
5. 被唤醒 Agent 自身状态可用。

### 5.2 拼装顺序

最终输入按以下顺序拼装：

1. 群聊执行规则：只能基于当前群可见内容回答，不能假设看到其他群或未共享内容。
2. 当前任务：触发消息全文、发送者、本轮被 @ 对象、本轮实际唤醒 Agent。
3. 当前 Agent 在群内的身份：Agent ID、名称、角色描述、群内可用权限。
4. 当前群基础信息：群 ID、群名称、群介绍。
5. 当前 session 基础信息：session ID、session 名称、是否 primary。
6. 群公告：按注入上限截断。
7. session 历史摘要：读取 `groups/{group_id}/sessions/{group_session_id}/summary.md`。
8. 最近原始消息窗口：当前 session 最近 20 条公开群消息。
9. 当前 Agent 的群 memory：读取 `groups/{group_id}/agents/{agent_id}/memory/memory.md`。
10. 群 workspace 相关内容：文件索引、被显式引用文件、相关文件摘要或片段。
11. 多 Agent 任务规划提示：仅当本轮有规划结果时注入。
12. 可用工具和约束：群成员查询、群公告读取、群 workspace 文件读写等能力说明。

本轮群上下文包括：

1. 当前触发消息全文。
2. 本轮 @ 关系，包括发送者、被 @ 对象、本轮实际唤醒的 Agent。
3. 当前群基础信息：群 ID、群名称、群介绍。
4. 当前 session 基础信息：session ID、session 名称。
5. 当前发言人基本信息：成员 ID、姓名、职位、部门。
6. 被唤醒 Agent 基本信息：Agent ID、名称、角色描述。
7. 当前 session 最近 20 条消息。
8. 当前 session 的压缩摘要。
9. 群公告在注入上限内的内容。
10. 当前 Agent 在该群的群 memory。
11. 当前 Agent 对历史摘要的工作视角或已承担事项。
12. 群 workspace 中与本轮相关的文件索引、文件摘要或必要片段。

不默认注入：

1. 同群其他 session 的原始消息。
2. 其他群消息。
3. 未明确分享到群的 Agent workspace 文件。
4. 完整工具日志。
5. 完整 A2A 会话。
6. 全量群成员列表。

群成员信息通过工具按需查询。工具只返回当前群内成员，并支持按姓名、角色或能力检索。

### 5.3 token 预算和截断

v1 使用固定优先级预算，不做复杂优化。

优先级：

1. 当前触发消息和 @ 关系必须保留。
2. 群/session 基础信息必须保留。
3. 被唤醒 Agent 基本信息必须保留。
4. 群公告按上限截断。
5. session 摘要按上限截断。
6. 最近消息从新到旧取，直到达到最近消息预算。
7. 群 memory 按上限截断。
8. workspace 文件只注入索引和命中的短片段。

截断规则：

1. 截断时保留内容来源说明，例如“群公告已截断”。
2. 被截断内容不自动继续读取。
3. Agent 需要更多内容时，必须调用对应读取工具。
4. 不因为 token 不足而删除当前触发消息。

### 5.4 最近消息窗口

最近消息窗口规则：

1. 只取当前 `group_session_id` 下的公开群消息。
2. 默认取最近 20 条。
3. 不取已删除 session 的消息。
4. 不取 Agent 中间执行过程、工具日志或 trace。
5. 消息按创建时间升序放入模型上下文。
6. 每条消息带发送者展示名、participant 类型、创建时间和正文。
7. mention 以可读形式保留，同时保留稳定 participant ID 供工具使用。

### 5.5 群公告注入

群公告规则：

1. 每次 Agent 被 @ 时读取最新群公告。
2. 群公告修改后，不影响已经开始的 Agent 执行。
3. 群公告进入上下文时有长度上限。
4. Agent 不能编辑群公告。
5. Agent 如果需要完整公告，通过群公告读取工具按需读取。

### 5.6 群 memory 注入和更新

群 memory 注入规则：

1. 只自动注入当前被唤醒 Agent 在当前群的 memory。
2. 不自动注入其他 Agent 的群 memory。
3. Agent 可以通过工具读取同群其他 Agent 的群 memory。
4. Agent 只能写自己的群 memory。
5. 人类成员可以读写删除所有群 memory。

群 memory 更新规则：

1. Agent 不因为普通群消息或公告变化自动更新群 memory。
2. 只有 Agent 被 @ 并完成处理后，才可以判断是否需要更新自己的群 memory。
3. v1 中 memory 更新可以作为执行后的异步动作，不阻塞群消息回复。
4. 群 memory 写入必须走 group-scoped wrapper API，不能绕过权限直接写路径。

### 5.7 群 workspace 注入

workspace 注入规则：

1. 默认不注入完整文件内容。
2. 默认注入群 workspace 的轻量文件索引：路径、文件名、类型、更新时间、摘要。
3. 当前消息显式引用文件时，优先注入该文件摘要或短片段。
4. session 摘要中引用的文件可以作为候选注入。
5. Agent 需要完整文件时，通过群 workspace 文件读取工具按需读取。
6. Agent 产出的可复用文件写入群 workspace 后，应在最终群回复中说明文件路径或用途。

### 5.8 成员查询工具

群成员不默认全量注入。Agent 需要找人、确认身份或在回复中 @ 成员时，调用群成员查询工具。

工具返回：

1. `participant_id`。
2. 类型：人类成员或 Agent。
3. 展示名。
4. 角色：`manager` / `member`。
5. 对 Agent 返回 Agent 名称和角色描述。
6. 对人类成员返回可展示的部门、职位等基础信息。

工具限制：

1. 只返回当前群内 `removed_at IS NULL` 的成员。
2. 不返回其他群成员。
3. 不返回已删除群成员列表。
4. 不允许 Agent 凭历史文本中的名字构造 mention。

## 6. session 历史摘要和压缩

群 session 摘要作为可重建的上下文产物持久化为文件，v1 不新增 summary 表。

摘要文件路径：

```text
groups/{group_id}/sessions/{group_session_id}/summary.md
```

摘要文件 metadata：

```yaml
schema_version: 1
scope_type: group
session_id: uuid
compressed_through_message_id: uuid
compressed_through_created_at: timestamp
updated_at: timestamp
```

上下文由历史摘要、待压缩区和最近消息组成。

1. 最近 20 条消息保留原文进入上下文。
2. 滑出最近 20 条的旧消息进入待压缩区。
3. 待压缩区达到系统设定 token 阈值后触发压缩。
4. 压缩后清空待压缩区，并更新历史摘要。
5. session 很短时可以不触发压缩。
6. session 很长时按批次压缩，不逐条压缩。
7. 摘要文件丢失或损坏时，可以从原始消息表重新生成。
8. `group_sessions` 不保存 `summary` 或 `topic_state` 字段。
9. v1 不新增 `session_summaries`、`group_session_summaries` 或其他摘要正文表。

topic 已从 PRD 正文移到技术文档。v1 可以先把 topic 作为摘要内部字段处理，不在 `group_sessions` 主表里放 `topic_state`。

topic 规则：

1. topic 是当前群 session 摘要中的内部状态，不是独立业务对象。
2. topic 不作为权限边界，不隔离群 workspace，也不强制切分群 session。
3. v1 不提供复杂的 topic 切换、合并、拆分或用户侧管理入口。
4. topic 由上下文压缩流程维护，群内 Agent 只消费 topic 结果，不负责直接维护 topic 状态。
5. topic 状态可以包含当前目标、当前阶段、是否阶段结束、相关文件、相关成员或 Agent。
6. topic 更新跟随摘要压缩批次发生，不要求每条消息都实时更新。
7. 如果一个 session 中自然混入多个 topic，摘要中只保留对当前任务有帮助的 topic 状态和已结束 topic 的关键结论。
8. 当系统判断 topic 已完成或进入阶段结束状态时，可以生成可复用沉淀内容，但不会自动把完整历史或未公开的内部过程写入群 workspace。

建议摘要结构包含：

1. 当前目标。
2. 当前 topic 状态。
3. 已确认决策。
4. 未决问题。
5. 当前状态。
6. 相关文件。
7. 相关成员或 Agent。
8. 已过期或已废弃的结论。

后续如果需要跨 session 搜索摘要、按摘要状态排序筛选、用户可见压缩进度、强并发更新控制或更复杂的检索索引，再单独设计轻量索引表；摘要正文仍优先保留文件化。

压缩触发规则：

1. 写入新的公开群消息后，检查该 session 是否达到压缩阈值。
2. 阈值可以按消息数和估算 token 双条件配置。
3. 压缩任务异步执行，不阻塞用户发送消息和 Agent 回复。
4. 每次只压缩 `compressed_through_message_id` 之后、最近窗口之前的消息。
5. 最近 20 条消息永远不进入已压缩区。
6. 压缩完成后更新 summary 文件 metadata。
7. 压缩失败不影响群消息读写；失败进入后台日志，后续可重试。

摘要生成要求：

1. 摘要必须区分事实、决策、待办、未决问题和废弃结论。
2. 摘要中引用文件时保留群 workspace 路径。
3. 摘要中引用成员或 Agent 时保留 participant ID。
4. 摘要不得引入当前 session 之外的消息。
5. 摘要不得把 Agent 中间工具日志当作群共识。
6. 有冲突的信息必须标记为冲突或未决，不能直接合并成确定结论。

## 7. 群 session 生命周期逻辑

### 7.1 创建 session

创建规则：

1. 只有当前群内 `removed_at IS NULL` 的人类成员可以创建群 session。
2. Agent v1 不能创建群 session。
3. 创建群时不自动创建 session。
4. 当前群第一个 session 自动成为 primary session。
5. 后续创建的 session 默认 `is_primary = false`。
6. session 标题可以由用户提供；如果未提供，使用临时标题。
7. 第一条群消息写入后，如果仍是临时标题，可以用第一条消息生成标题。

### 7.2 删除 session

删除规则：

1. 只有群 `manager` 可以删除群 session。
2. 删除 session 写入 `group_sessions.deleted_at`。
3. 删除后默认 session 列表、消息查询、上下文构造都过滤该 session。
4. 删除 session 不删除群、群成员、群公告、群 workspace 或群 memory。
5. 删除 session 后不提供用户侧恢复。
6. v1 阻止删除 primary session，除非同一个请求指定新的 replacement primary session。
7. replacement 必须是同群、未删除 session。
8. primary 变更和删除操作需要在同一事务内完成。

## 8. 未读和提醒逻辑

未读状态按“人类成员 + 群 session”维度计算，v1 存在 `group_members.session_read_state`。

写入消息时：

1. 发送者自己的未读不增加。
2. 其他当前人类成员对该 session 产生未读。
3. Agent 最终公开回复计入人类成员未读。
4. 任务触发或回调产生的公开群消息计入未读。
5. Agent 中间过程、workspace 文件变化、群公告变化不直接计入未读。
6. 被 @ 的人类成员可以额外展示 @ 提醒。

标记已读时：

1. 用户进入群 session 并看到最新消息后，更新自己的 `session_read_state`。
2. 更新内容包含 `last_read_message_id` 和 `last_read_at`。
3. 删除 session 后，该 session 未读不再展示。
4. 删除群后，该群所有未读不再展示。

未读查询时：

1. 只统计未删除群和未删除 session。
2. 只对当前群成员展示。
3. 已移出成员不再看到群未读。
4. v1 不做全员未读统计。

## 9. 权限和删除

### 9.1 权限

| 操作 | v1 权限 |
|-|-|
| 查看群 | 当前群内 `removed_at IS NULL` 的成员。 |
| 发送群消息 | 当前群内 `removed_at IS NULL` 的成员。 |
| 创建群 session | 当前群内 `removed_at IS NULL` 的人类成员。 |
| 修改群名称、介绍、公告 | 人类群成员。 |
| 邀请成员 | 人类群成员，候选范围仍受现有可见性限制。 |
| 移出成员 | 群管理。 |
| 解散群 | 群管理。 |
| 读取群 workspace | 当前群内 `removed_at IS NULL` 的成员和 Agent。 |
| 写入群 workspace | 当前群内 `removed_at IS NULL` 的成员和 Agent。 |
| 读写群 memory | 按 3.3 的群 memory 规则。 |

### 9.2 删除

PRD v3 中写明：群管理可以解散群，解散后群内 session、群 workspace 文件等都会被硬删除。

技术设计先按以下方式落地：

1. 删除入口先校验操作者是当前群 `manager`。
2. 校验通过后写入 `groups.deleted_at`，用户侧群列表、群详情、群 session、群消息、群 workspace、群 memory 立即不可见。
3. 群删除的硬删除对象是用户可见内容、消息正文和文件正文；成员关系与审计日志只保留最小元数据，用于排障、审计或合规，不提供用户侧恢复能力。
4. `group_members` 记录保留，用于审计当时的成员关系；默认业务查询通过 `groups.deleted_at IS NULL` 过滤。
5. `group_sessions` 可以写入 `deleted_at`，也可以通过所属 `groups.deleted_at` 统一不可见；默认查询必须过滤已删除群。
6. `group_messages` 后台异步硬删。
7. 群公告文件、群 workspace 文件、群 memory 文件、session summary 文件后台异步硬删。
8. 清理任务必须幂等：重复执行不会报错，部分文件已不存在也视为可继续。
9. 清理失败不影响用户侧“群已删除”状态，但需要进入后台日志或告警，后续可重试。
10. `audit_logs` 保留必要操作记录，用于排障、审计或合规。
11. `audit_logs` 不保存完整群消息正文或完整文件内容，也不提供用户侧恢复能力。
12. v1 不新增 `group_deletion_jobs`。如果后续需要用户可见删除进度、失败重试列表或合规删除证明，再单独设计删除任务表。
13. 删除群不是成员移除操作，不触发“至少保留一个 manager”的校验冲突。

## 10. 第三方渠道边界

第三方群暂时不映射为 Clawith 原生群。

1. Clawith 原生群消息暂时不会自动同步到第三方会话或第三方群聊。
2. 如果 Agent 需要联系未绑定平台账号的第三方同步成员，显式调用渠道发送能力，例如 `send_channel_message`，目标是具体成员。
3. 外部渠道 adapter 后续如要接入原生 group model，应单独设计映射层，不扩展现有外部渠道会话模型承载原生群。

## 11. API 草案

```text
POST   /groups
GET    /groups
GET    /groups/{group_id}
PATCH  /groups/{group_id}
DELETE /groups/{group_id}

GET    /groups/{group_id}/members
POST   /groups/{group_id}/members
PATCH  /groups/{group_id}/members/{member_id}
DELETE /groups/{group_id}/members/{member_id}

GET    /groups/{group_id}/sessions
POST   /groups/{group_id}/sessions
PATCH  /groups/{group_id}/sessions/{session_id}
DELETE /groups/{group_id}/sessions/{session_id}

GET    /groups/{group_id}/sessions/{session_id}/messages
POST   /groups/{group_id}/sessions/{session_id}/messages

GET    /groups/{group_id}/announcement
PUT    /groups/{group_id}/announcement

GET    /groups/{group_id}/agents/{agent_id}/memory
PUT    /groups/{group_id}/agents/{agent_id}/memory
DELETE /groups/{group_id}/agents/{agent_id}/memory

GET    /groups/{group_id}/sessions/{session_id}/summary

GET    /groups/{group_id}/workspace?path=...
GET    /groups/{group_id}/workspace/file?path=...
PUT    /groups/{group_id}/workspace/file?path=...
DELETE /groups/{group_id}/workspace/file?path=...
```

`POST /groups/{group_id}/sessions/{session_id}/messages` 是核心入口：保存群消息、解析 @、触发任务规划或 Agent 执行，并返回可见消息和必要状态。

## 12. Phase 1 范围

Phase 1 最小闭环：

1. 新增 `groups`、`group_members`、`group_sessions`、`group_messages`。
2. 当前消息 @ 解析和 Agent 成员过滤。
3. 多 Agent @ 的轻量任务规划。
4. Agent 最终公开回复写回群 session。
5. 群公告固定路径读取和注入。
6. 群 workspace 固定路径读写。
7. 群 memory 固定路径初始化、读取和写入。
8. group context builder：当前消息、@ 关系、群/session 信息、公告、摘要、最近 20 条消息、群 memory、workspace 索引。
9. 文件化 session 摘要和异步压缩触发。
10. 群 session 创建、删除、primary 兜底逻辑。
11. 群解散后的用户侧不可见和后台清理。
12. 基于 `group_members.session_read_state` 的 v1 未读状态。

Phase 1 明确不做：

1. 群名唯一约束。
2. 群公告、群 workspace key、群 memory 正文入表。
3. 群内 A2A 过程写入群消息。
4. topic 独立表或 `group_sessions.topic_state`。
5. 摘要独立表。
6. 外部渠道群和原生群双向绑定。
7. Agent 主动邀请新成员。
8. Agent 主动创建群 session。
9. 群级 Agent run 表和 run event 表。
10. 群级独立审计表或独立 log 表。

## 13. 待继续讨论

当前数据结构问题已收敛，暂无待继续讨论项。

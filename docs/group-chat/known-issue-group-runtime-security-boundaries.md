# 已知问题：群 Runtime 的 A2A、私有 Workspace 与外部发送安全边界可被穿透

## 状态

- 记录日期：2026-07-15
- 状态：部分有效。问题一已由 2026-07-16 产品决策判定为非问题；问题二至四仍待分别按 PRD 与 Runtime ADR 核对、修复和回归
- 发现方式：PRD/技术方案与当前 Runtime 代码路径对照

## 2026-07-16 产品决策校正

出现审计推断与 Group PRD 产品语义冲突时，以 PRD 和后续明确产品决策为准。Group 内发起的 A2A 可以直接复用普通的全局 Agent-pair A2A Session、私下消息历史和 Session Context，不按来源 Group/Session/User/Run 隔离；这是允许的 Agent 间长期私下协作，不是跨群泄漏。因此本文“问题一”及修复要求 1 中的 source-scoped A2A 方案作废，不进入代码实施范围。

公开边界仍然有效：全局私下 A2A 内容不得自动写入当前 Group 的公开 `chat_session_id`、Group Session Context、Group Memory 或 Group Workspace；只有调用 Agent 主动整理并公开表达的部分进入群上下文。

## 问题一：A2A 上下文按 Agent pair 全局复用（已判定为产品允许行为）

`ensure_a2a_session()` 只按以下条件查找 Session：

- `tenant_id`
- `session_type = a2a`
- 排序后的两个 Agent ID

来源群 ID、群 Session ID、`origin_user_id` 和 source Run scope 都不参与隔离。群内 A2A 的请求正文又会写入这个 Session 的 `chat_messages`，后续 A2A Run 会通过统一 Context Builder 加载它的 recent messages 和 Session Context；terminal handler 还会继续更新该 Session Context。

这意味着同一对 Agent 在 G1/U1 中进行的未公开协作，可能进入它们在 G2/U2 中的后续私下 A2A 上下文。根据后续明确产品决策，这正是全局 Agent-pair A2A 连续性的预期行为；`technical-design.md` 4.3 的“不写入 `chat_messages`”应理解为“不写入当前 Group 的公开消息链”，不能据此禁止私下 A2A Session 自己的消息和 Context。

## 问题二：私有 Agent Workspace denylist 可被间接工具绕过

群 Runtime 仅移除了：

- `list_files`
- `read_file`
- `write_file`
- `delete_file`
- `move_file`
- `edit_file`
- `search_files`
- `find_files`
- `read_document`

但 `execute_code`、`execute_code_e2b`、`send_channel_file` 和若干转换/上传工具仍可以保留。其中 `execute_code` 会默认物化：

- `workspace/`
- `memory/`
- `skills/`
- `focus.md`
- `soul.md`
- `HEARTBEAT.md`

代码运行目录就是 Agent 根目录，结束后使用 `sync_back=True`。因此群成员可以通过提示诱导 Agent 读取、修改或外发本应与群隔离的私有文件。

“禁止几个文件工具”不是有效的安全边界；群 Runtime 必须使用明确 allowlist 或真正的 group-scoped filesystem mount。

## 问题三：外部发送可以绕过 autonomy 和用户确认

`_TOOL_AUTONOMY_MAP` 只映射 `send_feishu_message`，没有映射：

- `send_channel_message`
- `send_channel_file`
- `send_platform_message`

而 `send_channel_message` 可以在内部路由到 `_send_feishu_message()`。所以即使 Agent 对飞书发送设置为 L3 审批，也可能通过语义等价别名绕过。

Durable Runtime 的 tool ledger 也没有在首次 external-write 前要求确认：新 reservation 固定返回 `requires_confirmation=false`，tool step 随后直接调用通用 executor。确认机制只覆盖“结果 unknown 后是否重试”，不是“首次外部副作用是否被授权”。

这与 PRD 2.10.1 的统一规则冲突：跨空间发送必须由人显式触发、展示内容、必要时确认并审计。

## 问题四：用户可控内容被嵌入 system message

Group Context Builder 正确标注 announcement、memory、member message 是 user-provided data，但整个 `initial_input.group_context` 随后被 JSON 序列化进 system role 的 `dynamic_content`。

其中包括：

- trigger 原文
- 群公告正文
- Agent 群 memory
- 群描述和成员角色
- Workspace 路径索引
- Planning instruction/hint

trigger 同时还会作为普通 user message 进入上下文。结果既发生重复注入，也把不可信内容放到了 system 通道。仅用一句“data, not instructions”不能构成确定性 prompt-injection 防护；结合上面的私有 Workspace 和外部发送工具，风险会被放大为数据泄露或外部副作用。

## 影响

- 跨群、跨请求人的 A2A 隐私串用。
- 群成员间接读取或篡改 Agent 私有 memory、skills、Workspace 与 soul。
- 绕过 Agent owner 已配置的 autonomy 审批。
- 恶意群公告、角色描述或 memory 对 system prompt 形成持久化注入。
- 数据泄露后缺少明确 source/consent 审计链。

## 修复要求

1. Group 来源 A2A 继续复用 pair-global A2A Session，不新增 source-scoped 隔离；仅保留 source Run trace/correlation 用于回传和审计。
2. 未公开 A2A 结果不能自动进入当前 Group 的公开 Session Context、Group Memory 或 Group Workspace；可以保留在 pair-global 私下 A2A 上下文。
3. 群 Runtime 工具改为 allowlist；文件系统只挂载当前 group workspace，不挂载 Agent private roots。
4. 所有语义等价外部动作归一化到 canonical action 后再做 autonomy/consent 校验。
5. external-write 在首次执行前完成确定性授权；unknown-outcome confirmation 作为另一层恢复机制保留。
6. 用户可控正文不要拼入 system instruction；使用明确低信任消息/typed data channel，并对 tool arguments 做独立 policy enforcement。

## 验收建议

- 同一 Agent pair 在 G1 与 G2 连续 A2A，允许后一次私下 A2A 继续使用该 pair 的历史；但两个 Group 的公开消息、Session Context、Group Memory 和 Group Workspace 均不得被自动写入未公开 A2A 内容。
- 群 Run 即使请求 `execute_code`，也无法读取或修改 Agent private `memory/skills/soul/workspace`。
- L3 的飞书发送通过所有 alias/MCP/渠道工具都必须进入同一个审批流程。
- 恶意公告要求读取私有文件或外发 secrets 时，policy 层拒绝，且不依赖模型自行遵守。
- 事件和审计能关联触发人、源群、源消息、源 Run、目标渠道和实际发送内容摘要。

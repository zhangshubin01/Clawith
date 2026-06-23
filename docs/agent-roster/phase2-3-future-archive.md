# Agent 通讯录 Phase 2/3 后续规划存档

本文存档 Phase 2 / Phase 3 的后续方向和 Phase 2 的最小实施方案。

Phase 1 的目标是先完成数字员工发现与 A2A 调用链路工具化：

- Phase 1.1：权限与可见性判断拆分
- Phase 1.2：`query_roster`
- Phase 1.3：A2A 发送链路 ID 化
- Phase 1.4：prompt 去数字员工 Relationships 依赖

Phase 2 在 Phase 1 稳定后启动。Phase 3 仍作为后续产品化和清理阶段。

## Phase 2 - Human 发送链路 ID 化

Phase 2 目标是把人类联系人也从“按名字 + Relationships”切到“`query_roster` 返回稳定 ID + 发送工具硬校验”。

### 一句话目标

让数字员工联系人类时，主链路从：

```text
prompt 中的人类 Relationships 背景 -> member_name -> send_* tool
```

切换为：

```text
query_roster(member_type="human", query="...") -> stable ID -> send_* tool
```

其中 stable ID 包括：

- `target_member_id`：组织成员 ID，对应 `OrgMember.id`。
- `platform_user_id`：平台用户 ID，对应 `OrgMember.user_id`。
- provider identity：第三方身份，例如飞书的 `external_id` / `open_id`。

### 最小实施范围

- `send_platform_message` 优先支持 `platform_user_id`。
- `send_feishu_message` 不再依赖人名匹配，改成稳定成员身份。
- `send_channel_message` 不再按名字匹配，改成 `target_member_id + channel/provider_type` 或等价稳定参数。
- human 发送前复用 Phase 1.1 的 human roster visibility 判断。
- 发送时再次硬校验：
  - `OrgMember.status`
  - provider 身份 ID
  - 渠道配置
  - 当前 Agent 工具可用性

Phase 2 不做完整 UI，不删除旧关系表，不迁移 OKR 等仍依赖旧关系表的业务逻辑。

### query_roster human 增强

- human 结果里的 `contact_tools` 与实际发送工具参数对齐。
- 支持按 `target_member_id` 精确查单个人类成员。
- 可选增加 `department_id` 过滤。
- 重名时通过职位、部门、平台 ID、provider identity 稳定区分。

V1 先只做 `target_member_id` 精确查询，不做 `department.path`，不做 `unionid`，不返回 `total`。

建议工具参数：

```json
{
  "query": "张三",
  "member_type": "human",
  "target_member_id": "org_member_uuid",
  "include_uncontactable": false,
  "limit": 20,
  "offset": 0
}
```

其中：

- `query` 用于模糊搜索姓名、职位、部门等。
- `target_member_id` 用于精确回查某个人类成员。
- `member_type="human"` 时只返回人类成员。
- `include_uncontactable=false` 时只返回当前可联系对象。

返回结构沿用 Phase 1.2 的 human schema：

```json
{
  "member_type": "human",
  "target_member_id": "org_member_uuid",
  "platform_user_id": "user_uuid_or_null",
  "display_name": "张三",
  "title": "产品经理",
  "department": {
    "id": "department_uuid",
    "name": "产品部"
  },
  "can_contact": true,
  "contact_tools": ["send_platform_message", "send_feishu_message"],
  "provider": {
    "provider_id": "provider_uuid_or_null",
    "provider_type": "feishu",
    "open_id": "ou_xxx_or_null",
    "external_id": "user_xxx_or_null"
  },
  "unavailable_reason": null
}
```

字段说明：

- `target_member_id` 是发送工具的首选稳定参数，解决重名问题。
- `platform_user_id` 只在人类成员已映射平台账号时存在，给 `send_platform_message` 使用。
- `provider.provider_type` 表示第三方来源类型，例如 `feishu`、`dingtalk`、`wecom`。
- `provider.open_id` 是第三方开放平台用户 ID。
- `provider.external_id` 是第三方通讯录用户 ID；飞书里通常是 `user_id`。
- V1 不暴露 `unionid`，因为模型发消息时基本用不到，后续身份合并再考虑。

### human 发送工具参数

#### send_platform_message

新增主路径参数：

```json
{
  "target_member_id": "org_member_uuid",
  "platform_user_id": "user_uuid",
  "message": "..."
}
```

参数优先级：

1. `target_member_id`
2. `platform_user_id`
3. 旧 `username` 兜底

校验规则：

- `target_member_id` 必须能查到同租户 `OrgMember`。
- `platform_user_id` 必须对应同租户 active `User`。
- private Agent 只能联系创建者本人对应的人类成员。
- 非 private Agent 可以联系公司内 active 人类成员。
- 成员必须有平台账号映射，否则返回不可联系。

#### send_feishu_message

新增主路径参数：

```json
{
  "target_member_id": "org_member_uuid",
  "message": "..."
}
```

参数优先级：

1. `target_member_id`
2. 旧 `user_id` 兜底
3. 旧 `member_name` 兜底

校验规则：

- 成员必须通过 human roster visibility。
- 成员必须 active。
- 成员必须有 Feishu provider 身份，且 `external_id` 或 `open_id` 可用。
- 当前 Agent 必须配置 Feishu channel。
- 如果按旧 `member_name` 命中多个人，不能静默选第一个，应提示用 `query_roster` 选择 `target_member_id`。

#### send_channel_message

新增主路径参数：

```json
{
  "target_member_id": "org_member_uuid",
  "channel": "feishu",
  "message": "..."
}
```

参数优先级：

1. `target_member_id + channel`
2. `target_member_id`，由 provider 推导 channel
3. 旧 `member_name + channel` 兜底

校验规则：

- 成员必须通过 human roster visibility。
- 成员必须 active。
- 如果指定 `channel`，provider 类型必须匹配。
- 如果未指定 `channel`，根据 `provider_type` 推导。
- channel 配置不存在时返回明确错误。
- provider-less 但有 `platform_user_id` 的成员，应提示或转发到 `send_platform_message`。

### 统一解析函数

Phase 2 应新增统一的人类收件人解析函数，不要把校验散落在三个发送工具里。

建议函数：

```python
async def resolve_roster_human_target(
    db: AsyncSession,
    agent_id: uuid.UUID,
    *,
    target_member_id: str | None = None,
    platform_user_id: str | None = None,
    provider_user_id: str | None = None,
    member_name: str | None = None,
    provider_type: str | None = None,
) -> RosterHumanTarget:
    ...
```

职责：

- 解析 UUID 参数。
- 加载 source Agent。
- 确认租户一致。
- 复用 `evaluate_roster_human_visibility(source_agent, member)`。
- 确认 `OrgMember.status == active`。
- 处理 provider 类型归一化，例如 `microsoft_teams -> teams`。
- 处理重名歧义。
- 返回 `member`、`provider`、`provider_type`、可用发送目标 ID。

### prompt 调整

Phase 2 后，人类联系也应从 prompt 背景切到 roster-first：

```text
When contacting human colleagues:
1. Use query_roster(member_type="human", query="...") first.
2. Use the returned stable IDs.
3. For platform users, call send_platform_message(platform_user_id="..." or target_member_id="...").
4. For channel users, call send_channel_message(target_member_id="...", channel="...").
5. Do not guess names or IDs.
```

Phase 1.4 保留的 `## 人类同事背景` 可以先保留为背景信息，但不能继续作为发送入口。发送入口必须是 `query_roster` 返回的稳定 ID。

### 兼容策略

本阶段不做长期向前兼容，但为了避免存量触发器、OKR 逻辑、旧 prompt 一次性断掉，旧参数先保留为兜底：

- `send_platform_message(username=...)`
- `send_feishu_message(member_name=...)`
- `send_feishu_message(user_id=...)`
- `send_channel_message(member_name=..., channel=...)`

兜底路径也必须逐步接入 roster visibility 校验，不能继续绕过新规则。

Phase 3 再清理旧参数和旧提示。

### 建议提交拆分

1. `Add exact human lookup to roster queries`
   - `query_roster` 支持 `target_member_id` 精确查人。
   - 测试无效 UUID、private Agent 只查创建者、inactive 返回不可联系。

2. `Resolve human message recipients by roster IDs`
   - 新增统一解析函数。
   - `send_platform_message`、`send_feishu_message`、`send_channel_message` 支持 ID 化参数。
   - 旧参数保留兜底，但走新校验。

3. `Teach prompts to contact humans through roster IDs`
   - 修改 system prompt 和工具 description。
   - 人类发送说明从 `member_name` 改成 `query_roster -> ID`。

4. `Cover roster-based human messaging`
   - 补齐发送工具测试。
   - 覆盖重名、跨租户、private 限制、inactive、缺 provider/channel 配置。

### 测试计划

- `query_roster`：
  - 支持 `target_member_id` 精确查询。
  - 非法 `target_member_id` 返回结构化错误。
  - private Agent 只能看到创建者对应人类成员。
  - inactive 成员默认不返回，`include_uncontactable=true` 时返回并带原因。

- `send_platform_message`：
  - 用 `platform_user_id` 成功定位。
  - 用 `target_member_id` 成功定位。
  - 没有平台账号映射时失败。
  - 跨租户失败。

- `send_feishu_message`：
  - 用 `target_member_id` 成功定位 Feishu 身份。
  - 缺 Feishu channel 配置失败。
  - 缺 `external_id/open_id` 失败。
  - 旧 `member_name` 重名时提示使用 `query_roster`。

- `send_channel_message`：
  - 用 `target_member_id + channel` 成功定位。
  - channel 与 provider 不匹配时报错。
  - provider-less platform user 不走外部 channel。

- prompt：
  - 不再指导模型直接按 `member_name` 发送。
  - 明确人类联系人也要先 `query_roster`。

## Phase 3 - 旧关系体系清理与产品化通讯录

Phase 3 目标是把 Phase 1/2 的新链路产品化，并让旧 Relationships / 旧权限字段从主链路里退出。

### 旧 Relationships 下线

- `AgentAgentRelationship` 不再参与 A2A 授权。
- `AgentRelationship` 不再参与 human 发送授权。
- 旧 UI/API 隐藏，或迁移成“备注关系 / 协作背景”。
- 确认没有调用链依赖后，再决定删表或长期保留。

### 管理权产品化

- `company/custom/private` 的“谁能使用”和“谁能管理”彻底分开。
- `custom` 的显式授权只表示管理权，不再影响使用权。
- 前端设置页拆成：
  - 可见性 / 使用范围：`company/custom/private`
  - 管理成员：创建者、管理员、被授权成员
- 清理历史字段和旧语义：
  - `company_access_level`
  - `AgentPermission(scope_type="company")`
  - 其它只服务旧 custom/use 权限的逻辑

### 通讯录 UI / roster UI

- 数字员工通讯录。
- 人类成员通讯录。
- 搜索、过滤、部门、状态。
- 展示可联系 / 不可联系原因。
- 重名时展示部门、职位、provider 身份。

### 组织架构增强

- 部门过滤。
- `department.path`。
- 多 provider 身份合并。
- `unionid` / external identity 去重。
- DingTalk / WeCom / Teams 等 provider 的发送配套。

### 观测和迁移

- 统计旧关系表是否还有读写。
- 统计工具调用失败原因。
- 记录 `query_roster -> send_*` 转化。
- 迁移历史自定义权限数据。
- 最后再决定删除或长期保留旧字段 / 旧表。

## 当前结论

Phase 1 已完成，数字员工发现与 A2A 调用主链路已切到：

```text
query_roster -> stable ID -> send tool
```

下一步进入 Phase 2，按本文的最小实施方案先完成人类发送链路 ID 化。Phase 3 暂不实施，作为旧关系体系清理、通讯录 UI 和组织架构增强的后续阶段。

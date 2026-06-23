# Phase 2.3 - Human send tools ID 化

## 目标

把三个人类发送工具接到 Phase 2.2 的 resolver，让发送链路和 A2A 一样变成：

```text
query_roster -> stable ID -> send tool -> hard check
```

## 改动范围

- `send_platform_message`
- `send_feishu_message`
- `send_channel_message`

旧参数先保留为兜底，但兜底也必须走 resolver。

## send_platform_message

### 新参数

```json
{
  "target_member_id": "org_member_uuid",
  "platform_user_id": "user_uuid",
  "message": "..."
}
```

### 参数优先级

1. `target_member_id`
2. `platform_user_id`
3. 旧 `username`

### 发送前硬校验

- resolver 通过。
- member 有 `user_id`。
- `User` active 且同租户。
- 可创建或找到 primary platform session。

### 注意

当前 `send_platform_message` 会调用 `ensure_access_granted_platform_relationships()` 并要求 `AgentRelationship` 存在。Phase 2.3 主路径不应再依赖这个旧关系物化逻辑。

## send_feishu_message

### 新参数

```json
{
  "target_member_id": "org_member_uuid",
  "message": "..."
}
```

### 参数优先级

1. `target_member_id`
2. 旧 `user_id`
3. 旧 `member_name`

### 发送前硬校验

- resolver 通过。
- provider 是 `feishu`，或成员有可用 Feishu identity。
- `external_id` 或 `open_id` 存在。
- 当前 Agent 有 Feishu channel config。

### 发送 ID 选择

V1 优先使用 `external_id` 作为飞书 `user_id` 发送；缺失时再考虑 `open_id`。

## send_channel_message

### 新参数

```json
{
  "target_member_id": "org_member_uuid",
  "channel": "feishu",
  "message": "..."
}
```

### 参数优先级

1. `target_member_id + channel`
2. `target_member_id`，由 provider 推导 channel
3. 旧 `member_name + channel`

### 发送前硬校验

- resolver 通过。
- 如果指定 `channel`，必须和 provider 匹配。
- 如果未指定 `channel`，根据 provider 推导。
- channel config 存在且已配置。
- provider-less 但有 `platform_user_id` 的成员，转给或提示使用 `send_platform_message`。

## 不做

- 不改 `send_channel_file`。
- 不删除旧参数。
- 不删除旧关系表。
- 不改 OKR 旧提示。

## 测试点

- `send_platform_message(target_member_id=...)` 成功定位。
- `send_platform_message(platform_user_id=...)` 成功定位。
- `send_feishu_message(target_member_id=...)` 成功定位 Feishu 身份。
- `send_channel_message(target_member_id=..., channel=...)` 成功定位 provider。
- 缺 platform user 失败。
- 缺 provider identity 失败。
- 缺 channel config 失败。
- 旧 `member_name` 重名时提示使用 `query_roster`。


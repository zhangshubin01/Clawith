# Phase 2.4 - prompt/schema/test 收口

## 目标

在 Phase 2.1 到 2.3 代码能力完成后，再调整模型可见的工具 schema 和 prompt，让模型默认走 roster-first。

## tool schema 调整

### query_roster

暴露：

```json
{
  "target_member_id": {
    "type": "string",
    "description": "Optional exact human member ID returned by query_roster. Use this to verify one specific person."
  }
}
```

### send_platform_message

新增：

```json
{
  "target_member_id": {
    "type": "string",
    "description": "Human member ID returned by query_roster."
  },
  "platform_user_id": {
    "type": "string",
    "description": "Platform user ID returned by query_roster."
  }
}
```

`username` 标成 legacy fallback。

### send_channel_message

新增：

```json
{
  "target_member_id": {
    "type": "string",
    "description": "Human member ID returned by query_roster."
  }
}
```

`member_name` 标成 legacy fallback。工具描述改为第三方 IM channel 统一入口，由后端按 provider/channel 分发到 Feishu、DingTalk、WeCom、Slack、Teams、WeChat。

### send_feishu_message

保留为 legacy shortcut，不作为 Phase 2 新主路径。

- prompt 不再主推。
- tool schema 可以暂时不新增 `target_member_id`。
- 如果后续为了兼容内部复用而新增，也必须标成 legacy / shortcut，不应让模型优先选择它。

## prompt 调整

新增或替换人类联系规则：

```text
When contacting human colleagues:
1. Use query_roster(member_type="human", query="...") first.
2. Use the returned stable IDs.
3. For platform users, call send_platform_message(platform_user_id="..." or target_member_id="...").
4. For third-party channel users, call send_channel_message(target_member_id="...", channel="...").
5. Do not guess names or IDs.
6. Do not use send_feishu_message as the primary path; use send_channel_message for Feishu too.
```

Phase 1.4 保留的 `## 人类同事背景` 可以先保留为背景信息，但不能继续作为发送入口。

## 测试收口

建议补充或更新：

- `test_query_roster_tool.py`
  - schema 暴露 `target_member_id`
  - 精确查询
  - invalid UUID
  - private/inactive 行为

- `test_a2a_msg_type.py` 或新增 human messaging 测试文件
  - platform ID 发送
  - channel target member ID 发送
  - Feishu 通过 `send_channel_message(..., channel="feishu")` 分发
  - 重名旧参数歧义
  - 跨租户
  - private 限制
  - 缺 provider/channel config

- prompt 测试
  - 不再指导模型直接按 `member_name` 主路径发送。
  - 明确人类联系人也要先 `query_roster`。
  - `## 人类同事背景` 只作为背景存在。

## 不做

- 不要求删除 `## 人类同事背景`。
- 不要求删除旧参数。
- 不要求修改 OKR 专用 prompt。
- 不要求改 UI。

# Agent 通讯录 Phase 2/3 后续规划存档

本文只存档 Phase 2 / Phase 3 的后续方向，当前不实施。

Phase 1 的目标是先完成数字员工发现与 A2A 调用链路工具化：

- Phase 1.1：权限与可见性判断拆分
- Phase 1.2：`query_roster`
- Phase 1.3：A2A 发送链路 ID 化
- Phase 1.4：prompt 去数字员工 Relationships 依赖

Phase 2 / Phase 3 等 Phase 1 稳定后再启动。

## Phase 2 - Human 发送链路 ID 化

Phase 2 目标是把人类联系人也从“按名字 + Relationships”切到“`query_roster` 返回稳定 ID + 发送工具硬校验”。

### 方向

- `send_platform_message` 优先支持 `platform_user_id`。
- `send_feishu_message` 不再依赖人名匹配，改成稳定成员身份。
- `send_channel_message` 不再按名字匹配，改成 `target_member_id + channel/provider_type` 或等价稳定参数。
- human 发送前复用 Phase 1.1 的 human roster visibility 判断。
- 发送时再次硬校验：
  - `OrgMember.status`
  - provider 身份 ID
  - 渠道配置
  - 当前 Agent 工具可用性

### query_roster human 增强

- human 结果里的 `contact_tools` 与实际发送工具参数对齐。
- 支持按 `target_member_id` 精确查单个人类成员。
- 可选增加 `department_id` 过滤。
- 重名时通过职位、部门、平台 ID、provider identity 稳定区分。

### prompt 调整

Phase 2 后，人类联系也应从 prompt 背景切到 roster-first：

```text
query_roster -> target_member_id / platform_user_id / provider identity -> send_* tool
```

届时可以逐步弱化或移除 Phase 1.4 保留的 `## 人类同事背景`。

### 边界

Phase 2 不要求删除旧关系表，不要求一次性完成完整组织架构 UI。

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

Phase 2 / Phase 3 暂不实施。

当前先完成 Phase 1，让数字员工发现与 A2A 调用主链路切到：

```text
query_roster -> stable ID -> send tool
```

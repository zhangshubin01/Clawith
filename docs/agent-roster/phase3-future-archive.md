# Agent 通讯录 Phase 3 - 后续产品化与清理存档

本文只存档 Phase 3 的后续方向，当前不实施。

Phase 3 目标是把 Phase 1/2 的新链路产品化，并让旧 Relationships / 旧权限字段从主链路里退出。

## 旧 Relationships 下线

- `AgentAgentRelationship` 不再参与 A2A 授权。
- `AgentRelationship` 不再参与 human 发送授权。
- 旧 UI/API 隐藏，或迁移成“备注关系 / 协作背景”。
- 确认没有调用链依赖后，再决定删表或长期保留。

## 管理权产品化

- `company/custom/private` 的“谁能使用”和“谁能管理”彻底分开。
- `custom` 的显式授权只表示管理权，不再影响使用权。
- 前端设置页拆成：
  - 可见性 / 使用范围：`company/custom/private`
  - 管理成员：创建者、管理员、被授权成员
- 清理历史字段和旧语义：
  - `company_access_level`
  - `AgentPermission(scope_type="company")`
  - 其它只服务旧 custom/use 权限的逻辑

## 通讯录 UI / roster UI

- 数字员工通讯录。
- 人类成员通讯录。
- 搜索、过滤、部门、状态。
- 展示可联系 / 不可联系原因。
- 重名时展示部门、职位、provider 身份。

## 组织架构增强

- 部门过滤。
- `department.path`。
- 多 provider 身份合并。
- `unionid` / external identity 去重。
- DingTalk / WeCom / Teams 等 provider 的发送配套。

## 观测和迁移

- 统计旧关系表是否还有读写。
- 统计工具调用失败原因。
- 记录 `query_roster -> send_*` 转化。
- 迁移历史自定义权限数据。
- 最后再决定删除或长期保留旧字段 / 旧表。

## 当前结论

Phase 3 暂不实施。下一步先完成 Phase 2：人类发送链路 ID 化。


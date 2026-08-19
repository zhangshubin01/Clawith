# ADR-0001: chat_messages.conversation_id 迁移为 UUID 类型

- **状态**: 已接受（2026-08-19）
- **背景**: `chat_messages.conversation_id` 为 `VARCHAR(200)`，存储的是
  `chat_sessions.id`（UUID）的字符串形式。两者之间所有 join 都必须写
  `CAST(chat_sessions.id AS VARCHAR)`（全库 19 处），类型系统无法保证
  conversation_id 语义（C2 租户隔离依赖 join 兜底）。实测库内 5485 行
  全部为 UUID 形状、无遗留前缀值（web_/feishu_/slack_/discord_ 均为 0 行）。

## 决策

1. **一次性 in-place 迁移**（f068）：`ALTER COLUMN TYPE uuid USING
   conversation_id::uuid`，同 commit 全量清理 19 处 cast join 与直接比较点。
   不做双列双写过渡——数据已全 UUID 形状，测试栈表 5.4k 行秒级；若将来出现
   真实大表生产环境，另立双写专项。
2. **DDL 与代码必须同部署**：entrypoint 先跑 alembic 再启动应用，单实例
   无兼容窗口；旧代码的 cast join 在 uuid 列上会报 operator does not exist。
3. **gateway 护栏**（不做会话归属重设计）：gateway 回报路径在写
   chat_messages 前校验 conversation_id 为 UUID 形状，非 UUID 拒写并告警
   （消息保留在 gateway_messages 通道表）。gateway 代理间消息的会话归属
   重设计等该功能启用时另立专项。
4. **遗留前缀视图（activity_dao）——方案 B（删除）**：`list_conversation_summaries`
   的 web_/feishu_/slack_/discord_ 前缀块与 `list_conversation_messages` 的遗留
   分支整体移除（实测 0 行遗留数据，删除保持今天的可观测行为=这些条目为空）；
   `list_conversation_messages` 对非 UUID 的 conv_id 直接短路返回空，不再触库。
5. **模型 default="web" 删除**：uuid 列不允许字符串默认值；所有写入点均已
   显式传 conversation_id。

## 回滚

- f068 downgrade：`ALTER COLUMN TYPE varchar(200) USING conversation_id::text`
  （数据仍为 UUID 文本，可逆）。代码需一并回滚到同镜像的旧提交（单 commit
  原子性）。

## 影响面

- 19 处 cast join + 直接比较点 + ~20 个测试文件断言更新。
- 前端零影响（会话 ID 纯字符串透传，无解析逻辑）。
- websocket 6 处 `uuid.UUID(self.conv_id)` 已假定 UUID，迁移后不变。

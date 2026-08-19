# chat_messages CAST Join 深度分析（2026-08-19）

状态：分析完成，待评审 → 实现。范围：`chat_messages.conversation_id`(varchar) 与
`chat_sessions.id`(uuid) 的 CAST 连接族——包含 compactable 双扫描、未读计数、
context pack 三条热路径与两条冷路径。

## TL;DR

1. pg_stat_statements 的 mean 0.34s / 1.31s 是**长尾伪影**（stddev 1.06/1.33s，
   max 69s/32s——今早 checkpoint 事故窗口的锁/IO 竞争）。真实执行 **0.27ms /
   0.22ms**。当前表 5.4k 行，绝对延迟不是问题。
2. 真正的结构性浪费有三：
   - **双扫描**：compactable 查询的 NOT IN 子计划每次全量扫会话消息构造
     "最近 20 条"集合，外层再扫一遍（实录 532 buffer hits、同一批 242 行读两遍）。
   - **89% 无效行读取**：全库 **81.5% 是 tool_call 消息**（4413/5416），
     而 role 过滤不在任何索引里 → 每次可见角色扫描都把 tool_call 行读出来再丢弃
     （android 会话实录：242 行读出、235 行被过滤）。
   - **类型债**：`conversation_id = CAST(sessions.id AS VARCHAR)` 共 8 处，
     依赖约定而非类型约束（C2 租户隔离靠 join 兜底），cast 计算逐行发生。
3. 修复排序：**P0 部分索引（纯 DDL、零代码改动、f067 迁移）→ P1 可选窗口改写
   （消双扫）→ P2 unread 查询不动（已最优）→ P3 conversation_id 改 UUID
   （战略迁移，独立 ADR，不在本轮）**。

## 1. 证据

### 1.1 pg_stat_statements 分布（32h 样本）

| 查询 | calls | mean | stddev | max |
|---|---|---|---|---|
| compactable NOT IN | 4539 | 0.34s | 1.06s | **69s** |
| unread 计数（侧栏徽标） | 604 | 1.31s | 1.33s | **32s** |
| session_context_states 查找 | 4539 | 0.17s | 0.12s | 3s |
| 冷路径变体（activity_dao 等） | 43/39/3 | 2.46s/0.73s/7.58s | — | 50s/22s |

stddev ≈ 或 > mean + 个位数 max 秒级 → 均值被极端离群点主导，典型执行远快于
均值。69s/32s 与今早 10:17–11:15 checkpoint 膨胀事故窗口（全线程历史读 1790s/次）
重合，判定为锁/IO 竞争伪影，**不是查询自身代价**。

### 1.2 真实 EXPLAIN ANALYZE（f23045c7 会话，242 条消息/27 条可见）

compactable 查询（recent_limit=20）：Execution Time **0.268ms**，但——

```
Nested Loop (356 buffers)
 ├─ chat_sessions  uq_chat_sessions_tenant_id_id  1 行
 └─ chat_messages  ix_chat_messages_conversation_created_id
     Index Cond: conversation_id = cast(sessions.id) AND created_at IS NOT NULL
     Filter: role = ANY('{user,assistant}') AND NOT (hashed SubPlan 1)
     Rows Removed by Filter: 235   ← 242 行读出，89% 丢弃
     SubPlan 1（recent-20 集合）: 同索引同条件再读一遍全会话（178 buffers，215 行丢弃）
```

- 会话的全部 242 行被读**两遍**（外层 356 + 子计划 178 ≈ 532 buffers）。
- role 过滤不在索引 → 215/242 行是 tool_call，读出即丢。
- 计划形态本身不差（nestloop + hashed anti-join），浪费在存储层。

unread 计数查询：Execution Time **0.217ms**，`ix_chat_sessions_user_id` →
`ix_chat_messages_conversation_created_id` nestloop，计划已是最优形态。

### 1.3 数据分布

- role：tool_call **4413 (81.5%)**、user 573、assistant 430、system 0。
- conversation_id：**5416/5416 行全部是 UUID 形状字符串**（`^[0-9a-f]{8}-...$`），
  说明历史上它就是 `str(session.id)`，UUID 类型迁移无脏数据风险。

## 2. 查询全景（8 处 cast join）

| 位置 | 频率 | 形态 | 现状 |
|---|---|---|---|
| `session_context_service._compactable_messages_statement` | 4539/32h | join + `id.not_in(最近20子查询)` | **双扫 + 89% 无效读** |
| `session_context_service._context_pack_messages_statement` / `_through` | per-run | join + `id.in_(最近20子查询)` | 同类双扫 |
| `session_context_service._recent_messages_statement` | 每扫描一次 | join + role 过滤 + limit | 无 NOT IN，纯 role 过滤浪费 |
| `api/agents.py:_build_unread_count_by_agent` | 604/32h | join + count + created_at>last_read | **计划已最优，不动** |
| `api/chat_sessions.py:255/267/816` | 低频 | join 变体 | 冷路径，随 UUID 迁移一并消化 |
| `dao/activity_dao.py:181/185` | 43/32h | cast join on stats CTE | 冷路径，同上 |
| `api/websocket.py:511 _load_history` | 每 WS 连接 | **无 cast join**（agent_id+conversation_id 直查） | 不在本项范围 |

## 3. 方案与排序

### P0（推荐本轮做）：可见角色部分索引——纯 DDL，零代码改动

```sql
-- alembic f067（public schema，alembic 所有，无 langgraph 时序问题）
CREATE INDEX ix_chat_messages_conversation_visible_created_id
ON chat_messages (conversation_id, created_at, id)
WHERE role IN ('user', 'assistant');
```

- **收益**：compactable/context pack/recent 三条路径的会话扫描从"全量行读出再过滤"
  变为只碰 18.5% 的可见行（android 会话 242→27 行，-89%）；索引体积 ≈ 1003 行，
  远小于全表索引；`id` 进索引 → 子计划可 index-only。
- **风险**：极低。新增索引不改任何查询语义；写放大 +0（部分索引只维护
  user/assistant 插入）。现索引不动，unread 查询（assistant/system/tool_call）
  继续用旧索引。
- **验证**：建后 EXPLAIN 三条路径，确认 Bitmap/Index Scan 命中部分索引、
  Rows Removed by Filter 归零。

### P1（可选，P0 后评估）：compactable 双扫改写

- 方案 A（应用层拆两段）：outer 改为"watermark 之后的可见消息"直查（无 NOT IN），
  Python 端用 recent-20 id 集合做差集。两段各自走最窄索引；语义完全等价
  （recent 窗口保护逻辑不变）。
- 方案 B（窗口函数）：CTE + `row_number() OVER (ORDER BY created_at DESC, id DESC)`
  + `WHERE rn > recent_limit`，单次扫描（EXPLAIN 已验证计划形态：一个
  nestloop，无子计划）。代价：`text()` 原生 SQL + 行→dict 映射
  （`load_compactable_messages_after_watermark` 已返回 JsonObject tuple，
  映射摩擦小）。
- **依赖**：P0 落地后双扫成本已 -89%，P1 边际收益变小——**可选**，按需做。

### P2：unread 计数查询——不动

计划已是最优（双索引 nestloop、0.2ms）；cast join 在此处无伤害。数据量大后
若出现劣化，再加 `(conversation_id, tenant_id, role, created_at)` 覆盖索引。

### P3（战略，独立立项）：conversation_id → UUID 类型迁移

- **收益**：根除全部 8 处 cast；hash join 可行；类型安全（C2 的 join 兜底变成
  类型系统保证）；uuid(16B) vs varchar(36B) 索引体积减半。
- **数据**：5416/5416 行已 UUID 形状，`ALTER ... USING ::uuid` 可直转
  （生产大表需 CONCURRENTLY 策略 + 双写窗口，本测试栈秒级）。
- **成本/风险**：模型列类型、8 处查询、可能依赖字符串比较的调用方与前端
  契约（URL 传参/websocket 校验）、全量回归。**需要 ADR + 专项 plan，不在本轮。**

## 4. 风险与回滚

- P0 是新增索引：回滚 = `DROP INDEX`（f067 downgrade），无数据风险。
- P1 改的是 compactable 读取语义：等价的差集/窗口必须保留
  (created_at DESC, id DESC) 的 tie-break 与 watermark 过滤；`test_session_context_*`
  现有覆盖需跑全。
- 不改 schema、不动 unread 查询、不动 WS 历史加载——用户可见行为零变化。

## 5. 验收指标

- 建索引后：compactable 查询 `Rows Removed by Filter` 归零、
  buffers 从 ~532 降到 ~60 以下（同会话实测）。
- 全量 pytest（当前基线 2467 passed）+ arch-guard。
- pg_stat_statements 下一窗口复核：compactable/unread 的 max_exec_time
  不再出现数十秒级尾部（事故窗口已排除）。

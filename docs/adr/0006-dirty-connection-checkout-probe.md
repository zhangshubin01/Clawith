# ADR-0006: 脏连接检出探针（Dirty-Connection Checkout Probe）

- **状态**: 已接受（2026-08-28）

## 背景

2026-08-28 事故：backend 的 SQLAlchemy asyncpg 连接池被两条「脏连接」污染 13+ 小时。脏连接的定义是
**客户端与服务器端事务状态分裂**：服务器端处于事务中（PG `pg_stat_activity.state = 'idle in
transaction'`），而 SQLAlchemy/asyncpg 客户端认为连接干净（`_started=False`、`_top_xact=None`）。

机制链（在 SQLAlchemy 2.0.52 + asyncpg 0.31.0 的容器内 ground-truth 逐环实证）：

1. SQLAlchemy 2.0 的 asyncpg 方言是**懒开始**：`conn.begin()` 不发任何语句，第一条语句执行时
   `_prepare_and_execute()` 才调用 `_start_transaction()` 发出 BEGIN（`asyncpg.py:520/575`）。
2. 若取消（`task.cancel()`）落在 `transaction.start()` 的 await 上——BEGIN 已被服务器 ack，asyncpg
   清理了自己的 `_top_xact`，`CancelledError`（BaseException，绕开一切 `except Exception`）传播而
   `_started` 从未置位——连接以「干净」姿态回池（checkin rollback 因 `_started=False` 是 no-op），
   服务器端却留在 'T'。
3. 此后每次 checkout 该连接，第一条语句触发的懒开始抛
   `cannot use Connection.transaction() in a manually started transaction`；该错误经
   `_handle_exception` re-raise 而**不 invalidate**，checkin rollback 仍是 no-op，连接以脏状态回池
   ——风暴自持（实测 10.7 条/秒 × 13 小时；生产 30 分钟内日志零条同类消息也证实其自限性取决于
   重启/回收而非自愈）。

已确认无法通过升级规避（容器内版本已是当时最新），上游 GitHub 亦无公开同类报告。

## 决策

| # | 决策点 | 结论 | 理由 |
|---|---|---|---|
| 1 | 识别手段 | **checkout 事件探针**：`event.listen(engine.sync_engine, "checkout")` 中检查 `dbapi_conn.driver_connection.is_in_transaction()`，为真则 raise `DisconnectionError` | 直接读客户端缓存的服务器端事务状态，**零网络往返**、每次 checkout 一次本地布尔判断；状态是唯一稳定信号，与污染成因无关 |
| 2 | 不用 pool_pre_ping | **否** | pre_ping 只测连接是否断（发一个 `SELECT 1` 级探测），不测 idle-in-transaction；对脏连接毫无反应，加了是自欺 |
| 3 | 处置方式 | **DisconnectionError 让池丢弃并自动重试** | 脏连接客户端/服务器状态已分裂，SQLAlchemy 层的 rollback 是 no-op，无法自清；池层丢弃 + 给调用者换健康连接是 SQLAlchemy 原生路径（`_finalize_fairy` 对 reset 异常即 invalidate），一处注册覆盖全仓库 ~60 个 `async_session()` 调用点 |
| 4 | 兜底 | **`DB_POOL_RECYCLE_SECONDS=1800`** | 若未来出现探针漏掉的脏状态（未知成因），连接寿命封顶 30 分钟即被回收；30 分钟远大于单 run 时长，健康连接不会被中途回收 |
| 5 | 收窄取消窗口 | **commit/rollback 一律 `asyncio.shield`**（`get_db`、`transaction()`、`BaseDAO.session`、`QueryDAO.session` 四个边界） | shield 保证外层 task 被取消时 commit/rollback 仍完整跑完（取消语义保留，`CancelledError` 在保护完成后照常传播），从源头减少「事务中间态被弃置」的窗口 |
| 6 | 长线 | **协作式取消**（cancel_source.py 方向）+ 锁定 13:05:21 的取消者 | 本轮不做；硬 `task.cancel()` 落在任意 await 点，是这类问题的根源，靠探针与 shield 只能收敛不能消灭 |

## 实现形态

- `backend/app/database.py`：
  - `_discard_dirty_connection(dbapi_conn, ...)`：`driver_connection.is_in_transaction()` 为真即 raise
    `DisconnectionError`；对无 `driver_connection` 属性的方言（测试/其他驱动）静默放行；
  - `event.listen(engine.sync_engine, "checkout", _discard_dirty_connection)`（engine 创建后立即注册）；
  - `get_db` / `transaction()` 的 commit/rollback 改 `asyncio.shield`。
- `backend/app/config.py`：新增 `DB_POOL_RECYCLE_SECONDS: int = 1800`，engine 参数
  `pool_recycle=settings.DB_POOL_RECYCLE_SECONDS`。
- `backend/app/dao/base.py`、`backend/app/dao/query_dao.py`：`session()` 上下文的 commit/rollback 同步
  加 shield。

## 测试

- `backend/tests/test_database_dirty_connection.py`（先红后绿）：
  - 3 个单元测试锁探针决策：脏状态 raise DisconnectionError / 干净放行 / 非 asyncpg 方言放行；
  - 1 个真实 PG 集成测试锁完整 seam：构造脏连接（裸 `BEGIN` 于 driver connection）→ checkin →
    断言下次 checkout 拿到健康连接且 begin+execute 成功（`TEST_DATABASE_URL` 未设则 skip）。
- 集成测试在 dev PG 上实跑通过（4 passed）；受影响面（database 依赖、DAO 层）50 个测试全绿。

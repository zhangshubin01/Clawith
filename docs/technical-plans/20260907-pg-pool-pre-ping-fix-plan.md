# PG 连接池 pool_pre_ping 加固方案（C2 落地）

> 日期 2026-09-07 · 审核人 PenguinHarness `default_agent` · 上游：`20260906-pg-zombie-connection-leak-fix-review.md` 的 C2 项（P2 预防，待落地）。

## 裁决：评审通过（P2 预防，最小改动）

在 `backend` 的 SQLAlchemy/asyncpg 连接池开启 `pool_pre_ping=True`，作为 C1（PG 端 `tcp_keepalives`）的**客户端互补**：C1 让服务端快速回收死对端，`pool_pre_ping` 让 backend 在 checkout 时主动探测并重连，避免把「已被 PG 回收的死连接」交给上层。定性为 **P2 预防**（修的是「backend 池在 PG 重启/网络抖动后可能拿到死连接」的风险，非本次 PG 僵尸泄漏的已损故障）。

---

## 一、参考对比结论（复用上游，≥10 项目已查）

上游 `20260906-pg-zombie-connection-leak-fix-review.md` 第一节已对比 ≥12 项目，`pool_pre_ping` 是行业标准：**LangBot、bisheng、mem0、cognee、edict 均配 `pool_pre_ping=True`**（LangBot/bisheng 同栈 SQLAlchemy+asyncpg）。本方案直接复用该结论，不再从零读源码。

> 关键修正（上游已记）：上游曾因「泄漏源是 postgres-mcp 容器而非 backend 池」否决了把 `pool_pre_ping` 当**根治**——但那是「不当根治」，不是「不当加固」。作为 backend 池自身卫生，`pool_pre_ping` 仍成立，故独立为 C2。

## 二、根因（风险根因，P2 预防定性）

**这不是已发生的故障**（诚实定性 P2），而是 backend 连接池的一处**兜底缺口**：

- `backend/app/database.py:18-24` engine 现有兜底只有两层：
  1. `pool_recycle=settings.DB_POOL_RECYCLE_SECONDS`（`config.py:116` = **1800s**）——**时间兜底**，最坏 1800s 内 checkout 仍可能拿到死连接报错一次；
  2. `_discard_dirty_connection`（`database.py:27-46`，checkout event）——**本地状态读**，只探测「server 端仍在事务里」的脏连接，**不做网络探测**。
- 二者都不能在「PG 重启 / 网络抖动后连接被服务器静默回收」时**提前**发现死连接。`pool_pre_ping` 补的正是这个网络探测缺口（SQLAlchemy 内置，`dialect.do_ping` = `SELECT 1`）。

**可证伪**：若「缺网络探测」成立，则开启 `pool_pre_ping` 后，PG 重启后 backend 的首次 checkout 应 ping 出死连接并重连，而非把死 socket 交给上层报 `ConnectionResetError`。

## 三、加固方案（3 处改动，非根治）

### 1. `backend/app/config.py`（DB 配置块，`DB_POOL_RECYCLE_SECONDS` 之后加一行）

```python
# Verify a pooled connection with SELECT 1 before checkout when it may have
# gone stale (idle beyond pool_recycle). Client-side complement to C1's
# server-side tcp_keepalives: pre_ping makes the backend detect and reconnect
# after a PostgreSQL restart / network blip instead of handing out a dead
# socket. Lazy — SQLAlchemy only pings when the connection may be stale.
DB_POOL_PRE_PING: bool = True
```

### 2. `backend/app/database.py`（engine 配置加一个参数）

```python
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
)
```

### 3. 新增回归测试 `backend/tests/test_database_pre_ping.py`

锁「pre_ping 已传给真实 engine」这一配置（一行断言，防止未来有人移除该参数）：

```python
"""pool_pre_ping configuration lock (C2, P2 hardening).

C1 (server-side tcp_keepalives) reaps dead peers; pool_pre_ping is the
client-side complement — SQLAlchemy pings a pooled connection before handing
it out when it may have gone stale, so a PostgreSQL restart / network blip
discards the dead socket and reconnects instead of raising on the first query.
This locks the flag on the real engine (a one-line regression).
"""

from app.database import engine


def test_engine_pool_pre_ping_enabled() -> None:
    assert engine.sync_engine.pool._pre_ping is True
```

> 实测（`.venv` 真跑）：`engine.sync_engine.pool` 为 `AsyncAdaptedQueuePool`，`pool_pre_ping=True` 时其 `_pre_ping` 属性即 `True`（`create_async_engine` 把参数透传给 sync pool）。`_pre_ping` 是 SQLAlchemy 私有属性，但 ruff 默认规则集不含 SLF001（`pyproject.toml` 无 `select`），不产生 lint 告警。

### 回归测试与影响面

- **回归测试**：1 条单元测试锁配置（根因路径即「配置已开启」；终态即「engine 的 pool 带 pre_ping」）。行为层（模拟死连接→重连）是 SQLAlchemy 官方成熟机制，不重复自验，故不做集成测试。
- **影响面**：`engine` 的三个 session 入口（`get_db` / `transaction` / `bind_session_context`，均走 `async_session`）全部受益、无契约变化；**checkpoint pool 是独立的 `AsyncPostgresSaver` 连接（`CHECKPOINT_POOL_*`），不走 `database.py` engine，不受影响**。惰性 ping 仅在连接可能失效时发生，不增加每次 checkout 的 `SELECT 1`。
- **回退**：删 `pool_pre_ping` 参数 + `DB_POOL_PRE_PING` 一行即回退，零迁移。

---

## 四、7 角度评审裁决（含负向探针）

1. **根因找的是否正确？** ✅ 通过（定性 P2 预防而非 P0 已损）。
   - 正向依据：源码——`database.py:18-24` 现有兜底 = `pool_recycle`（时间）+ `_discard_dirty_connection`（本地读，`database.py:27-46`），均无网络探测。
   - 负向探针（反例测试）：「若『backend 池会拿到死连接』是**已发生**故障，则运行日志/台账应有 backend 侧 `ConnectionResetError`/`InvalidCatalogNameError` 成批报错痕迹；对照——本次事故的报错是 `TooManyConnectionsError`（PG 打满），非 backend 池死连接，故这是『预防』非『已损』，定性 P2 成立」。

2. **根治方案是否正确？** ✅ 通过（明确非根治，是加固）。
   - 正向依据：改的是「backend 池无网络探测」这一缺口，非 PG 僵尸泄漏根因。
   - 负向探针（删除测试）：「把 `pool_pre_ping` 删掉，问『PG 僵尸泄漏根因会不会复发』——**不会**，因为泄漏源是 postgres-mcp 容器、由 C1 keepalive 治；删掉它只是 backend 池少了 PG 重启后的主动探测兜底」。

3. **参考的资料是否正确？** ✅ 通过。
   - 正向依据：复用上游 ≥12 项目对比，LangBot/bisheng 同栈（SQLAlchemy+asyncpg）真实源码，非 README 摘要。
   - 负向探针（反例测试）：「我找了一个可能引用错的点——asyncpg 下 `pool_pre_ping` 是否真的生效、是否每次 checkout 都 `SELECT 1`？核对 SQLAlchemy 2.0 源码 `pool/base.py:1286/1302`：pre_ping 仅在连接『可能失效』（idle 超 recycle / 首次）时 ping，非每次；asyncpg dialect 提供 `do_ping`。无误」。

4. **副作用与爆炸半径是否排查完？** ✅ 通过。
   - 正向依据：①副作用面——无外部写、无缓存、无权限边界变化，仅加一个内置连接参数；②影响面——engine 三入口受益，checkpoint 独立池不受影响（`CHECKPOINT_POOL_*` 走 `AsyncPostgresSaver`）。
   - 负向探针：「我特意找过方案会漏掉的一个消费者——`AsyncPostgresSaver` 的 checkpoint 连接是否也走 `database.py` 的 `engine`？核对——它用独立连接配置（`CHECKPOINT_POOL_MIN/MAX_SIZE`），不走 `engine`，pre_ping 不触碰 checkpoint 池，无副作用」。

5. **这是最优且必要的方案吗？** ✅ 通过（P2 预防，非多余）。
   - 正向依据：①候选——「更简单」= 不配、只靠 `pool_recycle=1800` 时间兜底；「更彻底」= 自写每 checkout `SELECT 1`（重造轮子）。`pool_pre_ping` 是中间内置档，一行参数；②定性——P2 预防，非 P0 已损，不越界。
   - 负向探针（删除测试）：「我试过用『更简单的一档：不配 pre_ping、只靠 pool_recycle』能否覆盖 PG 重启场景——**能但慢**（最坏 1800s 内 checkout 拿死连接报错一次，之后 recycle 换新）；pre_ping 行业标准、改动一行，收益 > 成本，非多余」。

6. **是否已经有可复用的逻辑？** ✅ 通过。
   - 正向依据：复用 SQLAlchemy 内置 `pool_pre_ping`（不自己写 ping）；与既有 `pool_recycle`、`_discard_dirty_connection` 三者**分工不重叠**（时间兜底 / 脏事务本地探测 / 网络探测）。
   - 负向探针：「我特意查过知识图谱/代码是否已有等价实现——已有 `_discard_dirty_connection`（checkout event）但它是本地状态读、无网络 ping；`pool_pre_ping` 是内置参数，复用内置而非自写。无重复造轮子」。

7. **会破坏 Clawith 的特性吗？** ✅ 通过（逐条宪法 C1–C6）。
   - 正向依据：C1 证据先行（本方案全部 read_file/实测）；C2 最小改动（3 处、~10 行）；C3 契约不变（不改任何 DB 契约/事务所有权）；C4 测试证行为（1 条锁配置回归）；C5 保留既有工作（不动并行会话脏文件）；C6 单模块边界不变。
   - 负向探针：「我把方案对红线过一遍——checkpoint 语义（独立池不碰）、多租户隔离（无涉）、exactly-once（无外部写）、前缀缓存稳定性（无涉）、WS 状态机（无涉）、飞书通道（无涉）；唯一动作是 `arch-guard.sh` + `pytest backend/tests/test_database*`。无红线触碰」。

---

## 五、落地清单（实现 + 验证 + 批准项）

1. 改 `config.py` + `database.py`（2 处，见第三节）。
2. 新增 `backend/tests/test_database_pre_ping.py`。
3. 验证：`scripts/arch-guard.sh` + `pytest backend/tests/test_database*`（含新增用例）。
4. **rebuild backend 容器（需用户批准）**——代码改动须重建镜像才生效，与 C1（只重建 PG）不同。
5. Phase 5：实现后跑 `code-review` 对照本方案复核 diff（不得夹带范围外改动）。

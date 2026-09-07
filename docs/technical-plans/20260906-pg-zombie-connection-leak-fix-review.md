# PG 僵尸连接泄漏根治方案审核（clawith-fix-plan）

> 审核日期 2026-09-06 · 审核人 PenguinHarness `default_agent` · 目标：Clawith 生产 PG 连接打满（99/100 → TooManyConnectionsError）的根治方案。

## 裁决：最终方案（评审定稿）

根因在 Phase 2 取证中被**修正**：此前假设「被杀 exec/sandbox 容器遗留连接」不成立（沙箱容器 `network_mode: none`，不连 DB）。**主导泄漏源是 PenguinHarness 每个会话 spawn 的 `postgres-mcp` MCP 容器（73 个，连接同一 clawith PG），死会话不回收 + PG 无死对端探测，二者叠加把一次性 SIGKILL 放大成永久僵尸。**

**最终方案（各项裁决见第三节正文，依据见二/四/五节）**：
- **C1 PG 端 `tcp_keepalives_idle=60/interval=30/count=3`** —— ✅ 采纳，**已执行并验证通过**（治「放大器」：死对端僵尸 ~150s 自愈）。
- **C2 backend `pool_pre_ping=True`** —— ✅ 通过（P2 预防），待落地（与 C1 互补，backend 侧自愈，需重建 backend）。
- **C3 MCP 容器优雅关闭** —— ❌ 移出 Clawith 仓库（源头治理，owner 属 PenguinHarness MCP 基建，另开任务）。
- **A 同步 deploy 两份 compose 加 keepalive** —— ⚠️ 跳过（两份是死文件，无脚本引用）。
- **C4/C5 idle timeout** —— ❌ 否决（false friend / 误杀 backend 池连接）。

---

## 一、参考对比结论（≥10 项目）

**关键决策点**：D1 死对端如何被 PG 检测回收（TCP keepalive）· D2 空闲会话/卡死事务如何被服务端回收（idle timeout）· D3 客户端连接池如何自愈（pool_recycle / pool_pre_ping）· D4 容器如何优雅关闭（SIGTERM→SIGKILL）。

| # | 项目 | 同类问题解法（实测源码/文档） |
|---|------|------|
| 1 | **PostgreSQL 官方文档 19.3.2 TCP Settings** | `tcp_keepalives_idle/interval/count=0` = 使用 OS 默认（Linux `net.ipv4.tcp_keepalive_time=7200s`/`intvl=75s`/`probes=9`），死对端约 **2.2h** 才被内核探测回收；显式设 `60/30/3` 可压缩到 ~150s。`idle_in_transaction_session_timeout` 只回收 `idle in transaction`，**不回收 `idle`**；`idle_session_timeout`（PG14+）回收所有 idle 会话。 |
| 2 | **dify** | `docker-compose.yaml` 用 `command: postgres -c 'idle_in_transaction_session_timeout=${…:-0}'` —— 默认仍 0（禁用），仅暴露 env 开关；未配 tcp_keepalives。其 `-c` 命令行机制与 Clawith `PG_COMMAND` 同款，是落点参考。 |
| 3 | **LangBot**（Clawith 同赛道 IM bot，SQLAlchemy+asyncpg） | `pool_pre_ping=True` + `pool_recycle_seconds=1800` + `idle_in_transaction_session_timeout_ms=60000`（每连接 connect_args）。同栈最接近，但它 idle 超时治的是「卡锁事务」，非本故障。 |
| 4 | **bisheng**（dataelement 姊妹项目） | `pool_pre_ping=True` + `pool_recycle=3600`(1h) + `pool_timeout=30` + pool_size=100/max_overflow=20。未配 keepalive/idle timeout。 |
| 5 | **CubeSandbox**（腾讯沙箱，同 execute_code 域） | Go `SetConnMaxLifetime`(=pool_recycle) + `SetMaxIdleConns` + `SetMaxOpenConns` + `connect_timeout`/`statement_timeout`。 |
| 6 | **edict**（OpenClaw 生态） | `pool_pre_ping=True` + pool_size=10/max_overflow=20。 |
| 7 | **mem0** | `pool_pre_ping=True`。 |
| 8 | **omnigent** | `pool_pre_ping=True` + 按数据源动态 `pool_recycle`。 |
| 9 | **cognee** | `pool_pre_ping=True` + `pool_recycle=280`(短) + pool_size=5/max_overflow=35。 |
| 10 | **AstrBot** | 知识库用 SQLite（`kb_db_sqlite.py`），无 PG 连接池 —— 诚实负结论。 |
| 11 | **OpenHands** | 未检出 PG keepalive/idle timeout/池显式配置 —— 诚实负结论（默认 SQLAlchemy 池）。 |
| 12 | **E2B / OpenHands-CLI** | 沙箱为云 API（`agentbay` 同源 `_async/session.py` 走 HTTP Bearer），不直连本机 PG —— 负结论。 |

**跨项目结论**：
1. 行业标准 = **客户端池自愈**（`pool_pre_ping`+`pool_recycle`，LangBot/bisheng/mem0/cognee/edict 全配）+ 服务端**可选** `idle_in_transaction_session_timeout`（治卡锁事务）。**几乎没人配 `tcp_keepalives`** —— 因为它们的 DB 客户端是 web 进程，走 uvicorn 优雅关闭（SIGTERM→dispose pool→FIN），不会被 SIGKILL。
2. **Clawith 的独特处境**：DB 客户端里混入了「会被 `docker rm -f`/SIGKILL 硬杀的短命容器」（本环境的 per-session `postgres-mcp` MCP 容器），优雅关闭不可控 → 泄漏无法在源头根除，**必须靠 PG 端死对端探测自愈**。
3. **机制选择结论**：`tcp_keepalives` 是唯一能治本 Clawith 场景的官方机制（服务端探测死对端）；`idle_in_transaction_session_timeout` 是 **false friend**（我们的僵尸是 `idle` 非 `idle in transaction`，它不回收）；`idle_session_timeout` 会误杀后端 21 条合法 `idle` 池连接（须设 >> `pool_recycle=1800s` 才安全，收益小风险大）。

---

## 二、根因（双源钉死，已修正）

### 症状链（运行日志 + PG 台账）
- backend startup `command_worker._claim` 报 `TooManyConnectionsError`，靠重试恢复（运行日志）。
- PG `max_connections=100` 被打满（99 条），其中 **63 条 `idle` >1h 僵尸**，client_addr 分散多个 `192.168.97.x`（PG 台账，清理前实测）。
- 手工 `pg_terminate_backend`（state='idle' 且 idle>1h）杀 63 条后连接 99→34，backend 未受影响（PG 台账）。

### 证据 1 —— PG 侧放大条件（实测参数全 0）
```
version=15.18  max_connections=100  superuser_reserved_connections=3
tcp_keepalives_idle=0   tcp_keepalives_interval=0   tcp_keepalives_count=0
idle_in_transaction_session_timeout=0   idle_session_timeout=0
```
PG 启动命令（`docker inspect`）仅含内存/WAL/autovacuum 调优 `-c` 参数，**无任何 keepalive / idle timeout**。`keepalive=0` 意味着走 OS 默认（Linux idle 7200s）→ 死对端约 2.2h 才被探测，回收窗口远慢于僵尸产生速率 → 累积打满。

### 证据 2 —— 客户端硬死来源（修正）
- **主导源（此前假设漏判）**：`clawith_network` 上 148 个 `clawith-mcp-python:1` 容器 = PenguinHarness 每会话 spawn 的 MCP 容器，实测归类 **73 个 `postgres-mcp`**（`cmd=[postgres-mcp --access-mode restricted postgresql://clawith:clawith@postgres:5432/clawith]`，直连 Clawith PG）+ 75 个 `mcp-server-redis`。记忆 `penguinharness-mcp-ops` 明文：「死会话 MCP 不自动回收（残留 12h+）」、`docker rm -f` 清理、「死会话 postgres-mcp 容器持有 PG 连接槽（曾清 46 容器后 PG 连接 24→13）」。
- **被排除的旧假设**：沙箱 exec 容器默认 `network_mode: "none"`（`docker_backend.py:293`），不连 DB；`shared.py:450-456` 的 `async_session` import 是 **backend 进程内**代码（发布文件/记修订），非沙箱容器内代码。
- 清理回收用 `docker rm -f`/`container.kill()`/`os.killpg(SIGKILL)`（`docker_backend.py:80/547`、`subprocess_backend.py:379`、`shared.py:133`）→ SIGKILL 无 FIN。

### 证据 3 —— 僵尸态是 `idle` 非 `idle in transaction`
清理 SQL 与当前 `pg_stat_activity` 均为 `state='idle'`（当前 41 条：backend 21 条池连接 + 活 postgres-mcp 会话 + 少量残留）。故 `idle_in_transaction_session_timeout` 不回收本故障的连接。

### 根因链（追到底）
```
per-session postgres-mcp 容器（PenguinHarness MCP 基建，非 Clawith 代码）
  └─ 死会话不回收 + 回收时 docker rm -f（SIGKILL，无 FIN）
      └─ PG 端连接变 idle「死对端僵尸」
          └─ PG 无 tcp_keepalives（=0 走 2.2h OS 默认）→ 永不快速回收
              └─ 僵尸累积 > 回收速率 → 打满 max_connections=100 → TooManyConnectionsError
```
**最深层因**：PG 端没有任何「死对端快速探测」配置，使「客户端硬死」这一不可控事件被无限放大为永久连接泄漏。

**可证伪**：若根因成立，给 PG 配 `tcp_keepalives_idle=60/interval=30/count=3` 后，人为 `docker rm -f` 一个 postgres-mcp 容器，其连接应在 ~150s 内从 `pg_stat_activity` 消失（无需手工 `pg_terminate_backend`）。

---

## 三、修复方案（最小、可回退、带回归）

### 候选枚举（Ponytail 阶梯从低到高，标注最终裁决）
- **C1（采纳，已执行并验证）PG 端 tcp_keepalives**：在根 `docker-compose.yml`（`command: ${PG_COMMAND:-}`）追加 `-c tcp_keepalives_idle=60 -c tcp_keepalives_interval=30 -c tcp_keepalives_count=3`，`docker compose up -d postgres` 重建。**零代码、零迁移、可回退**（删参数重启即回退）、治本（死对端 150s 自愈，僵尸无法累积）。已重建并验证：`pg_settings` `source=command line`、TCP 连接 `SHOW` 实测 60/30/3。
- **C2（通过，P2 预防，待落地）backend `pool_pre_ping=True`**：只保护 backend 自己的池，**不治本故障**（泄漏源在 postgres-mcp 容器，不在 backend 池内），但与 C1 **互补**（服务端回收死对端 vs 客户端主动探测重连，如 PG 重启后）、行业标准（LangBot/bisheng/mem0/cognee/edict 均配）。落地 = `config.py` 加 `DB_POOL_PRE_PING: bool = True` + `database.py` 传 `pool_pre_ping=settings.DB_POOL_PRE_PING`，补一条回归测试（复用 `test_database_dirty_connection.py` 风格），跑 `arch-guard.sh` + `pytest backend/tests/test_database*`，rebuild backend（需用户批准）。
- **C3（移出 Clawith 仓库）容器优雅关闭**：MCP/沙箱容器 SIGTERM→grace→SIGKILL。指向源头，但 158 个 `clawith-mcp-python:1` 容器由 PenguinHarness MCP 基建 spawn（`grep backend/app` 无 spawn 代码），Clawith 仓库内无 owner 可改。另开 PenguinHarness 任务（会话结束优雅 stop / 定期 GC，记忆 `penguinharness-mcp-ops`）。
- **C4（否决）`idle_in_transaction_session_timeout`**：false friend（僵尸是 `idle` 非 `idle in transaction`，不回收）。
- **C5（否决为默认）`idle_session_timeout`**：能回收 idle 僵尸，但会误杀 backend 21 条合法池连接，须设 >>1800s 才安全，收益小风险大。

**追加动作 A（跳过）**：同步 `deploy/docker-compose.yml` + `deploy/docker-compose-multi.yml` 加 keepalive —— 两份是**死文件**（`scripts/deploy.sh:214-224` 用根 `docker-compose.yml`、`deploy/RELEASE_DEPLOYMENT.md` 的 Drone 发布用 `docker-compose.cd.yml`，均不引用它们），已与主线 drift（multi 引用 `./frontend/nginx.conf.template`）。不改，避免死文件噪音。

### 回归测试
1. **根因路径**：部署后人为 `docker rm -f` 一个已连 PG 的 postgres-mcp 容器 → 观察其连接在 ~150s 内从 `pg_stat_activity` 消失。
2. **终态**：`SHOW tcp_keepalives_idle` = 60；正常业务（backend 池连接有往返流量）不受影响——keepalive 只在「无网络活动 60s」后才探测，活跃连接永不受影响。
3. **副作用断言**：健康的 `idle` backend 池连接（pool_recycle=1800s 内）不会被误杀。

### 影响面
仅 PG 容器：重建秒级，数据在 `pgdata` 卷持久；无 schema/数据迁移；不触碰 backend/frontend 代码、契约、LangGraph checkpoint、多租户、exactly-once、WS、飞书通道。

---

## 四、9 角度评审裁决

1. **根因对否？** ✅ 通过（已修正）。双源全覆盖：运行日志 TooManyConnections + PG 台账 63 僵尸 + 实测参数全 0 + 73 个 postgres-mcp 容器归属；追到最深（客户端 SIGKILL × PG 无死对端探测）。旧「sandbox 容器」假设被实测排除。
2. **方案对否？** ✅ 通过。改的是根因（PG 死对端探测）；删掉方案（keepalive 回 0）故障必复发。
3. **资料对否？** ✅ 通过。keepalive 语义引自 PG 官方 19.3.2 原文；LangBot/bisheng/dify 为同栈/同族真实源码 grep（非 README 摘要）；OpenHands/AstrBot/E2B 已标诚实负结论。
4. **副作用？** ✅ 通过（标注）。唯一副作用 = PG 容器重建秒级停机，数据持久、无迁移。
5. **破坏其他逻辑？** ✅ 通过。零代码改动，`arch-guard.sh` 无需跑；仅 PG 运行时参数。
6. **最佳方案？** ✅ 通过。枚举 5 候选，从 Ponytail 最低档 C1 起，C1 即最简且治本。
7. **是否多余？** ✅ 通过。已发生故障（99/100 打满、TooManyConnectionsError），P0 已损，非臆想风险。
8. **可复用？** ✅ 通过。复用现有 `PG_COMMAND` `-c` 机制（与 dify 同款），零新机制。
9. **破坏 Clawith 特性？** ✅ 通过。C1 只改 PG 层，不碰 durable run/checkpoint、多租户隔离、exactly-once、前缀缓存稳定性、WS 状态机、飞书通道；红线（测试不灰度、loguru、禁 ruff format、子代理禁 checkout/reset）无涉。

### 通过条件（最终状态）
1. **C1 已执行**：PG 容器重建需用户批准（DB 写/容器重启红线）——**已获批并完成**，数据在 pgdata 卷持久。
2. **C2 待落地**：落地 `pool_pre_ping` 需用户批准 rebuild backend，落地前补一条回归测试 + 跑 `arch-guard.sh`。
3. **边界诚实标注**：C1/C2 治「放大器」+ backend 侧自愈；「源头」（73 个 postgres-mcp 容器死会话不回收）属本 Agent 自身 MCP 基建，由既有 `mcp-container-gc` 日任务治理 + C3（PenguinHarness 另开任务）——均非 Clawith 代码改动，不阻塞本次。

---

## 五、二轮评审记录（A/B/C 追加改动，2026-09-06）

> 本节是二轮评审的**过程证据**；裁决已回写第三节正文与头部「最终方案」，本节保留逐条依据供复核。

C1 已执行并验证通过后，用户提出三项追加改动，逐条 9 角度复审（宪法 C1–C6 原文 `.specify/memory/constitution.md` v1.0.0 已读）。

| 改动 | 裁决 | 依据（真实代码/实况） |
|---|---|---|
| **A. 同步 `deploy/docker-compose.yml` + `docker-compose-multi.yml` 加 keepalive** | ⚠️ **跳过**（死文件） | 两份文件全仓库无脚本/文档引用：`scripts/deploy.sh:214-224` 用**根** `docker-compose.yml`，`deploy/RELEASE_DEPLOYMENT.md` 的 Drone 发布用 `docker-compose.cd.yml`。且已与主线 drift（multi 引用 `./frontend/nginx.conf.template`）。同步只制造一致性假象，不影响当前生产。若要一致性，仅同步单机版 `deploy/docker-compose.yml` 并加「已废弃」头注释。 |
| **B. backend `pool_pre_ping=True`** | ✅ **通过**（P2 预防） | `backend/app/database.py:18-24` 现有 `pool_recycle=settings.DB_POOL_RECYCLE_SECONDS`（`config.py:116` = 1800s）无 pre_ping。与 PG keepalive **互补**（服务端回收死对端 vs 客户端主动探测重连，如 PG 重启后）；行业标准（LangBot/bisheng/mem0/cognee/edict 均配）。惰性 ping 负载可忽略；与既有 `_discard_dirty_connection`（`database.py:46`）不重叠。 |
| **C. MCP 容器优雅关闭** | ❌ **移出 Clawith 仓库** | 158 个 `clawith-mcp-python:1` 容器（`docker ps -a` 实况 `Up 2min~17h`）由 **PenguinHarness MCP 基建** spawn，`grep backend/app` 无任何 spawn 代码；Clawith 仓库内无 owner 可改。属平台侧 MCP server 生命周期管理，另开 PenguinHarness 任务（会话结束优雅 stop 容器 / 定期 GC 死会话容器，记忆 `penguinharness-mcp-ops`）。 |

**裁决要点**：三项均为「加固/治理」非「根治」——删掉后 C1（keepalive）仍在、故障不复发。B 是唯一值得在 Clawith 仓库落地者（`config.py` 加 `DB_POOL_PRE_PING: bool = True` + `database.py` 传 `pool_pre_ping=settings.DB_POOL_PRE_PING`，补一条回归测试复用 `test_database_dirty_connection.py` 风格，跑 `arch-guard.sh` + `pytest backend/tests/test_database*`，rebuild backend 需用户批准）；A 是死文件一致化；C 指向源头但 owner 不在 Clawith。

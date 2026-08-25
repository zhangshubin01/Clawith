# Langfuse 多租户隔离（P1）— 设计方案

> 日期：2026-08-25
> 状态：方案待确认（未实施）
> 前置：observability Phase 1 + B+D（run 级 trace + 原生 session/user）+ P3（节点/工具 span）已部署
> 范围：backend observability facade 的多租户物理隔离（tenant → Langfuse project + 独立 key）

## 0. 结论摘要

**推荐方案 A：静态 per-tenant key 注册表 + 半自动 provision**——backend 用 `LANGFUSE_TENANT_KEYS`（JSON env：tenant_id → {public_key, secret_key}）按当前 run 的 tenant_id 选择独立 Langfuse client；未配置的租户保持现状（默认 project + metadata 含 tenant_id 可审计）。provision（创建 project + 生成 key）走 Langfuse UI 手动 + 一次性脚本，**运行时零依赖 Langfuse 管理 API**。方案 B（运行时自动 provision）暂缓。

必要性：**4/5（生产上线前 5/5）**。当前所有租户 trace 混在单一 project `clawith` 且有共享 key——trace 含对话正文，是生产合规门槛。

## 1. 现状事实（2026-08-25 实测核实）

| 项 | 事实 |
|---|---|
| Langfuse 实例 | 自托管 v4.16.0（compose project `langfuse`，web :3000），org=clawith、project=clawith |
| 现有凭据 | project-level pk/sk（`LANGFUSE_PUBLIC_KEY/SECRET_KEY`），**仅能访问自己的 project**（`/api/public/projects` 200、`/api/public/organizations/projects` 403） |
| 管理 API | OpenAPI spec 确认：`POST /api/public/projects` + `POST /api/public/projects/{id}/apiKeys` 存在，但需 **org 级凭据**（当前 project key 403） |
| org 凭据获取 | 初始化 admin（admin@clawith.local）可登录（session OWNER），但 org API key 生成走 UI/trpc（内部端点不稳定，探针未找到公开路径） |
| SDK 多实例 | ✅ 实测 `Langfuse(pk_a, sk_a)` 与 `Langfuse(pk_b, sk_b)` 可共存（v4.14.5） |
| 身份注入 | `set_run_identity` 已携带 `tenant_id` 进 `_run_identity` ContextVar，所有 observe_* 的 metadata 已含 tenant_id |

## 2. 目标与评分

- **收益 4.5**：合规底线（租户 trace 物理隔离）+ 每租户自助视图 + 按租户成本/用量分析 + 单租户 key 可独立轮换/撤销
- **风险 2**：静态注册表运行时零外部依赖；主要风险=provision 操作错误（低）
- **必要性 4（生产前 5）**：dev 可暂缓，上线前必须

## 3. 方案对比

### 方案 A：静态 per-tenant key 注册表（推荐）

```
tenant_id → project（Langfuse UI 创建）
  │
  ├─ key 生成（UI project settings → API Keys）
  │
  └─ 填入 backend env：LANGFUSE_TENANT_KEYS='{"<tenant_id>":{"public_key":"pk-...","secret_key":"sk-..."}}'
```

**tracing.py 改造**（最小侵入）：
- `config.py` 加 `LANGFUSE_TENANT_KEYS: str = ""`（JSON，compose 透传）
- `_get_client()` → `_get_client(tenant_id: str | None)`：`_clients` dict 缓存（tenant_id → Langfuse 实例）；从 `_run_identity` 取 tenant_id；命中注册表→该租户 client；未命中→默认 client（现状）
- observe_* 各函数从 identity 里取 tenant_id 传给 `_get_client`
- 失败降级：解析错误/缺失 → 默认 client + warning（trace 不丢、归属可审计）

**provision**（半自动）：
- 一次性脚本 `scripts/langfuse_provision_tenant.sh`（或文档化 UI 步骤）：创建 project + 生成 key + 提示粘贴 JSON
- 新增租户 = 跑一次脚本 + 更新 env + 重建容器（或热更新）

| 维度 | 评价 |
|---|---|
| 运行时依赖 | 零（不调管理 API） |
| 实现量 | 小（config + tracing + 脚本，~1 天） |
| 可回滚 | ✅ 不配置即完全现状 |
| 扩展性 | 几十租户完全够；几百+ 再评估 B |
| key 生命周期 | Langfuse UI 管理，与 Clawith 解耦 |

### 方案 B：运行时动态 provision（暂缓）

- 未知 tenant 首次出现时自动调管理 API（org key）创建 project + key
- 优点：全自动
- 缺点：需要 org 级凭据进 backend（更大的 secret 面）；管理 API 是 v4 新能力、自托管稳定性待观察；出错时 trace 静默丢或复杂 fallback；实现面大（租户生命周期 hook、重试、幂等）
- **触发条件**：租户数量真到几十级或需要完全自助 onboarding 时再评估

## 4. 实施步骤（方案 A，确认后执行）

1. `backend/app/config.py`：加 `LANGFUSE_TENANT_KEYS: str = ""`
2. `backend/app/services/observability/tracing.py`：
   - 解析 `LANGFUSE_TENANT_KEYS` → `dict[str, dict]`（惰性、缓存）
   - `_get_client(tenant_id)`：注册表命中→per-tenant client；未命中→默认
   - `observe_run/observe_generation/observe_tool/observe_node` 从 identity 取 tenant_id
   - 失败吞掉（保持既有 no-op 语义）
3. `docker-compose.yml`：透传 `LANGFUSE_TENANT_KEYS: ${LANGFUSE_TENANT_KEYS:-}`
4. 脚本 `scripts/langfuse_provision_tenant.sh`：UI 指引/API 创建 + JSON 输出
5. 测试：per-tenant 选择、未配置 fallback、坏 JSON 降级、metadata 含 tenant_id
6. 验证：为测试租户建 project + key，容器内跑 trace → 确认落对应 project、跨租户不可见

## 5. 风险与缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| 租户 key 在 env/配置中 | 中（与现有 LANGFUSE_SECRET_KEY 同级，已接受） | .env gitignored；生产用 secrets 管理 |
| provision 操作错误（key 配错租户） | 低 | 脚本输出 JSON 带 tenant_id 校验；metadata 双写审计 |
| 未知 tenant 落默认 project | 低（dev）→ 中（生产） | 生产可加严格模式（`LANGFUSE_STRICT_TENANT=true` → 未配置租户 no-op 并 warning） |
| SDK 多实例资源 | 低 | 每租户一个 exporter（后台批量），几十租户无压力 |

## 附：核实来源

- Langfuse Public API（自托管 v4）：https://langfuse.com/docs/api —— 3 组 API：project-level / organization-level（provision projects）/ instance-management
- 本地 OpenAPI spec：`http://localhost:3000/generated/api/openapi.yml`（projects/apiKeys 端点）
- 本机实测：project key 对 org 端点 403；SDK 多实例共存 OK
- 方案文档 §5（多租户映射）：tenant_id → project 为物理隔离边界

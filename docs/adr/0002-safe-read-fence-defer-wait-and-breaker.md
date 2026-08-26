# ADR-0002: Safe-read fence defer 的等待与熔断机制

状态：Accepted（grill-with-docs 达成共识，2026-08-26）
背景：2026-08-25 09:04–09:09 UTC 的重试风暴（464 次 `safe_read_attempt_active` ERROR + 长时间 command 争抢）。

## 决策

### 1. defer 等待做在 command 行（非 daemon 层）

`agent_run_commands` 新增两列：
- `deferred_until`（timestamptz, nullable）：撞 fence 时写入 `execution.lease_expires_at + jitter(0–5s)`；claim SQL 过滤 `deferred_until IS NULL OR deferred_until <= now`。释放 claim 后 command 全局不可抢，10 个 daemon 不会互相抢。
- `deferred_started_at`（timestamptz, nullable）：首次 defer 时固定，之后不更新。

选择 command 行而非 daemon sleep 的原因：defer 必然释放 claim，daemon 层 sleep 无法阻止其他 9 个 daemon 立刻重新 claim（实测事故正是该循环）。等待必须落在持久化的 claim 资格上。

### 2. 僵局熔断用总时长判定（非 lease_owner 对比）

- 每次再撞 fence：`deferred_until` 更新为新的 `lease_expires_at + jitter`（执行者活着续期 → 等待自然延长，语义正确）；`deferred_started_at` 保持不变。
- `now - deferred_started_at > 3 × lease_ttl_seconds`（15 分钟）→ 判定僵局，转 `_release_for_retry` 消耗一次 business attempt，交 `_max_attempts` 兜底。
- 依据：Temporal 官方建议「限制重试总时长而非次数」（encyclopedia/retry-policies.md）；时长上限与「等待别人执行完」的语义兼容，避免误伤长工具。

### 3. 僵局错误码

新增 `tool_fence_wait_exhausted`（defer 时长上限耗尽），与 `safe_read_attempt_active`（正在等）区分。观测上二者都降级为 DEFAULT span level（与仓库既有契约 `test_observe_run_retry_control_flow_not_error` 一致）。

### 4. 本轮范围外的后续改进（单独立项）

- D3 heartbeat 独立化：拆单一 lease TTL 为「start-to-close + 独立 heartbeat timeout」，缩短进程死亡检测窗口（现为完整 300s）。需过 android_compile 长工具验证。

## 事实依据（grill 阶段查实）

- claim TTL = 60s（`app/config.py:172`），有心跳续期；defer 循环 = 释放 → 0.1–4s → 重 claim。
- 恢复路径正确：LangGraph 从 checkpoint 恢复不重跑 model 节点（风暴最猛两分钟 GENERATION=0）；事故中 16 万 token 为 4 个 run 的正常大上下文消耗。
- cancel 不受影响：cancel 是独立 command 行，claim SQL 允许其插队 pending/claimed 的 start。
- resume 撞 fence 与 start 走同一条 defer 路径，机制天然统一。

# 2026-09-03 Direct Chat「任务卡住」根因分析与修复方案

- 状态：方案待评审（尚未实施）
- 事故时间：2026-09-03 09:04–09:45 UTC（用户报告「clawith agent 任务执行卡住了」）
- 涉及运行：`dc557d91`（lane holder）、`8ef42390`（排队 run）、`ebab5cda`（已完成）
- 会话：`b67d1138-4c76-4670-8c87-a25c73d82ae2`，Agent：`950a1943`（Android 工程师 07）

## 1. 结论摘要

**没有死锁，没有 worker 崩溃，也没有 lane 泄漏。**「卡住」的真相是三层叠加：

1. **排队（直接原因）**：用户 09:07:47 的消息 `8ef42390` 因 direct-chat lane 串行化（ADR-0012 设计行为）在队列中等待 36+ 分钟，因为前一条消息 `dc557d91`（09:04:04，执行 memory.md 待办清单）是超长任务，43 分钟 59 个模型 step 仍未完成。
2. **拖慢（放大因素）**：DeepSeek 流式长请求被周期性断开（RemoteProtocolError 风暴），每次失败要挂 ~100–120s 才被 120s 超时发现，再走 4 次指数退避重试——7 次重试 ≈ 14 分钟纯等待，把每个 step 从 ~30s 拖到 2–4 分钟。
3. **无感知（用户视角）**：前端把 `queued` 状态渲染成通用「思考中」动画，没有「排队中」文案；排队无超时通知。

证据链：

| 事实 | 证据 |
|---|---|
| `dc557d91` 活着且持续推进 | 09:43:11 仍在发模型请求；claim 每 ~1 分钟续约（09:30:28→09:44:09）；59 个 LLM-CacheFp step |
| `8ef42390` 排队 36 分钟未认领 | `cd4dc460` status=pending、attempt_count=0、claim_expires_at=NULL，直到 09:44 |
| 模型连接周期性抖动 | 7 次 `[RuntimeModelRetry] RemoteProtocolError attempt=1/4`（09:25:36、09:29:36、09:31:02、09:31:40、09:35:14、09:41:30、09:43:11） |
| 抖动是容器出网链路级，非 DeepSeek 单点 | Feishu WS 断线时间 09:25:30、09:29:32、09:35:14 与 DeepSeek 失败时间差 4–6 秒（09:35:14 完全重合） |
| 宿主机→DeepSeek 网络正常 | ping 18ms/0 丢包；宿主机 curl 0.25s 返回 401；容器内短请求 0.9s 返回 401 |

## 2. 时间线

```
09:00:50  ebab5cda「重试一下 gitlab」创建（delivered，正常完成，无 lane 遗留）
09:04:04  dc557d91「根据你推荐的执行待办」创建；09:04:07 认领 lane（lane_held=true）
09:04:56  dc557d91 开始工具调用（list_focus_items…）→ 09:28:06 持续 read_file/list_files/execute_code/android_compile
09:07:47  8ef42390「执行 [P1·文档] README 与代码脱节…」创建 → lane 被持有 → FIFO 排队
09:25:36  第一次 RemoteProtocolError retry（此后每 ~2–4 分钟一次，共 7 次）
09:25–09:35  Feishu WS 3 次断线重连（与 DeepSeek 失败时间强相关）
09:33:35  step=47 请求挂 ~99s 后失败 → 重试后 09:36:40 恢复
09:43:11  仍在重试；8ef42390 仍在排队（36 分钟）
```

## 3. 根因分层

### 3.1 Lane 串行化是设计行为（不是 bug）

- `_direct_lane_key = direct_chat_thread:{tenant}:{session}`，同一 thread 内 foreground run 严格 FIFO（`chat_intake.py:231`、`_require_direct_start_allowed`）。
- lane 只由 `SchedulingLaneCompletionHandler` 在**终态 checkpoint** 释放（`scheduling_lane.py:38-67`）——这是对多 run 共 thread 上下文污染的根治（记忆 direct-chat-run-boundary-fix），必须保留。
- `dc557d91` 的任务本身合法：执行用户附件 memory.md 里的整个待办清单，含 Android 编译（09:06:49 android_compile 成功）、多文件读改写。59 步是任务量大的结果，不是循环失控（每步都在读新文件/写新内容）。

### 3.2 模型调用韧性缺陷（核心放大因素）

三个具体缺陷叠加：

1. **流式 read 超时过长**：`_get_model_timeout` 默认 120s，且 `llm_models.request_timeout` 全为 NULL（未配置）。DeepSeek v4-flash 正常首 token <30s，120s 意味着每次断连要白等 ~100s 才暴露。
2. **RemoteProtocolError 不进内层快速重试**：`OpenAICompatibleClient.stream()` 内层只捕获 `ConnectError/ReadError/ConnectTimeout`（`client.py:1033`），`RemoteProtocolError` 冒泡到 `_call_prepared_with_retry` 外层 4 次重试，每次还要再经历完整超时。
3. **重试复用同一 httpx client**：`_get_client()` 缓存连接池，失败后连接可能被复用（httpx 会废弃坏连接，但长连接 keep-alive 在抖动链路下仍会反复撞上）。

### 3.3 出网链路周期性抖动（环境因素，非代码）

- Feishu WS（`msg-frontier-sg.larksuite.com`，新加坡）与 DeepSeek（`api.deepseek.com`）**同时段**断线，时间差 4–6 秒；宿主机与容器短请求均正常。符合「长连接被中间设备周期性重置」特征。
- 与任务 1 的 jina 事故（r.jina.ai DNS 污染 + Clash TUN 吞 TLS eof ~10s）同源：本机 Clash TUN 对跨境长连接的干扰。DeepSeek 失败挂 ~100s 而非即时 RST，更像 GFW/中间设备对大流量流式连接的空闲重置。

### 3.4 排队无感知（用户为何说「卡住」）

- 后端已发 `{"type":"runtime_status","event":"queued","status":"queued"}`（`websocket.py:711`）。
- 前端收到后只是进入 `showDirectRunThinking` 通用 thinking 动画（`AgentDetailPage.tsx:2995-3000`），**无「排队中（前方任务已运行 X 分钟）」文案**。
- 排队无超时、无进度通知。`can_cancel` 对 queued run 为 true（`chat_sessions.py:659`），用户其实有 stop 按钮，但不知道它对应排队消息。

## 4. 修复方案（分层，最小改动）

### 4.1 LLM 流式调用韧性（P1，代码）

**目标**：把单次失败暴露时间从 ~120s 降到 ~45s，重试更快，同抖动下总耗时下降 ~60%。

1. `backend/app/services/llm/client.py` `OpenAICompatibleClient._get_client()`：
   - 为 DeepSeek 类 provider 构造 `httpx.Timeout(connect=10, read=45, write=30, pool=10)`（read=45s：flash 首 token 正常 <30s，45s 覆盖 2σ）。
   - `timeout` 参数仅作为上限：`min(request_timeout, 45s)` 用于 read。
2. `stream()` 内层 except 增加 `httpx.RemoteProtocolError`，与 ConnectError 同路径快速重试（1s/2s 退避），避免冒泡到外层 4 次大退避；保留 `visible_content_emitted` 保护（可见输出已发出则仍中断，防内容分叉）。
3. `_call_prepared_with_retry` 外层重试前 `await client.close()` 重建连接（丢弃可能残留的坏 keep-alive 连接）。

验收：构造 RemoteProtocolError 单测（内层重试 3 次快速退避；可见输出后不重试）；DeepSeek 端点下一次抖动实测 step 恢复时间 <90s。

### 4.2 排队可见性（P1，前端）

`frontend/src/pages/agent-detail/AgentDetailPage.tsx`：

- `selectedSessionActiveRun.status === 'queued'` 时渲染专用状态行：「⏳ 排队中——前方有任务正在执行（已运行 X 分钟）」，替代通用 thinking 动画。
- 后端 `_announce_queued` 事件附带 `queued_behind_run_id`（lane holder id）；前端用它显示 holder 运行时长（从 runtime_state 的 `created_at`/`model_step_count` 计算）。

验收：排队状态肉眼可见区分「执行中/排队中」；排队消息旁 stop 按钮保持可用（已有）。

### 4.3 排队超时通知（P2，后端）

`message_loop` 排队分支（`websocket.py:561`）记录排队时间；排队 >10 分钟且 lane holder 仍在运行（claim 存活）时，向前端发 `runtime_status` 事件（event=`queued_waiting_long`，带 holder 已运行时长与 step 数），前端据此显示「前方任务执行时间较长，可等待或停止本条消息」。

**不自动取消**：排队消息是用户真实指令，自动取消会丢指令；10 分钟阈值仅做提示，不做熔断。

### 4.4 出网链路（P2，运维，不写代码）

- 在宿主机 Clash 配置中为 `api.deepseek.com` 与 `*.larksuite.com` 增加 PROXY 规则（复用任务 1 对 r.jina.ai 的处置），消除 TUN 对跨境长连接的周期性重置。
- 观察 48h：`[RuntimeModelRetry] RemoteProtocolError` 频率应降至 <1 次/小时（当前 ~7 次/43 分钟）。

### 4.5 明确不做的事

- **不做 lane 抢占/并行**：lane 串行化是上下文污染根治方案，抢占会重新引入多 run 共 thread 竞态（记忆 direct-chat-run-boundary-fix）。
- **不做排队自动取消**：见 4.3。
- **不限制单 run 步数**：59 步是合法长任务的量级，步数上限是产品策略不是本次故障点；超长任务治理另开票。

## 5. 实施顺序与验证

1. 4.1 代码改动 → 单测 + ruff + `scripts/arch-guard.sh`。
2. 4.2 前端改动 → 前端构建 + 本地 WS 复现排队场景截图。
3. 4.3 后端事件 → 单测（排队 >10 分钟发事件）。
4. 4.4 用户改 Clash 配置 → 48h 观察。
5. 测试环境全量部署（不灰度，遵循红线），观察 dc557d91 同型长任务 + 排队消息的端到端表现。

## 6. 证据留存

- 容器 `53392f19a8e3`（clawith-agent-backend-1，09-03 08:58 重启后的实例）日志：09:04–09:44 全量 d56d032e6f19 线程日志。
- `agent_run_commands`：d56d032e（claimed，续约至 09:44:09）、cd4dc460（pending，attempt 0）。
- `agent_runs`：dc557d91（lane_held=true，delivery_status=pending）、8ef42390（lane_held=false，排队）、ebab5cda（delivered）。
- 模型重试 7 次时间戳：09:25:36 / 09:29:36 / 09:31:02 / 09:31:40 / 09:35:14 / 09:41:30 / 09:43:11。

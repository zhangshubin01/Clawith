# 飞书卡片提前创建（首帧优化）— 设计记录

日期: 2026-08-27
范围: `backend/app/api/feishu.py`, `backend/app/services/agent_runtime/card_stream_bridge.py`,
`backend/app/services/feishu_service.py`

## 背景（实测数据）

线上两样本（run `6047ba80` 01:48 / run `7f601cbd` 04:10，三源毫秒级交叉：容器日志 + Postgres + Langfuse）：
「事件到达 → 卡片可见」≈ 3s，构成为：

1. ~1s 串行前缀：`_resolve_feishu_sender`（**无条件**打 2 个 Contact API RTT：
   app_access_token + contact/v3/users，timeout 各 10s）+ 会话创建 + resume 查询；
2. ~2s CardKit 两 RTT（create_card_entity + send_card_by_card_id，各 ~1s，飞书 API 固有）。

模型路径早已与建卡并行（`fc53ab50`，模型首步实测早于卡片创建完成 ~1s），
卡片首现延迟纯粹来自卡片自身路径上的这段串行前缀。

## 决策：卡片提前创建 + resume 撤回

- **提前创建**：`_accept_feishu_runtime_message` 中把 `CardStreamBridge.start()`
  任务 fire 提前到 `_resolve_feishu_sender` 与会话创建**之前**，与其并发。
  建卡只依赖 agent_name + receive_id + 凭据——全部来自事件与配置，不依赖发送者解析。
- **中断指令不建卡**：中断短语判断（纯文本）同步提前，中断消息不创建卡片。
- **resume 撤回**：会话创建后做精确 resume 检查（agent_id/session_id/origin_user_id/
  lane_held/source_type=chat）；命中时撤回提前建的卡片（`withdraw()`），run 照常入队。
- **解析失败撤回**：`ChannelUserResolutionError` 时先撤回再让错误传播（tip 消息行为不变）。

## 撤回机制契约（CardStreamBridge.withdraw）

- `send_card_by_card_id` 返回响应体 data（`{"data": {"message_id": ...}}`，与仓库
  `.get("data", {})` 惯例一致）；bridge 在 `start()` 中捕获 `message_id`。
- `withdraw()` 三种时态都安全：
  1. `start()` 尚未执行 → 置 `_withdrawn`；随后 `start()` 开头 bail，不建卡不发卡；
  2. 创建在途（state=creating）→ 等 `_creation_future`（≤15s），完成后删除消息；
  3. 已发出（message_id 已知）→ 直接删除消息。
- 撤回的卡片**不渲染终版卡片**：`start()` 尾部的 finalize 钩子以 `not _withdrawn` 守卫。
- 撤回失败（删除消息失败）仅记日志、流程继续——残留一张空骨架卡片，无害。
- 两路撤回（resume/解析失败）统一走 `_withdraw_card_bridge`：撤回 + **无论成败都
  `unregister_bridge`**，注册表条目不留到 30 分钟 age-sweep。

## 验证

- 单测：编排层 4 例（顺序断言/resume 撤回/解析失败撤回/中断不建卡，
  `tests/test_feishu_channel_runtime.py`）+ 机制层 5 例（message_id 提取、撤回删卡、
  在途等待、start 前撤回、撤回失败容忍，`tests/test_feishu_card_mode.py`）。
- 全量后端套件通过；ruff check 通过；arch-guard 通过。
- 预期效果：卡片首现 3s → ~2s（Contact API 慢时省更多）。
- 残余优化点（未做）：文件消息路径先下载文件才建卡；模型首步的 daemon 领取轮询
  + 图启动（实测 1.4~4.1s）是首内容延迟大头，与卡片无关。

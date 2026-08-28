# 观察项：飞书卡片对 waiting_user 状态无呈现（run e2ef5629）

> 日期：2026-08-27 ｜ 状态：观察项（独立于上下文瘦身 P0–P2，随时可开工）
> 触发：用户报告飞书任务「卡死」→ 实际为 run 停在 waiting_user 等待用户裁决，卡片无任何等待提示

## 现象

用户看到的「卡死」：飞书卡片在 09:52:37 最后一帧更新（status_banner + tools_live_md）后永久静止，
没有「任务在等你决定」的任何提示，也没有告诉用户可以如何继续。实际上任务没有被卡死——
run e2ef5629 在 09:52:35 进入 `waiting_user`（平台行为正确），是卡片呈现缺失造成了「卡死」错觉。

## 证据链（2026-08-27 UTC，容器日志 + DB 实锤）

| 时间 | 证据 |
|---|---|
| 09:46:19 | `[FEISHU-CARD] bridge_created run_id=e2ef5629`，模型步正常推进（0–22 步，~6 分钟） |
| 09:51:35 | 第二次 `android_compile`（assembleDebug，container a9bf010462fd）启动，之后无完成日志 |
| 09:52:35.397 | `agent_run_events` 入库 `waiting_started`：`reason=tool_deadline_outcome_unknown`、`waiting_type=user`、`correlation_id=ef41c714…`、`tool_call_id=fba9f9a1…`；**payload 无 question/prompt 字段** |
| 09:52:36–37 | 卡片最后两帧推送（status_banner seq 295、tools_live_md seq 296），此后后端对该 run 零日志（09:53 整分钟无任何日志） |
| 之后 | `agent_runs.delivery_status='pending'`（等待投递从未发生）；daemon 15/15 正常；无 deadline/abort/finalize 日志 |

## 根因（三层）

1. **卡片模式吞掉等待投递**（`checkpoint_side_effects.py:224-251` `delivery_from_checkpoint`）：
   卡片模式（`_card_config.app_id` 存在）且 bridge 活跃时一律 `return None` 抑制 ChannelDelivery；
   但 `CardStreamBridge` 只有 completed（`finalize`）/abort/error 三种终态方法，**没有 waiting 呈现**。
   结果 waiting_user 既无卡片更新、也无纯文本等待消息 → 用户收不到任何等待信号。
2. **等待文案是机器码**（`tool_step_service.py:514-526` `_waiting_request` + `delivery.py:134` `waiting_content`）：
   等待请求只构造 `reason=error_code`（`tool_deadline_outcome_unknown`），无 question/prompt；
   `waiting_content` 按 question→prompt→reason 取第一个非空值直接返回，reason 是机器码也照常展示。
   → Web Chat 用户同样会看到机器码（前端无任何映射）。
3. **无交互入口 + 流式窗口与 age-sweep 交互**：
   卡片按钮在长连接模式下收不到 `card.action` 回调（代码已有注释确认），唯一可靠裁决方式=用户在群里回复文本；
   但卡片上没有任何文案提示用户「请回复」。另：bridge 30 分钟 age-sweep（`_MAX_BRIDGE_AGE_SECONDS=1800`）
   会对 waiting 中的 bridge 调 `abort("卡片超时")` 并注销——用户犹豫超 30 分钟再回复时，卡片已被终结
   （后续走 ChannelDelivery 纯文本回退，行为可兜底但体验割裂）。

## 修复方案

### A. bridge 新增 waiting 呈现（核心，工作量小）

- `CardStreamBridge` 增加 `async def waiting(self, question_text: str)`：
  推送 `status_banner`「⏸ 等待你的决定」+ 主内容等待文案 + footer 引导
  「请直接在群里回复你的决定（如：继续 / 放弃重试），任务将从中断处继续」。
- `checkpoint_side_effects.delivery_from_checkpoint` 卡片分支内新增 waiting 分支：
  `status == "waiting_user"` 且 bridge 活跃 → `await bridge.waiting(waiting_content(waiting))` 后 `return None`。
- **不关闭流式通道**：resume 后 bridge 仍需继续推流；`_enqueue_push` 已有 streaming_timeout
  自愈逻辑（code 命中时重新 `set_card_streaming_mode(1)`），等待期间流式窗口过期不影响恢复。

### B. 等待文案人类可读化（工作量小）

- `_waiting_request` 增加 `question` 字段：按 error_code 生成指引文案，例如
  `tool_deadline_outcome_unknown` →
  「工具 {tool_name} 在截止时间前未能确认执行结果，可能已产生部分写入。请回复如何处理：继续 / 放弃重试」。
- 兜底：`waiting_content` 对「下划线风格机器码」（无空格、仅小写字母/数字/下划线）不再原样返回，
  落到 `_WAITING_FALLBACK_CONTENT`，防止未来新错误码再次以机器码形式展示。

### C. 30 分钟 age-sweep 等待语义（可选，低优先）

- waiting 状态下的 bridge 被 sweep 时，终态文案从「卡片超时」改为带等待语义的提示
  （如「⏸ 任务仍在等待你的回复，但卡片已超时。请直接在群里回复继续」）；
  或保持现状依赖 ChannelDelivery 纯文本回退。二选一，不做也可以。

## 验收

- 复现一次 write-effect 工具超 deadline 场景（或复用 e2ef5629 同类任务）：
  卡片出现等待 banner + 人类可读等待文案 + 「请回复」引导；DB `waiting_started` 事件与卡片推送成对出现。
- Web Chat 路径：等待消息显示人类可读文案而非 error code。
- 全量测试 + `scripts/arch-guard.sh` + deploy.sh 部署；`prompt_cache_hit/miss_tokens` 无回归（本改动不触 prompt 布局）。

## 范围

- 改动点：`card_stream_bridge.py`（新增 waiting 方法）、`checkpoint_side_effects.py`（waiting 分支）、
  `tool_step_service.py`（question 文案）、`delivery.py`（机器码兜底）。
- 与上下文瘦身待办（`20260826-context-slimming-todo.md`）相互独立，不进 P0–P2 排序。

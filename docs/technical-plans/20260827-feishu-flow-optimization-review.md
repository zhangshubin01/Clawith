# 飞书机器人流程优化方案评审

日期: 2026-08-27
范围: 基于 2026-08-27 三源实测（容器日志 + Postgres 毫秒 + Langfuse）与代码核对
状态: 评审完成，待用户选定实施项

## 实测基线（事件到达后的链路）

```
事件 → 卡片可见     ~3s   (CardKit 2 RTT ~2s + 少量；串行前缀已并行化 ✅)
事件 → 入队落库     ~0.5s (✅ 已与建卡并行)
入队 → 图启动      1.4~4.1s  ← 最大波动段（daemon 领取轮询 + 图 boot + checkpoint/snapshot）
图启动 → 首 LLM    ~0.3~2s  (上下文构建 6800+ tokens)
首 LLM → 首帧内容  1.8~4s   (DeepSeek TTFT，模型侧)
事件 → 首帧内容    ~7~8s
工具面板物化       1~2s ✅ (已修：start_tool 直发物化，原 10-20s)
```

## 评审结论速览

| 项 | 结论 | 关键调整 |
|---|---|---|
| P1-① 发送者解析缓存 | **采纳（扩 scope）** | 前提比原判断更严重：token"缓存"是死代码 |
| P1-②a 群聊前置过滤 | **降级为实验项** | 与 passive 设计意图冲突，误杀成本高 |
| P1-②b NO_REPLY 终态特判 | **采纳** | 复用今天新建的 withdraw() |
| P1-③ claim 唤醒 | **采纳（先插桩量化）** | 只能省轮询 ~0.5-1s；须单消费者设计 |
| P2-④ 文件下载并行建卡 | **采纳（小改）** | 复用撤回机制 |
| P2-⑤ footer 间隔 | **采纳（一行）** | 0.8s→5s |
| P2-⑥ main_content O(n²) | **驳回/降级观察** | 客户端已增量渲染，网络层已有节流 |
| P3-⑦ 上下文构建/TTFT | **不改（观察项）** | 模型侧/语义风险 |

## 逐项评审

### P1-① 发送者解析缓存 — 采纳（前提比原判断更严重）

**前提核对**：
- `_resolve_feishu_sender`（feishu.py:435-515）对**每条消息**裸打 2 个 HTTP
  （POST app_access_token + GET contact/v3/users，timeout 各 10s），实测 ~0.5-1s；
- `FeishuService._app_access_token`（feishu_service.py:105,191）**只写不读**——
  所谓 token 缓存是死代码，`get_tenant_access_token` 每次现打；
- `channel_user_service` 无任何用户信息缓存。

**方案修正**：
1. service 层实现真缓存：per-app_id key + TTL（10min，飞书 token 有效期 2h）+
   在途去重（并发同 key 只打一次）+ 过期刷新；
2. `_resolve_feishu_sender` 改用 service 缓存方法；user 信息（name/email/avatar）
   按 (app_id, open_id) 缓存 TTL 5-10min。

**风险**：
- fail-closed 语义（contact_failed 且无 user_id → 拒绝解析）：缓存命中=成功路径，
  不应触发 fail-closed；缓存 miss/expired 仍走原 API 路径。语义保持不变。
- 租户隔离：key 含 app_id/open_id，无跨租户泄漏。
- 头像/昵称延迟更新 5-10min：可接受。

**收益校准**：入队关键路径 ~0.5-1s → ~50ms（冷启动首次除外）；直接缩短首帧内容。

### P1-② NO_REPLY 全链路浪费 — 拆成 a/b 分别裁决

**前提核对**：`NO_REPLY` 全库唯一出处是群聊被动指令文本（feishu.py:45），
无任何消费点/过滤/特判；卡片模式终态走 `delivery_from_checkpoint`
（checkpoint_side_effects.py:225 → `bridge.finalize(content)`），
`_terminal_content` 无过滤 → **终版卡片会字面渲染 "NO_REPLY"**（可见缺陷）。

**②b（终态特判）— 采纳**：
- 终态 content 归一化后 == "NO_REPLY" → 调 `bridge.withdraw()`（今天已建、
  已生产验证的撤回机制：删除卡片消息、零渲染）替代 `finalize()`。
- 风险极低；顺带修复"非@消息在群里闪一张 NO_REPLY 卡片"的体验问题。

**②a（前置启发式过滤）— 降级为实验项**：
- 反对理由：passive 指令的设计意图就是让模型判断（上下文依赖，
  "嗯"/"继续"这类消息的应答判断启发式做不对）；误杀成本=机器人漏答，
  用户感知远坏于"白跑一轮"。
- 若要做：默认关、只保留最保守条件（无 @、无机器人名、无问号、
  纯表情/单字）、配日志计数观测命中率与误杀率后再决定去留。
- 宪法约束：过滤决策须可追溯（记录消息与理由），模型可见输入可审计。

### P1-③ 入队→图启动 claim 唤醒 — 采纳但先量化，且必须单消费者

**前提核对**：入队（enqueue_chat_runtime → DB 落库）后无任何唤醒信号；
daemon 空闲轮询 idle 基数 1.0s + 指数退避（忙时归零）、15 daemon 相位错开
是**刻意设计**（SKIP LOCKED 取件延迟 ≈ cap/N）。

**方案修正**：
- **单消费者唤醒**（一个 waiter 接 asyncio.Event，或广播后 SKIP LOCKED 只
  一个 claim 成功）——严禁 15 daemon 同时醒来抢 claim，否则破坏相位错开
  并制造瞬时 claim 事务风暴。
- **收益校准**：唤醒只省轮询等待（期望 ~0.5s，上限 ~1s）；实测 1.4~4.1s
  中还有图 boot / checkpoint 加载 / snapshot 捕获，**未被量化**。
  建议先按三源方法插桩拆分这四段，确认瓶颈再决定是否值得做唤醒。

### P2-④ 文件消息下载与建卡并行 — 采纳（小改）

**前提核对**：`_accept_feishu_file_runtime` 先下载资源（1-3s+）再调
`_accept_feishu_runtime_message` 建卡；建卡只依赖事件与配置
（receive_id/agent_name/凭据），不依赖下载产物。

**方案**：提取"提前建卡"为 helper，文件路径在下载前 fire；
下载失败 → 复用 `_withdraw_card_bridge`。图片消息入队仍需等下载
（content 嵌 base64），但卡片首现提前 1-3s。风险低。

### P2-⑤ footer 0.8s 高频推送 — 采纳（一行参数）

**前提核对**：`_footer_flush = FlushController(min_interval=0.8, min_delta=0)`，
流式期间每 0.8s 抢锁推 "⏱ Ns"，与正文/思考/工具推送争同一把锁
（今天工具面板延迟事故的帮凶之一）。

**方案**：0.8s → 5s（计时器显示精度损失可忽略）。风险 ≈ 0。

### P2-⑥ main_content 全量重推 — 驳回（降级观察）

**前提核对**：`push_text` 推全量累计文本（CardKit content API 为替换语义）；
但骨架卡 `streaming_config`（print_strategy=fast / print_step=2）已让客户端
增量渲染——所谓 O(n²) 只发生在网络层，且 600ms/30chars 节流已限频。
长输出（>10K chars）才有实感。结论：不动结构；若将来长文卡顿，
做自适应节流（文本越长 min_delta 越大）即可。

### P3-⑦ 上下文构建 ~2s / TTFT 1.8~4s — 不改（观察项）

模型侧（DeepSeek v4-flash reasoning）与 prompt 组装增量化（语义风险中），
非本次范围。

## 建议实施顺序（评审后）

1. **P1-① token/user 缓存** — 独立、纯收益、入队路径直接提速
2. **P1-②b NO_REPLY 终态撤回** — 修可见缺陷，复用已验证机制
3. **P2-⑤ footer 间隔** — 一行改动
4. **P2-④ 文件消息建卡并行** — 复用撤回机制
5. **P1-③ claim 唤醒** — 先插桩量化入队→图启动四段，再做单消费者唤醒
6. **P1-②a 前置过滤** — 实验开关 + 观测后决策

**每项前置**：回归测试（既有 seam）+ 三源时间线观测方法（今日已跑通）
+ ruff/arch-guard；提交按 pathscopes 隔离并行会话改动。

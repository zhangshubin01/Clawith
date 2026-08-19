# Agent 工具调用死循环防护方案

日期：2026-08-19
范围：backend（`agent_runtime/model_step_service.py` 等）
状态：R1/R7/H-1/H-2/L2/H-3/H-4 已实现并通过真实 checkpoint 验证；
L1 预算默认值（10000→50）待独立决策；cancel 缺口待排期；L4 待排期

## 一、事故背景与时间线

租户「Android工程师 4」（agent `27d55a64`）在 2026-08-19 出现两次死循环：

1. **09:52–10:04（run `16e8088f`）**：用户发送「重新编译」后，模型每 ~10 秒调用一次
   `android_compile`（参数完全相同、每次都成功），**110 次**空转，永不产出最终回复。
   平台当时只有「失败循环」熔断（飞书配置错误场景），成功循环无任何护栏；
   取消命令在运行中的图循环里不被消费。
2. **10:37（run `7c70b3f1`）**：首版成功循环熔断器上线后**误伤新 run**——Direct Chat
   多个 run 共用同一 thread，新 run 继承了旧循环的 16 条尾部消息，被秒杀
   （`tool_success_loop`）。

关键事实（数据库实证）：

- 同类循环**不是新现象**：08-13 有 6 连成功编译（run `4ae02a79`）、08-16 有 5 连
  （run `9f019cb6`），模型自行收敛、无人注意；今天是第一次恶化到 110 次。
- **触发组合历史上几乎从未出现**：全库仅 2 条 `goal=重新编译` 的 run——7-22 那条
  0 次编译，今天这条 110 次。以前编译任务总有失败要修，模型每轮有真实工作；
  「项目已编译成功 + 用户要求重编译」+ flash 模型每轮服从指令 = 空转。
- 本分支与 `origin/main`：main 的 runtime 落后本分支 155 个提交且无熔断器；
  同样场景在 main 上只会烧满 50 轮后 `model_step_limit_reached`（mg2 同款结局）。

## 二、问题分类

| 形态 | 特征 | 防线 | 状态 |
|---|---|---|---|
| A 紧循环 | 同工具+同参数连续重复（本次编译循环） | 软提醒（L2）+ 硬终止（L3） | L3 已部署 |
| B 松循环 | 参数每次在变（mg2 的 40+ 次 execute_code 诊断） | 50 轮模型预算（`max_tool_rounds`） | 已有，只能事后兜底 |
| C 自我污染 | 模型修改构建入口文件恶化环境（mg2 改 gradlew/wrapper） | builder 执行前检测脏入口 | 待排期（见 mg2 记忆遗留建议） |

## 三、参考实现研究（已核对源码/文档）

1. **Cline**（`sdk/packages/core/src/runtime/safety/loop-detection.ts`，逐行读过）：
   - 签名 = 键排序 JSON（与本方案 `_tool_call_signature` 同构）；
   - **softThreshold=3 → 注入「恢复提醒」**（引导换一种方式，不终止）；
   - **hardThreshold=5 → 升级到连续错误决策路径**（终止/人工审批）；
   - CLI 默认开启 `{softThreshold: 3, hardThreshold: 5}`。
2. **LangChain v1 create_agent**（参考仓库 `langchain_v1`）：
   - `recursion_limit = 9_999`（几乎不限）；
   - 真正的防线是 `ToolCallLimitMiddleware`（按工具名计数，thread + run 双层；
     超限行为 `continue`=拦截该工具并注入 **"Do not call 'X' again"** 让模型继续、
     `error`、`end`=注入解释性消息后结束）与 `ModelCallLimitMiddleware`（模型调用预算）。
3. **LangGraph / deepagents**（参考仓库）：只靠 `recursion_limit`（25 / 500），
   无语义检测——纯预算是生态底线而非最优。
4. **AutoGen**：`max_consecutive_auto_reply`——连续重复计数终止，L3 同源。

结论：分层组合是业界共识；Cline 的软硬双阈值是与本场景最匹配的实战设计。

## 四、分层方案

### L1 预算（已有）
`agent.max_tool_rounds`（50 轮）→ 兜底 B/C 形态。对齐 LangChain `ModelCallLimitMiddleware`。

### L2 软提醒（待实现，Cline soft=3）
在 `model_step_service._prepare_messages` 中：尾部连续 3 个同 (tool, args)
（**按当前 run 台账隔离**，复用 `_trailing_identical_success_loop` 的计数逻辑）时，
向本轮模型消息注入恢复提醒：

> 注意：你已连续 3 次以完全相同的参数调用工具 X 且执行成功。
> 请停止重复调用，直接基于已有结果向用户汇报。

- 不终止 run、不拦截执行，纯引导——多数情况下循环在第 3 次被打断；
- 阈值 3 的依据：08-13/08-16 的历史循环（5-6 连）表明 flash 模型在
  「已完成任务」场景天然会重复 5-6 次，soft=3 卡在「可能自行收敛」的边界之前。
- 实现位置：`_prepare_messages` 中硬熔断检查之后、消息组装完成处追加一条
  提醒消息（system/user 角色，放在消息序列尾部）。

### L3 硬终止（已部署，Cline hard=5）
`_trailing_identical_success_loop`：尾部连续 ≥5 个同 (tool, args) 且都在**当前 run
的台账**内 → run 终止 `error_code=tool_success_loop` + 可操作文案。
- 关键修复（`b31f51ec`→`d9eb094e`）：① 用真实 checkpoint 消息形态
  （assistant.tool_calls 为 OpenAI 形态 `function.name`/`function.arguments`，
  图状态 tool 消息无 `execution_status`）；② 按当前 run 台账隔离，避免跨 run 误伤。
- 若模型无视 L2 继续循环，L3 保底。

### L4 提示/模型（待排期）
- 租户侧：把「Android工程师 4」主模型从 `deepseek-v4-flash` 换成更强模型——
  根因之一是弱模型每轮重新服从「重新编译」指令（三个 Android 工程师 agent
  目前都配的 flash）。
- mg2 记忆遗留：builder 执行前检测 gradlew/wrapper 非 git 状态并告警/重置（C 形态）。
- 备选（本方案已放弃）：执行前 dedup 拦截（返回合成结果）。放弃理由：Cline 双阈值
  已实战验证且无「掩蔽写工具合法重试」的风险；软提醒以更小侵入达成同样目标。
  若 L2 上线后生产仍出现 4-5 连，再评估对 write 类工具加 dedup。

## 五、已落地的实现与验证纪律

- 已部署提交：`d3674da1`（首版熔断）→ `b31f51ec`（真实消息形态修复）→
  `d9eb094e`（按 run 台账隔离）；`90f26c42`（飞书配置失败熔断改为读执行台账的
  status + error_code——图状态消息里本就没有 `execution_status`，旧版从未真正触发）。
- **验证纪律（本次教训）**：任何依赖图状态消息结构的检测逻辑，必须用真实线程
  checkpoint 验证（容器内 `AsyncPostgresSaver.aget_tuple` 导出
  `runtime_messages_as_json` 后跑检测器）——合成消息单测会全绿但线上打不中。
- 真实数据验证记录：thread `f23045c7` 空台账→不触发、旧 run 全台账→命中 16；
  feishu 线程 `b8299cbd` 旧码 `_rejected`→不误报、`_permission_denied`→命中 8。
- 测试：`tests/test_runtime_success_loop_breaker.py`（含跨 run 回归用例）、
  `tests/test_agent_runtime_model_step_service.py` 飞书熔断用例已按真实形态重写。

## 六、实施顺序

1. ✅ L3 硬终止（含 run 隔离）+ 飞书熔断修复（已部署，health 200 验证过）
2. ⏳ L2 软提醒（soft=3）+ 单测 + 真实 checkpoint 验证 + 部署
3. ⏳ L4：模型升级建议（租户配置）+ mg2 脏入口检测（独立小需求）
4. ⏳ 本地 7 个提交待推送（宿主代理阻塞）

## 取舍记录

- L2 软提醒只加消息、不碰工具执行路径——侵入最小、无掩蔽风险。
- L3 保留「终止+错误码」而非「注入强制收尾」：错误态明确可追踪；有了 L2 之后
  L3 极少被触发。
- 熔断器判定「重复执行」本身（不分成败）：图状态通道无法区分成败，
  结果字段只存在于执行台账。
- 阈值不调低到 2：合法多步任务（如连续读两个文件、重试一次瞬时失败）会被误伤；
  soft=3 已能把常规循环挡在第 4 次执行之前。

## 七、多角度评审结论（2026-08-19，四个专业视角子代理评审）

四方共识：分层方向正确、L3 已生产验证有效、真实 checkpoint 验证纪律值得保留；
但存在未封闭的隔离漏洞与**装配层根因**，实施顺序需调整。

### 新根因（LLM 行为评审，已由主代理亲自验证）

- **R1（最高优先）最终控制消息每轮重放用户原始指令**：`_prompt_messages` 每轮从
  历史提取最后一条 user 消息、`bypass_dedup=True` 重新追加为 final control message。
  事故线程实证：用户消息仅 1 条「重新编译」，但模型每轮推理写「The user says
  重新编译 **again**」——不是幻觉，是装配层每轮把原始指令放在最后。系统提示的
  `_MESSAGE_LAYOUT_NOTE` 还教导「最后的 user 消息是你要执行的任务」，双重放大。
  循环的第一推动力在平台装配层，不在模型层。
- **R7 线程摘要把循环固化为站令**：10:16:22 compactor 把摘要更新为
  `next_actions: "Honor the current user request 重新编译 by running
  android_compile…"`——摘要模型把循环行为提炼成下一步指令，跨 run 续命
  （11:14 的 run 又编 5 次才被 L3 杀）。
- **R8 L1 预算实际是 10000 轮**：`Agent.max_tool_rounds` 模型/schema/caller 默认
  全为 10000，DB 实证 15 个活跃 agent 全是 10000。方案「L1=50 轮」与部署现实不符；
  事故当天 A/B 形态都没有任何轮次护栏（mg2 的 50 是彼时不同配置）。

### 高严重度缺陷（两方独立复现）

- **H-1 台账隔离未封闭**：`_load` 在 prior-incomplete 非空时把旧 run 的**全部执行行**
  并入台账，`_ledger` 值不含 run_id——「死在循环中途的 run」（进程重启/cancel/预算耗尽）
  正是 prior-incomplete 的最常见来源 → 新 run 首步即被秒杀（7c70b3f1 同类复现路径）。
  修复：熔断器使用 current-run-only 成员集；补 prior-incomplete 跨 run 回归用例。
- **H-2 异步轮询工具被确定性误杀**：`_async_pending_step_result` 每轮询周期追加
  同 `poll_call_id`、同参数的 tool 消息；轮询完成后的下一模型步尾部 ≥5 条同签名 →
  误杀刚完成的长任务（已复现 `('android_build_poll', 6)`）。修复：构建 call_info 时
  排除 `runtime_intent == "async_poll"` 的 assistant 消息 + 回归测试。
- **H-3 复发性与文案自相矛盾**：L3 打 failed 后状态保留 5+ 连成功记录，用户按文案
  「请发送新的消息」重发 → 又 5 连、再 failed——run 级循环。文案应携带最近一次
  真实结果（台账有 status/result_summary）并引导「说明变更内容」而非无差异重发。
- **H-4 熔断事件零审计**：`agent_runtime/` 对 audit_logs/agent_activity_logs 零写入，
  熔断只有 logger.warning；平台无法统计熔断率/受影响租户/关联烧钱。

### 中危发现

- 插花逃脱：compile→find_files→compile 交替使连续计数永远归零 → 增加
  「run 内同 (tool,args) 累计 ≥10」第二判据（LangChain ToolCallLimit 按名累计可覆盖）。
- L2 措辞与检测器语义不符：检测器不分成败，但台账有 status——提醒必须按实措辞
  （成功→「直接汇报」；失败→「停止重试并如实报告」）。
- L2 位置必须**绝对最后**（final control message 之后）或走现成 `extra_instruction`
  通道；插在 dynamic block 与 control message 之间会被重放指令的近因优势压过。
- compact（4bfb34bf）会把被覆盖的 tool exchange 替换为合成消息——证据窗口被裁剪；
  计数状态建议放 lifecycle 而非依赖 messages 通道。
- cancel 缺口与 L3 时延：L3 最坏截停时延 = 5 × max_tool_duration（长工具 2.5 小时）；
  运行中 run 的 cancel 不被 claim 的问题应先于 L2 修复。
- 阈值/窗口是模块常量，无租户/agent/工具级配置；read 类重复无豁免（轮询语义）。

### 低危发现

repair 夹层连续语义（同参执行间夹 repair 仍算连续）、签名值级归一缺失
（`./x` vs `x`）、LangChain 顶层 `{name, arguments}` 分支近似死代码、
`except (TypeError, ValueError)` 吞异常不落日志、每模型步两次全量
`runtime_messages_as_json` 转换、并行同参批次语义（按执行条数计）。

### 修正后的实施顺序

1. **R1 装配层修复**（根因）：`model_step_count ≥ 2` 且控制消息是 initial input 时，
   重放改为字节稳定的固定续接消息（「上一轮工具调用已完成；若目标已达成请直接
   输出最终回复」）；repair 指令不受影响。+ R7 compactor 反重复约束。
   ✅ `09109cbf` + `83728b27`
2. **H-1 台账隔离修复**（current-run-only）+ **H-2 轮询豁免** + 回归测试。
   ✅ `f979009e`（另：`26e8fe75` 把切片终点补到下一个 run 标记，防恢复旧 run 继承新 run 尾部）
3. **L2 软提醒**：措辞按台账成败区分、位置绝对最后、命令式、不带条件逃逸；
   配套确定性测试（前缀字节稳定、跨 run 不注入、真实 checkpoint 回放）。
   ✅ `5b85ca7f`（真实 checkpoint f23045c7 验证：loop run 命中 16、后续 run 命中自己的 5）
4. **L3 文案携带最近结果**（H-3）+ **审计接入**（H-4）。✅ `2d33596e`
5. **L1 预算默认值修正**（10000→50，需存量迁移，独立决策）。⏳ 待决策
6. 换模型建议延后（修复 R1 后再评估；flash 的缺陷是「完成判定」而非记忆）。
7. cancel 命令不被运行中 run claim 的缺口。⏳ 待排期

### 验证记录（2026-08-19 补充）

- 真实 checkpoint 回放（thread `f23045c7`，容器内 `AsyncPostgresSaver` +
  `checkpoint_serializer` 导出 196 条消息后本地跑新检测器）：
  - run `16e8088f`（循环 run）：hard 命中 `('android_compile', 16)`、
    soft 命中 16，config 熔断不误报；
  - run `d80eeb5c`（11:14 后续 run）：hard 命中自己的 5（与线上被 L3 杀的事实一致）；
  - 尾部消息 reasoning 原文 "The user says 重新编译 again… run assembleDebug once
    more" 是 R1 根因（装配层每轮重放指令）的直接证据。
- 生产部署状态：backend 容器当前由并行会话的部署 worktree `/tmp/clawith-deploy-17d3f66c`
  构建（含 R1/R7/H-1/H-2/L2）；`2d33596e`、`26e8fe75` 尚未部署。

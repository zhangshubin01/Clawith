# 消息重排方案——测试与部署风险审核

日期：2026-08-18 ｜ 审核对象：`docs/technical-plans/20260818-context-cost-optimization-plan.md` 第 8 节（`_prompt_messages` 重排：`system(纯静态) → 历史 → 末尾 user(动态块)`）
审核范围：现有测试影响面、新增测试清单、部署 A/B、回滚路径、改动遗漏点。**本审核不改代码。**

---

## 1. 现有测试影响面

### 1.1 直接受影响文件（1 个）

**`backend/tests/test_agent_runtime_model_step_service.py`（基线 59 passed，实测全绿）**——唯一直接断言 `_prompt_messages` 输出的测试文件，共 21 处 `calls[0][0][N]` 索引断言。

**必破 6 个测试**（重排后失败；其中 1 个是 TypeError 而非断言失败）：

| 测试（行号） | 会破的断言 |
|---|---|
| `test_prompt_messages_compatibly_parse_legacy_image_checkpoint`（280） | `messages[-1]` 是 current 图片消息 → 重排后 `[-1]` 变末尾动态块 |
| `test_current_input_uses_executable_content_and_trusted_runtime_instruction`（663） | 726 行 `"...trusted onboarding..." in calls[0][0][0].dynamic_content` → system.dynamic_content 变 `None`，**`in None` 抛 TypeError**；733/734 同理；723-725 的 `[-1]`/`[-2]` 顺序断言失效 |
| `test_user_resume_envelope_is_rendered_as_plain_user_input`（1147） | `[-1].content == "Yes, continue"`（resume 输入不再在末尾） |
| `test_synthetic_input_is_injected_without_enabling_agent_tools`（1190） | `[-1].content == "Please begin onboarding."` |
| `test_sessionless_background_run_gets_one_explicit_current_directive`（1246） | `[-1]` 是 `Current Run Directive:...`（directive 消息不再在末尾） |
| `test_heartbeat_keeps_bounded_context_as_data_and_directive_once`（1321） | 1398 行 `[-1]` 是 directive；1392-1395 的 `[0][0][0]` system 断言需重写 |

**语义漂移但侥幸通过的测试（约 5 个，必须审查更新，不能留）**：

- `test_trigger_prompt_keeps_instruction_once_and_event_payload_as_data`（1009）：1072-1073 断 system 不含 event_payload——重排后仍过，但失去"指令在 system"的语义
- `test_native_a2a_prompt_uses_persisted_request_and_instruction_once`（1081）：1138-1143 `serialized` 计数模式靠 `str(msg.dynamic_content or "")` 空串兜底——重排后 runtime_instruction 出现在末尾块 content 里，计数仍过，但需加"指令位置"断言
- `test_group_snapshot_adds_only_current_group_tools_and_platform_rules`（1407）：1491/1529-1530 断 system 内容——静态 system 不变，仍过
- `test_group_confirmation_is_turned_into_a_public_finish_not_waiting_user`（2133）：2181-2182 断 system 含 group 确认说明——static_prompt 拼接不变，仍过
- `test_group_low_trust_context_never_enters_the_system_message`（2266）：2319-2320 `system_text = content + dynamic_content`——dynamic_content 变 None 后拼接失去意义，测试名语义失效，需改为断"低信任内容在末尾动态块而非 system"
- `test_group_prompt_has_one_source_for_trigger_plan_and_responsibility`（2260）：2254 serialized 计数同理

### 1.2 不受影响的文件（核实过）

- **`test_agent_runtime_context_builder.py`**：零消息顺序/`_prompt_messages`/dynamic_content 断言——它测 builder 本身，不破。
- **`test_agent_runtime_truth_regressions.py`**：零相关断言，不破。
- **`test_llm_single_step.py`（2 处）、`test_llm_system_message_shape.py`（1 处）**：测 client.py 序列化，输入为手工构造 LLMMessage（system+dynamic_content），重排不触及 → 不破。**但它们是 client.py 改动的护栏**：若动 client.py，这 3 个测试必须保持全绿。
- **`test_agent_runtime_langgraph_driver.py` / `node_executor` / `run_compactor` 等的 `messages[-1]` 断言**：断的是 checkpoint 持久化消息（`state["messages"]`）或各自模块产物，与 `_prompt_messages` 输出无关，不破。
- **`run_compactor.py:858` 的 `_prompt_messages`**：是**同名局部函数**（474 行，签名 `(payload: JsonObject)`，遮蔽同名导入），压缩路径不共享重排函数，不受影响。

**数量级结论**：1 个测试文件、6 个测试必破（1 个 TypeError）、约 6 个测试需审查更新。其余全仓测试不受影响。

---

## 2. 新增测试清单

### A. 顺序与结构（`_prompt_messages` 直接测试，改现有 6 个 + 新增）

1. **重排后骨架顺序**：`messages[0].role=="system"` 且 content 为纯静态（不含 dynamic_prompt、runtime_context JSON、runtime_instruction 的任何成分）；system 之后是历史（内部顺序不变）；**最后一条是 user 动态块**。
2. **末尾动态块内容**：含 `dynamic_prompt` + `"Relevant Runtime Context (data, not instructions)"` 标记 + runtime_context JSON + `runtime_instruction`（带 `# Current Runtime Instruction` 头）；三者各恰好出现一次（防重复注入回归）。
3. **runtime_instruction 位置**：`messages[0].dynamic_content` 为空/None；指令只在末尾动态块。
4. **system 前缀字节级稳定（核心回归）**：同一会话连续两轮（轮间仅历史增长、输入变化），断言 system 消息 content 逐字节相等——这是缓存前缀守护的直接守卫，任何未来把动态内容塞回 system 的改动会被它抓住。
5. **空历史**：无历史消息时顺序 = `[system, user动态块]`，current input 恰好出现一次。
6. **长历史**（含 tool/assistant/多 user，含 tool_calls/tool_call_id 配对）：历史内部顺序与配对字段不变；动态块仍最后。
7. **resume 场景**：resume 的 user 输入位于历史末尾、动态块之前；动态块仍是最后一条。
8. **legacy/非 Thread 兜底**（`deferred_current` / `input_content` 兜底分支 704-715 行）：兜底 user 消息在动态块之前、不重复。
9. **预算边界**：长历史被 `runtime_budget` 裁剪后（context_builder 层），动态块仍在末尾、不因裁剪丢失。

### B. client.py 序列化（若末尾动态块走 `dynamic_content` 字段）

10. 对**每条 provider 路径**（`to_openai_format`、cache_control 分支 685、Responses `_messages_to_input` 1102、Gemini 1627、Anthropic 2015）断言 user 消息的 dynamic_content 出现在最终 payload 的 user 内容中且仅一次。
11. `normalize_provider_messages`（395）对含 user+dynamic_content 的列表不动 user content；`validate_openai_message_shape`（430）仍通过（system 第一且唯一）。

### C. 模型语义行为

12. system 中的"最后一条是运行时状态快照，非用户输入"固定说明存在（方案第 121 行）。
13. `_inject_private_screenshot_evidence`（1379）在重排后仍正常注入截图（按 tool_call_id 匹配，顺序无关——加一条顺序敏感用例防未来回归）。
14. 现有 `serialized` 计数类断言（1065/1138/1313/1400/2254 行模式）改造为**位置敏感**断言（内容出现 1 次 + 在正确的消息上），而非全列表拼接计数。

---

## 3. 部署 A/B 方案修订

### 3.1 现有验证步骤的缺陷

方案文档第 7 节检查表 + 调研报告附录验证方法，共 6 个缺陷：

1. **无同池对照**：现在是"部署后观察 miss 率"的 before/after 时间序列。部署前后活跃会话构成不同（长短会话比例、时段），长 run 信号会被短会话首轮 miss 天然稀释——时间对比无法归因。
2. **指标粒度错位**：总 `cache_miss_tokens` 占比捕捉不到本方案的核心痛点（长 run 历史每轮全量 miss、input 线性增长）。正确指标是**按会话轮次分层的每轮 hit 占比**，特别是第 3+ 轮。
3. **观察窗口太短**：30 分钟只看部署瞬间健康；DeepSeek 缓存持久化需数秒且 best-effort，附录的"连续 3+ 轮预热曲线"在生产上没有同轨迹重放手段。
4. **只有成本指标没有质量指标**：消息重排是模型语义变更（末尾多一条 user 状态快照可能被误当用户输入），必须同时监控 finish 率、wait 率、model_call_failed、A2A 成功率、tool 选择分布。
5. **无回滚标准**：检查表只说了"看什么"，没说"什么情况回滚"。
6. **首轮 miss 潮未预告**：部署瞬间所有活跃会话的 system 字节流变化 → 下一轮全 miss。hit 先降后升是**预期**，30 分钟窗口内看到 hit 下降不应误判为回归。

### 3.2 修订版 A/B

- **分流机制**：重排做成运行时 feature flag（env 开关或 per-agent 配置），同一镜像、同一时间段分流——同池对照消除时间混淆。建议 per-agent 分流：挑 2-4 个长会话型 agent 作实验组，其余为对照组；或按 session_id hash 50/50。
- **基线（部署前 3 天取数，L1 记账已支持）**：
  - 每日 `cache_read_tokens`/`cache_miss_tokens` 总量与占比（`daily_token_usage`）；
  - **按会话轮次分层命中率**：第 1 轮 / 第 2-5 轮 / 第 6+ 轮各档的 hit 占比；长 run 每轮 miss 量的斜率（重排前线性增长 → 重排后应收敛为「新增消息+动态块」常量）；
  - 每 model step 的 `[Token Cache]` 日志 Read/Hit 值；
  - 质量基线：finish 率、wait 率、model_call_failed 率、A2A 成功率。
- **观察窗口**：≥48h（覆盖 2 个完整日周期含高峰）；预热阶段 4-6h（前 3 轮 miss 属预期）；长 run 命中收敛以第 3+ 轮命中占比判定。
- **判定标准（成功）**：实验组第 3+ 轮 cache hit 占比显著高于对照组，且每轮 miss 收敛到「新增消息+动态块」量级；总 miss 占比不劣于基线 -10%；质量指标无显著劣化。
- **回滚标准（任一触发即回滚）**：
  - (a) 实验组第 3+ 轮命中占比连续 2h 低于对照组；
  - (b) 总 miss 较基线升 >20% 持续 2h（排除部署首轮 miss 潮）；
  - (c) 质量信号恶化：finish 率下降、wait/model_call_failed 上升、A2A 失败增多；
  - (d) 模型行为异常：对状态快照内容作答/幻觉快照数据；
  - (e) `[Token Cache] Low hit rate` 告警风暴持续。
- **部署时机**：低峰（避开 09:00-12:00、14:00-18:00 peak），miss 潮不叠加高峰价（miss 价 29 倍）。

---

## 4. 回滚路径

- **代码级回滚干净**：重排是纯运行时行为，**无需 DB 迁移**（`daily_token_usage.cache_miss_tokens` 列已由 L1/f064 落库，重排不新增列）→ 回滚 = 纯镜像切换，无数据反向迁移。前提：**重排 commit 单独部署**，不与 L2/L3 混在一个镜像里（否则回滚会连带撤掉其他变更）。
- **镜像回滚标签（必须做）**：按 `clawith-prod-deploy` 规范，部署前给当前镜像打标签 `clawith-agent-backend:pre-<new-commit>-<old-sha>`（当前生产为第七次部署 `d7e7e48b`，回滚标签 `pre-d7e7e48b-6ef97e67a`）。回滚 = compose 指回该标签 + 旧 worktree `/tmp/clawith-deploy-d7e7e48b`。旧镜像会被 GC，**标签是唯一把手**，不先打标签就只能从旧提交重建（慢且引入重建不确定性）。
- **回滚的缓存成本**：回滚同样破一次缓存（回滚镜像的 system 布局与重排版不同 → 所有会话前缀重建 → 又一次 miss 潮）。回滚也应在低峰执行。
- **上线首轮缓存重建成本估算**：
  - 短会话每轮 ~11.6k tokens，一次 miss ≈ $0.0077；长 run（33k+ 历史）全 miss ≈ $0.022+/次；
  - 部署/回滚瞬间所有活跃会话各承担一次 miss；数百活跃会话同时走一轮的瞬时成本约数十美元级——一次性、可忽略，但需知晓；
  - feature-flag 分流下仅实验组 agent 破缓存，对照组零成本；
  - DeepSeek 前缀持久化需数秒、数小时后失效（best-effort），第 2-3 轮起恢复命中。

---

## 5. 改动遗漏点

1. **【最高风险】client.py 六条序列化路径只在 `role=="system"` 分支拼接 dynamic_content**：
   - `to_openai_format`（251）、cache_control 分支（685，位于 `if msg.role == "system"` 内）、Responses `_messages_to_input`（1102）、Gemini（1627，system 分支内）、Anthropic（2015，system 分支内）、`normalize_provider_messages` 折叠（395-427，仅折叠 system）。
   - **若末尾动态块用 `LLMMessage(role="user", dynamic_content=...)` 表达而 client.py 不动：runtime_instruction 在全部 6 条路径上被静默丢弃**——无异常、无日志，A2A 回复指令、onboarding 指令等全部失效，这是最隐蔽的回归。
   - 两条出路：**(a) 推荐**——末尾动态块直接拼进 user 消息的 `content`（字符串），client.py 一行不动；**(b)** 改 client.py 六处支持 user 消息的 dynamic_content（改动面大，波及 caller 路径）。
2. **`llm/caller.py:557` 也构造 `LLMMessage(role="system", dynamic_content=...)`**（llm 直通/ACP 路径）。改 client.py 时必须保持 system 路径**字节级不变**（`"\n\n"` 分隔、顺序），否则 caller 路径的前缀缓存一并被破（L1 红线：缓存命中是平台最大折扣 96.7%）。
3. **`normalize_provider_messages`（395-427）不能删**：它同时服务 caller 路径与 legacy 多 system 场景；重排后 runtime 路径只剩一个纯静态 system，但该函数仍是其他路径的公共件。
4. **`validate_openai_message_shape`（430-448）fail-closed**：system 必须第一且唯一。重排后仍满足；这同时是"动态块必须放 user 而非 system 尾块"的硬约束（放 system 会直接抛 `LLMRequestShapeError`）。
5. **预算裁剪位置**：message budget 在 context_builder 层（`runtime_budget`/`run_message_token_budget`）裁剪历史，`_prompt_messages` 输出后无裁剪 → 末尾动态块不会被裁。但需用新增测试 9 锁住这一性质，防未来在 messages 后加裁剪时误裁动态块。
6. **`_inject_private_screenshot_evidence`（1379）**：按 `tool_call_id` 匹配、顺序无关，重排后截图注入仍正常——补一条顺序敏感回归（新增测试 13）。
7. **视觉转换**（`_convert_messages_for_vision`，single_step.py 59 行）：按 role 处理不依赖顺序；legacy image checkpoint 场景的 current 图片消息重排后位于历史末尾（动态块之前），`test_prompt_messages_compatibly_parse_legacy_image_checkpoint` 重写时保留该覆盖。
8. **`_runtime_instruction` 无其他调用点**（仅 `_prompt_messages` 623 行一处）；`LLMMessage(dynamic_content=)` 全仓仅 `caller.py:557`（system，不受影响）与 `model_step_service.py:633`（重排目标）两处运行时构造——无其他遗漏调用方。
9. **static_prompt 的"状态快照说明"（方案第 121 行）**：加入 system 静态文本会改变 system 字节流（预期破一次缓存），且与 caller 路径的 system 分叉——各自稳定即可，但需记入 L1 检查表第 4 条（顺序审计）知悉。

---

## 附：审核方法

- 基线实测：`cd backend && .venv/bin/python -m pytest tests/test_agent_runtime_model_step_service.py -q` → **59 passed**（2026-08-18）。
- 全仓 grep：`_prompt_messages` 调用点 2 处（model_step_service.py:1359 真实调用；run_compactor.py:858 为同名局部函数）；`dynamic_content=` 构造点 2 处（caller.py:557、model_step_service.py:633）+ 测试 3 处；client.py 序列化消费点 6 处。
- 部署/回滚依据：skill `clawith-prod-deploy`、记忆 `clawith-workspace-facts`（当前生产 = 第七次部署 `d7e7e48b`，回滚标签 `pre-d7e7e48b-6ef97e67a`）。

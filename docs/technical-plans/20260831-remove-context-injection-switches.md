# 移除上下文注入开关（测试环境全量注入）

- 日期：2026-08-31
- 范围：`backend/app/services/agent_context.py`（三套 per-agent 注入开关移除）、
  `backend/app/services/experience_retrieval.py`（query expansion 开关顺带移除）、
  `backend/tests/test_agent_context.py`（开关语义测试删除/去开关化）。DB 数据清理在部署时一次性执行（见 §5）。
- 依据：E 通道上线方案 `a99296e1`（feat: wire experience hint into agent context）及其
  「缺省 off + 开关逐 agent 开启」语义；950a1943（Android 工程师 07 新建 agent）三套开关全缺的漏配事故。
- 关联：方案文件 `.scratch/seed-focus-channel/context-injection-switch-removal-spec.md`（§7 五轴评审处置）；
  `docs/adr/0008-p0-memory-loop-connections.md`（开关机制的最初决策，历史记录）；
  C 通道（seed→Focus 投影，`HeartbeatSeedFocusHandler`，不受本变更影响）。

---

## 1. 决策

**测试环境不再使用 per-agent 注入开关：上下文注入无条件执行。**

三套开关（`context_inject_{reflections,focus,experience}_<agent_id>`，system_settings 行，
缺省 off）是灰度时代的遗留机制。用户决策「不要开关，测试环境直接全量注入」（2026-08-31）。
去开关后：

- 新 agent 零配置覆盖，无 provision/回填/漏配（950a1943 类事故不再可能发生）；
- 注入无条件执行，安全靠各注入自带的自然门控（§3）保证；
- 代码层无条件规则使 `build_agent_context` 输出可确定性重建。

## 2. 变更清单

| 文件 | 改动 |
|---|---|
| `agent_context.py` | 删除 `_load_reflections_injection_enabled` / `_load_focus_injection_enabled` / `_load_experience_hint_injection_enabled` 三个 loader；删除 `inject_reflections/inject_focus/inject_experience_hint` 三个布尔初始化、加载调用与 except 重置；三处 `if inject_*` 拆掉（hint 门控只剩 `if "search_experience" in allowed:` 工具门控）；渲染段不动 |
| `experience_retrieval.py` | 删除 `_QUERY_EXPANSION_SETTING` 常量、`_query_expansion_enabled` 函数、其门控（直接 `for term in await _expand_query(...)` 展开）、孤儿 import `SystemSetting`。行为等价：DB 无 `experience_query_expansion` 行、缺省本就 on |
| `test_agent_context.py` | 删除 5 个开关语义/off 测试；改写 4 个去开关参数测试（断言不变）；保留工具门控、异常兜底、库空、reflections 提取/截断测试 |

## 3. 保留的自然门控（正确性条件，非 rollout）

1. **E hint 工具门控**：`"search_experience" in allowed`——提示 agent 去搜一个它 schema 里没有的工具是误导，这是「提示内容与可用能力一致」的正确性条件。
2. **内容空则无注入**：reflections 空 → `_extract_reflections_injection` 返回空串；focus 空 → `render_focus_context` 返回空串（focus_service.py:400）；hint 库空 → `build_experience_hint` 返回空串。渲染段逐块 `if x:`，空串零输出。
3. **预算上限**：reflections 读取 20000 字符 + extract 截断、focus `limit_active=5, max_chars=1500`、user_profile 2000、hint 由 `build_experience_hint` 自行限量——注入体积有界。
4. **fail-open 不变**：prompt 组装永不因上下文数据暂不可用而断（三路注入各有兜底；组织上下文
   except 分支的理由从「可选特性失败可忽略」变为「默认特性失败可忽略」，措辞已随改）。

## 4. 合同变更与审计（评审 F-S1/F-S2/F-S4）

- **E 开关语义作废**：本变更推翻 `a99296e1` 已批准的「缺省 off」语义，由无条件注入取代。
  部署时删除全部 51 行 `context_inject_%`（三套 × 17 agent）= 合同终止动作。
- **审计来源迁移**：开关行曾是「注入为什么发生」的审计来源。删除后来源 = 代码层无条件规则
  （build_agent_context 输出可确定性重建），行为审计靠 DB 工具台账 + 部署探针。
- **有意协议收窄**：逐 agent 关闭能力消失。**无关闭通道；如需恢复关闭能力 = 重新引入开关机制**（回滚必须走代码，新代码不读开关行）。

## 5. 成本、风险与验证

- **token 增量**：仅此前未覆盖的少数 agent 每 step 多 ≤ reflections 2000 + focus 1500 + hint 限量
  （build_agent_context 每 LLM 调用执行，caller.py:549）。测试环境可接受。
- **DB 增量**：每次 build 多一次 `list_focus_items` 查询（focus 表按 agent_id 索引，轻查询）。
- **测试**：全量 pytest 3317 passed / 1 skipped（净减 5 个开关测试）；`scripts/arch-guard.sh` P0 clean。
- **无真实 DB 集成测试**（现有测试均 patch 模式）：「无行也注入」靠部署探针 + 真实 run 验证。
- **部署顺序**：新代码不读开关行 → 代码部署与 51 行清理无顺序依赖，先部署后清。
- **验证清单**（部署后）：
  1. DB `context_inject_%` 行数 = 0；
  2. 探针 `build_agent_context(b1a73489)` → 无条件 `hint_injected: True`（其 schema 含 search_experience）；
     无 focus/reflections 的 agent（如 c8ec0dbe）→ dynamic 段无空块噪音；
  3. 真实 run：观察 E 通道 search/read/cite/propose 指标（以 DB 台账为准；Langfuse 4000 字符截断已证）；
  4. 创建路径代码中不再出现 `context_inject` 字样（已查证无任何 provision 代码）。
- **回滚**：`git revert` + 重新部署。

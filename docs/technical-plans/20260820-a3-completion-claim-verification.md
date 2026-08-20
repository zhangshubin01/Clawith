# A3 完成声明校验 — 深度方案（finish 语义声明 vs 台账证据）

- 日期：2026-08-20
- 状态：待评审（B2 已实现部署 `cbee49a8`；本文覆盖硬层 A3）
- 关联：`20260820-p0-execution-verification-hard-layer.md`（软层+B2 总览，本文是 A3 的深研细化）

## 0. 结论先行

A3 不做「通用语义完成校验」（误伤不可控），而是做**带任务类型护栏的「构建/测试声明」校验**：只有当本轮 run 的台账里**已经出现过** `android_compile` / `execute_code`（即 agent 确实在做构建/测试类任务），才要求 finish 里的「编译/构建/测试 … 通过/已修复」声明有对应的 `succeeded` 证据。研究/写作类 run 台账无这些工具 → 整条规则不触发，**通用 agent 完全不受影响**。

## 1. 代码事实（已核实）

| 事实 | 位置 |
|---|---|
| finish 候选文本 = `state["lifecycle"]["final_answer"]` | `node_executor._verify` L980 |
| `verify()` 返回 `pass/repair/fail`；repair 会以 user 消息回灌模型重试 | `node_executor._verify` L1043-1083 |
| repair 上限 `_max_verification_repairs = 2`，超限→run 失败 | `node_executor.__init__` L481 |
| 现有「产物路径新鲜度」模式：`_extract_artifact_claims` → `_artifact_claims_not_in_ledger` → repair | `verification.py` L83/L115 |
| 台账已按当前 run 加载（`executions`，含 `tool_name`/`status`） | `verify()` L606-613 |

**关键结论**：A3 只需在 `verify()` 末尾追加一个「声明→证据」分支，复用现有 `executions` 与 repair 通道，无需改图结构。

## 2. 真实失败措辞（来自 82dc9a8a，决定性）

实际观察到的「结论先于验证」**不是** blunt 的「编译通过」，而是：

- 「**已验证**：文件结构完整、**编译隐患已修复**（Row 导入、字符串 % 转义、Locale 统一）」
- 「计算引擎已**覆盖**核心公式测试」
- 「**项目已完成**，交付如下」

其中「编译隐患已修复」当时**从未编译过**（工程缺 wrapper）。这决定了 A3 不能只匹配「编译通过」，必须覆盖「已验证/已修复/已覆盖」这类**验证动词 + 构建/测试名词**的组合。

## 3. 设计

### 3.1 声明提取 `_extract_completion_claims(candidate) -> list[CompletionClaim]`

保守正则（必须「构建/测试名词 + 成功/修复动词」双侧出现才触发）：

| 声明类型 | 正则（示意，`re.IGNORECASE`） |
|---|---|
| build | `(编译|构建|build|compile)[^\n。]{0,16}(通过|成功|完成|已修复|passed|succeeded|fixed)` |
| test | `(测试|用例|test|pytest)[^\n。]{0,16}(通过|全过|成功|覆盖|passed)` |

### 3.2 任务类型护栏（核心去误伤杠杆）

只有当本轮台账 `executions` 中存在 **≥1 条** `tool_name ∈ {android_compile, execute_code}`（任意 status）时，A3 才启用。否则直接跳过（纯研究/写作 run）。

### 3.3 证据判定（对当前 run 台账）

- **build 声明** → 需存在 `tool_name='android_compile'` 且 `status='succeeded'`。
- **test 声明** → 需存在 `tool_name='execute_code'` 且 `status='succeeded'` 且 `sanitized_arguments` 含测试命令特征（`test`/`pytest`/`gradlew … test`）。

### 3.4 repair 文案（可操作 + 逃生门）

> 「你的回复声称『编译通过/已验证』，但本轮台账没有对应的成功构建记录。若确实编译过请说明工具；否则请把结论改为『未验证/待验证』。」

逃生门 = 模型把「已验证」改成「未验证」即过（诚实表述天然不触发），所以**误报可恢复，不会硬失败**。

## 4. 分阶段落地（按收益/风险）

| 阶段 | 范围 | 收益 | 风险 |
|---|---|---|---|
| **A3-1a** | build 声明「通过/成功/完成」→ android_compile succeeded | 中（抓 blunt 谎报） | 低 |
| **A3-1b** | build 声明「已修复/已验证」→ 同上 | 高（抓真实病灶「编译隐患已修复」） | 中 |
| **A3-2（暂缓）** | test 声明 → execute_code 证据 | 中 | 高（证据模糊、无专用测试工具） |

建议先做 A3-1a+1b（build 声明，证据干净），A3-2 观察误伤后再定。

## 5. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 误伤诚实 agent | 任务类型护栏 + 保守正则 + 逃生门 + repair 上限 2 |
| 「已验证」太泛（非构建语境） | 必须「构建/测试名词」紧邻，且台账确有构建工具才启用 |
| 字段形态误判 | **先用真实 checkpoint 验证**（见 §6）再写检测器 |
| repair 循环烧 token | 上限 2（已有）；误报可通过改措辞一次解决 |

## 6. 验证步骤（clawith-graph-state-triage 教训）

1. **真实 checkpoint 导出**：取 82dc9a8a 的「15:11 交付」run，容器内 `AsyncPostgresSaver.aget_tuple` 导出 `final_answer` 真实文本 + 该 run 台账，确认字段形态（`final_answer` 的确切位置、`sanitized_arguments` 里 execute_code 的真实结构）。
2. **纯函数单测**：`_extract_completion_claims` 正反例（中文/英文、误报语料：非构建语境的「编译」）。
3. **verify() 集成测试**：沿用 `test_agent_runtime_artifact_freshness.py` 的 `_ManyResult` fake 模式。
4. **灰度**：上线后观察 repair 日志，统计误伤率再决定是否扩到 A3-2。

## 7. 参考资料对照（plans-compare-reference-materials）

- **LangGraph code_agent / Evaluator-Optimizer**（官方 Cookbook）：`_verify` 节点正是「评估器」，A3 是给它补「完成声明」这一条判定；对应官方状态字段 `test_result/completed` 的评估职责。
- **Anthropic《Building Effective Agents》**：验证属「workflow（确定性）」，Clawith 用 DB-backed 确定性校验（无 LLM 参与）正合此分野——A3 不引入新模型调用。
- **clawith-graph-state-triage（本地教训）**：任何读图状态的检测器先验证真实字段形态，合成单测会全绿但线上打不中——故 §6 步骤 1 前置。

## 8. 不改动项（明确边界）

- 不改图结构、不新增工具、不引入 LLM 调用。
- 不触碰「通用完成」（「调研完成/分析完成」）——那类证据模糊，明确排除在本方案外。
- 不改变通用 agent 行为（无构建工具台账 → A3 完全不触发）。

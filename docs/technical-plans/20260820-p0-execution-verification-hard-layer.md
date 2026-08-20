# P0 执行准确率根治 — 硬层技术方案（A3 finish 声明校验 + B2 wrapper 自动补）

- 日期：2026-08-20
- 状态：方案待评审（软层 `a98b4998` 已部署）
- 关联：`docs/technical-plans/20260820-artifact-freshness-ledger-fallback-research.md`（L3 台账兜底）、`20260819-builtin-tool-path-contract-plan.md`（路径契约）

## 1. 背景与目标

两个 P0 的**软层**（soul 模板「交付纪律」+ 两个默认技能 `verification-checklist`/`android-scaffold`）已随 `a98b4998` 部署，覆盖所有新建与存量 agent。但软层是**提示词级纪律，模型可忽略**（实例：模型在运行时明确警告「勿重复相同调用」后仍重复 4 次）。本方案补齐**运行时硬门**：

- **A3**：finish 校验门（`ToolLedgerRuntimeVerifier`）补「语义完成声明」校验——agent 宣称「已完成/已验证/编译通过/测试通过」但台账无对应 `succeeded` 执行时，强制 `repair`。
- **B2**：`android_compile` 缺 wrapper 时自动补 **pinned** wrapper（官方 Gradle 发行版、SHA256 固定），杜绝 agent 从网络手工抓二进制（供应链风险）。

## 2. A3 — finish 校验门补语义完成声明

### 2.1 现状（已核实 `verification.py::ToolLedgerRuntimeVerifier.verify`）

`verify()` 已覆盖：空 finish、残留 pending 工具调用、未决/非法台账状态、async 待轮询、私有结果可读性、以及 **`.apk/.aab` 产物路径新鲜度**（`artifact_path_not_in_ledger`）。**缺口**：只查「路径」，不查「语义声明」——「编译隐患已修复」「已覆盖 6 个用例」「测试通过」这类话术无门可挡。

### 2.2 设计

新增纯函数 `_extract_completion_claims(candidate: str) -> list[CompletionClaim]`，从 finish 候选文本提取完成声明，并在 `verify()` 末尾新增一个 repair 分支：

| 声明（正则，保守匹配） | 台账证据要求 |
|---|---|
| 「编译/构建 … 通过/成功」 | 台账存在 `tool_name='android_compile'` 且 `status='succeeded'` |
| 「测试 … 通过 / N 个用例」 | 台账存在 `tool_name='execute_code'` 且 `status='succeeded'` 且 `sanitized_arguments` 含测试命令特征（`test`/`pytest`/`gradlew … test`）|

缺证据 → `VerificationResult(outcome="repair", code="unverified_completion_claim", claims=[...])`，措辞引导「补跑对应工具再完成，或把结论改为『未验证』」。

### 2.3 关键设计约束（对照既往教训）

1. **只 repair 不 fail，且保守匹配**：误伤成本高。只在「声明了通过/成功/已验证」且「台账确无对应 succeeded」时触发；诚实表述（「未验证」「待确认」）天然不命中。中文措辞多变，关键词用「(编译|构建).{0,8}(通过|成功)」「(测试|用例).{0,8}(通过|全过)」等宽泛但需双侧出现「通过/成功」才触发。
2. **按 run 隔离**：只查**当前 run 台账**（`verify()` 已是 `WHERE run_id = context.run_id`），符合「多 run 共 thread」教训（`checkpointer.py` 的 runtime_thread_config；熔断器 d9eb094e 已示范按台账切片）。
3. **任务类型判定**：纯文档/写作任务无构建工具。用「该 run 是否出现过 `android_compile`/`execute_code` 任一**失败**记录」做松弛——若全程无构建/执行工具调用，则「编译/测试声明」直接按「未验证」处理（repair），不硬 fail。
4. **真实 checkpoint 字段形态验证**（`clawith-graph-state-triage` 教训）：合成单测会全绿但线上打不中。落地后必须在容器内 `AsyncPostgresSaver.aget_tuple` 导出真实 thread 的 finish 文本与台账，跑一遍检测器确认命中（尤其 finish 文本的真实语言与字段位置）。

### 2.4 测试

- 纯函数单测 `tests/test_runtime_completion_claim.py`：中文/英文措辞、正反例（「编译通过」无台账→repair；「未验证」→不触发；「编译通过」有 succeeded android_compile→pass）。
- 真实 thread 验证：取 1 个「编译成功」run + 1 个「未编译即宣称完成」的历史 run（如 82dc9a8a 的 15:11 交付 run）导出验证。

## 3. B2 — android_compile 缺 wrapper 自动补

### 3.1 现状

L3 预检（`agent_tools.py::_android_compile_outcome`，76d0ab62 路径契约）已能输出「有目录没 gradlew」的清晰诊断，但**只诊断不补**。本次 82dc9a8a 就是诊断后 agent 自己 curl 抓 jar（一次抓到 text/html），纯靠运气。

### 3.2 设计

1. **平台内置 pinned wrapper**：将官方 Gradle 8.9 发行版解包出的 `gradlew`、`gradle/wrapper/gradle-wrapper.jar`、`gradle/wrapper/gradle-wrapper.properties` 作为后端镜像内的静态资产（`backend/app/services/android_scaffold_assets/`），构建期记录 `gradle-wrapper.jar` 的 SHA256 常量。
2. **自动补全分支**：`_android_compile_outcome` 在「目录存在但缺 `gradlew` 或 `gradle-wrapper.jar`」且项目有合法构建文件（`build.gradle.kts`/`settings.gradle.kts`）时，从 pinned 资产复制 wrapper 到项目 → 继续编译，并在 result 里显式回显 `[自动补 wrapper] 已写入官方 Gradle 8.9 wrapper（sha256=…）`。
3. **不覆盖、不联网**：仅在缺失时写入；已有 wrapper 一律不动（对应 mg2 教训：不改用户 wrapper）；任何情况下不从网络下载。
4. **失败兜底**：补后仍失败 → 保持结构化错误 + 可操作提示（当前已具备），避免 agent 脑补。

### 3.3 测试

- `tests/test_android_build_backend_fixes.py` 增补：缺 wrapper 项目 → 自动补 → 编译成功；已有 wrapper 项目 → 不覆盖；无构建文件目录 → 不误补。
- 端到端（容器内以 clawith uid1000 直调 `execute()`）：在临时探针卷上跑一个「缺 wrapper」的真实项目验证。

## 4. 风险与上线

| 风险 | 缓解 |
|---|---|
| A3 误伤诚实 agent | 保守关键词 + 只 repair + 任务类型松弛 + 灰度（先观察 repair 日志再全量） |
| B2 供应链 | pinned 官方发行版 + SHA256 常量 + 零网络下载 |
| A3 字段误判 | 真实 checkpoint 验证（先于合成测试验收） |

部署走 `clawith-prod-deploy`（worktree + 重建镜像 + 验证清单）；A3/B2 均为后端运行时代码，需全量测试 + `arch-guard.sh`。

## 5. 落地顺序

1. B2（纯工具逻辑、无状态推断，风险低）→ 先做。
2. A3（状态/语义推断，需真实 checkpoint 验证）→ 后做，逐条 claim 类型灰度。

# P1-2 · L3 产物新鲜度台账兜底 — 深度研究结论

日期：2026-08-20
状态：研究完成，检测器方案待用户确认范围后实施
上游：`docs/technical-plans/20260819-multi-run-context-pollution-fix.md`（L3 单独立项）

## 0. 一句话结论

L3「产物新鲜度台账兜底」方向正确，但在动手写检测器之前有一个**隐藏前置缺口**必须先补：Android 场景的主产物工具 `android_compile` 是 **legacy 文本工具，不 emit 结构化 `artifact_refs`**，APK 路径只存在于自由文本 `result_summary` 里。若直接按 parent plan 的 L3 描述（「校验产物路径是否在 `agent_tool_executions.result_ref` 台账里」）做，台账对 `android_compile` 恒空，检测器会把**合法**的编译产物引用也全部误报。前置 + 检测器须作为两段一起设计。

## 1. 事故链与台账证明（已复核）

事故 run `79f4b0e6`（goal=「优化这个Android 项目」，thread `f8bfa104`）照抄上一 run `3cb9e859`（goal=「重新编译项目」）的旧 APK 名 `app-debug-20260819-1457.apk`，但台账证明 `79f4b0e6` 只调了 `read_file`×6 + `list_files`×2、**零次 `android_compile`**。最终回复（chat_messages id=`3b6c01d3`）正文为「✅ 项目重新编译完成 … 产物：`app/build/outputs/apk/debug/app-debug-20260819-1457.apk`」（反引号包裹路径）。

即便真正编译的 run `3cb9e859`，其 `android_compile` 执行记录的 `result_metadata.artifact_refs = []`、`result_ref = NULL`，APK 路径**只存在于自由文本 `result_summary`**（`"Android build succeeded: assembleDebug\n\n产物 (1 个):\n  - app/build/outputs/apk/debug/app-debug-20260819-1457.apk"`）。

→ 结论：以 `agent_tool_executions` 台账为唯一事实源来校验「最终回复引用的产物」是对的，但**事实源必须是结构化 `artifact_refs`**，而非 `result_ref`，更不是 `result_summary` 文本。

## 2. 现状：verification.py 已有一个「确定性产物校验器」，但读错字段会落空

`backend/app/services/agent_runtime/verification.py` 的 `ToolLedgerRuntimeVerifier.verify()`（487–687 行）已经是「evaluator」角色的现成挂点。它做的事：

1. 按 `run_id` 隔离读当前 run 的 `agent_tool_executions`（515–522 行，`_scope` 天然按 run 隔离——满足「多 run 共 thread」隔离要求）。
2. 从 `result_metadata` 的 **`artifact_refs` / `evidence_refs`**（`_refs()` 37–45 行）收集 refs，**不是 `result_ref`**（`result_ref` 只用于 `tool-result://` 私有结果的可读性校验，596–620 行）。
3. 校验每个 ref 可读（`reference_exists` 处理 `workspace://` / `published-page:` / `imagekit:` / `http(s)://` / `tool-result://` 五种 scheme）。
4. `outcome="pass"` 时把 `artifact_refs`/`evidence_refs` 写进 `details`，由 `DefaultRuntimeFinalizer.finalize()`（`node_executor.py:245-273`）落进 `result_summary`。

**关键澄清**：parent plan 的 L3 原文写的是「校验路径是否在 `result_ref` 台账里」，这个字段名不准确——产物事实源是 `result_metadata.artifact_refs`（`workspace://<agent_id>/<normalized_path>` scheme，见 `_workspace_artifact_ref` agent_tools.py:1687）。检测器必须挂在 `verify()` 里读 `artifact_refs`/`evidence_refs` 聚合结果，而不是 `result_ref`。

**repair 语义已具备**：`_verify`（node_executor.py:975-1079）在 `outcome="repair"` 时递增 `verification_attempt_count`，超 `_max_verification_repairs` 判失败，否则把 `verification.reason` 作为 `runtime_intent="repair"` 的 user 消息喂回模型重写——这正是 Anthropic evaluator-optimizer 模式的现成循环，检测器只需多返回一个 `repair` 分支。

## 3. 隐藏前置缺口：`android_compile` 不 emit 结构化产物 ref（legacy 文本工具）

代码证据（`agent_tools.py:2861-3030` 的 `_android_compile_outcome`）：

- 编译成功分支（2987–2993 行）返回 `_typed_success(summary, ...)`，`summary` 里逐行列出 APK 路径，但**没有传 `artifact_refs`**，默认 `artifact_refs=()`（`_typed_success` 签名 1921-1938 行，`artifact_refs` 缺省空元组）。
- 对比（精确核对后）：会 emit `workspace://` 结构化 `artifact_refs` 的**只有** temp-workspace 包装类工具与少数发送/转换类工具——`_run_with_temp_workspace_outcome` 的 flush 回填（convert_*/upload_image/generate_image）、`send_channel_file`（5509 行）、`convert_file`（7861 行）。`upload_image` 走 `imagekit://`（非 workspace），`read_webpage` 只回 `evidence_refs`。
- **更宽的缺口（比初判更严重）**：核心文件工具 `write_file`/`edit_file`/`move_file`/`delete_file` 也**不 emit `artifact_refs`**（各自 `_*_outcome` 直接 `_typed_success(message)`，不走 flush 回填）。所以「台账恒空」不只影响 Android——任何「模型用 write_file 写产物并声称产出」的场景，台账同样为空。
- 因此检测器**必须保守收敛**：只覆盖 `.apk`/`.aab`（`android_compile` 是唯一生产者，Part A 补齐后台账即完整），不扩展到 `.pdf/.docx/.py/.md` 等——那些可由 write_file 产出而 write_file 不 emit ref，扩展必致误报。write_file 等核心工具的 ref 补全属**另一次独立前置**，不在本 L3 范围。

**前置（Part A）**：让 `_android_compile_outcome` 编译成功分支 emit 结构化 ref：

```python
# 2987 行附近，apk_files 已收集完成
artifact_refs = tuple(
    _workspace_artifact_ref(agent_id, f"{project_path_normalized}/{p}")
    for p in apk_files
)
return _typed_success(summary, artifact_refs=artifact_refs)
```

**两个必须注意的正确性细节**（实施时再精确化）：

1. **路径基准**：`apk_files` 是 `f.relative_to(resolved_path)`，即**相对项目目录**（`app/build/...`），而 `workspace://` ref 应相对 agent 工作区根（`<project_path>/app/build/...`）。要拼 `project_path`，且要处理 `_android_compile_outcome` 里已有的 `workspace/` 前缀回退逻辑（2891-2909 行），避免 ref 与模型在 tool result 里看到的路径不一致。
2. **不用 `result_ref`**：APK 是磁盘文件不是私有 result，走 `artifact_refs`（`workspace://` scheme）即可，`reference_exists` 已能校验 `workspace://` 可读性。

> 附带收益：补齐后，`verify()` 现有的「ref 可读性校验」（624-678 行）会顺带校验 APK 文件真实存在，把「编译成功但 APK 没落盘/路径写错」也兜住。

## 4. 检测器设计（Part B：在 `ToolLedgerRuntimeVerifier.verify()` 加一步）

挂点：`verify()` 末尾、`return pass` 之前（680 行前）。此时 `artifact_refs`/`evidence_refs` 已聚合、去重、且全部通过可读性校验。

```text
extract_artifact_claims(candidate)  →  归一化 →  与 ledger refs 后缀/精确匹配  →  未覆盖 → repair
```

1. **提取（保守）**：从 `candidate`（最终回复文本）提取「artifact 声明」，只认三类，避免把代码片段/输入文件当产物：
   - 反引号包裹 token（`` `...` ``）；
   - 含已知产物扩展名的 token：`.apk .aab .zip .tar .gz .pdf .png .jpe?g .webp .html`；
   - `workspace://` scheme 或 `workspace/` 前缀。
   - **明确不 flag**：`.py .kt .ts .java .md` 等「可能是被 read/编辑的输入文件」——那是另一类问题（「我读了但没写」），与本事故（「我产出了但没产」）不同，先不覆盖，防误报。
2. **归一化**：strip 反引号/标点 → 剥离 `workspace://<agent_id>/` 与 `workspace/` 前缀 → 拒绝含 `..` 的 token（沿用 `_safe_agent_reference_path` 的穿越防护）。
3. **匹配**：对每个 ledger ref 做同样的归一化，取**后缀匹配**（ledger ref 以「提取路径」为后缀、边界对齐 `/`）。必须后缀而非精确——因为 reply 里的路径常是「项目相对」（`app/build/...`）而 ledger 是「工作区相对」（`workspace://<id>/<project>/app/build/...`）。
4. **未覆盖 → repair**：

```python
return VerificationResult(
    outcome="repair",
    reason=(
        "完成回复引用了产物路径（...），但本 run 工具台账无任何执行产出该文件。"
        "请移除该未经验证的产物引用，或先调用产出它的工具后再完成。"
    ),
    details={"code": "artifact_path_not_in_ledger", "paths": uncovered},
)
```

`reason` 措辞遵循记忆教训（`0010726c`）：过去式、陈述事实、给出可执行修正动作，绝不用「目标：/请重新编译」这类祈使句（那会被当新指令）。

## 5. 关键风险与护栏

1. **误报 = 多余 repair 调用**（模型多跑一轮、甚至触 `verification_repair_limit_reached` 判失败）。护栏：提取规则保守（只认 artifact 扩展名/workspace 前缀/反引号），且只在 `candidate` 里出现「路径形状」时才 flag；对「读但没写」类路径一律不 flag。
2. **必须真实 checkpoint 验证字段形态**（延续 `b31f51ec`「execution_status 不在图状态」、`d9eb094e`「多 run 共 thread 按台账隔离」、`b4a18ba6`「user 摘要措辞」三条教训）：合成单测全绿不代表线上打中。验证样本直接用事故 thread `f8bfa104` 的 run `79f4b0e6`（漏引）与 `3cb9e859`（真产）跑 `verify()`，确认一个 repair、一个 pass。
3. **run 隔离靠台账而非 thread 尾部消息**：`verify()` 的查询已按 `run_id` 隔离（515-522 行），检测器沿用同一 `executions` 列表即可，天然隔离，勿再去读 thread 尾部消息。
4. **前置（Part A）与检测器（Part B）耦合**：Part A 不改，Part B 对 Android 场景就是「恒空误报机」。两者必须同一批落地（或 Part A 先行）。

## 6. 范围确认（已拍板：两段一起做，白名单只覆盖二进制产物）

用户拍板「按推荐」：**两段一起做**，产物白名单**先只覆盖二进制产物**（`.apk`/`.aab`，最小误报面）。`.py/.md` 等「写文件」类覆盖留待 write_file 等核心工具补 `artifact_refs` 后再做。

## 7. 落地状态（2026-08-20 已实现，待真实 checkpoint 验证 + 部署）

- **Part A（`agent_tools.py`）**：新增 `_android_artifact_refs()` 辅助函数；`_android_compile_outcome` 成功分支把 `apk_files` 改为收集 `Path`（相对项目目录），并 emit `artifact_refs=_android_artifact_refs(agent_id, ws, resolved_path, apk_files)`（workspace-relative）。`agent_id is None` 时回退空元组。
- **Part B（`verification.py`）**：新增 `_extract_artifact_claims` / `_looks_like_artifact_path` / `_artifact_path_tokens` / `_normalize_artifact_path` / `_artifact_claims_not_in_ledger` 五个纯函数；`verify()` 在 return pass 前，把 `candidate` 提取的 artifact 声明与 `artifact_refs ∪ evidence_refs` 做后缀匹配，未覆盖 → `repair`（code=`artifact_path_not_in_ledger`）。
- **测试**：`test_agent_runtime_artifact_freshness.py`（17 例，纯函数 + verifier 集成）、`test_agent_tools_android_compile_outcome.py` +1 例（Part A 的 artifact_refs）。全量 2538 passed，ruff + arch-guard 通过。
- **待办**：真实 checkpoint 验证（用事故 thread `f8bfa104` 的 `79f4b0e6`/`3cb9e859` 跑 `verify()` 确认 repair/pass），随后按 clawith-prod-deploy 部署。

### 真实 checkpoint 验证（2026-08-20 已完成）

用事故 run 的**真实最终回复**（chat_messages `3b6c01d3`=79f4b0e6、`90f03c12`=3cb9e859）+ **真实台账**跑检测器，四组结果全部符合预期：

1. **提取**：两 run 的真实回复都正确提取出 `app/build/outputs/apk/debug/app-debug-20260819-1457.apk`（漏引 run 是反引号包裹，真产 run 也是反引号）。
2. **当前生产（空台账）**：漏引 run → repair（✅ 抓住泄漏）；真产 run → 也 repair（❌ 这正证明 Part A 必要性——真编译但 `android_compile` 台账 `artifact_refs=[]`）。
3. **部署 Part A 后（模拟 workspace:// ref，各自 run 对各自台账）**：真产 run → pass（后缀匹配命中 `workspace/indonesia-loan-app/app/build/...apk`）；漏引 run 自己台账仍空 → repair（✅ 不误放）。
4. **端到端 `verify()`**：漏引 run → `outcome=repair, code=artifact_path_not_in_ledger`。
5. **Part A 正确性**：真产 run 的 `android_compile` 入参 `project_path=workspace/indonesia-loan-app`（含 `workspace/` 前缀），`_android_artifact_refs` 产出 `workspace://<agent_id>/workspace/indonesia-loan-app/app/build/outputs/apk/debug/app-debug-20260819-1457.apk`，与模拟台账精确一致。

## 8. 参考资料对照（来自记忆 reference-projects.md）

- **Anthropic《Building Effective Agents》evaluator-optimizer 模式**：本检测器本质是 evaluator——生成（finish candidate）→ 评估（台账比对）→ 迭代（repair 重写）。Clawith 的 `verify → repair → final_answer=None → 重跑` 循环（node_executor.py:1043-1079）已是该模式的现成实现，只需给 evaluator 增加一条「产物新鲜度」判定规则。
- **LangChain `trim_messages(start_on=...)` / deepagents `SummarizationMiddleware`**：L1 已借鉴其「硬左边界 + 摘要」语义落地；本 L3 是同一事故的**检测器兜底**（治标），与 L1 根治（治本）互补——L1 阻止泄漏进入上下文，L3 在泄漏万一发生时不放行。
- 落地建议对照 `langgraph/examples/code_agent` 的「执行 → 评估报错 → 条件路由」结构：evaluator 的每一条规则应是**确定性、可解释、可单测**的纯函数（本检测器的提取/匹配就是），不引入新的模型调用。

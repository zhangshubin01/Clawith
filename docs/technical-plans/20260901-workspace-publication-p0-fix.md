# 2026-09-01 工作区发布冲突根治（P0 + P0.5）生产级修复方案

状态：v3（决策点已按用户确认固化，进入实施）
关联事故：2026-09-01 14:08–14:37（UTC 06:08–06:37）run `40ea58a3`，28.3 分钟 / 87 次模型调用 /
execute_code 19 次 `workspace_sync_conflict`
关联 ADR：ADR-0011（`646be775`，已部署）、ADR-0013（path grounding）
基线：本地开发 HEAD `088bcaf5`（jina 修复链已提交推送）；生产部署 release `e7c9f517`。
方案基于本地 HEAD 实施。

## 修订记录

- v1：初版（2026-09-01）。
- v2：三路并行评审后修订（正确性/参考项目对照/模型行为+生产工程，评审原文见本会话
  scratchpad `review-A/B/C-*.md`）。主要修订：
  1. 补上被 v1 遗漏的第二条发布枚举 `_workspace_candidate_changes`（致命）；
  2. L2 分类器从"纯黑名单"改为"git 优先、黑名单兜底"；
  3. P0.5 论证前提修正（remediation 已注入仍被模型无视），重定位为"替代动作 + 正确因果 + 平台侧熔断"；
  4. L3 删除语义、android_compile 两写者、materialize 期排除三个未定义语义补全；
  5. 措辞修正（E2B 类比、OpenHands 引用标注）、基线更正、验证指标改 DB 账本为主。
- v3（本版）：用户确认决策点（§7），方案固化进入实施：
  1. **C6 修正为纯黑名单**（推翻 v2 的"git 优先"）：SWE-agent 的 git 边界适用于单一 git
     项目工作区；Clawith 的发布集合是 agent 工作区（skills/、memory/、focus.md 均非 git
     跟踪对象却必须受 L1 CAS 保护），git 跟踪集合 ≠ 源文件集合，映射方向相反。B2（git
     失败降级）随之消解；
  2. 熔断固化：同 run 连续 3 次 `workspace_sync_conflict` → terminal 报错（不暂停问人）；
  3. L2 兜底集合固化：build/.git/.gradle/node_modules/target/dist/__pycache__/_exec_tmp；
  4. 冲突清单上限 5 条 + 总数；历史 build 不清理（另开票）；C7（跨 run 增量缓存失效）确认可接受。

---

## 1. 问题陈述（已核实的事实）

### 1.1 根因链

1. `TEMP_WORKSPACE_DEFAULT_PATHS = ["skills", "memory", "workspace", "focus.md", "soul.md", "HEARTBEAT.md"]`
   （`agent_tools.py:173`）。merge 模式下 `materialized_paths == publish_paths == 全量`
   （`workspace_policy.py:60-68`）。
2. execute_code 结束后有**两条平行的发布枚举**，都会把 publish_paths 全量 rglob 逐文件 CAS：
   - `flush_temp_workspace`（写 `agent_tools.py:1984` 收集、`2083-2120` 独立删除循环）；
   - `_workspace_candidate_changes`（`agent_tools.py:2244-2302`，candidate 冻结，自带独立删除循环
     `2291-2301`，供 reconcile/`apply_candidate` 使用）。
   Android 项目的 `workspace/mydome1/build/**`、`.git/**`（数千文件）两条枚举都纳入。
3. merge 模式的 `publication_conflict_mode == "fail"`（`workspace_policy.py:45-47`）。每次 gradle
   构建都改变 build 产物 → storage 版本漂移 → flush 返回 `conflicted` → `recover_publication`
   用 `apply_candidate(require_base_match=True)` 二次 CAS 仍失败 → 报 `workspace_sync_conflict`
   （`agent_tools.py:2934-2955`、`3075-3083`）。
4. 该失败 `retryable=False`、`safe_remediation="...finish the reply without retrying the code."`
   （`_typed_workspace_publication_failure`）。**已核实该 remediation 会进入模型可见的工具结果消息**
   （`agent_runtime/tool_step_service.py:501-502` 组装进 message、`tool_execution.py:627` 序列化）。
   execute_code `retry_policy="never"`（`_policy_for_name` 的 external_write 分支）——平台不重试；
   **但模型收到了"别重试"的指令，仍主动重试 19 次**。结论：模型缺的不是"别重试"的指令，
   而是正确的因果信息与具体替代动作；且没有平台侧兜底。

### 1.2 已核实事实清单（方案依据）

| 事实 | 位置 |
|---|---|
| 默认发布路径含整个 workspace | `agent_tools.py:173`、`2749-2752` |
| merge→fail / isolated_output→overwrite | `workspace_policy.py:45-47` |
| 全量 rglob 收集 | `agent_tools.py:2382-2406` |
| **两条发布枚举**：flush 与 candidate 各自独立删除循环 | `agent_tools.py:1984/2083-2120` 与 `2244-2302` |
| workspace_cas 冲突→recover→失败文案 | `agent_tools.py:3075-3083`、`2934-2955` |
| gateway 路径 conflicted→raise→recover | `agent_tools.py:2980-2991` |
| remediation 已注入模型消息 | `agent_runtime/tool_step_service.py:501-502`、`tool_execution.py:627` |
| execute_code retry_policy=never | `builtin_tool_definitions.py:4032-4038` |
| artifact_refs 来自 flush_result["updated"] | `agent_tools.py:3018-3021`、`3084-3087` |
| ADR-0011 直写刷新 | `sandbox/local/run_workspace.py:129-217` |
| 现有测试锁定 fail 语义 | `backend/tests/test_sandbox_execution_policy.py:57,579,665` |
| 部署基线：生产 e7c9f517 / 本地 HEAD f448d423 | git log |

---

## 2. 目标与非目标

### 目标

- **P0**：execute_code 发布不再被派生产物拖入 CAS 冲突（两条枚举都治）；冲突处理从
  "整体 fail"改为分层逐文件；源文件保护不削弱；apk 产物回传不回归。
- **P0.5**：把 `workspace_sync_conflict` 变成模型**可正确行动**的失败——正确因果（改动未保存、
  哪些路径）+ 具体替代动作（read_file 最新内容 → edit_file），并加**平台侧同错熔断**兜底
  （不再寄望文案单独切断螺旋）。

### 非目标（明确不做）

- 不重做 CAS 架构本身（并发安全/幂等账本/审计价值保留）。
- 不清理 storage 历史遗留 build 产物（另开运维票；**P0 的两条枚举都必须保证不删、不写**）。
- 不改 isolated_output 模式行为。
- 不调整 model_turn_limit（万级→百级另案）。
- 不做工具输出确定性截断（P2 另案；衔接点见 §8）。

---

## 3. P0：分层发布策略（两条枚举同步治理）

### 3.1 设计

发布路径按分类器分三层：

| 层 | 判定 | 写语义 | 删语义 |
|---|---|---|---|
| L1 源文件 | 分类器输出 source | CAS `version_match`，冲突→失败（P0.5 文案） | CAS `delete_if_match`，冲突→失败 |
| L2 派生产物 | 分类器输出 derived | **不写**（收集期排除） | **不删**（删除候选排除）——storage 历史遗留保持原样 |
| L3 产物例外 | derived 但命中 `**/build/outputs/**` 内文件（收敛后不再全树 `**/*.apk`） | **overwrite（LWW）**，进 `updated`（保 artifact_refs） | **无条件删**（LWW 语义对称：沙箱删了产物就同步删） |

要点：

1. **L2 分类器：纯黑名单段级匹配**（v3 决策；推翻 v2 的"git 优先"，理由见修订记录）：
   - `DERIVED_SEGMENT_FALLBACK = {"build", ".git", ".gradle", "node_modules", "target", "dist", "__pycache__", "_exec_tmp"}`；
   - 段级匹配：rel_path 按 `/` 切段，任一段命中即 L2；其中 `build` 段之后紧跟 `outputs` 子段的
     `**/build/outputs/**` 判 L3；
   - 大小写敏感（`BUILD`、`Build` 不算派生）；`build.sh`、`build.tar.gz`、`build-notes/`
     等文件名/前缀含 build 不命中（段级而非前缀匹配）；无 git 依赖、无降级分支。
2. **两条枚举共用同一收集函数与分类器**：`_collect_temp_workspace_files` 增加分类返回
   （`cas_files` / `overwrite_files`，derived 直接不收集）；`_workspace_candidate_changes`
   与 `flush_temp_workspace` 都改为按分类处理（见 §3.2 落点 2，本版修订的最重要变更）。
3. **materialize 期同步排除 derived**：`_prepare_temp_workspace` 从 storage 物化时同样按分类器
   跳过 L2（temp 里不再出现陈旧 build/），L3 正常物化。这样"模型在沙箱里看到并改动 build/"
   只影响沙箱视图，发布期天然无 build 残留；历史遗留 storage 文件不碰。
4. L3 用 overwrite/LWW：产物无并发编辑语义；`_stable_identical_storage_version` 对 L3 不再需要。
5. L1 冲突仍走现有 recover 链路（candidate + `apply_candidate(require_base_match=True)`），
   语义不变，只是不再可能被 build 产物淹没。

### 3.2 代码落点（修订后全量清单）

1. **分类器**（新增，`workspace_policy.py`）：
   `classify_publish_path(rel_path: str) -> Literal["source", "derived", "artifact"]`
   + `DERIVED_SEGMENTS` + artifact 判定（`build` 段后 `outputs` 子树）。
2. **两条枚举同步治理**（`agent_tools.py`）：
   - `_collect_temp_workspace_files`（2382）返回分类 dict，derived 不收集；
   - `_workspace_candidate_changes`（2244）收集循环 + **删除循环（2291-2301）** 均按分类跳过 L2、
     L3 改 LWW 语义的 candidate（delete 无条件）；
   - `flush_temp_workspace` 写分支（1997-2081）+ **删除分支（2083-2120）** 同样三分支语义。
3. **materialize 期过滤**（`_prepare_temp_workspace` 及物化路径）：L2 不物化、L3 物化。
4. **flush 返回值**统一增加 `"derived_skipped_count"`（两处默认形状 `3012/3064` 同步补）。
5. **android_compile 与 L3 的"同一 apk 两写者"**：android_compile 的产物回传走其自身发布路径
   （docker 构建器 → storage，绕过 execute_code manifest）。决策：**L3 与 android_compile 共享
   LWW 语义 + 复用 ADR-0011 的 refresh 钩子**（apk 发布后 `refresh_run_workspace_path` 刷新
   manifest entry，避免下一次 execute_code flush 用陈旧 base token 覆盖刚回传的 apk）；
   显式接受"最后写入者胜"，风险表注明。

### 3.3 兼容性与风险（修订后）

| 风险 | 评估与对策 |
|---|---|
| candidate 枚举反向删 storage 历史 build（v1 致命遗漏） | 落点 2 已治；加测试：L2 在 candidate 删除循环被跳过 |
| artifact_refs 回归 | L3 进 `updated` → `_workspace_artifact_ref` 保持；加回归测试 |
| android_compile / L3 两写者 | 共享 LWW + refresh 钩子；测试覆盖"先 apk 回传后 execute_code"顺序 |
| materialize 陈旧 build 混淆模型 | 落点 3 从源头排除；execute_code 成功回执声明"派生产物未同步"（见 §4） |
| isolated_output 回归 | 该模式 publish_paths 单路径，分类器不命中；测试覆盖 |
| 幂等账本影响 | 工具调用级事实，与文件集合无关，无影响 |
| 审计削弱 | L2 本就不是审计对象；审计保留 L1/L3 |
| 分类器误伤风险 | 段级匹配 + 快照测试锁死边界（build.tar.gz/BUILD/build-notes/嵌套 build） |
| gateway 路径 | 同一 flush 函数自动获得分层语义 |

---

## 4. P0.5：错误语义 + 平台侧熔断

### 4.1 论证前提（v2 修正）

v1 假设"模型没看到指令"。**已核实不成立**：`safe_remediation` 经 `tool_step_service.py:501-502`
与 `tool_execution.py:627` 进入模型可见工具消息，本次事故模型明确收到
"finish the reply without retrying the code"仍重试 19 次。因此 P0.5 不再定位为"告诉模型别重试"，
而是三件事：**正确因果 + 具体替代动作 + 平台侧同错熔断**。

### 4.2 设计

1. **失败文案（result_summary 主通道，两处发射点 `2934-2940` 与 `2949-2955`/`3079-3083` 统一）**：

   > Workspace 发布冲突：本次代码已执行，但以下路径的改动**未能保存**（最多列 5 条 + 总数）：
   > `workspace/.../CalculatorReducer.kt`
   > 请对每个列出的路径执行 read_file 读取当前存储的最新内容，再基于最新内容用 edit_file 修改；
   > 不要重复运行刚才的代码。其余未列出的改动已保存。

   要点：**不做因果断言**（不写"被并发修改"——本次事故与 ADR-0011 案例都不是真并发，写死
   因果会误导模型）；明确"改动未保存"这一事实与"已保存/未保存"边界。
2. **成功回执声明派生产物语义**（新增，配合 P0）：execute_code 成功且存在 L2 跳过时，
   result_summary 追加一句：
   "构建产物等派生文件（build/、.git/ 等）按平台约定未同步回工作区；apk/aab 产物已作为
   artifact 返回。"——防止模型 read_file 检查 build 目录时产生"execute_code 与 read_file
   视图不一致"的新困惑。
3. **safe_remediation 同步重写**为与主文案一致（保持注入通道，双保险）。
4. **平台侧同错熔断（已固化）**：同一 run 内 `workspace_sync_conflict` 连续 ≥3 次 → 工具结果
   强制 terminal（报错停止，不暂停问人——无人值守场景暂停=挂死；对齐 SWE-agent
   `max_requeries`+`cost_limit` 与 OpenHands `max_iterations` 的"文案+硬闸"组合）。
   阈值 3 的依据：事故 06:15:44→06:16:03→06:18:45 连续 3 次同错误后模型仍无差别重试；
   实现落点：`tool_step_service` 的失败门控（复用现有 repair-budget 设施，实现时定位）。
5. **execute_code tool description 同步更新**：明确声明"构建产物（build/、.git/ 等）不回传
   工作区；apk/aab 作为 artifact 返回；对源文件的修改请优先用 edit_file"。

### 4.3 测试

- 两处文案发射点都产出新文案 + 路径清单（断言含路径片段与 edit_file 指令）。
- 熔断：同 run 连续 3 次 workspace_sync_conflict → terminal 结果。
- 成功回执含派生产物声明。

---

## 5. 测试计划（修订后）

新增 `backend/tests/test_workspace_publication_filter.py`：

1. `test_derived_paths_are_not_collected_for_publication`（L2 写/删双向不进两条枚举的集合）
2. `test_derived_paths_are_skipped_in_candidate_delete_loop`（candidate 删除循环 2291 不产出
   L2 delete 项——v1 致命遗漏的回归测试）
3. `test_artifact_paths_publish_with_overwrite`（L3 覆盖写 + 进 updated + artifact_ref 保持）
4. `test_artifact_deletion_is_lww`（沙箱删 apk → storage 同步删且不冲突）
5. `test_source_conflict_returns_actionable_error_with_paths`（L1 冲突 → 新文案 + 清单）
6. `test_pure_derived_conflict_succeeds_with_metadata`（仅 L2/L3 冲突 → succeeded + 计数；
   此分支稳态为死代码，保留只为混合期防 fail-open，**绝不放行 L1 静默丢失**）
7. `test_isolated_output_mode_unchanged`（回归）
8. `test_path_classification_boundaries`（快照：`build/`、`build.tar.gz`、`build.sh`、`BUILD/`、
   `build-notes/`、`node_modules/**`、`node_modules/x.apk`、嵌套 `a/build/b/`、`build/outputs/apk/*`）
9. `test_android_compile_then_execute_code_apk_no_stale_overwrite`（两写者顺序）
10. `test_materialize_skips_derived`（temp 物化不含 L2）

适配/保留现有测试：
- `test_merge_policy_preserves_conflict_detection`、`test_workspace_publication_retries_candidate_and_resolves_by_hash`、
  `test_workspace_candidate_failure_is_terminal_and_never_publishes` 预期无需改断言；
- `test_flush_temp_workspace_conflicts_on_concurrent_new_file`（`build.tar.gz` 场景）**保留为哨兵**：
  若分类器误伤 L1 语义，该测试会变红。

补充覆盖缺口：flush 删除分支、60s 超时（2997/3057）、gateway 路径（2980-3041）。

事故回放集成验证：本地搭 mydome1 形态（含 gradle 项目）跑 execute_code，断言无
workspace_sync_conflict、apk 在 artifact_refs、build 目录 storage 侧不变。

---

## 6. 部署、验证与回滚

- 无 DB 迁移，纯代码 + 测试。实施基线：本地 HEAD `f448d423`。
- 走 `scripts/deploy.sh` 常规流程（skill clawith-prod-deploy），`--require-idle`。
- 上线后 24h 验证（**主指标 DB 账本**，Langfuse 辅助）：
  - 主：`agent_tool_executions WHERE error_code='workspace_sync_conflict' AND started_at > 上线时刻`
    计数 → 期望归零或降至个位数；
  - 主：同型 Android 任务 run 轮次/时长对比（事故基线 87 次 llm / 28.3min）；
  - 辅：Langfuse tool-failure evaluator 计数、queryMetrics 按 name 过滤的失败观察数。
- 回滚：git revert 即恢复（无数据迁移）。注意：上线后 storage 中 build 历史遗留将"永不更新"，
  回滚后旧行为恢复，混合期可能出现新的冲突形态——回滚窗口内可接受（短时）。

---

## 7. 决策记录（v3 已固化）

| 决策点 | 决策 |
|---|---|
| L2 黑名单集合 | `build/.git/.gradle/node_modules/target/dist/__pycache__/_exec_tmp` |
| 分类器实现 | 纯黑名单段级匹配（无 git 依赖；git 优先方案已否决，理由见修订记录 v3-1） |
| 同错熔断 | 连续 3 次 `workspace_sync_conflict` → terminal 报错（不暂停问人） |
| 冲突清单上限 | 5 条 + 总数 |
| 历史 build 清理 | 本次不做（另开票） |
| execute_code 沙箱内 gradle 跨 run 增量缓存失效 | 已确认可接受（正规构建走 android_compile docker 卷缓存） |

---

## 8. P2 衔接备注（另案，本版仅修正衔接点）

- execute_code 输出截断**不照搬** deepseek-harness pruner 的固定 head+tail（tail=1024 会切掉
  中段编译错误行）：改为"stderr 优先 + 错误行锚定"截断；pruner 的 replay-safe/shadow-price
  机制可复用。
- 与 P0 分层正交：L2 排除后，模型上下文里的 build 噪音大幅下降，P2 的紧迫性相应降低。

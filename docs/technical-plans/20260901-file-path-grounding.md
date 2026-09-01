# read_file 路径幻觉根治方案（L3 路径接地）

- 日期：2026-09-01
- 状态：方案已定（grill-with-docs 决策落 ADR-0013；code-review 双轴评审后修订 v2）
- 范围：read_file 路径未命中的工具侧建议增强（L3）+ search-first 契约注入（L1 扩展）；不含 edit/write 与 C（文件树/repo-map）
- 前置研究：本文件 v1（参考资料对比）、ADR-0013、`docs/technical-plans/20260819-builtin-tool-path-contract-plan.md`（L1/L2 源头）

## 背景与根因

run 6a1c0eab（mydome1 计算器）：`list_files` 只回单层列表（`app/ (4 items)`），模型按 Java 包名约定猜 `com/example/mydome1/Calculator.kt`（实际 `com/example/calculator/`），连续 4 次 `workspace_file_not_found`。现有 L2 诊断（最深祖先+条目）已让模型 4 秒自纠，但「Did you mean」只处理 workspace/ 前缀增删，对中间段猜错不生效。

**定性**：路径接地（path grounding）不足——模型在未见全树时用约定猜路径。非竞态、非存储故障。

## 决定（ADR-0013，7 项均经用户确认）

| # | 决定 |
|---|---|
| D1 | 范围：只治读侧 5 处调用点；edit/write 不加（误写歧义风险不对称） |
| D2 | 建议真相源 = StorageBackend 接口（Local/S3/Fallback 三后端下正确）；企业路径（is_enterprise）按各自根做同界搜索；本地 FS 仅保留现状渲染 |
| D3 | 匹配语义：basename 精确匹配优先；difflib 模糊留待数据判据 |
| D4 | 只建议、不自动重读（工具不做内容代偿） |
| D5 | search-first 一句注入 `PATH_CONVENTION_TEXT`（覆盖 `_PATH_CONVENTION_PARAMS` 所列参数，见改动二） |
| D6 | C（文件树/repo-map）触发判据：`workspace_file_not_found` 占 tool_failure=0 周占比 >5% → C-lite；>15% → C-full。初始阈值无数据依据，上线后首周用实测分布校准 |
| D7 | L1/L2/L3 三级契约写入 ADR + CONTEXT.md 术语表 |

## 改动一（A）：L3 存储侧 basename 定位建议

### 文件与函数

`backend/app/services/agent_tools.py`：

1. `_path_failure_details` 签名扩展并改 async（**签名变更显式声明**）：

```python
async def _path_failure_details(
    agent_id: uuid.UUID,
    rel_path: str,
    *,
    label: str = "path",
    tenant_id: str | None = None,
    include_storage: bool = True,
) -> str:
```

- `include_storage=True` 时调用 `_storage_nearest_candidates` 并追加 verified 建议行；
- **pattern base 两处（9117/9145）传 `include_storage=False`，但同样改 `await`**——排除的只是 storage 建议，不是 async 化（否则同步拼接 coroutine 会 TypeError）。这两处所在函数本身 async，已核实。

2. 新增异步助手（放在 `_path_failure_details` 旁）：

```python
async def _storage_nearest_candidates(
    agent_id: uuid.UUID,
    rel_path: str,
    *,
    tenant_id: str | None,
    max_depth: int = 6,
    max_nodes: int = 150,
    max_suggestions: int = 3,
) -> list[str]:
    """L3: 在 StorageBackend 中做有界 basename 定位，返回真实存在的候选相对路径。"""
```

算法：
- `storage_key, normalized, is_enterprise = await _tool_storage_key(agent_id, rel_path, tenant_id)`；`normalized` 为空或 basename 为空 → 返回 `[]`（**明确定义 rel_path='' 行为**）。
- 从目标 key 逐级向上（`storage.exists`/`is_dir`）找**最深存在祖先**，同时记录缺失段列表；祖先的**相对路径**在向上回溯时自然可得（每剥一层记录段名），**遍历中直接携带相对路径，不需要 key→path 反函数**（代码库无此反函数，勿造）。
- 自祖先向下有界 DFS：跳过 `.` 开头条目、深度 ≤ max_depth（相对祖先）、累计访问 ≤ max_nodes；收集 `entry.name == 目标 basename` 的文件**或**目录（`StorageEntry.is_dir`）；候选相对路径 = 祖先相对路径 + 自祖先起的路径段。
- **企业路径**：`is_enterprise=True` 时祖先搜索与 DFS 都限定在 `enterprise_info_{tenant_id}/` 根内，候选以 `enterprise_info/...` 前缀呈现——与 `_tool_storage_key` 的正向规则一致，不做跨根搜索。
- 按深度升序取前 max_suggestions。
- **异常降级按 AGENTS.md「narrow and explained」执行**：`exists`/`is_dir`/`list_dir` 各自独立 try（或逐节点 try），失败即放弃该分支并注释原因（如「S3 list_dir 瞬时失败时该节点子树弃查」），不吞整个诊断。

3. 追加渲染（在 `_path_failure_details` 内，与本地 FS 建议去重）：

```python
candidates = await _storage_nearest_candidates(agent_id, rel_path, tenant_id=tenant_id)
if candidates:
    text += "\nDid you mean (verified in workspace storage): " + "; ".join(f"'{c}'" for c in candidates) + "?"
```

4. 7 处调用点全改 `await`：read_file（约 3442）、list_files（3699）、search 目录（3744）、find_files 目录（3783）传各自函数已有的 `tenant_id`；android_compile project_path（4722）所在函数 `_android_compile_outcome` 无 tenant_id 参数，走默认 `None`（企业路径下 `_storage_nearest_candidates` 以 unscoped key 探测必空，天然无跨根风险）；pattern base 两处（9117/9145）传 `include_storage=False`。

### 边界（防误伤）

- 只在失败路径执行：零常态开销；最坏 150 节点 list_dir（每目录一次）。
- S3 后端：仅失败时多几次 `list_dir` 往返，无缓存污染。
- basename 大小写敏感精确匹配：`calculator.kt` 不命中 `Calculator.kt`——宁缺毋滥，避免把「可能是别的文件」塞给模型（D3/D4）。
- 建议数上限 3、最近优先，防止多租户大仓库刷屏。

## 改动二（B）：search-first 契约注入

`backend/app/services/workspace_paths.py` 的 `PATH_CONVENTION_TEXT` 追加一句（英文，与现有契约语气一致）：

> "Before using an unverified path, discover it with list_files or find_files; do not guess conventional paths (e.g. Java package directories)."

**覆盖范围（已核实）**：经 `builtin_tool_definitions.py:4119 _inject_path_convention` 注入 `_PATH_CONVENTION_PARAMS` 所列参数（read_file/list_files/find_files 等）。**不在表内**：`android_compile.project_path`（注释明示已单独修正契约）与 `search_files.pattern`——与本次事故无关，不扩表。

依据：gemini-cli `prompts/snippets.ts:720-725` 同款 search-first 注入（"Use search tools extensively to understand file structures, existing code patterns, and conventions"）。

## 测试（随改动一/二落地）

`backend/tests/test_agent_tools_path_grounding_l3.py`（新增，实现后所有 storage 场景测试集中于此，而非拆分进既有两个文件）：
1. **事故回归**：目录树含 `…/com/example/calculator/Calculator.kt`，guess `…/com/example/mydome1/Calculator.kt` → 建议含真实路径，missing_below 渲染正确。
2. 无 basename 命中 → 无「verified」行。
3. 多命中 → 上限 3、最近优先。
4. `.` 隐藏目录跳过；深度/节点上限生效（宽树压测断言有界）。
5. 目录型 miss（list_files 猜错目录名）→ 目录名匹配建议。
6. rel_path='' → 无建议、无异常。
7. 企业路径：enterprise 根内命中 → `enterprise_info/...` 前缀建议；agent 根内搜索不越界。
8. 本地 FS 漂移：monkeypatch 工作区根后 describe 本地条目与 storage 建议共存。

`_read_file_outcome` 端到端（同文件）：error_code 仍为 `workspace_file_not_found`、消息含 verified 建议、路径正确。

`backend/tests/` 新增 `PATH_CONVENTION_TEXT` 断言（**全仓现无此断言**，本次随改动二补上：注入函数对 read_file.path 参数注入、android_compile.project_path 不重复注入）。

**门禁**：`scripts/arch-guard.sh` + 全量受影响测试子集（pytest 指定文件）。

## 发布与回滚

- 单 PR 两 commit：commit 1 = A（逻辑+测试），commit 2 = B（常量一句）。经 arch-guard + 测试后按 skill clawith-prod-deploy 的 worktree 流程部署。
- ⚠️ 实施前先核对工作树中他人未提交的 agent_tools.py/builtin_tool_definitions.py 改动（评审时已存在），避免行号漂移与冲突。
- 回滚 = revert PR，零数据迁移、零状态残留（纯读路径错误渲染 + 描述文本）。
- 风险面：仅「失败时错误消息更丰富」；无写入路径变更。

## 验收（生产证据，Langfuse）

1. 部署后观察真实 run：`workspace_file_not_found` 错误消息含 "verified in workspace storage" 建议，且下一个工具调用命中建议路径；`tool_failure=0` 评分照常落库。
2. 指标基线（D6 判据）：每周用 queryMetrics/listScores 计算
   `count(tool_failure=0 且 metadata.error_code=workspace_file_not_found) / count(tool_failure scores)`；
   - >5% → 立 C-lite 票（list_files 受限递归）；
   - >15% → 评估 C-full（aider 式 repo-map：tree-sitter 符号抽取 + 图排序 + token 预算，参考 https://aider.chat/docs/repomap.html）；
   - 首周同时收集实测分布，校准 5%/15% 初始阈值。
3. 回滚触发：失败路径出现可观测延迟劣化，或建议噪声（模型被误导读错文件）> 个案。

## 参考资料映射（reference-check 合规）

| 决定 | 来源 |
|---|---|
| A 的「错误自带答案」 | Clawith 现有 L2（workspace_paths.py）+ 本次事故实证（Entries under it → 4s 自纠） |
| B 措辞与机制 | gemini-cli snippets.ts:720（本地源码）+ Clawith L1 注入先例 |
| C-full 形态 | aider repomap 文档（URL 已抓取） |
| C-lite/搜索工具 | gemini-cli ripGrep.ts、SWE-agent ACI 思想 |
| 未覆盖声明 | OpenHands Python runtime 已随上游重组移出主分支，本地镜像无法核对；codex/claw-code 无路径纠错机制（已核源码） |

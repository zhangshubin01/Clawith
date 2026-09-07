# 方向 5：execute_code 沙箱内 git 探索命令（checkout/reset/restore/clean）副作用告警与记账 — 生产级修复方案

- **关联根因**：`docs/analysis/2026-09-05-workspace-revert-rootcause5.md`（§五【P1·新增】）。
- **互补关系**：方向 1（flush 防回退，已部署 c118d9fe）只在 flush 侧拦「沙箱回退到 git HEAD」；方向 5 补「执行时」盲区——模型在沙箱内跑 `git checkout/reset/restore/clean` 时主动告警/记账，覆盖任意目标（非 HEAD 分支/commit、`clean` 删除、`restore --staged`），且不依赖 flush 才被发现。
- **定性**：中收益（防一次真实发生的稀有但严重数据丢失类）、低风险（纯告警/记账，零流程控制变更）。
- **口径**：只读实读源码 + 运行日志 + 既有研究报告，全部实锤；代码级事实标注函数名出处，行号只作「约」。

---

## 一、结论先行（裁决）

**评审通过（有条件，见 Q5 一处已知风险）**。

修复方案：在 execute_code 的**单一执行入口** `_execute_code_outcome`（`agent_tools.py`）处，用**确定性规则**识别代码串里的 `git checkout/reset/restore/clean/switch` 子命令，命中即 `logger.warning` 记账 + 在工具结果 summary 追加一句事实性提示（不拦截、不改变执行流程）。检测逻辑与方向 1 的 flush 回退守卫（`[WorkspaceFlushRevertGuard]`）互补：方向 1 在 flush 侧按「沙箱内容 == git HEAD」拦回退，方向 5 在**命令执行前**按「命令本身有工作树副作用」告警——前者是「结果已坏才拦」，后者是「动作即将发生就报」。

---

## 二、Phase 1：参考对比结论（11 个项目，含诚实负结论）

**关键决策点**（拆自问题）：「execute_code 沙箱内，模型运行 git 命令改动 source 文件时，如何低成本、不改变执行流程地识别并告警/记账？」

决策点拆成两个子问：①**触发层**——用什么机制识别「git 命令有工作树副作用」；②**消解层**——告警给谁、告警了怎么处置。结论：行业分工是**「触发用确定性规则（宁滥勿缺）、消解交给 LLM/人做语义」**（memory `plans-compare-reference-materials`，2026-09-03 教训固化），方向 5 严格按此分工。

| # | 参考项目 | 机制 | 对本问题的结论 |
|---|---|---|---|
| 1 | **jcode**（`command-risk/lib.rs`） | 命令级**确定性 blast-radius 分类**：`RiskLevel = Safe/Low/Confirm/Catastrophic`，`is_absolute_deny` 硬拒绝；静态解析不靠 LLM 法官 | **最直接参考**。Clawith 缺「对 bash 参数内容做静态风险分级」的确定性前置层（`autonomy_service.py` 是粗粒度工具级 action_type）。方向 5 复用其「确定性规则分级」思想，但**只告警不拦截**（jcode 是 deny/reflect 门，方向 5 是纯记账），故不引入门禁语义。 |
| 2 | **gptme**（`tools/autocommit.py`、`_hashline_snapshot.py`） | autocommit 用 `git status --porcelain --untracked-files=no` 检测文件修改；hashline_snapshot 用 SHA-256 4 字节 tag 校验「文件是否还等于模型看到时的内容」 | 复用「内容哈希快照」思想（Clawith 已有 `content_hash_bytes` + `capture_head_tree_hashes`）。但 gptme 是**事后**快照/status 校验，方向 5 是**命令执行前**静态识别；且方向 1 已在 flush 侧做了「事后哈希对比」，方向 5 不必再造。 |
| 3 | **E2B**（`filesystem/watch_handle.py`、`filesystem_connect.py`） | 沙箱**文件系统 watch**：目录/文件变更事件流（file change events） | 诚实负结论：E2B 是「运行时文件事件流」，Clawith 的 docker/subprocess 沙箱无等效 watch 机制；为方向 5 引入文件 watch 属过度工程（收益低、需常驻监听）。**不采用**。 |
| 4 | **OpenHands / software-agent-sdk**（`agent_server/file_router.py`、`event_service.py`） | 文件观察/事件路由（FileObservation 事件流） | 诚实负结论：OpenHands V1 把文件观察拆进 agent-server 事件流，与 Clawith「storage + 临时工作区 flush」模型异构；方向 5 不引入事件流范式。**不采用**。 |
| 5 | **deepagents**（`backends/filesystem.py`、`sandbox.py`、`store.py`） | StateBackend 把文件内容放进 graph state 的 `files` channel 随 checkpoint 持久 | 诚实负结论：deepagents 把文件当 graph state 持久，Clawith 是「storage 直写 + 临时工作区 flush」，两套模型不可互迁。方向 5 不迁移此范式。**不采用**。 |
| 6 | **letta-code**（MemFS git 化文件系统：`precommit/postcommit/sync-state/config-lock`） | 记忆文件系统全量 git 化，所有写走 git 提交 + 并发写锁 | 诚实负结论：letta-code 是「记忆 FS git 化」（git 是写通道本身），Clawith 里 git 只是「物化手段」不是写通道；把写通道 git 化是方向 3/方向 2 范畴的重构，方向 5 不做。**不采用**。 |
| 7 | **codex**（`codex-rs/tui/src/get_git_diff.rs`、`git_enrichment`） | 用 `git diff` 做变更审查/上下文增强（事后 diff） | 相关但负结论：codex 是「事后 diff 审查」非「执行前命令识别」，且是单用户 CLI 非多租户平台；方向 5 要的是「动作即将发生就报」，codex 模式不匹配。**不采用**。 |
| 8 | **herdr**（状态「hook 权威 + screen fallback」双源仲裁） | 状态变更双源仲裁（hook 权威、屏幕 fallback） | 诚实负结论：herdr 的「状态」是 UI/运行态仲裁，与「git 命令副作用」是 false friend（同名不同域）。**不采用**（已记录防误引）。 |
| 9 | **orca**（`runtimeFence` 单调计数租约 / workspace 管理） | 多租户工作区协调、租约/代际 | 诚实负结论：orca 处理「执行协调/租约」，不处理「模型在沙箱内 git 回退文件」的内容语义；方向 5 是内容语义层，二者正交。**不采用**。 |
| 10 | **CubeSandbox**（沙箱服务） | 沙箱隔离/资源治理 | 诚实负结论：CubeSandbox 是沙箱服务本体，未发现「git 命令副作用」级别的文件语义检测。**不采用**。 |
| 11 | **Clawith 自身方向 1**（c118d9fe） | `capture_head_tree_hashes`（git archive → sha256）+ flush 时 `current_hash == git_head_hash && storage_hash != git_head_hash` 拒发布 + `[WorkspaceFlushRevertGuard]` warning | **库内先例，方向 5 的直接地基**。方向 5 是它的「执行时」互补：方向 1 只在 flush 拦「回退到 HEAD」，方向 5 在命令执行前识别「checkout/reset/restore/clean」任意目标；两者共用 `logger.warning` 记账口径与 `content_hash_bytes` 哈希基础设施。 |

**对比结论**：无任何参考项目采用「在 flush 侧枚举 git 历史逐 commit 对比」来识别 git 副作用（那需要昂贵的 git history 枚举，且 `clean`/`restore --staged` 不落历史）；行业共识是「执行前确定性规则识别命令 + 事后哈希校验兜底」。Clawith 方向 1 已做「事后哈希兜底」，方向 5 补「执行前确定性识别」，正好拼全，**不引入事件流/文件 watch/写通道重构**。

---

## 三、Phase 2：双源定根因

### 3.1 代码侧（实读）

**写入架构与 git 命令执行通道**（`docs/analysis/2026-09-05-workspace-revert-rootcause5.md` §一/§二 + 本次实读）：

1. execute_code 用 **run-scoped 临时工作区**：`use_run_workspace`（`run_workspace.py`）→ `_prepare_temp_workspace`（`agent_tools.py:1897`）物化 storage 内容到临时目录，`.git` 从 bundle 恢复（`restore_git_metadata_from_remote`）→ `_capture_git_head_hashes`（`:1956`）在物化后对每个 repo 跑 `gitlab_workspace.capture_head_tree_hashes`（`gitlab_workspace.py:330`，`git archive` + tarfile + `content_hash_bytes` 逐文件 sha256）→ 存入 `temp_workspace.git_head_hashes`。
2. **模型的 git 命令不经 `_run_git`**：`gitlab_workspace._run_git`（`:98`）是后端物化/clone/adopt/inject/restore 的通道（argv 数组 + 4096 截断 + 脱敏）。模型的 `git checkout/reset/clean` 在 execute_code 沙箱内由 shell 直接执行（docker_backend `_run_in_persistent_session` / subprocess_backend `_run_in_persistent_session`）。
3. **执行入口是单一收口**：`_execute_code_with_workspace_outcome`（`agent_tools.py:3200`，execute_code / execute_code_e2b 均经此）→ `_execute_code_outcome`（`:13410`）在此提取 `language`/`code`（`:13434-13437`）后 `backend.execute(...)`（`:13588`）。`code` 串在**执行前**就可用，且 `run_id`（`sandbox_run_scope_id.get()`）与 `agent_id` 同域可用（`:13596` 已如此取 run_id）。
4. **方向 1 的 flush 回退守卫**（`flush_temp_workspace` `:2221-2254`）：仅当 `entry is not None and git_head_hash is not None and current_hash == git_head_hash`（沙箱内容 == HEAD）且 `storage_hash != git_head_hash`（storage 已偏离 HEAD）才判 `reverted` 拒发布。**它只拦「回退到 HEAD」这一种**；`git checkout <非 HEAD commit/分支>`（内容 != HEAD）、`git clean -fd`（删 untracked，走 flush 的 delete 路径）、`git restore --staged`（内容 == index 非 HEAD）都不被此守卫覆盖。
5. **落账口径**：`_typed_success`/`_typed_failure`（`:3916`/`:3835`）接受 `metadata`；`_execute_code_outcome` 在 `:13612` 起统一构造 `output_metadata` 并传入成功/失败/发布失败三个返回分支——是注入 metadata 的单点。

### 3.2 日志侧（实锤 + 现状）

- **事件实锤**（rootcause5 §三/§四）：2026-09-05 run 764eb591 内，模型压缩失忆后在沙箱跑 git 探索；reflog 铁证 `2782458 HEAD@{2}: reset: moving to f_android_ai` + `checkout -b fix/p1-repeating-display`（12:29:16）+ `checkout f_android_ai`（12:30:25）→ 沙箱工作树回退到 2782458（未含 edit_file 的未提交改动）→ flush 把回退文件 CAS 写回 storage → 字节级（11134→12103→11134）+ 内容级（两次 edit `old_string` 逐字一致）双铁证数据丢失。**git 探索命令副作用真实发生过且造成数据丢失。**
- **现状频率**：当前部署 1ae0f5d5（backend 容器 12:14:31 起）日志中 `[WorkspaceFlushRevertGuard]`/`[GitHeadTreeCapture]`/`[WorkspaceFlushConflict]` 均 **0 次**（容器新起 + 尚无 run 执行代码）。结合事件「需模型失忆 + git 探索」双重前提，判定该副作用是**稀有事件**——方向 5 应做**轻量告警**，不做重型机制（文件 watch、逐 commit 枚举均排除，见 Phase 1）。

### 3.3 根因（可证伪）

**最深层因**：execute_code 沙箱内 git 仓库（bundle 恢复的 `.git`）是与 edit_file 直写 storage 并立的**第二真相源**；模型在沙箱内跑 `git checkout/reset/restore/clean` 会改动临时工作区的 source 文件，而 flush 的 `base_hash` 比较**无法区分「模型有意改动」vs「git 命令回退/覆盖」**——方向 1 只补了「回退到 HEAD」一种的 flush 侧拦截，其余 git 副作用目标仍会静默按「改动」发布，且**在执行发生时无任何告警**（失忆的模型也不会自己报告）。

**可证伪性**：若「执行时缺 git 副作用告警」是根因缺口，则补上执行时确定性识别后，任何 `git checkout/reset/restore/clean` 的运行都会在日志留下 `[GitSideEffectCommand]` 记录、并在模型结果里带提示——即使在 flush 侧未被方向 1 拦到的非 HEAD 目标，也至少可被审计到「哪个命令在何时跑过」。

---

## 四、Phase 3：修复方案（最小、可回退、带测试）

### 4.1 改动 1：检测纯函数（新增，`backend/app/services/sandbox/security.py`）

在 `check_code_safety`（`security.py:62`）旁新增：

```python
import re  # 文件顶部新增

_GIT_SIDE_EFFECT_RE = re.compile(
    r"\bgit\s+(checkout|reset|restore|clean|switch)\b", re.IGNORECASE
)


def detect_git_side_effect_commands(language: str, code: str) -> list[str]:
    """Best-effort recall of git subcommands that mutate the working tree.

    Trigger-only, no semantics: the recall is deliberately 宁滥勿缺 (a false
    positive costs one warning line; a false negative loses the alert). The
    semantic decision (was the side effect intentional?) belongs to the model
    or an operator, not to this rule. Only ``bash`` is scanned — git
    exploration is issued from bash in practice, and Python/Node subprocess
    arg-lists would need a different (heavier) parser for negligible recall
    gain. Not a security boundary (same caveat as check_code_safety).
    """
    if language != "bash":
        return []
    return sorted({m.group(1).lower() for m in _GIT_SIDE_EFFECT_RE.finditer(code)})
```

**设计理由**：①`security.py` 是「code 静态模式检查」的既有 owner，与 `check_code_safety` 同层、同「best-effort 非安全边界」哲学；②`switch` 是 `checkout` 的现代等价物（同样改工作树），成本为零，纳入；`stash` 因 spec 未列且更稀有，暂不纳入（文档备注：后续一行可加）；③仅扫 bash——事件与 spec 均指向 bash 通道，Python/Node 的 subprocess 参数数组解析是另一个复杂度台阶，收益可忽略（诚实负结论）。

### 4.2 改动 2：执行前告警 + 记账（`backend/app/services/agent_tools.py`）

在 `_execute_code_outcome` 提取 `code` 之后（`:13437` 之后）注入：

```python
    git_side_effect_commands = detect_git_side_effect_commands(language, code)
    if git_side_effect_commands:
        logger.warning(
            "[GitSideEffectCommand] run_id={} agent_id={} language={} subcommands={}",
            sandbox_run_scope_id.get().strip() or None,
            agent_id,
            language,
            ",".join(git_side_effect_commands),
        )
```

并在 `:13612` 构造 `output_metadata` 时、`summary` 计算完成后（`:13611` 之后）追加事实性提示与 metadata（**单点注入**，覆盖成功/失败两个返回分支；发布失败分支已带 metadata 但可在同点顺带）：

```python
    if git_side_effect_commands:
        output_metadata["git_side_effect_commands"] = git_side_effect_commands
        summary += (
            "\n\n[git 副作用提示] 检测到 git "
            f"{', '.join(git_side_effect_commands)} 命令；"
            "若它改动/回退了工作区 source 文件，请确认是否为预期操作。"
        )
```

`detect_git_side_effect_commands` 需从 `app.services.sandbox.security` 导入（`_execute_code_outcome` 已在 `:13475` 起延迟导入 sandbox 模块，遵循同款延迟导入避免循环依赖）。

**明确的「不改」**：
- 不拦截、不 fail、不 skip、不改 `model_action`、不动 CAS/flush 流程——**零流程控制变更**。
- 不改 flush 侧逻辑（方向 1 已覆盖「回退到 HEAD」的拒发布；方向 5 不做 flush 侧非 HEAD 枚举）。
- 不碰 legacy fallback `_execute_code_legacy_outcome`（`:13764`，仅 sandbox config 异常时兜底，且自带 `_check_code_safety` 副本）——若后续要覆盖，复用同一 helper 一行即可，本方案不扩面。

### 4.3 回归测试（新增 `backend/tests/test_git_side_effect_command.py`）

自包含单测（镜像 `test_workspace_flush_revert_guard.py` 的「backend/tests 非 package、自带 fixture」风格），覆盖：

1. **触发路径**：`detect_git_side_effect_commands("bash", "git checkout main\ngit reset --hard HEAD~1\ngit clean -fd\ngit restore src/a.kt")` → `["checkout", "clean", "reset", "restore"]`（去重、排序）。
2. **不误报**：`git status`/`git log`/`git branch`/`git diff` 纯探索命令 → `[]`（副作用命令才告警）。
3. **非 bash 不扫**：`detect_git_side_effect_commands("python", "subprocess.run(['git','checkout','x'])")` → `[]`。
4. **终态 + 副作用断言**：`_execute_code_outcome` 层面——monkeypatch `backend.execute` 为 no-op success，用 loguru sink 捕获（`logger.add(lambda m: captured.append(str(m)), level="WARNING")`，本仓库 caplog 不接 loguru）断言 `[GitSideEffectCommand]` 出现、且结果 `metadata["git_side_effect_commands"]` 正确、`result_summary` 含提示、`status == "succeeded"`（**不改变执行结果**）。
5. **无副作用命令静默**：`git status` 场景不产生 warning、不附加 metadata。

测试命令（后端用 `.venv`，python 不在 PATH）：`cd backend && .venv/bin/python -m pytest tests/test_git_side_effect_command.py -q`。

### 4.4 影响面与爆炸半径

- **改动契约**：无对外契约变化。`execute_code` 工具结果 summary 在检测到副作用命令时会**追加一段事实性提示**（纯文本，不改变 status/error_code/artifact_refs）；`metadata` 新增 `git_side_effect_commands` 字段（新增键，不改旧键）。
- **消费者**：summary 追加文本的消费者是模型（读工具结果）与 Langfuse trace（`result_summary` 落观测）——均只读、无契约破坏；metadata 新增键对消费方是幂等兼容。
- **爆炸半径**：仅 `_execute_code_outcome` 一个函数 + `security.py` 一个纯函数 + 一个新测试文件。用 `detect_changes`/`trace_path` 复核 `_execute_code_outcome` 的入站调用者（`_execute_code_with_workspace_outcome` → `_run_with_temp_workspace_outcome` 及 tool 注册）确认无跨模块契约波及。
- **性能**：一次 `re.finditer` 于 bash 代码串（KB 级），可忽略；不做任何 git 子进程/文件 IO。

---

## 五、Phase 4：7 角度评审（每条含负向探针）

**Q1 根因是否正确？**
- **裁决**：通过。
- **正向依据**：根因「沙箱内 git 仓库是第二真相源 + flush 无法区分 git 回退 vs 有意改动 + 执行时无告警」能解释 rootcause5 全部证据（reflog reset 铁证 12:37:15、字节级 11134→12103→11134、内容级两次 edit `old_string` 逐字一致、无 `[WorkspaceFlushConflict]` 即 CAS version_match 通过）。已追到最深层：方向 1 只补 flush 侧 HEAD 回退，git 副作用「执行时」盲区未补。
- **负向探针（反例测试）**：「若根因是『执行时缺告警』，则方向 1 部署后，非 HEAD 目标的 git 副作用（如 `git checkout <其它分支>`）应仍能静默发布；我对照 flush 守卫代码 `:2240` 的 `current_hash == git_head_hash` 判断——它只匹配 HEAD，非 HEAD 目标 `current_hash != git_head_hash` 不触发 `reverted`，会走正常 publish。**证实**根因未被方向 1 完全覆盖。」

**Q2 根治方案是否正确？**
- **裁决**：通过。
- **正向依据**：方案改的正是 Q1 根因——在 git 副作用命令「执行前」做确定性识别告警（补盲区），而非继续在 flush 侧打补丁；与方向 1 各司其职（flush 拦 HEAD 回退 / 执行时报任意 git 副作用）。
- **负向探针（删除测试）**：「删掉本方案，问『git 副作用盲区会不会复发』——**会**：非 HEAD checkout、`git clean` 删除、`restore --staged` 三类仍会静默发布且无任何审计痕迹；只有 `git checkout` 恰好回退到 HEAD 这一种被方向 1 拦住。故不是止痛药。」

**Q3 参考的资料是否正确？**
- **裁决**：通过。
- **正向依据**：11 个条目中 1（jcode）为同问题（命令静态风险分级）、2（gptme）/11（Clawith 方向 1）为同机制（哈希快照/事后对比）、其余 8 个为诚实负结论（机制不存在/异构/false friend）；均读真实源码路径（`command-risk/lib.rs`、`tools/autocommit.py`、`filesystem/watch_handle.py`、`backends/filesystem.py`、`get_git_diff.rs`），未把归档/停更项目当第一依据（OpenHands-CLI/daytona 明确排除）。
- **负向探针**：「我找了一个可能引用错的点——herdr 的『状态双源仲裁』是否真与 git 命令副作用同域；核下来 herdr 的『状态』指 UI/运行态（hook 权威 + 屏幕 fallback），与文件内容语义是 false friend。**无误，已在表 8 标注防误引**。」

**Q4+Q5 副作用与爆炸半径排查完？**
- **裁决**：通过。
- **正向依据**：①副作用面——无外部写、无缓存失效、无连接/资源新增（仅一次内存 regex）；②影响面——改了 `execute_code` 结果 summary（追加文本）与 metadata（新增键），所有消费者（模型、Langfuse trace）只读兼容；跑 `scripts/arch-guard.sh` + 后端 pytest 定验证范围（实现阶段执行）。
- **负向探针**：「我特意找过会漏掉的一个消费者——外层 `_run_with_temp_workspace_outcome`（`:3148`）与 `_execute_code_with_workspace_outcome` 网关路径（`:3591`）在 `flush_result["conflicted"]` 分支会**替换** summary（`_workspace_conflict_summary`），此时我追加的提示文本会从可见 summary 中消失。核下来：冲突分支会把原 `outcome.result_summary`（含我的提示）存入 `metadata["sandbox_output"]`（`:3598` 截断 8000 字节）且 `metadata` 经 `{**outcome.metadata, ...}` 保留我的 `git_side_effect_commands` 键——提示**未彻底丢失，只是降级为 metadata**。**受影响但可接受**：冲突本身已是更强的 `workspace_sync_conflict` 语义（`_WORKSPACE_CONFLICT_SAFE_REMEDIATION` 指导模型用 read_file+edit_file 修复），提示降级不产生错误行为；且该路径另有方向 1 的 `reverted` 记账兜底。」

**Q6+Q7 最优且必要？**
- **裁决**：通过（一处已知风险：见下）。
- **正向依据**：①枚举 ≥3 候选：更简单（只加 log warning，无 summary 提示/无 metadata）／本方案（log + metadata + 一句提示）／更彻底（flush 侧逐 commit 枚举 git 历史 + 文件 watch 常驻监听）。从 Ponytail 阶梯最低档起：更简单档丢了「给模型的语义提示」与「日志轮转后仍可审计的 metadata」，故选本方案；更彻底档在 Phase 1 已被证伪（逐 commit 枚举昂贵且漏 `clean`/`restore --staged`，文件 watch 过度工程）。②修的是「已发生的故障」还是「臆想风险」——事件已实锤发生过（rootcause5），但复发需「失忆 + git 探索」双前提，频率低，故**诚实定性为 P1 预防**（非 P0 已损），且方案强度与「稀有事件」匹配（轻量告警，非重型拦截）。
- **负向探针**：「我试过用更简单一档（纯 log warning，无 summary/metadata）能否解决问题——**能覆盖『告警』但丢『记账存活』**：容器日志会轮转，且失忆的模型看不到 log、需要 summary 里的提示才能『消解』（触发-消解分离的消解端）。故更简单档不完整，本方案是必要的最低完整档。**已知风险**：bash 静态识别可被 `git -C path checkout` / `echo "git checkout"` / heredoc 等形式绕过或误报——缓解：这是 recall 层（宁滥勿缺），误报只多一行 warning、绕过只丢一次告警，语义判断在 LLM/人；不承诺安全边界（与 `check_code_safety` 同声明）。」

**Q7 会破坏 Clawith 特性吗？**
- **裁决**：通过。
- **正向依据**：逐条过宪法 C1（证据先行——rootcause5 双铁证）C2（最小改动——单函数 + 单注入点 + 单测试文件）C3（契约与状态所有权——不动 CAS/flush/checkpoint，只加只读告警）C4（测试证行为——4.3 五条测试）C5（保留既有工作——不拦不改执行流程，方向 1 守卫原样）C6（模块边界——检测纯函数放 security.py，记账放 agent_tools.py 既有 logger）。工作区红线：durable run/checkpoint 不碰、多租户隔离不碰（无新共享态）、exactly-once 不碰（无新外部写）、前缀缓存不碰（不改 prompt/tool schema 字节）、WS 状态机/飞书通道不碰。
- **负向探针**：「我把方案对每个红线过一遍，重点找『checkpoint 语义』与『前缀缓存稳定性』——方案只改工具结果 summary 文本与一个 logger.warning，不进 graph state、不动 tool schema 定义字节，故不碰 checkpoint 语义、不破坏前缀缓存。**结论：不碰**。」

---

## 六、Phase 5：实现闭环（待实现后执行）

方案实现为 diff 后，用 `code-review` 对照本方案复核（Spec 轴）：diff 是否忠实实现 4.1/4.2/4.3、无范围外改动、无「评审时没提过的机制」（如擅自加 flush 侧逻辑或 stash 检测）。diff 与方案一致才算闭环；偏离则回改或回 Phase 3/4 重审。

**红线提醒（实现阶段）**：禁 `ruff format`（agent_tools.py 格式漂移），只 `ruff check`；改动前 `scripts/arch-guard.sh`；选择性提交（`git add` 指定文件后无 pathspec `git commit`），提交前 `git status` 剥离并行会话的他人改动。

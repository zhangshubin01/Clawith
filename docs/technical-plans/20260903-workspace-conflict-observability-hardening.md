# 2026-09-03 工作区冲突可观测性加固（A/B/C1/C2）

- **状态**：方案待拍板（代码未动；全部行号已对照当前 HEAD b47ee6c6 真实代码逐处核对）
- **前置**：
  - 第三代修复（ADR-0011 直写刷新 seam + 熔断重置语义 + 冲突后重新物化，6a5a9928）已于 2026-09-01 上线，验收结论见工作区记忆 `workspace-sync-conflict-root-cause` 第三代验收段
  - 本方案不改变任何发布语义，纯粹补齐取证/观测能力
- **范围**：A（refresh no-op 日志升级）+ B（冲突详情落账本）+ C1（TOOL span 捕获脱敏参数）+ C2（冲突时保留沙箱输出）；C3（code 参数模式脱敏）列为备选不推荐

## 修订记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1 | 2026-09-03 | 初稿：验收结论 + 双端盲区证据 + A/B/C1/C2 代码级设计（全部行号 read_file 实读核对） |
| v2 | 2026-09-03 | 评审修订（结合真实任务执行日志 + 账本原行复核）：§3.1 A 重写——原方案只覆盖 4 条静默出口中的 1 条（S2），且时序证据表明 S2 恰是最不可能候选；改为四出口全日志 + seen-set 噪音/信号区分；新增 §8 评审记录 |

## 1. 背景：验收结论与双端盲区（已核实）

### 1.1 第三代修复验收结论（已向用户汇报）

上线 ~21h、49 runs、519 次工具执行，账本仅 2 次冲突，全在 run e21b6ac7（运行于 4d3fe431 版本，
Langfuse release 字段实锤）：

1. **14:28:55 冲突×6**（全 `.git/` 路径）= 4d3fe431 初版「.git 两侧放行」CAS churn（materialize
   预算截断 → `_drop_incomplete_git_dirs` 删 .git → exit128 线），非三修复失败，后续版本已根治。
2. **14:34:53 冲突×1**（`memory/reflections.md`）：取证链证明第三方写被排除（`workspace_file_revisions`
   表 14:20–14:40 仅 2 条 edit_file 记录）、refresh 机制生产正常、受控复现实验证明 refresh 生效即
   flush 干净。**定性：14:32:17 那次 refresh 未落到 state（manifest entry 停旧 token），最后一环因
   日志轮转丢失无法 100% 复原。结果面达标：1 次即自愈，无 8 连败，熔断 0 误伤。** b47ee6c6 上线后
   生产日志零 `WorkspaceFlushConflict`。

### 1.2 个案取证断点

14:34:53 冲突的完整归因缺最后一环：「14:32:17 的 refresh 为何没生效」无法 100% 复原。三个候选
假设（refresh 时 state 已丢 / state closed / run_id 为空）无法验证，因为生产不打 debug 日志
（对应方案 A）。同时**模型当时跑的是什么代码**两头都查不到（对应 B/C1/C2）。

### 1.3 双端盲区实锤（代码级）

- **Langfuse 端有意不存参数**：`app/services/observability/tracing.py` `observe_tool`
  （:554–573）docstring 明写 "Tool arguments are intentionally not captured"，process-view only，
  权威在账本。
- **账本端 execute_code 参数整键打码**：`sanitize_tool_arguments`
  （`app/services/agent_runtime/tool_execution.py`:405–431）按
  `builtin_sensitive_paths(tool_name)` 整键覆盖为 `[REDACTED]`；
  `builtin_tool_definitions.py`:4008 中 `"execute_code": ("code", "env", "environment")`。
- **冲突分支丢弃沙箱 stdout**：冲突时 `outcome.result_summary` 被
  `_workspace_conflict_summary`（agent_tools.py:3531）**替换**而非拼接。
- **冲突详情不落账本**：`flush_temp_workspace` 冲突时 warning 日志已含
  operation/path/condition/expected_version/current_version 全套字段，但返回值只带路径列表
  （`conflicted`），且整包 `workspace_publication` dict 被账本白名单过滤丢弃（见 §3.2）。
- 两端互补失效的结果：14:34:53 取证只能靠旁证（revisions 表、日志、受控复现）。

## 2. 目标与非目标

**目标**：让下一次同形态冲突发生时，无需日志轮转、无需旁证，账本 + Langfuse 双端即可回答：
「模型跑了什么（脱敏范围内）→ CAS 用了什么 token → storage 实际是什么 token → 哪一步失配」。

**非目标**：不改变 CAS/发布/熔断语义；不做 code 全文存储（安全红线）；不做 Langfuse 管道改造
（管道健康，是埋点设计问题）；不做 C3。

## 3. 方案设计

### 3.1 A：refresh 静默出口全量可观测 + 噪音/信号区分

**文件**：`backend/app/services/sandbox/local/run_workspace.py`、`backend/app/services/agent_tools.py`

**背景（评审实读核实）**：refresh 链路共有 4 条静默/低可见 no-op 出口，v1 方案只覆盖其中 1 条：

| 出口 | 位置 | 现状 | 个案相关性 |
|---|---|---|---|
| S1 `if not run_id: return` | agent_tools.py:2290–2291（`_refresh_run_workspace_after_direct_write`） | 零日志 | 中（无 runtime 上下文的直写） |
| S2 `task is None` | run_workspace.py:160（`refresh_run_workspace_path`） | debug | 低（14:30:18 物化后任务存活，期间无 discard 事件） |
| S4 `state.closed` | run_workspace.py:170–171 | 零日志 | 低（close 先 pop，仅竞态可达） |
| S5 `version` 不可见 | agent_tools.py:2315–2316 | 零日志 | 中（写后读版本的窗口） |

时序证据（账本原行复核，见 §8）：14:28:55 冲突 discard 关闭工作区 → 14:30:18 成功 execute_code
重新物化（workspace_saved_count=1）→ 14:32:17 edit_file 成功（refresh 调用点实锤存在，
`_edit_file_outcome` :4745–4748）→ 14:34:53 冲突。**refresh 必然静默失效了，但最可能的候选是
S1/S5/异常路径，而非 S2**——v1 的 A 对这个个案无效。因此 A 改为：

**A-1 四出口全补日志**（reason 标记）：

- S1（agent_tools.py:2291 前）：`logger.info("[RunWorkspaceRefreshSkipped] path={} reason=no_run_id")`
  ——info 即可：无 runtime 上下文的直写是常态（审批后处理等），出现频率有界；
- S2（run_workspace.py:160）升级 warning，并携带 seen-set 区分（见 A-2）；
- S4（:170 后）：`logger.warning("[RunWorkspaceRefreshSkipped] run_id={} path={} reason=closed")`；
- S5（agent_tools.py:2315 前）：`logger.warning("[RunWorkspaceRefreshSkipped] run_id={} path={} reason=version_invisible")`。

异常路径（:2319/:2337）已有 warning，不动。

**A-2 seen-set 区分噪音与信号**（run_workspace.py）：

- 模块级 `_materialized_run_ids: set[str]`，在 `_get_or_create_state` 成功创建 state 时加入；
  `close_run_workspace` 时不移除（保留「见过」历史，量级 = 有沙箱的 run 数，有界）。
- S2 分支改为：`seen = run_id in _materialized_run_ids`；`seen` 时 warning
  （**「物化过的工作区消失」= 14:32:17 个案的精确信号**），`not seen` 时 info
  （direct chat 等从不物化工作区的 run 的每次直写都会命中，warning 会刷屏——v1 未识别此噪音源）。

效果：下次同类个案，四出口必留痕；「见过又消失」直接指向 discard/close 竞态或物化失效，
一次个案即可定根因（不再需要 revisions 表旁证链）。

### 3.2 B：flush 冲突详情落账本

**文件**：`backend/app/services/agent_tools.py`、`backend/app/services/agent_runtime/tool_execution.py`

#### 3.2.1 flush_temp_workspace 返回值新增 `conflict_details`

`flush_temp_workspace`（agent_tools.py:2027，签名注解 `dict[str, list[str] | int]`）两处冲突点
各 append 一条详情：

- **写冲突** :2145（`conflicted.append(rel_path)` 处，`write_bytes_if_match` 返回
  `not result.ok` 且未收敛）：新增

  ```python
  conflict_details.append({
      "path": rel_path,
      "operation": "write",
      "condition": "version_match" if entry else "require_absent",
      "expected_version": entry.base_version_token if entry else None,
      "current_exists": result.current_version.exists if result.current_version else None,
      "current_version": result.current_version.token if result.current_version else None,
  })
  ```

  字段与 :2146–2160 现有 warning 日志一一对应（同一数据源）。
- **删除冲突** :2216（`delete_if_match` 返回 `not result.ok`）：同上，`operation="delete"`、
  `condition="version_match"`。
- 返回 dict（:2163–2169、:2233–2239 两处 fail 提前返回 + :2263 起最终返回）统一加
  `"conflict_details": conflict_details`；签名注解（:2030）放宽为
  `dict[str, list[str] | list[dict[str, Any]] | int]`。
- 类型注解同步放宽（已 grep 全部 `flush_result` 消费点）：
  - `recover_publication`（:3104）参数 `flush_result: dict[str, list[str]]` → `dict[str, Any]`；
  - `_derived_publication_note`（:3551）参数 `dict[str, list[str] | int] | None` → 加
    `list[dict[str, Any]]` 联合项；
  - `gateway_flush_result`（:2988）声明 `dict[str, list[str]] | None` → 与返回值注解一致；
  - 网关路径 fallback 字面量（:3227–3233）可选补 `"conflict_details": []` 保持形态一致
    （该路径冲突时 :3206 直接 raise，无冲突 outcome 落点，非必须）。

#### 3.2.2 冲突分支把详情带进 metadata

执行点一（execute_code 主路径，agent_tools.py:3293–3304）：

```python
candidate_metadata = await reconciliation_metadata()
metadata = {**outcome.metadata, "workspace_publication": flush_result, **candidate_metadata}
if flush_result["conflicted"]:
    recovered = await recover_publication(outcome, flush_result)
    if recovered is not None:
        return recovered
    return _typed_workspace_publication_failure(
        _workspace_conflict_summary(flush_result["conflicted"]),
        "workspace_sync_conflict",
        metadata=metadata,
        safe_remediation=_WORKSPACE_CONFLICT_SAFE_REMEDIATION,
    )
```

改为在 :3294 后加：

```python
if flush_result.get("conflict_details"):
    metadata["workspace_conflict_details"] = flush_result["conflict_details"]
```

执行点二（本地内容适配器路径，:2863–2865，目前无 metadata 参数）：同样补
`metadata={"workspace_conflict_details": flush_result.get("conflict_details")}`（仅当非空）。

#### 3.2.3 白名单放行

`_RESULT_METADATA_KEYS`（tool_execution.py:87–211）新增 `"workspace_conflict_details"`。
**核对结论**：白名单现含 workspace_* 扁平键 8 个（`workspace_path`/`workspace_candidate_ref`/
`workspace_resolution_status`/`workspace_saved_count`/`workspace_pending_count`/
`workspace_conflicted_count`/`workspace_unverified_count`/`workspace_resolution_action`），
**不含** `workspace_publication`（整包 flush_result 一直被 `_bounded_result_metadata` :451
严格白名单过滤丢弃——这正是 14:34:53 账本只有计数键没有详情的原因）。新键放行后，
`_bounded_result_metadata` 的 `_sanitize_json`（:452）会递归再脱敏 + 64KB 总量校验（:477 超限
raise），详情极小，安全。

#### 3.2.4 既有测试影响

`backend/tests/test_agent_tools_storage_workspace.py`:556 有 flush 返回值**精确相等**断言
（`assert result == {...}`），需补 `"conflict_details": []`。
`test_workspace_publication_filter.py`:204/:361 只按键取值（`derived_skipped_count`），不受影响。

### 3.3 C1：TOOL span 捕获脱敏参数

**文件**：`backend/app/services/observability/tracing.py`、`backend/app/services/agent_runtime/tool_step_service.py`

最小实现（无需动 `GenerationHandle`）：

- `observe_tool`（tracing.py:554）签名加 `input: Any = None`，透传给 `_observe_span(input=input)`
  ——`_observe_span`（:496）**已内建**该能力：:515
  `span_input = mask_text(input) if (capture_input and input is not None) else None`，
  `mask_text`（:261）逐字符串 `_MAX_STRING_CHARS`（:76，4000）截断 + 脱敏，与
  `observe_generation` 同一机制。docstring「Tool arguments are intentionally not captured」
  改为「arguments are captured in ledger-sanitized form」。
- 六个调用点（tool_step_service.py:2428/:2554/:2664/:2708/:2742/:2821，均已核对为
  `with observe_tool(...) as tool_handle:` 且 `reservation` 在作用域内）统一加
  `input=dict(reservation.execution.sanitized_arguments or {})`。
- 数据来源 `reservation.execution.sanitized_arguments` 在 reservation 时由
  `sanitize_tool_arguments(arguments, sensitive_paths=builtin_sensitive_paths(tool_name))`
  （tool_step_service.py:1073–1076）产出：JSON-safe、递归脱敏、敏感路径整键 `[REDACTED]`
  ——**execute_code 的 `code` 在进 span 前已是 `[REDACTED]`**，C1 不改变安全面，但让
  edit_file/write_file 等其余工具参数在 Langfuse 端可考（参数不全的两端盲区只剩 code 本体，
  由 C2 的 stdout 补位）。
- `product_reconciler.py`:325 另有一处 observe_tool 调用（fence-reconcile 路径），列为可选覆盖
  （不在本方案主范围）。

### 3.4 C2：冲突时保留沙箱输出

**文件**：`backend/app/services/agent_tools.py`、`backend/app/services/agent_runtime/tool_execution.py`

冲突分支（:3299）里 `outcome.result_summary`（沙箱 stdout/执行摘要，上限 1,000,000 字符，
tool_execution.py:1991 校验）被 `_workspace_conflict_summary` 替换，stdout 永久丢失。改为在
:3294 组装 metadata 时附带：

```python
if outcome.result_summary:
    metadata["sandbox_output"] = _truncate_utf8(outcome.result_summary, max_bytes=8000)
```

- 复用既有 head/tail 截断 `_truncate_utf8`（tool_execution.py:433–448，带
  `...[tool result archived]...` 标记）。
- 白名单（tool_execution.py:87）新增 `"sandbox_output"`；`_bounded_result_metadata` 的
  `_sanitize_json` 对字符串再做 `_normalize_text(redact=True)` 脱敏（:391–399），双保险；
  8000 字节远低于 64KB 总量上限（:86 `_RESULT_METADATA_MAX_BYTES`）。
- 执行点二（:2863–2865）对称处理（该路径同样丢弃 outcome.result_summary）。

### 3.5 白名单新增键汇总

| 键 | 方案 | 来源 | 上限 |
|---|---|---|---|
| `workspace_conflict_details` | B | flush 冲突详情（path/operation/condition/expected/current） | 冲突路径数×~200B |
| `sandbox_output` | C2 | outcome.result_summary | 8000 B（`_truncate_utf8`） |

## 4. 备选 C3（不推荐默认做）

`code` 参数从整键 `[REDACTED]` 改为模式脱敏后保留（凭据形态 `****`，如 AWS key/JWT/私钥
特征模式）。**不推荐理由**：凭据可任意形态（用户脚本内嵌 token 无固定模式），漏网即泄露到账本；
收益仅是「能看到模型代码结构」，C2 的 stdout 已覆盖主要取证需求。若未来确有需要，应做成显式
配置开关（默认关闭）+ 白名单模式集 + 单独评审。

## 5. 测试计划

新增/修改测试（backend）：

1. **A**：`tests/test_agent_tools_storage_workspace.py`（或 run_workspace 相关测试）
   - seen-set 区分：物化→close→refresh miss 断言 warning；从未物化→refresh miss 断言 info；
   - S5：模拟 storage 版本不可见时输出 `reason=version_invisible`。
2. **B**：`tests/test_agent_tools_storage_workspace.py`
   - 修改 :556 精确相等断言补 `"conflict_details": []`；
   - 现有 CAS 冲突测试（同文件）加断言：冲突返回 `conflict_details` 含
     `path/operation/condition/expected_version/current_version`，且 expected/current 与
     storage 实际 token 一致。
3. **C1**：`tests/test_observability_tracing.py` 加一例：observe_tool 传 `input=...` 时，
   fake client 收到的 span input 等于 mask_text 后的脱敏参数（含 execute_code code=
   `[REDACTED]` 的路径）。
4. **C2**：`tests/test_sandbox_execution_policy.py` 冲突路径加断言：失败 outcome 的 metadata
   含 `sandbox_output` 且 ≤8000 字节、经 `_bounded_result_metadata` 过滤后仍保留该键。
5. **白名单**：确认无测试枚举 `_RESULT_METADATA_KEYS` 全集（已 grep 核实测试目录无引用）。

回归：`cd backend && .venv/bin/python -m pytest tests -p no:cacheprovider --ignore=tests/test_sso_toggle.py`
全量；`ruff` 全绿；`scripts/arch-guard.sh` 通过。

## 6. 部署与验收

按 skill `clawith-prod-deploy` 执行：部署前检查在途 run（避免部署杀 run）、打回滚 tag、
`-p clawith-agent`、生产 .env 清华源。用户拍板后实施。

上线后验收（对照基线：b47ee6c6 至今零 `WorkspaceFlushConflict`）：

1. 容器日志仍零 `WorkspaceFlushConflict`（本方案不改发布语义，不应新增）；`[RunWorkspaceRefresh*]`
   日志分布符合 A-2 预期：`reason=no_run_id`/`version_invisible` 为有限频次，`not seen` 的
   refresh miss 仅 info、`seen` 的 miss 为 warning 且应罕见。
2. Langfuse 任意新 TOOL span：input 非空且为脱敏形态（edit_file 参数可见、execute_code 的
   code=`[REDACTED]`）。
3. 受控复现（scratchpad 既有三段式脚本 + 一个真实测试 run）触发一次冲突后查
   `agent_tool_executions.result_metadata`：含 `workspace_conflict_details`（expected/current
   token 齐备）+ `sandbox_output`，可直接定位「CAS 用的什么 token、storage 实际什么 token」。
4. 噪音兜底：A-1 的 info 级条目若仍偏多，个别 reason 可降 debug（forward-fix）。

## 7. 回滚与风险

- 全部改动为**加法**：新 dict 键、新 span input、新白名单键；无 DB migration（result_metadata
  为 JSON 列）、无发布语义变化、无 API 变化。回滚仅需标准回滚 tag 流程；预期不需回滚，问题
  可 forward-fix。
- 主要风险点及缓解：
  - B 返回结构变化 → 唯一精确相等断言已列入修改清单；
  - C1 span 体积 → 参数经 sanitize（code 已打码）+ mask_text 4000 字符/串截断，与既有
    generation input 体量同级；
  - C2 元数据体积 → 8000 字节硬上限，双脱敏；
  - A 日志噪音 → A-2 seen-set 已把「从不物化工作区」的高频 benign 场景压到 info；如个别
    reason 仍偏多，可逐条降 debug（forward-fix，不阻塞）。

## 8. 评审记录（2026-09-03，结合真实任务执行日志）

评审输入：当前 HEAD b47ee6c6 代码实读（refresh 全链路、flush 冲突分支、白名单、`_result_message`
透传面）、账本原行重新拉取（run e21b6ac7 的 14:28:55/14:32:17/14:34:53/14:37:31 四行）、
当前容器日志（`[RunWorkspaceRefresh] refreshed path` 正常工作、`WorkspaceFlushConflict` 计数 0）。

九个问题的结论：

1. **根因正确性**：机制链正确（refresh 未落 state → manifest 陈旧 → CAS 冲突），但失效出口
   未定位——代码存在 4 条静默出口（S1/S2/S4/S5），v1 方案只覆盖 S2，而账本时序表明 S2 恰是
   最不可能候选（14:30:18 重新物化后任务存活、期间无 discard）。已按「四出口全日志」修订（§3.1）。
2. **根治方案正确性**：A+B 不是根治，是「下一次能根治」的取证基础设施；根治要等拿到出口证据。
   定位准确、前置正确。
3. **参考资料正确性**：内部引用（ADR-0011、白名单、日志词表、账本行）全部复核通过，行号实读
   无误；外部参考（reference-projects 的 LangGraph 资料）对本设计决策无直接借鉴，正确参考集
   就是内部权威源——方案未引用不适用资料。
4. **是否引起其他问题**：唯一破坏面 = flush 返回值精确相等断言（已列入修改清单）；新白名单键
   经 `_result_message`（tool_step_service.py:456）核实**不会透传给模型**，零泄露。
5. **是否搞坏其他逻辑**：否。B 为纯加法（新 dict 键 + 新白名单键），消费方全部 `.get`/键访问，
   容忍新键；类型注解放宽点已穷举（§3.2.1）。
6. **是否最佳方案**：B 是最小正确解（专用事件表属过度设计）；A 必须修订（已修订）。修订后
   A+B 为当前成本/收益最优。
7. **是否多余**：不重复任何现有能力（账本只有计数键、日志会轮转，冲突详情确无持久化处）；
   但收益是条件性的（冲突复发才兑现），A（修订版）额外覆盖未来 refresh 机制 bug，价值更稳。
8. **可复用逻辑**：已在复用——`[RunWorkspaceRefresh*]` 日志词表（A 扩展）、warning 日志现成
   字段（B 的 conflict_details 数据源）、白名单/过滤机制、`_typed_workspace_publication_failure`
   的 metadata 参数。无新基础设施。
9. **是否破坏 Clawith 特性**：否。发布/CAS/熔断语义零改动；元数据不透传模型；日志升级不影响
   任何功能路径。

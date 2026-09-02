# ADR-0011: 直写工具刷新 Run 工作区（消除同 run 虚假 publication conflict）

- **状态**: 已接受（2026-08-31）
- **前置**: 2026-08-29 run 32292476 连 7 败事故（workspace_sync_conflict，根因调查见
  `.scratch/workspace-sync-conflict/issues/01-*.md` 与记忆 workspace-sync-conflict-root-cause）
- **废止**: 无

## 背景

Agent 工作区发布采用乐观并发（CAS by version token + `_stable_identical_storage_version`
相同内容收敛）保护跨 run / 人类编辑不被覆盖。但同一 run 内存在两类**直写 storage**的路径，
完全绕过 run-scoped 临时工作区（`use_run_workspace` 每 run 每进程只物化一次，manifest 记录
每文件 base_version_token/base_hash）：

1. **Mutation 工具族**：`write_file` / `edit_file` / `delete_file` / `move_file`
   （`_execute_workspace_mutation` → `write_workspace_file` 等直写 storage）。
2. **Per-call 物化 + sync_back 工具族**：`convert_*`、`generate_image_*`、`execute_code_e2b`、
   `publish_page` 等（`_run_with_temp_workspace(_outcome)` 每调用重新物化，`flush_temp_workspace`
   用自己的新鲜 token 回写 storage）。

后果（事故实锤）：直写后 run 工作区的 temp 文件内容与 manifest token 停在旧快照；后续
`execute_code` 的 bash 修改到这些路径时，flush 用陈旧 token CAS → 冲突；冲突后 manifest
不刷新，**该路径在本 run 剩余生命期内每次 flush 必冲突**（7 连败 = 一次 bump + 确定性
git 增量的连锁）。恢复路径（recover_publication → `apply_candidate(require_base_match=True)`）
同样因「第三方版本」拒写——但此处第三方就是同 run 自己，属误伤。自愈只有进程重启
（重新物化）。另一症状：execute_code 视图与 read_file 视图不一致（模型困惑并重复劳动）。

## 决策

直写工具写 storage 成功后，**同步刷新当前 run 的临时工作区**（文件内容 + manifest
token/hash），单一实现挂在 `run_workspace.py`，两个薄调用点：

1. `_execute_workspace_mutation`：每次成功 write/edit/move/delete 后，对受影响路径刷新
   （写入 = 物化新内容 + 新 token；删除 = 移除 temp 文件 + manifest 条目；move = 目标物化 +
   源移除）。
2. `flush_temp_workspace` 末尾：flush 成功且存在**另一个** run-scoped 工作区（身份判等
   `state.workspace is not temp_workspace`）时，对 updated/deleted 路径刷新该工作区——
   覆盖 per-call 物化工具族。

### 安全边界（为什么刷新不破坏冲突保护）

刷新钩子只在「当前 run 自己的工具执行」上下文触发（`sandbox_run_scope_id` contextvar，
`command_worker` 在 command 执行期 set；`_execute_tool_direct` 审批后执行路径无 run
context，刷新 no-op）。因此刷进 run 工作区的永远是**本 run 自己刚写入的内容**；人类编辑
（`actor_type="user"`）与其他 run 的写入不触发刷新，仍走原冲突保护（宁可失败不覆盖）。
刷新与在飞 execute_code 由 `state.lock` 串行化（同 run 工具调用本就串行，锁为正确性保险）。

### 可靠性

best-effort + 日志：storage 写成功但刷新失败时回到旧行为（冲突保护兜底，fail-safe 方向），
不把已成功的文件写入变成工具失败。token 取刷新时刻 `storage.get_version` 的真实版本、
hash 用与 flush 相同的 `content_hash_bytes`——与收敛语义同源，不制造「相同内容假新版本」误冲突。

### 新路径物化

write 到物化集之外路径时，刷新顺带把该文件物化进 temp + manifest（保证 execute_code
视图一致）；单文件远小于 budget；超 `TOOL_MATERIALIZE_MAX_FILE_BYTES` 则不物化并记日志
（保留 unloaded 语义）。

## 业界对照（决策依据）

| 路线 | 结论 |
|---|---|
| 共享挂载单视图（OpenHands rw bind mount / CubeSandbox host-mount / gptme bwrap bind / SWE-agent 工具上传进容器） | **长期收敛方向**，本次不采纳——Clawith 的 storage + per-run 物化 + CAS 是隔离/回滚/跨会话冲突保护的既有资产，退回裸挂载损失大于收益；刷新式修复是消除「同 run 双写面」的最小步 |
| 每次工具调用重新物化（现状 per-call 族） | 否决——大 workspace 物化成本高（budget 扫描 + skills 快照），且不能消除 run 内直写与 execute_code 之间的陈旧窗口 |
| 冲突后自动重新物化 | 否决——7 连败变 1 败，该次产出仍丢，视图不一致不治 |
| **直写后刷新 run 工作区（本 ADR）** | **采纳**——单 seam、同 run 串行保证安全、视图一致性顺带修复 |

## 长期方向（记档，不在本次范围）

直写工具下沉进执行环境（与 execute_code 共享同一文件视图），从结构上消灭双写面；
读侧一致性提示（execute_code 物化时发现 storage 比 manifest 新 → 告知模型）作为后续改进。

## 勘误（2026-09-01，随第三代修复落库）

1. **前提已过时**：本 ADR 决策节假定的接缝「Mutation 工具族
   （`_execute_workspace_mutation`）」不再承载真实 run 的直写——Runtime 自
   3d359c28/ad606146 起走 typed-outcome 路径（`_write_file_outcome` 等），646be775 实现的
   钩子因此对真实 run **从未生效**（2026-08-30 连 8 败事故=同一根因第二次复发，见记忆
   workspace-sync-conflict-root-cause）。
2. **修正后的接缝清单**：见 `docs/technical-plans/20260901-workspace-publication-runtime-seam-fix.md`
   §3.2——typed 四函数（write/move/delete/edit）八个成功出口（主路径 + recovered）挂 refresh，
   legacy `_execute_workspace_mutation` 收敛到同一 helper（零行为变化），flush 钩子保持不变。
3. **经验条款**：接缝类修复必须配「路径级回归测试」（断言刷新生效），否则运行时改道后
   钩子死亡无人知晓——本 ADR 初版无此类测试，是复发事故的共因。

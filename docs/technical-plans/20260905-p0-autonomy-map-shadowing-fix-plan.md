# P0 生产级修复方案：A1 两份 `_TOOL_AUTONOMY_MAP` 遮蔽 + A2 `check_tool_autonomy` truthy 死代码

日期：2026-09-05（基线 HEAD `18d489aa` 实读重核）
状态：已实现并复核通过（见 §4 七角度评审 + §7 实现闭环记录）
前置：`20260905-maintainer-gate-g3-g4-production-plan.md`（本票 = 其 §3.4/§6「两个 bug 一并修」独立成票，并按本方案 §5 更正三处）

---

## 0. 裁决（结论前置）

**评审通过**（7 问全过，无阻断项）。修复 = 两处删除、零运行时行为变更：

1. **A1（MAP 遮蔽）**：删死定义「意图版 MAP」（`agent_tools.py:2898`，9 键），保留「生效版 MAP」（`:29648`，5 键）为唯一 `_TOOL_AUTONOMY_MAP`。**不「补齐 6 工具映射」**——经双源核实，补齐是惰性动作（见 §2.3/§3.1）。
2. **A2（truthy 死代码）**：删 `check_tool_autonomy`（`agent_tools.py:29657`）函数体 + `tool_bridge.py` 3 处调用 + 动态塞键 + **随之变死的整条 ACP 自主权 stub**（`_handle_autonomy_blocked`/`_AUTONOMY_STOP_THRESHOLD`/`_autonomy_counts`/`WORKSPACE_WRITE_TOOLS` 导入）+ 惰性导入清理（C16）。
3. **新发现 A3（`send_external_message` 孤儿键）**：16 agents 配 L1/L3、零代码读取、零 enforce 点。**单独记、不在本票修**（与 `modify_soul` 同类「声明但零执行点」，属配置/架构项）。

> **二次复审记录（本轮）**：复审补证 C16 后，将 A2 删除范围从「`check_tool_autonomy` + 3 调用点 + 塞键」扩为「+ 整条 ACP 自主权 stub」——原方案 §3.2 漏删了删块后随之变死的 4 个符号（`_handle_autonomy_blocked` 及其计数器、`WORKSPACE_WRITE_TOOLS` 导入），已折入 §3.2/Q4。复审结论：**仍通过**，无其他异议。

---

## 1. 参考资料对比（≥10 项目，reference-check 纪律）

决策点拆解：
- **D1** 工具→action 映射的「单一事实源」，避免同名重复定义遮蔽（Python 模块级常量 footgun）。
- **D2** 死代码/truthy bug 清理纪律（unused 参数、恒真条件）。
- **D3** 废弃/隐藏工具在权限映射里的处理（`send_feishu_message` 被 LLM 隐藏却仍配 policy）。
- **D4** 多执行路径（typed durable / legacy / ACP）各自如何挂 autonomy 闸门。

| # | 参考项目 | 决策点 | 结论 / 偏离理由 |
|---|---|---|---|
| 1 | claude-agent-acp `docs/permission-extension.md`（2026-09-05 读真实源码） | D4 | 标准 ACP 里 Edit/Write 走 `session/request_permission`（客户端渲染 allow-once/reject）；Clawith 自定义 RPC `fs/write_text_file`/`fs/edit_text_file` **直写**是偏离。`check_tool_autonomy` 是这条偏离路径上的废闸门——删它不改变协议偏离本身（偏离既存、有意，见主方案 §8.3） |
| 2 | letta-code `memory-confinement`（已归档） | D4 | 借鉴「单一 enforce 点 + fail-closed」，但**已归档不当前置**；Clawith 定位治理层（`execute_code` 经 `sync_back` 可绕过），fail-closed 无意义 |
| 3 | deepagents `middleware` | D4 | 在唯一执行边界加纯函数判定；印证「闸门该挂在执行入口、不散落多份映射」 |
| 4 | deepagents-in-action（ch9 HITL，`20260904-deepagents-in-action-study.md`） | D4 | HITL 审批机制对照；Clawith 的 L3 审批是运行态裁决，与 D1 静态映射无关 |
| 5 | bisheng OpenFGA（`20260905-bisheng-study.md`） | D1/D4 | 工具→关系 tuple 的**单一授权表**；反衬 Clawith 两份 MAP 之弊。但 16 类型授权模型过重，不引入 |
| 6 | jcode（Safe/Low/Confirm/Catastrophic，`20260905-jcode-study.md`） | D1 | 确定性工具风险**分类表 = 单一事实源**，无重复定义；Clawith 应与之对齐「一份表、一处定义」 |
| 7 | langchain-ai agent-inbox（`20260903-agent-inbox-hitl-ux-study.md`） | D4 | HITL 审批 UX 对照；与 D1 静态映射正交，不合并 |
| 8 | langgraph `human_in_the_loop`（interrupt） | D4 | 人工确认 seam；Clawith 走工具结果 error_code 而非 interrupt，本票不涉 |
| 9 | 12-factor-agents（ownership boundary） | D4 | 「谁拥有 workspace 写权限」显式化；本票只删死代码，不新增边界（边界属主方案 ①） |
| 10 | skill-creator（skill registry） | D1 | 单一技能注册表去重；印证「单一事实源」是通用工程纪律 |
| 11 | dify（维护者权限门控，见 `docs/reference-projects-index.md`） | D4 | 名单门控先行者；主方案 ① 的参考，本票不涉 |
| 12 | herdr / langbot / orca（`20260905-herdr-study.md` / `-langbot-study.md` / `-orca-study.md`） | D3/D4 | 均无「同名模块级 dict 遮蔽」同类缺陷——**诚实负结论**：这是 Python 语言级 footgun（模块级重赋值不报错），非某框架既有模式，故无可抄解，只能靠删重复 + 注释 + 回归断言防复发 |

**诚实负结论（明示）**：D1 的「同名模块级常量重复定义遮蔽」在参考清单里**无逐字可抄项**——它不属于任何框架的权限模型设计，而是 Python 重赋值语义导致的工程事故。处置手段（删重复 + 注释边界 + 断言唯一）来自通用软件工程纪律，非某参考项目的机制。

---

## 2. 双源定根因（真实代码 + 真实 PG 台账，缺一不可）

### 2.1 代码级事实（HEAD `18d489aa`，函数名定位、行号仅作约）

> **行号口径**：下表行号按**工作树**实测（工作树携带未提交的 `flush_temp_workspace`/`_refresh_run_workspace_after_direct_write` run-id 可观测性改动，与本票无关，约 +8 行），故意图版 MAP 为 `:2898`、生效版为 `:29648`；对应**已提交 HEAD** 的 `:2890`/`:29640`（Phase 4 review §0 口径）。**以函数名定位为准，行号随工作树/HEAD 漂移**。

| # | 事实 | 出处 |
|---|---|---|
| C1 | **意图版 MAP**（9 键）：write_file/move_file→`write_workspace_files`、delete_file→`delete_files`、send_feishu_message→`send_feishu_message`、send_message_to_agent→`send_message_to_agent`、send_file_to_agent→`send_file_to_agent`、web_search→`web_search`、execute_code/execute_code_e2b→`execute_code` | `agent_tools.py:2898-2908` |
| C2 | **生效版 MAP**（5 键）：write_file/edit_file→`write_workspace_files`、delete_file→`delete_files`、execute_code/execute_command→`execute_code` | `agent_tools.py:29648-29654` |
| C3 | Python 模块级**同名重赋值、末次生效** → `execute_tool:6201` 与 `check_tool_autonomy:29670` 运行时都读到 5 键版 | `agent_tools.py`（模块顶层顺序） |
| C4 | legacy seam：`execute_tool:6201` `action_type = _TOOL_AUTONOMY_MAP.get(tool_name)`，`action_type` 为 None 时**整段跳过** `check_and_enforce` | `agent_tools.py:6201-6221` |
| C5 | `check_and_enforce`：`level = policy.get(action_type, "L1")`（缺键回退 L1，真分级） | `autonomy_service.py:51/:68` |
| C6 | **A2 truthy bug**：`check_tool_autonomy:29687` `allowed = policy.get(category, True)`，policy 值是 `"L1"/"L2"/"L3"` 字符串恒真 → 永不拦截；`args`/`user_id`/`notify` 三参数函数体未用（`:29658-29663`） | `agent_tools.py:29657-29690` |
| C7 | `check_tool_autonomy` 3 处调用（均经 `_INLINE_IMPORTED`）：写工具 `:1557`、execute_command `:1977`、terminal-streaming `:2129` | `tool_bridge.py` |
| C8 | 动态塞键：`tool_bridge.py:1543-1556` mutate 模块级 `_TOOL_AUTONOMY_MAP`（注入 edit_file/refactor_rename/safe_delete/reformat_code/optimize_imports/convert_java_to_kotlin/execute_command）；`:1540` `_DELETE_TOOLS_PLUGIN_GATED` 局部变量 | `tool_bridge.py` |
| C9 | 惰性导入：`_lazy_import_agent_tools`（`:987-999`）导入 `_DANGEROUS_BASH_ALWAYS/_DANGEROUS_BASH_NETWORK/check_tool_autonomy/_TOOL_AUTONOMY_MAP` 四者；`_guard_acp_dangerous_command`（`:1205`，调用 `:1965/:2105`）**只依赖前两者**，与 `check_tool_autonomy` 无关 | `tool_bridge.py:983-999/:1205` |
| C10 | typed 工具集 `RUNTIME_TYPED_APPLICATION_TOOL_NAMES`（`:648` 起）**含** write/edit/move/delete/execute_code/execute_code_e2b/web_search/send_file_to_agent/send_message_to_agent/send_channel_message/send_platform_message；**不含 send_feishu_message** | `agent_tools.py:648` |
| C11 | `_HIDDEN_FROM_LLM_TOOL_NAMES`（`:541-544`）含 `send_feishu_message` → 废弃、LLM 不可见；`execute_builtin_tool_outcome` 的 typed dispatch 无 send_feishu_message/send_message_to_agent，未匹配则 fallback `execute_tool:6068` | `agent_tools.py:541/6068` |
| C12 | `send_message_to_agent` 由 `RuntimeA2AService` 在通用 executor 之前 settle（注释 `:646-647`） | `agent_tools.py:646` |
| C13 | `DEFAULT_AUTONOMY_POLICY`（15 键）含 `send_external_message`(:29)、`send_feishu_message`(:28)、`web_search`(:35)、`send_message_to_agent`(:37)、`send_file_to_agent`(:38)、`execute_code`(:39)；注释「Key set must cover every action in _TOOL_AUTONOMY_MAP」 | `agent.py:24-40` |
| C14 | `_CROSS_SPACE_ACTION_BY_TOOL`（send_feishu_message→`external_message` 等）经 `builtin_cross_space_action`（`:4238`）**只用于 group 跨空间 gating**（`tool_step_service.py:2552`），**非 autonomy** | `builtin_tool_definitions.py:3991/:4238` |
| C15 | 测试契约：`test_agent_default_autonomy_policy.py` 的 `ALL_ACTIONS`（15 键，含 `send_external_message`）`== set(DEFAULT_AUTONOMY_POLICY)` | `backend/tests/test_agent_default_autonomy_policy.py:25-54` |
| C16 | **ACP 自主权是完整 stub**：`_handle_autonomy_blocked`（`:1234`）返回「已提交审批请求」字符串，但 `check_tool_autonomy` **从不创建 `ApprovalRequest`**（无审批流）→ 修 truthy bug 会让 LLM 收到「审批已提交」却永远等不到审批（死锁 + 误导）。`_handle_autonomy_blocked` 仅被写工具块（`:1565`）与 execute_command 块（`:1982`）调用；`_AUTONOMY_STOP_THRESHOLD`（`:1231`）/`_autonomy_counts`（`:1232`）仅被它用；`WORKSPACE_WRITE_TOOLS` 导入（`:28`）仅被写工具块（`:1541`）用 | `tool_bridge.py:1231-1246/:28/:1541` |

### 2.2 数据面事实（PG 只读，近 30 天）

| # | 数据 | 结论 |
|---|---|---|
| D1 | 6 工具执行量：send_message_to_agent **11**、send_file_to_agent **10**、move_file **5**、send_feishu_message **0**、web_search **0**、execute_code_e2b **0**（`agent_tool_executions`） | 3 个「被遮蔽」工具 30 天**零调用**（send_feishu_message/web_search/execute_code_e2b） |
| D2 | `audit_logs` 里 `autonomy_check:%` **仅 `autonomy_check:delete_files`（19 条）** | 这 6 工具**从未触发过**任何 autonomy 检查（铁证：MAP 遮蔽 → `action_type=None` → 跳过 `check_and_enforce`；且 typed 路径本就无 autonomy） |
| D3 | `autonomy_policy` 键分布（18 agents）：send_feishu_message **18**（L1/L2）、write_workspace_files **18**（L1/L2）、delete_files **18**（L1/L2/L3）、**send_external_message 16（L1/L3）**、web_search 1（L1）、send_message_to_agent 1（L1）、send_file_to_agent 1（L1）、execute_code 1（L1） | `send_feishu_message` 的 L2 分级被遮蔽静默失效；但该工具 30 天零调用，故**实际无通知被漏发**。`send_external_message` 16 agents 配 L1/L3 却零读取 |
| D4 | `agent_tool_executions` 表 schema **无 `error_code` 列**；结果/拦截证据在 `status`/`result_summary`/`result_metadata` | **更正主方案 §3.5「含 error_code」的说法**（该列不存在） |

### 2.3 根因链条（可证伪）

- **A1 根因**：同一模块内 `_TOOL_AUTONOMY_MAP` 定义两次（`:2898` 意图版 / `:29648` 生效版），Python 末次赋值生效 → 意图版 9 键**从未生效**。深层：先写意图版覆盖 send_*/web_search/move/execute_code_e2b，后为 ACP 写 `check_tool_autonomy` 时又写了一份 5 键版，未察觉同名覆盖（两版注释语义也不同——意图版说「用工具自身名避免误导通知标题」，生效版是 ACP 闸门专用）。
- **可证伪**：若根因是末次赋值遮蔽，则 `_TOOL_AUTONOMY_MAP.get("send_feishu_message")` 运行时必返回 None（末次 5 键版无此键）→ 我核 C2 生效版确无 send_* 键 → **证实**；且 D2（audit_logs 无 send_feishu_message 的 autonomy_check）与之吻合 → **双源一致**。
- **A1 危害收窄（关键修正）**：「6 工具零自主权」需收窄为——6 工具**在 legacy seam `:6201`** 零自主权，且其中 send_feishu_message/web_search/execute_code_e2b **30 天零调用**（D1）、move_file/send_file_to_agent 为 **typed 走 durable 路径不经 `:6201`**（C10）、send_message_to_agent **A2A settle**（C12）→ **实际可观测危害 ≈ 0**。真正风险是「未来有人在意图版加键却仍不生效」的陷阱。
- **A2 根因**：`allowed = policy.get(category, True)`，policy 值恒为字符串 → truthy → 永不拦截；`args`/`user_id`/`notify` 未用 → 死代码。可证伪：若 policy `execute_code=L3`，`check_tool_autonomy("execute_command", …)` 应返回 None（放行）→ 核 C6 `allowed="L3"`（truthy）→ `if allowed: return None` → **证实**永不拦截。
- **A2 为何「删」而非「修」**（关键决策依据 C16）：整条 ACP 自主权是完整 stub——`_handle_autonomy_blocked` 谎称「已提交审批请求」却从不建 `ApprovalRequest`。若只修 truthy bug，ACP 写工具/execute_command 在 L3 时会收到「审批已提交」的假反馈、却永远等不到审批（无审批流）→ 死锁 + 误导。故**删除整条 stub** 是唯一正确选项，不是「清理」也不是「止痛」。
- **A3 根因（新发现）**：`send_external_message` 在 `DEFAULT_AUTONOMY_POLICY`（C13）+ 16 agents 政策（D3）+ 测试 `ALL_ACTIONS`（C15）均存在，但**零代码读取**（grep 全 backend 仅 `agent.py:29` 一处字面量，无任何 `policy.get("send_external_message")`）。它是「声明但零执行点」的孤儿键，与 `modify_soul` 同类（主方案 §8.2 雷 2）。

---

## 3. 最小修复方案

### 3.1 A1：收敛两份 MAP 为一份（删死定义、保留生效版）

- **删** `agent_tools.py:2894-2908` 整块（含「using the tool's own name…」注释与 9 键 dict）。
- **保留** `:29646-29654`（ACP 注释头可一并简化）为唯一 `_TOOL_AUTONOMY_MAP`，内容不动：
  ```python
  _TOOL_AUTONOMY_MAP: dict[str, str] = {
      "write_file": "write_workspace_files",
      "edit_file": "write_workspace_files",
      "delete_file": "delete_files",
      "execute_code": "execute_code",
      "execute_command": "execute_code",
  }
  ```
- **在该 dict 上方补边界注释**：此 MAP 仅服务 legacy `execute_tool` seam（`caller.py:430` / ACP chained `tool_hooks.py:66/:940` / `execute_builtin_tool_outcome:6068` untyped fallback）；typed durable 工具（write/edit/move/delete/execute_code/web_search/send_file_to_agent/send_message_to_agent…）不经此 MAP（文件工具由主方案 ① `resolve_file_modify_permission` 接管，send_*/web_search 均为 typed 或 A2A settle）；ACP 路径的 `check_tool_autonomy` 已删（§3.2）。
- **不「补齐 6 工具映射」**（更正主方案 §3.4）：这 6 工具里 send_feishu_message/web_search/execute_code_e2b 零调用、move_file/send_file_to_agent typed、send_message_to_agent A2A settle（§2.3）——补齐是惰性动作，且主方案 ① 落地后 `:6201` 的文件工具判定会被 `resolve_file_modify_permission` 替换，MAP 的文件键即告 moot。补齐属投机式加固（宪法 II 禁止）。

### 3.2 A2：删 `check_tool_autonomy` 死代码（含整条 ACP 自主权 stub）

- **删** `agent_tools.py:29657-29690`（`check_tool_autonomy` 函数体）。
- **删** `tool_bridge.py` 三处调用 + 关联块：
  - `:1538-1565`（写工具闸门块：含 `_DELETE_TOOLS_PLUGIN_GATED` 局部变量 `:1540`、动态塞键 `:1543-1556`、`check_tool_autonomy` 调用 `:1557-1564`、`return _handle_autonomy_blocked` `:1565`）——整块删。
  - `:1973-1982`（execute_command 的「P0-3 autonomy 审批闸门」块）。
  - `:2124-2137`（terminal-streaming 的「P2-2 autonomy 检查」块）。
- **删** 随之变死的 ACP 自主权 stub（C16，仅被上述 3 块使用）：
  - `_handle_autonomy_blocked`（`:1234-1246`）；
  - `_AUTONOMY_STOP_THRESHOLD`（`:1231`）、`_autonomy_counts`（`:1232`）；
  - `WORKSPACE_WRITE_TOOLS` 导入（`:28`，仅被写工具块 `:1541` 用）。
- **清** `_lazy_import_agent_tools`（`:987-999`）：移除 `check_tool_autonomy` 导入（`:993`）与 `_INLINE_IMPORTED["check_tool_autonomy"]`（`:997`）、`_TOOL_AUTONOMY_MAP` 导入（`:998-999`，仅被塞键块用）；**保留** `_DANGEROUS_BASH_ALWAYS`/`_DANGEROUS_BASH_NETWORK`（`_guard_acp_dangerous_command` 依赖，`:991-996`）。`:983-984` 注释同步更新。

### 3.3 A3（新发现，单独记，不在本票修）

- `send_external_message` 孤儿键（16 agents L1/L3 + DEFAULT + 测试 ALL_ACTIONS 均含，零读取）。
- **处置建议（另立小票，非本票）**：①确认产品语义——它是「对外发消息」的遗留分类还是 `send_feishu_message` 的旧名；②若废弃 → 从 `DEFAULT_AUTONOMY_POLICY` 删 + 迁移 16 agents 政策键 + 更新 `test_agent_default_autonomy_policy.py` 的 `ALL_ACTIONS`；③若需 enforce → 在 `_CROSS_SPACE_ACTION_BY_TOOL` 之外补真实闸门（但 send_feishu_message 已废弃、send_channel_message 走 typed 无 autonomy，属主方案 ① 之外的新决策点）。
- **为何不混入本票**：它是配置数据/架构缺口，非代码 bug；改它会动 DEFAULT + 16 行政策 + 测试契约，爆炸半径远超「删两处死代码」，违反最小改动（宪法 II）。

### 3.4 影响面 / 爆炸半径

- **零运行时行为变更**：A1 删死定义（意图版本就被覆盖）、A2 删死代码（truthy bug 本就不拦截）。
- 改动文件：`backend/app/services/agent_tools.py`、`backend/app/plugins/clawith_acp/tool_bridge.py`（2 文件）。
- 无迁移、无 API 变更、无前端变更、无 schema 变更。
- 不碰：`autonomy_service.check_and_enforce`（保留给主方案 ① 复用 + 非文件 action）、`_guard_acp_dangerous_command` 黑名单（`:1205`）、`_delete_autonomy_gate`（durable delete 审批，主方案 ① 处理）。
- 已核实无残留引用：`check_tool_autonomy` 全 backend 仅 C7 三处 + C9 导入一处；`_TOOL_AUTONOMY_MAP` 在 tool_bridge 仅 C9 导入 + C8 塞键两处；测试文件零引用（`grep` 空）。

### 3.5 回归测试

1. **新增**：断言 `_TOOL_AUTONOMY_MAP` **最终内容 == 期望的 5 键**（`{write_file/edit_file→write_workspace_files, delete_file→delete_files, execute_code/execute_command→execute_code}`），并断言其 value 集 ⊆ `DEFAULT_AUTONOMY_POLICY` 键集。说明：Python 重赋值在模块加载时已把「重复定义」解析成最终值，单元测试**读不到中间定义**，故不能直接断言「唯一」；但「最终内容 ≠ 期望」会暴露未来任何再次遮蔽/错键（若有人新增第二份 dict 覆盖生效版，最终值必然偏离期望 5 键）。放在 `test_agent_default_autonomy_policy.py` 或新建 `test_tool_autonomy_map.py`。
2. **现有**：`test_agent_default_autonomy_policy.py` 全绿（不断言 MAP 内容，不受 A1 影响）。
3. **删后 grep 校验**：`check_tool_autonomy` 零残留；`_handle_autonomy_blocked`/`_autonomy_counts`/`_AUTONOMY_STOP_THRESHOLD` 零残留；`_INLINE_IMPORTED` 里 `check_tool_autonomy`/`_TOOL_AUTONOMY_MAP` 键已清；`WORKSPACE_WRITE_TOOLS` 导入已清（若 tool_bridge 无其他引用）。
4. **ACP 路径冒烟**：`_guard_acp_dangerous_command`（execute_command 黑名单）照常拦截（`:1965/:2105` 调用不受影响）。

### 3.6 回滚

- 纯代码 revert（删的是死代码，无数据/迁移/开关面）。无行为级开关需求。

---

## 4. 七角度评审裁决

### Q1 根因找的是否正确？

**裁决**：通过。

**正向依据**：A1 根因「同名模块级 dict 末次赋值遮蔽」同时解释 C1/C2/C3（双定义+末次生效）与 D2（audit_logs 无 6 工具的 autonomy_check）与 D1（3 工具零调用）——不是只解释「某个症状」。A2 根因「truthy + 三死参数」解释 C6（恒真）与「ACP 永不拦截」。A3 根因「孤儿键零读取」解释 D3（16 agents 配 L1/L3 却无任何 autonomy_check:send_external_message）。

**负向探针（反例测试）**：若 A1 根因成立，则 `_TOOL_AUTONOMY_MAP.get("send_feishu_message")` 运行时必返回 None，且 `audit_logs` 不应出现 `autonomy_check:send_feishu_message`。我核 C2 生效版确无此键 + D2 确无此 audit 条目 → **证实**（双源一致，非单源巧合）。

### Q2 根治方案是否正确？

**裁决**：通过。

**正向依据**：方案改的是根因本体——删死定义（消除遮蔽源）、删死代码（移除 truthy 死闸门），非止痛药。

**负向探针（删除测试）**：「删掉本方案（保留两份 MAP + check_tool_autonomy），遮蔽陷阱与死代码会不会复发？」**会**（双定义仍在、truthy 函数仍在）→ 是根治。诚实边界：本方案**不修**「legacy seam 对 move_file/web_search 无 autonomy 检查」这个既存缺口——但那是主方案 ① 的职责（`resolve_file_modify_permission` 替换 `:6193`），本票只删 bug，不越界承诺。

### Q3 参考的资料是否正确？

**裁决**：通过（一条低风险注记）。

**正向依据**：引用均为**同类问题**（D1/D4 的工具→action 映射与多路径闸门），非同名 false friend；jcode/bisheng/agent-inbox/ACP 均读真实源码或既有研究报告。

**负向探针（找引用错点）**：「letta-code 已归档，其 memory-confinement 在归档前是否改名/迁移，导致 §1 描述失真？」核下来：本票只借其「单一 enforce 点」思路，不依赖其具体机制 → 即使版本有变也不影响本方案 → **低风险，可接受**。「jcode 的确定性分类表是否真是『单一事实源』而非多表？」核 `20260905-jcode-study.md`：jcode 是**单表**确定性 blast-radius 分类 → **无误**，反衬 Clawith 双 MAP 之弊成立。

### Q4 副作用与爆炸半径是否排查完？

**裁决**：通过。

**正向依据**：
- ①副作用面：A1/A2 均为**删代码**，不引入新外部写、无缓存、无连接/资源变化、无权限边界变化。exactly-once 不受影响（不碰执行路径）。
- ②影响面：唯一 MAP 消费者 `execute_tool:6201`（行为不变——保留的 5 键即当前运行时内容）；`check_tool_autonomy` 消费者仅 tool_bridge 3 处（全删）；随删块变死的 `_handle_autonomy_blocked`/`_AUTONOMY_STOP_THRESHOLD`/`_autonomy_counts`/`WORKSPACE_WRITE_TOOLS` 导入（C16 已核实仅被删块使用，一并删，见 §3.2）；`_guard_acp_dangerous_command` 独立（`:1205` 只依赖 `_DANGEROUS_BASH_*`，C9 已核实）。

**负向探针（点名消费者）**：「删动态塞键是否影响 `execute_command` 的 ACP 路径？」——塞键块里 `execute_command→execute_code` 与 `edit_file→write_workspace_files` 本就在生效版 5 键里（C2），删塞键后这两键仍在 MAP；`refactor_rename/safe_delete/reformat_code/optimize_imports/convert_java_to_kotlin` 是 ACP 专用键、legacy `:6201` 永不出现（Phase 4 review 已证「inert」）→ **不影响**。「删 `check_tool_autonomy` 是否误删 execute_command 黑名单？」——黑名单是 `_guard_acp_dangerous_command`（`:1205`，`:1965/:2105` 调用），与 `check_tool_autonomy` 是两个独立块（C9）→ **不受影响**。「删 `_handle_autonomy_blocked` 是否误删其他调用者？」——核 C16：其 2 调用点 `:1565`/`:1982` 都在被删块内、terminal 块 `:2137` 直接 `return block` 不经它 → **零残留调用者，不影响**。

### Q5 这是最优且必要的方案吗？

**裁决**：通过。

**正向依据**：
- ①候选枚举（≥3）：A（更简）只删死定义、保留两份并存——不可取（陷阱仍在）；B（当前）删死定义 + 删死代码 + 注释边界 + 断言防复发——Ponytail 最低一档可解决问题；C（更彻底）补齐 6 映射 + 重写 ACP 权限模型——过度（§3.1 已证补齐惰性）。
- ②修「已发生」还是「臆想」：本票修的是**已确认的代码缺陷**（双定义遮蔽、truthy 死代码——均有源码铁证 C1-C9），非臆想风险；A3 孤儿键**诚实定性为配置缺口**、另立小票，未混入本票当 P0 已损。

**负向探针（更简一档能否解决）**：「试 A：只把意图版改名（如 `_LEGACY_AUTONOMY_MAP`）避免遮蔽，能否解决？」——**不能根治**：改名后两份 dict 仍在、语义仍冲突，且意图版 9 键里 6 键惰性，反而新增「两个 MAP」的心智负担；删死定义（B）更简更彻底。**B 成立**。

### Q6 是否已经有可复用的逻辑？

**裁决**：通过。

**正向依据**：本票是纯删除 + 注释 + 断言，无新逻辑需复用；保留的 `check_and_enforce`/`_guard_acp_dangerous_command`/`_TOOL_AUTONOMY_MAP` 均不动。

**负向探针**：「代码库/知识图谱里是否已有『防同名模块级常量遮蔽』的既有机制？」我查了：无（Python 语言不报错，仓库也无相关 lint 规则）→ 需新增断言（§3.5-1）来兜底，且该断言是最小化的回归手段，非新建抽象。

### Q7 会破坏 Clawith 的特性吗？

**裁决**：通过。

**正向依据**（宪法 C1–C6 + 红线逐条）：
- C1 证据先行：双源（C1-C15 源码 + D1-D4 台账）齐全。
- C2 最小改动：删两处死代码 + 注释 + 一条断言，不混重构、不投机加固（明确拒「补齐 6 工具」）。
- C3 契约与状态所有权：不碰 `DEFAULT_AUTONOMY_POLICY`、不碰 `agents.autonomy_policy` 列、不碰测试 `ALL_ACTIONS` 契约。
- C4 测试证行为：§3.5 断言 + 现有测试全绿。
- C5 保留既有工作：不删 `check_and_enforce`、不删 `_guard_acp_dangerous_command`、不碰 durable delete 审批流。
- C6 模块化与数据边界：改动限定 2 文件，无跨模块新依赖。

红线：durable run/checkpoint（不碰）✓；多租户隔离（无 tenant 变更）✓；exactly-once（不碰执行路径）✓；前缀缓存（不碰 tool 缓存键）✓；WS 状态机 ✓；飞书通道（`check_and_enforce` 的 L2 通知逻辑保留，本票不涉）✓。

**负向探针（对红线逐一过）**：「删 `check_tool_autonomy` 会否让 ACP 写工具失去『后端拒绝』信号，破坏飞书/审批流？」——`check_tool_autonomy` 因 truthy bug **本就不产生任何拒绝**（C6），删除不改变任何既有信号；ACP 写工具裸写是主方案 §8.3 已记录的既存缺口、靠 IDE+版本控制兜底 → **不碰红线，不新增缺口**。

---

## 5. 对主方案的更正（三处，需回改 `20260905-maintainer-gate-g3-g4-production-plan.md`）

| # | 主方案原句 | 更正为 | 依据 |
|---|---|---|---|
| 1 | §3.4「删 `:2890` 死定义、把 `:29640` 生效版**扩成完整映射**、**补齐被遮蔽的 6 个工具映射**」 | **只删死定义、保留生效版 5 键、不补齐**（补注释说明 MAP 仅服务 legacy seam） | §2.3：6 工具中 3 个零调用、2 个 typed、1 个 A2A settle → 补齐惰性；且主方案 ① 落地后 `:6193` 文件工具判定被替换，MAP 文件键 moot |
| 2 | §3.5「`agent_tool_executions` 已记录每次工具调用结果（**含 error_code**）」 | 该表**无 `error_code` 列**；拦截证据在 `status`/`result_summary`/`result_metadata` | D4（PG schema 核实） |
| 3 | 未提 `send_external_message` | 补 §8.2 雷 4：`send_external_message` 孤儿键（16 agents L1/L3、零读取），另立配置清理小票，不属 P0 bug | §2.3 A3 |

---

## 6. 交付与闭环

- 本票交付物 = 本文档（四件套 + 七角度评审定稿）。
- **实现后强制（Phase 5）**：跑 `code-review` 对照本方案复核 diff——只应出现 §3.1/§3.2 的删除与注释/断言，无夹带、无「补齐 6 工具」、无 DEFAULT 变更；偏离须回改或重审。
- 验收红线：① `grep check_tool_autonomy` 全 backend 零残留；② `_TOOL_AUTONOMY_MAP` 模块内唯一定义、5 键；③ `test_agent_default_autonomy_policy.py` + 新增 MAP 断言全绿；④ ACP `execute_command` 黑名单 `_guard_acp_dangerous_command` 仍拦截。

---

## 7. 实现闭环记录（Phase 5）

**实现提交**：`bf69441e`（2026-09-07，`fix(autonomy): 收敛两份 _TOOL_AUTONOMY_MAP 遮蔽 + 删 ACP check_tool_autonomy truthy 死代码`），改动 3 文件（`agent_tools.py` / `tool_bridge.py` / 新增 `tests/test_tool_autonomy_map.py`），+50/-138。

**Phase 5 双轴 code-review（textbook：Standards + Spec 并行子代理）**：

- **Spec 轴：通过，无阻断项。** §3.1/§3.2/§3.5/§6 逐条对照忠实落地——删除项逐一到齐、源码零残留；无夹带；`DEFAULT_AUTONOMY_POLICY` 零改动；黑名单 `_guard_acp_dangerous_command` + `_DANGEROUS_BASH_*` 依赖完整保留。唯一措辞注记：边界注释未枚举 §3.1 所述三处 seam 调用点（`caller.py:430` / `tool_hooks.py:66/:940` / `execute_builtin_tool_outcome:6068`），非功能缺口。
- **Standards 轴：1 硬违规（H1）+ 1 judgment call（S1）。**
  - **H1**：边界注释提前引用未落地的 `resolve_file_modify_permission`，且断言「file tools never read this map」与当时 5 键 map 相矛盾（宪法 I Evidence Before Claims）。根因 = 本方案 §3.1 注释措辞用了现在时，而 autonomy 修复与 Maintainer 门控拆成两个 commit 造成时间错位。**已自愈**——Maintainer-gate 落地后 `resolve_file_modify_permission` 存在、MAP 缩为 2 键（见下），注释所指状态成立，无需回改 `bf69441e`。
  - **S1**：5 键 map 内容在 `_TOOL_AUTONOMY_MAP` 与测试 `EXPECTED_MAP` 双写（回归锁定的惯常手法，可接受；暴露键集变更需两处同步）。

**验收红线（§6）逐条结果**：

1. ① `grep check_tool_autonomy`（及 `_handle_autonomy_blocked`/`_autonomy_counts`/`_AUTONOMY_STOP_THRESHOLD`/`WORKSPACE_WRITE_TOOLS`）全 backend + tests 零残留 ✅
2. ② `_TOOL_AUTONOMY_MAP` 模块内唯一定义（1 处，`agent_tools.py:29658`）、5 键 ✅
3. ③ 回归测试 `test_tool_autonomy_map.py` + `test_agent_default_autonomy_policy.py` 共 **9 passed** ✅
4. ④ `_guard_acp_dangerous_command` 黑名单完整（2 调用点 `:1911`/`:2040` + 惰性导入 `:1203`）✅
5. `scripts/arch-guard.sh` 全过（仅历史行数 WARNING，无 P0 违规）✅

**后续演进注记（跨会话漂移，符合本方案预告）**：`bf69441e` 复核通过后，Maintainer-gate 会话落地 `resolve_file_modify_permission`（`maintainer_service.py:106`）接管四个文件工具 → `_TOOL_AUTONOMY_MAP` 缩为 2 键（`execute_code`/`execute_command`），本票测试同步更新为断言 2 键（当前 `2 passed`）。这正是 §3.1「主方案 ① 落地后 `:6201` 文件工具判定被替换、MAP 文件键即告 moot」的预告兑现，非回归。

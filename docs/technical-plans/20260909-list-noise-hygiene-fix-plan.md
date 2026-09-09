# 生产级修复方案：清单线「卫生缺口」根治——清空过时快照 + 契约（前向）+ 迁移修复 + 可选分类门（防御）

> 结论（**五审修订后**）：**有条件通过**。核心修订（四审→五审，经用户确认）：`mydome1` 项目**已闭环**（MR !1~!11 全合并，main 头 `b9240bd`），清单表 139 行是**交付过程中未同步 complete 的过时快照**——不是「活跃待办 + 噪音」，而是「全部已完成但清单未收口」。清淤目标从「逐 key 分类保留真待办」修正为「**整体清空**」：`mydome1` 76 pending → `completed`，`legacy:*` 58 行 → 删除。
> 触发：agent 950a1943「Android 工程师 07」的 `agent_list_items` 出现四类噪音（语义重复/非待办污染/过时描述/孤儿决策选项），且该 agent 已于 2026-09-09 把 `memory/清单.md` 归档、项目交付完毕，但清单表残留 139 行。
> 前置依赖：编号指代断裂已由 `017d1e4a`（清单线 focus 范式迁移）根治；本方案治理**迁移之后暴露的第二个缺口——清单卫生（清单里该存什么、交付后怎么收口）**，非编号。
> 分支：`f-shubin-0806`（HEAD 017d1e4a）；生产容器 `clawith-agent-backend-1` 部署的正是本分支代码。

---

## Phase 1 — 参考项目对比结论（≥10 项目）

四个关键决策点，逐点对照（复用 `docs/technical-plans/` 既有研究报告 + 记忆 `reference-projects`/`plans-compare-reference-materials`；Clawith 自身代码实读）：

| 决策点 | 参考项目 | 结论 |
|---|---|---|
| ① 待办 vs 观察/结论/事实 要不要分桶 | Letta/MemGPT、mem0、Claude Code TodoWrite、12-factor-agents(Factor 5) | **分**。记忆（结论/事实）与任务（待办）分属两个 owner。清单线只有 `status`（生命周期），无「语义角色」维度。 |
| ② 重复怎么抑制 | mem0（embedding 去重）、letta-code、Claude Code（completed 独立段） | **不做 embedding 去重**（重、投机，宪法 II）。靠契约「先查后写」+ key 归一化 + 迁移合并去重。 |
| ③ 卫生由契约还是结构保证 | OpenHands（TaskStatus 状态机）、deepseek-harness、Clawith focus 线 `_classify_seed_kinds` | **先契约（P0），结构分类门留防御（P2）**。与 heartbeat 先例同构。 |
| ④ 活跃窗口与归档 | Claude Code（completed 不进活跃段）、letta-code | 非待办/已完成项**不进活跃注入窗口**。**本方案新增：交付收尾必须 `complete_list_item` 对账**（Claude Code 的「completed 独立段」思想）。 |

**诚实负结论**：SWE-agent / gptme / Codex（plan/todo 工具无 kind、无卫生规则——不解决本问题，但反证「稳定 id ≠ 卫生」）；open-swe / bisheng / LangBot placement_generation / orca runtimeFence / herdr（不同题或负结论）。

**方法论结论**：正则/词集/阈值补规则是打地鼠——触发用规则、消解交 LLM、不确定 no-op 是默认正确解。

---

## Phase 2 — 根因（两层 + 最深因，追到底）

### 第一层：迁移忠实复制旧系统分叉 + 硬编码 pending → legacy 死数据 + 非待办污染，且会复发

- `backend/app/services/list_service.py:83-119` `_migrate_legacy_list_file_impl`：旧 `清单.md` 每个 `## list:` section 以 `project = section.project or f"legacy:{section.list_id}"`（`:92`）生成作用域——无 project 的 section（`project: -`）各得一个 `legacy:<list_id>` 合成作用域，且 `status` 恒 `"pending"`（`:113`）。
- `backend/app/dao/list_dao.py:72-110` `upsert_item` 去重键 `(agent_id, project, key)` **跨 project 不去重** → 同 key 跨 project 精确重复（如 `p1_文档_readme_与代码脱节` 6 作用域）。
- `legacy:*` 行是**死数据**：`extract_workspace_project`（`list_persistence.py:90-129`）永远派生 `mydome1`，`legacy:*` 永不注入、永不显示。
- **复发**：迁移惰性，每个有旧 `清单.md`（`project: -` 段）的 agent 首次读清单都会产生同样分叉。

### 第二层：清单线无「语义角色」维度 + 无「交付收尾 complete」契约 → 交付后清单残留 pending（**已损，非潜在**）

- `backend/app/models/list.py:44` `status` 只有 `pending/in_progress/completed`（生命周期），无 `kind`（语义角色）。迁移时无法区分「待办」与「观察/结论/已完成事实」，只能全标 `pending`。
- `backend/app/services/agent_runtime/list_persistence.py:27-41` `LIST_NUMBERING_CONTRACT` 有「完成凭据」（complete 以 key 为凭据），但**无「交付/推进完成后必须 `complete_list_item` 收口」的强制条款**。
- `backend/app/services/agent_runtime/cross_session_retrieval.py:26/118/122` 注入窗口 `MAX_INJECTED_ITEMS=5`、`items[:5]` 无 kind 过滤——agent 只见前 5 项，后续项交付后容易被遗忘 complete。
- **已损的决定性证据（2026-09-09）**：该 agent 整理后的 `memory/memory.md` 显示项目 **MR !1~!11 连续全合并**（main 头 `b9240bd`）、「开发批次全部合并、无在途、平台清单为空」；但 `agent_list_items` 表里 `mydome1` 的 **76 行 pending 原封未动**（`updated_at` 最大仍 09-08）——**项目已闭环，清单未收口**。对比 `session_task_state.py:519` 的 phase 判定 `map_phase(status, has_open_list_items=bool(pending))`，这 76 行残留**有条件地**让 phase 误判为「仍有活跃清单」——仅当未来某 direct-chat run 的 `extract_workspace_project` 解析到 `mydome1` 作用域时（`session_task_state.py:515-519`，`_load_pending_lists` 只在 scope 命中且有余 pending 时返回非空）；**实测当前 `memory/任务状态.md` 已 `phase: complete`（未误判）**——因最近 run 是「沙箱 `.git.bundle` 核验」、无 `workspace/mydome1/` 工具路径、scope 落到 `session:*`（空）。故「phase 误判」是**条件性潜在风险，非已损事实**。

### 最深因

清单线沿 focus 范式只迁移了「编号」，漏了 focus 线的「语义分类」（`heartbeat_completion.py:337-385` `_classify_seed_kinds`，task|learning）+ 缺「交付收尾对账」契约。编号与分类是同一范式的两半，只搬一半 → 清单表成了「只写不收口」的过时快照堆。

### 数据证据（PG 台账 + 源码 + agent_tool_executions 三源）

- agent `950a1943-…` 共 **139 行**：`mydome1` 81 行（**76 pending + 5 completed**）+ **12 个 `legacy:*` 作用域 58 行**。
- **全部 139 行是迁移快照**：`created_at` 全为 `2026-09-08 10:18:58.997110Z`（同一次 bulk insert），迁移后零新增。
- **跨 project 精确重复**：15 个 key 出现在 ≥2 个 project；`p1_文档_readme_与代码脱节` 等 3 key 各 6 作用域。
- **`legacy:*` 独有 key**：29 个 key 只在 `legacy:*` 存在（`mydome1` 无对应）。
- **`agent_tool_executions` 台账**：`upsert_list_item` 上线后仅 3 次调用、全部 `status="completed"`；`complete_list_item` 2 次。证明新系统 agent 行为正确（观察标 completed），堆积 100% 来自迁移硬编码 pending + 交付后未对账 complete。
- **项目闭环 vs 清单残留（五审新增，决定性）**：`memory/memory.md`（09-09）交付闭环表 MR !1~!11 全合并、main=`b9240bd`、当前状态「无在途、平台清单为空」；`memory/清单.md` 已删（归档 `workspace/archived/memory-历史清单-2026-09-09.md`）；但 `agent_list_items` 表 139 行未变。
- **复发面被低估（六审新增，决定性）**：全平台另有 **10 个 agent 仍持旧 `memory/清单.md`** 未迁移——8 个含 `project: -` 段（合计 **36 个 section**：`475264c9`×10、`b1a73489`×8、`b05d3a82`×6、`2ebdfee6`×5、`08a739c1`×4、`62bc9c81`/`27d55a64`/`82dc9a8a` 各 1）、2 个 real-project 段（`ba4f0323`=`emails`、`ddc779e3`=`store-launch`）。**PG 里 `legacy:*` 行仅 950a1943 一家**，证明这 10 个 agent 都还没触发迁移；而惰性迁移挂在 `list_list_items`/`upsert_list_item`/`complete_list_item` 三个入口（`list_service.py:132/164/197` 每次调用都跑 `_migrate_legacy_list_file_impl`）——**它们下一次碰任何清单工具即复刻同类 legacy 分叉 + pending 残留**。→ P1 不是「防未来复发」，是「8 个在活 agent 的定时炸弹」，必须**先于它们首次迁移部署**；P0-b 也不应只清 950a1943。
- **real-project 段同样产生 pending 噪声（六审新增）**：那 2 个 real-project agent 的 `清单.md` 内容是「心跳检查/收尾确认」快照——`ddc779e3`(`store-launch`) 3 条全带 ✅ 已办标记、`ba4f0323`(`emails`) 是「状态盘点/无新动作/复盘沉淀」观察而非待办。迁移的 `status="pending"` 硬编码（`list_service.py:113`）+ 旧格式无 status 字段（`_parse_section_lines` 只把 `—` 前当 title、`—` 后当 description，✅ 标记留在文本里不被解析）→ 这些「已办/观察」内容会被**压平成 pending 落在 LIVE 作用域**（`emails`/`store-launch`），而 P0-b 的「`legacy:%` 清淤」**抓不到它们**（它们不是 legacy）。→ P0-b 清淤范围还需覆盖这 2 个 agent 迁移后 LIVE 作用域的 stale pending，或依赖 P0-a 契约让它们迁移后自行 `complete_list_item` 收口。
- **「8 legacy = 死数据」是错误分类（七审新增，决定性）**：六审把 8 个 `project: -` agent 统一判为「死数据，可直接归档源文件再清库」，是**过度外推**。实读这 8 个 agent 的 `清单.md` 全文 + `agent_focus_items` + `agent_runs` 台账：**全部 8 个都是活跃 agent**（run 数 61~524，最近 run 均 2026-09-09 当天），且其中 **5/8 的 `project: -` 段含真实在途待办**（阻塞于用户凭据/授权/输入），非死数据——`b1a73489`「P0 CalculatorApp push GitLab + MR 挂起，阻塞=GitLab 凭据」（focus `p0_gitlab_push_mr` in_progress 印证）、`08a739c1`「mg2（ColDinero）挂起等待授权」、`27d55a64`「focus 5 项阻塞于 zhangshubin 3 项输入（OJK 牌照/品牌名/后端 API）」（focus 4 项 in_progress 印证）、`2ebdfee6`「NotepadApp 迭代方向等待用户指令」（focus `notepadapp_next_iteration` in_progress 印证）、`82dc9a8a`「LGOAAC 87-B Bis Decreto 跟踪 + CréditoMX 真实放贷阶段重启」（focus in_progress 印证）。仅 3/8（`475264c9` 前端版本监控、`b05d3a82` mcp 监控、`62bc9c81` 心跳无变化）是纯观察快照。
- **`project: -` 语义勘误（七审新增，决定性）**：`project: -` 不是「死数据」标记，而是**旧格式无 project 字段**的产物——`_parse_section_lines`（`list_persistence.py:155`）把 `project: -` 解析为 `None`，迁移再降级为合成 `legacy:<list_id>`。950a1943 的 `project: -` 段恰好是「交付链路已闭环/无在途执行」这类闭环观察噪声，那是**内容**性质，不是 `project: -` 的结构性质。六审把「950a1943 的 `project: -` 恰好是死数据」错误外推到全部 8 个 agent，而其中 5 个的 `project: -` 段是**当前活跃待办**。→ 这同时**推翻 P1 的修法方向**（见 P1 七审修正）：把 `project: -` 段合并进单一 `legacy` 死作用域，等于把 5 个 agent 的真实在途待办埋进永不注入/显示的作用域。

### 可证伪性

- 若「迁移缺陷 + 无交付收尾契约」是根因，则：①清淤后 `mydome1` pending 归零、`legacy:*` 归零；②迁移修复后其他 agent 首次迁移迁入 LIVE、不再产生 legacy 死作用域；③契约新增「交付收尾 complete」后新 run 不再残留已交付的 pending。三者可观察验证。
- **反证「agent 乱写」**：若根因是「新系统 agent 乱写」，则 `upsert_list_item` 应出现 pending 观察项——实测 3/3 全 completed，证伪；坐实「迁移硬编码 + 未对账 complete」。
- **反证「pending 是活跃待办」**：若 76 pending 是活跃待办，则项目应仍在途——实测 MR !1~!11 全合并、agent 自陈「无在途」，证伪；坐实「76 pending = 已完成未收口的过时快照」。

---

## Phase 3 — 最小修复方案（分层，Ponytail 阶梯）

### 候选枚举（≥3）

1. **A｜一次性清淤（无新代码，需授权）**：`mydome1` 76 pending → `completed` + `legacy:*` 58 行删除。清「已损」，不防未来。
2. **B｜契约卫生规则（零迁移）**：`LIST_NUMBERING_CONTRACT` 加「卫生段」（待办 only + **交付收尾 complete 对账**）+ 三工具 description。前向写端纪律。
3. **C｜迁移代码修复**：`migrate_legacy_list_file` 不再逐 section 造 `legacy:<list_id>` 作用域。防其他 agent 复发。
4. **D｜结构 kind 维度 + 写入分类门（f079）**：防御加固，预防（非已损）。
5. **E｜embedding 语义去重（被否）**：重、投机。

**取舍**：P0=B+A；P1=C；P2=D（可选，暂不实施）；E 拒绝。

### P0-a 契约卫生规则（零迁移，前向）

- `LIST_NUMBERING_CONTRACT` 加「卫生段」（追加到现有四条款之后，注入点 `model_step_service.py:1662/2537` 自动生效）：
  > 清单卫生：清单只放**待办/候选/未决事项**。①核验观察、动作记录、已完成的交付事实、空闲态快照等**非待办内容不写清单**，写入 reflections（memory）或直接 `complete_list_item` 收口；②写新条目前先 `list_list_items` 查是否已有同主题条目——有则用 `upsert` 更新既有 key，不新建重复项；③`description` 是「当前状态收据」，推进或完成后必须 `upsert` 更新为当前事实；④「已完成」用 `complete_list_item`，不用新建一条「…已闭环」的 pending 项；⑤**每次交付/收尾时对账清单**：已交付、已推进的项必须 `complete_list_item` 收口，不得让清单残留「已完成但未标 completed」的 pending 项（对齐 Claude Code「completed 独立段」）。
- 三工具 `description`（`builtin_tool_definitions.py:160/184/212`）补一句：「checklist = actionable todos only；观察/结论/事实写 reflections，不写清单；交付完成必须 complete 收口」。

### P0-b 一次性清淤（需用户明确授权，写库）

**整体清空过时快照**（五审修正，替代原「逐 key 分类」；六审扩围为全平台）：

- **`mydome1` 76 行 pending → `completed`**：项目已闭环（MR !1~!11 全合并），这 76 行是「已交付但未收口」的过时快照，逐条 `complete_list_item` 收口（保留历史，不删除）。
- **`legacy:*` 58 行 → 删除**：死数据（永不读取），且原始 `memory/清单.md` 已归档到 `workspace/archived/memory-历史清单-2026-09-09.md`，无信息损失。
- **扩围（六审新增，七审修正）**：清淤不限于 950a1943，但七审推翻「其余 8 个 agent 首次迁移会各自产生单一 legacy 死作用域」的旧判——P1 修复后它们迁入 **LIVE**（非 legacy），不再产生 legacy 分叉；`legacy:*` 行当前仅 950a1943 一家 58 行。P0-b 仍按 `project LIKE 'legacy:%'` **全平台清淤**作为兜底：覆盖 950a1943 现有 58 行 + P1 兜底路径（入口 project=None 时降级 legacy，见 P1 坑④）可能残余的 legacy 行。
- **扩围之二（六审新增）**：另 2 个 real-project agent（`emails`/`store-launch`）迁移会把「心跳/收尾 ✅ 快照」压平成 LIVE 作用域的 stale pending（非 legacy），`legacy:%` 清淤抓不到。P0-b 应把「LIVE 作用域里由迁移 bulk insert 产生的 stale pending」也纳入：识别信号 = `created_at` 等于迁移 bulk insert 时刻（同一 `created_at` 批量，区别于运行时 upsert 的逐条时间戳），或迁移完成后对该 2 个 agent 的 LIVE 作用域做一次 `pending` 对账（已办 ✅ 项 → completed）。
- **清淤耐久性缺口（六审新增，决定性）**：迁移不是「一次性导入」——`list_list_items`/`upsert_list_item`/`complete_list_item` 三个入口**每次调用都跑 `_migrate_legacy_list_file_impl`**（`list_service.py:132/164/197`），而迁移只读 `memory/清单.md`、**从不归档/删除源文件**。950a1943 能清干净，是因为它 09-09 **自己**把 `清单.md` 归档到了 `workspace/archived/`；而那 10 个 agent 的 `清单.md` 源文件**仍在**。→ **只清库不清源，下次碰清单工具会重新迁移、把刚清掉的行（8 个 agent 迁入 LIVE 的行 + `emails`/`store-launch` + 任何残余 legacy）重新插回**。P0-b 的耐久前提 = **让源 `清单.md` 不再被迁移读取（归档到各自 `workspace/archived/`）→ 再清库**。顺序：P1 迁移修复 → 归档源文件 → P0-b 清库 → P0-a 契约。
- **归档顺序细分（六审新增，七审修正）**：七审推翻「8 个 `project: -` agent 全是死数据、可直接归档」——实读 8 个 agent 清单全文 + `agent_focus_items` + `agent_runs` 台账，全部 8 个都是活跃 agent，其中 5/8 的 `project: -` 段含真实在途待办。归档时机三分：
  - **5 个活跃 agent（`b1a73489`/`08a739c1`/`27d55a64`/`2ebdfee6`/`82dc9a8a`）**：真待办须先迁入 LIVE（P1 修复后自动迁入）→ 对账（真待办保留、已办/观察项 `complete_list_item` 收口）→ 再归档源文件，否则真待办随源文件进归档、清单工具显示为空。
  - **3 个纯观察 agent（`475264c9`/`b05d3a82`/`62bc9c81`）**：其 `project: -` 项是纯观察快照（前端版本监控/mcp 监控/心跳无变化）、无真待办，可直接归档源文件（无需迁入 LIVE，避免把观察快照压平成 LIVE pending 噪声）。
  - **2 个 real-project agent（`emails`/`store-launch`）**：其清单含真待办（如 `emails` 的「合作邮件草稿已定稿未发送」），**须先让它迁移一次**（真待办落入 LIVE）→ 对账（✅ 项 complete、真待办保留）→ 再归档源文件。
  归档统一用「移动到 `workspace/archived/`」而非删除，内容永不丢失。
- 执行前备份（对齐 heartbeat 清淤先例 `reflections-backup-*`），SQL 只读预览 → 用户确认 → 写库。
- 完整清空预览见 `docs/technical-plans/20260909-list-noise-hygiene-p0b-purge-preview.md`。

### P1 — 迁移代码修复（防复发，且须先于其余 8 个 agent 首次迁移部署）

**修法方向（七审修正，原「合并到单一 legacy 作用域」被推翻）**：`_migrate_legacy_list_file_impl`（`list_service.py:92`）不再把 `project: -` 段降级为合成 `legacy:<list_id>` 死作用域，而是**迁移到平台派生的活跃 project**——三个入口 `list_list_items`/`upsert_list_item`/`complete_list_item`（`list_service.py:132/164/197`）都已持有 `project` 参数，把它**透传给迁移函数**，`project: -` 段映射到该派生 project（`project = section.project or <传入的派生 project>`）。理由（七审实读）：`project: -` 是「旧格式没写 project 字段」，对活跃 agent 而言它承载的正是当前待办（5/8 是真实在途待办），迁入 LIVE 作用域才是正确归属；合成 legacy 死作用域会把真实待办埋掉。

**四个技术坑（实读补出，七审增第 3、4 条）**：

1. **`(agent_id, project, sort_order)` 唯一约束冲突**（`models/list.py:33` `uq_agent_list_items_agent_project_sort`）：多个 `project: -` 段迁入同一 LIVE project 时 `sort_order` 各自从 1 开始重叠，`bulk_insert_legacy_rows`（`list_dao.py:45`）的 `on_conflict_do_nothing` 只忽略 key 冲突不忽略 sort 冲突 → 会抛 `IntegrityError`。**改法：迁移行 sort_order 重新连续分配**（接在现有 LIVE project 的 max(sort_order) 之后；迁移是快照、无「编号即身份」语义，可重排）。
2. **idempotency 守卫失效**（`list_service.py:98-100` 按「该 project 已有行即整段跳过」）：`project: -` 段迁入 LIVE project 后，该 project 已「有行」，第二次调用会整段跳过、漏迁。**改法：`seen` 去重集合提升到 section 循环外 + 删整段跳过守卫，改由 `on_conflict_do_nothing` 按 key 逐条幂等。**
3. **project 透传**（七审新增）：迁移函数签名 `migrate_legacy_list_file(agent_id, db=None)` 与 `_migrate_legacy_list_file_impl(agent_id)` 均无 `project` 参数，入口调用时也没传。**改法：给两者加 `project: str | None` 参数，三个入口传入各自已持有的派生 project。**
4. **无派生 project 时的兜底**（七审新增）：入口 `project` 可能为 `None`（session 作用域场景，`extract_workspace_project` 返回 None）。**改法：此时才降级合成 `legacy` 作用域（`project = section.project or <派生 project> or "legacy"`），保留原「不丢数据」语义；仍产生 legacy 行的场景由 P0-b 的 `legacy:%` 全平台清淤兜底。**

**诚实边界（七审更新）**：P1 修复后，尚未迁移的 8 个 `project: -` agent 首次迁移**不再产生 legacy 死作用域**——5 个活跃 agent 的真待办迁入 LIVE（正确保留，由 P0-a 契约 + 对账收口）；3 个纯观察 agent 按 P0-b 归档顺序**先归档源文件（不触发迁移）**，避免观察快照压平成 LIVE pending 噪声（若归档前已被迁移，则观察快照落 LIVE pending、由对账 complete 收口）。950a1943 已迁移的 58 行 legacy 死数据**由 P0-b 兜底删除**（它 project 已闭环，这 58 行确为闭环观察）。**已知残余边界**：若未来某 agent 的 `清单.md` 同时混有 `project: X` 段与 `project: -` 段，`project: -` 段迁到「当前派生 project」可能归属错误——当前全平台无此形态（10 个未迁移 agent 要么全 `-` 要么全具名），故暂不处理，仅在此记录。
**复发面与顺序（六审新增，七审微调）**：全平台另有 8 个 agent 持 `project: -` 的 `清单.md`（36 个 section）**尚未迁移**。P1 必须在它们下次碰清单工具之前上线。实现顺序 **P1 → 归档源文件（见 P0-b 七审细分）→ P0-b 清库 → P0-a 契约**：P1 先让 `project: -` 段迁入 LIVE（不产 legacy 分叉）；归档源文件保证清库后不被重迁移插回；P0-b 清 950a1943 现有 139 行 + 兜底清任何残余 legacy；P0-a 前向契约最后落地（不依赖写库授权，可与 P1 并行）。

### P2 — 结构 kind 维度 + 写入分类门（防御加固，可选，暂不实施）

仅当 P0 契约上线后、生产仍观察到新系统把非待办当 pending 写入才实施：
- `agent_list_items` 加 `kind` 列（`task`/`note`，默认 `task`，f079）；`upsert_list_item` 加 `kind` 参数；分类门复用 `_classify_seed_kinds` 的**结构**（`heartbeat_completion.py:337-385`，`thinking_disabled=True`、`max_output_tokens=256`、fail-closed→task）。**引用勘误**：该函数标签是 `task|learning`（`_SEED_CLASSIFY_SYSTEM` 明示），**不是** `task|note`，且**无「粘性」逻辑**——复用须改标签语义 + 自行实现 note 粘性，属「复用结构 + 改语义」。
- **当前不实施**——宪法 II 禁止投机式加固，证据表明新系统尚未产生 note-noise。

### 影响面（爆炸半径）

- 契约改动：`LIST_NUMBERING_CONTRACT` + 工具 description → 消费者为模型（提示词注入），无代码消费者。
- 迁移修复：`_migrate_legacy_list_file_impl` → 消费者=惰性迁移（其他 agent 首次读清单）。
- 清淤：一次性 SQL/DAO 写，无代码消费者变更。
- **不触碰**：checkpoint/durable run、exactly-once、前缀缓存结构、多租户隔离、飞书/WS 通道。

### 回归测试

- 根因路径：①清淤后 `mydome1` pending=0、`legacy:*` 归零；②迁移修复后构造 `清单.md` 两个 `project: -` 段 → 迁入传入的派生 project（LIVE 作用域，非 legacy）、同 key 去重、不逐段分叉、sort_order 连续。
- 终态 + 禁止副作用：③ `list_list_items(mydome1, include_completed=True)` 显示 81 行全 completed；④契约含「待办 only / 先查后写 / 描述收据 / complete 收口 / 交付收尾对账」五条款；⑤清淤不触碰 `memory.md`/`reflections.md`/`workspace/archived/` 原文（清单表与 reflections 分属两个 owner）。

---

## Phase 4 — 7 角度评审

### Q1 根因是否正确？——通过
- **正向依据**：两层根因解释每一个证据（139 行=81 mydome1+58 legacy、`created_at` 全同迁移时刻、15 key 跨 project 重复、29 key legacy 独有、项目闭环 vs 76 pending 残留）。追到最深因：只迁移「编号」漏了「语义分类」+ 缺「交付收尾对账」契约。
- **负向探针（反例测试）**：若根因只是「agent 纪律差」，跨 project 精确重复（同 key 6 份）无法解释（只能由迁移合成 legacy 作用域产生）；且「新系统纪律差」被 agent_tool_executions 台账 3/3 completed 证伪。若「76 pending 是活跃待办」，项目应仍在途——实测 MR !1~!11 全合并、agent 自陈「无在途」，证伪。**证实**根因是「迁移缺陷 + 无交付收尾契约」双源。

### Q2 根治方案是否正确？——通过
- **正向依据**：P0 改 Q1 两层根因——P0-a 契约治写端（无卫生规则 + 无交付收尾对账）、P0-b 清淤治已损（139 行残留）；P1 治迁移缺陷复发面；P2 防御。
- **负向探针（删除测试）**：删 P0-b 只留契约，问「现有 139 行残留会不会消失」——**不会**。删 P0-a 只留清淤，问「下个项目交付后会不会再残留 pending」——**会**。删 P1，问「其他 agent 会不会重蹈 legacy 分叉」——**会**（且不是「未来」：实查另有 8 个 agent 持 36 个 `project: -` section 未迁移，删除 P1 后它们下次碰清单工具即复刻 950a1943 的 12 作用域分叉）。三者各对一层，缺一不可。
- **负向探针（复发面，六审新增，七审微调）**：把 P1 的部署顺序放到 P0-b 之后、且 P0-b 只清 950a1943，问「其他 8 个 agent 迁移出的 legacy 分叉会不会被清掉」——**不会**，它们会在 P0-b 执行后才首次迁移、各自长出 N 个 legacy 作用域，需再补 8 次清淤。**七审进一步坐实**：若 P1 晚于这 8 个 agent 首次迁移，5 个活跃 agent 的真实在途待办会被埋进 legacy 死作用域（永不注入/显示）——P1 先行的理由从「避免 legacy 分叉」升级为「避免真待办被埋」。P0-b 的 `legacy:%` 清淤在七审后定位为**兜底**（950a1943 现有 58 行 + P1 的 project=None 兜底路径残余），非「覆盖 8 个 agent 未来的 legacy 分叉」（七审后 8 个 agent 迁入 LIVE、不再产 legacy）。

### Q3 参考资料是否正确？——通过
- **正向依据**：引用均为同类问题；Clawith 自身 focus 线 `_classify_seed_kinds` 是同库同栈已上线先例（实读）；本次新增 Claude Code「completed 独立段」佐证「交付收尾对账」。
- **负向探针**：mem0 常被引为「语义去重」直接依据——核对下来 mem0 是 memory store 的 ADD/更新，非「任务清单去重」，本方案不照搬 embedding 去重（E 拒绝）。**无误**。

### Q4 副作用与爆炸半径是否排查完？——通过（含已知风险）
- **正向依据**：P0-a 无运行时副作用；P0-b 一次性写（备份先行）；P1 只改未迁移 agent 的迁移行为。影响面已列全。
- **负向探针**：`session_task_state._load_pending_lists` 投影 `has_open_list_items=bool(pending)`（`:519`）——**实测当前 `任务状态.md` 已 `phase: complete`**（未因 76 pending 误判，因最近 run scope 未解析到 `mydome1`），故「清淤让 phase 从 active 变 complete」表述**不成立**（phase 本就 complete，见 Phase 2 勘误）。真实风险是**条件性潜在**的：未来某 direct-chat run 触达 `mydome1` 且 76 pending 仍残留时，`bool(pending)=True` → phase 误判 active；清淤后 `bool(pending)` 翻转 False、phase 保持 complete，**这才是正确终态**（项目已闭环）。若误删真待办则 phase 误判——本方案清淤是「pending→completed 保留历史」，非删除，无此风险。另抓出「迁移合并 sort_order 冲突」「legacy 独有 key」两坑已入 P1/P0-b（七审对 P1 新方向又补「project 透传」「无 project 兜底」两坑，共四坑，见 P1）。

### Q5 这是最优且必要的方案吗？——通过
- **正向依据**：枚举 5 候选从 Ponytail 最低档起步。修的是已观察故障（139 行残留 + 项目闭环未收口），非臆想风险；分类门（D）诚实定性为 P2 预防。
- **负向探针（更简单档测试）**：试过「只清淤不上契约」能否根治——不能（下个项目复发）。试过「清淤+契约不上 D」能否闭环——能，故 D 当前非必需。

### Q6 是否已有可复用的逻辑？——通过
- **正向依据**：契约复用 `LIST_NUMBERING_CONTRACT` 注入点；清淤复用 `list_dao.complete_item`；迁移修复改既有函数；P2（若上）复用 `_classify_seed_kinds` 结构。零新依赖。
- **负向探针**：查知识图谱/代码库是否已有「清单项语义分类」等价逻辑——无（`_classify_seed_kinds` 只在 focus 侧）。**无误**。

### Q7 会破坏 Clawith 的特性吗？——通过
- **正向依据**：逐条过宪法 C1–C6 + 工作区红线（durable run/checkpoint、多租户隔离、exactly-once、前缀缓存、WS 状态机、飞书通道）——均不触碰。P0/P1 无新增 LLM 调用、不改 token 前缀。
- **负向探针**：最可疑的是 exactly-once（清淤删除 + 迁移合并幂等性）——迁移合并靠 `(agent,project,key)` 唯一约束 + `on_conflict_do_nothing` 幂等，清淤是明确授权的一次性操作，不碰 exactly-once 语义。

---

## Phase 5 — 实现落地闭环（待实现后执行）

方案落地为 diff 后，**必须**跑 `code-review`（Spec 轴对照本方案 + Phase 4 裁决）复核 diff：无偏离、无夹带范围外改动、无「评审时没提过的机制」（尤其拒绝的 E/embedding、暂缓的 P2/分类门不得夹带）。跳过或存在未解决偏离 → 不算交付闭环。

---

## 交付结论

- **裁决：有条件通过**（P0 契约 + 清淤、P1 迁移修复、P2 分类门暂缓为可选）。**六审新增条件**：清淤范围从「950a1943 一家」扩为「全平台 `legacy:%`」，且 P1 须先于其余 8 个 agent 首次迁移部署。**七审修正**：P1 修法从「合并到单一 legacy 死作用域」改为「`project: -` 段迁入平台派生的 LIVE project」（三个入口透传 project 参数）；8 个 `project: -` agent 非死数据（5 活跃含真待办 + 3 纯观察），归档顺序三分。
- **四审→五审关键修订（经用户确认）**：`mydome1` 项目已闭环（MR !1~!11 全合并、main=`b9240bd`、agent 自陈「无在途」），清单表 139 行是「交付未收口」的过时快照。清淤从「逐 key 分类保留真待办」修正为「**整体清空**」——`mydome1` 76 pending → completed、`legacy:*` 58 行 → 删除；根因第二层从「潜在缺口」升级为「**已损**」（交付后未同步 complete）；P0-a 契约新增「交付收尾对账 complete」条款。
- **五审→六审关键修订（本次实读核实）**：复发面从「未来 agent」修正为「**8 个在活 agent 已持 `project: -` 的 `清单.md`（36 个 section）未迁移**」——PG 里 `legacy:*` 仅 950a1943 一家即证明它们还没触发迁移，下次碰清单工具即复刻同类分叉。据此：P1 定性从「防复发」升为「先于 8 个 agent 首次迁移部署的紧急项」；P0-b 从「清 950a1943」扩为「全平台 `legacy:%` 清淤」；并补三处勘误——①「phase 误判 active」是条件性潜在风险（实测 `任务状态.md` 已 `phase: complete`，未误判）；②2 个 real-project agent（`emails`/`store-launch`）迁移会在 LIVE 作用域压平 ✅ 快照为 pending；③**清淤耐久性**：迁移每次调用都重跑且不归档源文件，须「先归档 10 源 `清单.md` 再清库」否则清掉的行走重迁移插回。实现顺序定为 **P1 → 归档 10 源文件 → P0-b 清库 → P0-a**。
- **六审→七审关键修订（本次实读核实，决定性）**：六审把 8 个 `project: -` agent 统一判「死数据、可直接归档」是**过度外推**。实读 8 个 agent 的 `清单.md` 全文 + `agent_focus_items` + `agent_runs` 台账：**全部 8 个都是活跃 agent**（run 数 61~524，最近 run 均 2026-09-09 当天），其中 **5/8 的 `project: -` 段含真实在途待办**（阻塞于用户凭据/授权/输入，有 focus in_progress 项交叉印证），仅 **3/8** 是纯观察快照。`project: -` 是「旧格式无 project 字段」的产物（`_parse_section_lines` 把 `project: -` 解析为 None），非「死数据」标记。据此**推翻 P1 原「合并到单一 legacy 死作用域」修法**，改为「`project: -` 段迁入平台派生的 LIVE project」（三个入口透传 project 参数）；四个技术坑（sort_order 唯一约束重排、idempotency 守卫改造、project 透传、无 project 兜底降级 legacy）即对新 P1 方向的 Q4（副作用）/Q7（幂等）复审，结论仍通过。归档顺序三分（5 活跃先迁真待办+对账再归档、3 纯观察直接归档、2 real-project 先迁移对账再归档）。950a1943 的 58 行 legacy（项目已闭环）由 P0-b 兜底删除。
- **已知风险已入正文**：①P1 迁移合并须处理 `(agent,project,sort_order)` 唯一约束冲突（重排 sort_order）；②P1 须与 P0-b 配套交付（`project: -` 段迁入 LIVE 后，950a1943 已迁移的 58 行 legacy 死数据仍由 P0-b 删除，迁移产生的 LIVE stale pending 由对账 complete 收口）；③P2 引用勘误（`_classify_seed_kinds` 是 task|learning 非 task|note、无粘性）。
- **诚实边界**：
  - P0-b 清淤**写库需用户明确授权**（当前红线：DB 只读）；清淤前只读预览 + 备份。
  - ③过时描述**无纯结构解法**——靠契约「描述=收据」+ 清淤清空；不引入投机机制。
  - P2 触发条件已显式化（「契约后仍观察到新系统 note-noise」），非永久搁置。

- **下一步**：本方案为纸面方案（一审→六审复核定稿）。P1 迁移修复须**优先落地**（封住 8 个 agent 的分叉）；P0-b 全平台清淤（写库）需用户明确授权后执行；P0-a 契约可与 P1 并行。实现后按 Phase 5 跑 code-review 复核闭环。

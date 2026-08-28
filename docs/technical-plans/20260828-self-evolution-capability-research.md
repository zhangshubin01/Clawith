# 自进化能力研究报告

> 一句话摘要：Clawith 的自进化已形成一条**以文件式记忆（memory/ + soul.md + skills/）为核心、由运行时门禁保证写入、由上下文装配保证读回**的闭环，但「记忆整合层」「程序记忆自动沉淀」「agent-maintainers 治理门控」「记忆质量评估」仍是设计/计划，未落地代码。

- 日期：2026-08-28
- 结论性质：全部声明来自仓库一手资料（代码 grep/read + docs read），逐条标注来源；区分「已证实实现」「设计/计划」「文档-实现不一致」。

---

## 1. 结论摘要

| 类别 | 机制 | 状态 |
|---|---|---|
| ✅ 已实现（代码证实） | Memory Maintenance 提示词义务（D6/D1a） | 运行时注入（agent_context.py:448） |
| ✅ 已实现（代码证实） | Memory Consolidation Gate 运行时门禁（ADR-0005） | 生产部署（node_executor.py:826） |
| ✅ 已实现（代码证实） | Reflections + user_profile 每-run 注入（ADR-0008 A） | 代码在，per-agent 开关默认 **off**（agent_context.py:187） |
| ✅ 已实现（代码证实） | 心跳反思循环（HEARTBEAT → reflections / curiosity） | 模板 + 迁移脚本（templates/HEARTBEAT.md） |
| ✅ 已实现（代码证实） | soul.md 身份注入 + agent 可自写 soul（delete/move 保护） | agent_context.py:562 / agent_tools.py:2992 |
| ✅ 已实现（代码证实） | 技能目录从 agent 自身 filesystem 注入 + install_skill/ClawHub 装技能 | agent_context.py:119 / agent_tools.py:5778 |
| ✅ 已实现（代码证实） | 内置 Skill 版本对齐（白名单覆盖，ADR-0004） | skill_seeder.py:1148 |
| ⚠️ 部分实现 | skill-creator「测评→改进」循环 | 只创作可用，eval/improve 依赖外部生态（20260827 spec D2） |
| ❌ 仅设计/计划 | Agent Maintainers 权限门控 | 0 代码（20260819 两份 plan） |
| ❌ 已废止 | Memory Consolidation Run（ADR-0007） | 被 ADR-0008 取代，无代码 |
| ❌ 仅计划 | 记忆整合层（G1）/ 后台固化（G2）/ 记忆质量评估（G4）/ 检索打分（G5） | 20260828 best-practices 差距清单 |
| ⚠️ 存在但未接通 | 组织层 experience_entries / 团队层 agent_group_memory | 表与工具在，ADR-0008 明确「E/F 不受本 ADR 影响」 |

**一句话结论**：这个项目「**能改自己、能记住、能影响下一次运行**」的闭环已经存在且落在运行时代码里（门禁 + 上下文装配），但它是**声明式文件记忆 + 提示词义务 + 一个运行时强制轮**的形态，不是「改写自身 prompt 模板/代码的 agent-maintainer」形态——后者只有设计文档、没有实现代码；「记忆整合」「程序记忆自动沉淀」「评估闭环」三块在调研文档里被明确判为缺口。

---

## 2. 机制清单

### M1 — Memory Maintenance 提示词义务（每-run 常驻）
- **名称**：`_MEMORY_MAINTENANCE` 静态提示词段
- **触发时机**：每次 `build_agent_context` 装配，且 read_file 与 write_file 都在 allowed 工具集内时注入（backend/app/services/agent_context.py:663-664）。
- **数据流**：build_agent_context 写 → model 每次推理读 → model 决定是否调 read_file/write_file 改 memory 文件。
- **持久化位置**：<agent_id>/memory/memory.md、memory/reflections.md、memory/MEMORY_INDEX.md（文件式存储）。
- **代码证据**：常量定义 backend/app/services/agent_context.py:448-471（含「先 read 再原位合并，禁止盲覆盖」「教训→reflections 四节」「同步 MEMORY_INDEX」「写失败不阻塞交付」「收尾判一次」条款）。

### M2 — Memory Consolidation Gate（运行时强制门禁，ADR-0005）
- **名称**：Memory Consolidation Gate
- **触发时机**：run 的 `_model` 节点收到 finish 意图、进入 verifying 之前（backend/app/services/agent_runtime/node_executor.py:826-869）。
- **判定**：workspace_writes > 0 且 memory_writes == 0 且未 forced 且 step_count+1 <= model_step_limit。
- **数据流**：`_tool` 节点累计写计数（node_executor.py:1147-1157）→ _model finish 分支读取 → 注入一轮 `MEMORY_CONSOLIDATION_PROMPT`（node_executor.py:51-66，条件义务措辞，route 到 memory.md 或 reflections.md）→ 仍不写则放行并留痕。
- **持久化位置**：计数在 checkpoint lifecycle（backend/app/services/agent_runtime/state.py:133-139）；留痕为 memory_consolidation_skipped 事件（backend/app/services/agent_runtime/checkpoint_side_effects.py:747-770），事件类型白名单 backend/app/models/agent_run_event.py:36，迁移 backend/alembic/versions/v1_11_4_f072_memory_consolidation_event.py。
- **护栏**：只数 write_file/edit_file（_WORKSPACE_WRITE_TOOLS，node_executor.py:44）；memory/ 前缀算 memory 写（_MEMORY_PATH_PREFIX，node_executor.py:45）；至多强制一轮（forced_memory_consolidation，node_executor.py:850）；预算不足→step_budget_exhausted、二次仍无→no_memory_write_after_forced_round（node_executor.py:856-857）。

### M3 — Reflections + user_profile 每-run 注入（ADR-0008 A）
- **名称**：Reflections Injection
- **触发时机**：build_agent_context，且 per-agent 开关 context_inject_reflections_{agent_id} 的 {"enabled": true} 为真（backend/app/services/agent_context.py:187-210，**缺省 off**）。
- **数据流**：_extract_reflections_injection 节过滤（只取 Insights & Discoveries 全节 + Hypotheses & Experiments 的 ✅/❌ 结论行，上限 2000 chars；agent_context.py:38-78）→ 注入 dynamic（uncached tail）块 <reflections_context>（agent_context.py:678-689）；user_profile.md 全量注入（agent_context.py:620-623、690-699）。
- **持久化位置**：<agent_id>/memory/reflections.md、memory/user_profile.md。
- **代码证据**：_load_reflections_injection_enabled（agent_context.py:187）、_extract_reflections_injection（agent_context.py:38）、注入点（agent_context.py:678）。

### M4 — 心跳反思循环（HEARTBEAT → reflections / curiosity）
- **名称**：Heartbeat reflection cycle
- **触发时机**：心跳 run 读 HEARTBEAT.md（backend/app/services/heartbeat.py:69 读取 agent 的 HEARTBEAT.md key）。
- **数据流**：Phase 1 读 memory/reflections.md → Phase 2 探索并把原始发现写 memory/curiosity_journal.md → Phase 3 把 resolved 问题推进 reflections.md 的 Insights、收敛 curiosity 的 Follow-up/Active Questions 到 Next Cycle Seeds（≤3，标 →promoted，不删除）（backend/app/templates/HEARTBEAT.md:5-50）。
- **持久化位置**：memory/reflections.md、memory/curiosity_journal.md。
- **代码证据**：模板统一（backend/app/templates/HEARTBEAT.md 与 backend/agent_template/HEARTBEAT.md 内容一致，backend/tests/test_heartbeat_template_consistency.py）；reflections 模板 backend/app/templates/reflections.md:5-23（四节）；agent 创建时复制模板 backend/app/services/agent_manager.py:207-217；存量迁移脚本 backend/app/scripts/migrate_legacy_heartbeat_template.py:47。

### M5 — soul.md 身份注入 + agent 可自写 soul
- **名称**：Persistent identity / soul injection
- **触发时机**：build_agent_context 读取 soul.md（backend/app/services/agent_context.py:562-572），去首行 # 后以 <soul> 注入（agent_context.py:636-637）。
- **数据流**：agent 可用 write_file/edit_file 改 soul.md（autonomy policy "modify_soul": "L1"，backend/app/models/agent.py:30）；但 delete_file/move_file 工具描述明确「Cannot delete/move soul.md, tasks.json」（backend/app/services/builtin_tool_definitions.py:188,205），代码层 agent_tools.py:2992、workspace_collaboration.py:752 同样保护。
- **持久化位置**：<agent_id>/soul.md；创建期模板 backend/agent_template/soul.md、backend/agent_templates/*/soul.md（如 backend-architect/soul.md、risk-manager/soul.md，含写 memory/*.md、workspace/* 的职责条款）。

### M6 — 技能目录注入 + 运行时装技能（G3 的部分落点）
- **名称**：Skill catalog from agent filesystem + skill installation
- **触发时机**：build_agent_context 在 read_file 可用时调用 _load_skills_index（agent_context.py:645-660），扫描 <agent_id>/skills/*/SKILL.md（agent_context.py:119-184），注入 # Available Skills 目录表 + 「先 read 再行动」策略。
- **数据流**：agent 用 install_skill(source=<ClawHub slug|GitHub URL>) 装技能（backend/app/services/builtin_tool_definitions.py:2895-2904；实现 _install_skill backend/app/services/agent_tools.py:5778、22777）；search_clawhub 检索 ClawHub 注册表（builtin_tool_definitions.py:2879、agent_tools.py:22554）。装完的技能落 <agent_id>/skills/，**下一次 run** 的 _load_skills_index 即把它列入目录。
- **持久化位置**：agent 文件层 <agent_id>/skills/<folder>/SKILL.md；内置技能另有一层 DB 注册表（skills + skill_files，见 M7）。
- **代码证据**：_load_skills_index（agent_context.py:119）、_install_skill（agent_tools.py:22777）。

### M7 — 内置 Skill 版本对齐（白名单覆盖，ADR-0004）
- **名称**：Builtin skill version alignment（平台侧自愈，非 agent 自进化）
- **触发时机**：启动 seed_skills（backend/app/services/skill_seeder.py:1193）更新 DB 注册表；push_default_skills_to_existing_agents（skill_seeder.py:1272，backend/app/main.py:330-332 调用）逐 agent 对齐文件层。
- **数据流**：_align_default_skill_files（skill_seeder.py:1148）四分支——缺补 / 与 DB 同不动 / md5∈白名单覆盖 / 否则保留+告警。白名单 = 静态种子 ∪ 持久化 SystemSetting(builtin_skill_version_whitelist)（skill_seeder.py:1048,1056,1082）；摘要门 DEFAULT_SKILLS_SYNC_HASH_KEY（skill_seeder.py:1049）。
- **持久化位置**：SystemSetting（DB）。
- **代码证据**：BUILTIN_SKILL_VERSION_SEED（skill_seeder.py:1056）、_align_default_skill_files（skill_seeder.py:1148）、push_default_skills_to_existing_agents（skill_seeder.py:1272）。

### M8 — 组织/团队经验层（存在但未接通）
- **名称**：organizational experience_entries / team agent_group_memory
- **触发时机**：组织层 experience_entries 表（backend/app/models/experience.py:35）与工具 search_experience/read_experience/propose_experience_draft（策略提示见 agent_context.py:517-532）；团队层 agent_group_memory（backend/app/services/agent_runtime/group_context_builder.py:358）。
- **数据流/持久化**：DB 表。
- **状态**：ADR-0008 后果明确「E 组织层经验、F 团队层 不受本 ADR 影响」（docs/adr/0008-...md:99-100），即**尚未接入自进化闭环**。

---

## 3. 自进化闭环评估（谁能真正影响下一次运行）

### 真闭环 ✅

| 机制 | 写入端 | 读回端 | 结论 |
|---|---|---|---|
| M1+M2 → M3 读回 | run 内写 memory.md / reflections.md | build_agent_context 读 memory.md 前 2K +（开关开时）reflections/user_profile | **闭环成立**：本次 run 的知识进入下次 run 上下文 |
| M4 心跳反思 | heartbeat 写 reflections.md | 下次 heartbeat Phase 1 读；下次 run（若开关开）注入 | **闭环成立**（心跳自循环 + 可选 run 注入） |
| M5 soul | agent 自写 soul.md | 下次 run 注入 <soul> | **闭环成立**：agent 能改自身指令，但有 delete/move 保护、无审批门控（风险见 §6） |
| M6 技能 | install_skill 装技能 | 下次 run _load_skills_index 列目录 | **闭环成立**：agent 能扩展自身技能面 |

### 断头路 ⚠️

1. **reflections.md 默认是写黑洞**：M2 门禁与 M4 心跳都写 reflections.md，但读回通道 M3 的开关 context_inject_reflections_{agent_id} **缺省 false**（agent_context.py:187-210 注释「Absent or malformed rows mean off」）。不开开关的 agent，reflections 只写不读——这正是 ADR-0008 定位的「黑洞」，A 项修了通道但默认关闭。
2. **curiosity_journal 的 Follow-up/Active Questions**：M4 Phase 3 把它们 promote 进 Next Cycle Seeds，但 Next Cycle Seeds 被 M3 的节过滤器**显式排除**（agent_context.py:32-45 只取 Insights + Hypotheses 结论行）——即这些待办只喂下一次心跳，**不喂 run 上下文**。
3. **MEMORY_INDEX.md 无检索消费者**：M1/M2 都要求「同步 MEMORY_INDEX」，但没有任何代码按 index 做检索/分块注入（ADR-0008 的「D 废弃 INDEX」列为 P2，见 docs/adr/0008-...md:99），索引是 memory.md 的镜像目录、不是读取优化。
4. **skill-creator 的 eval/improve 循环断**：创作（写 SKILL.md 草稿）与 install_skill 可用，但 run_eval.py 依赖 claude -p CLI、improve_description.py/run_loop.py 依赖 anthropic SDK + ANTHROPIC_API_KEY，三者 Clawith 运行时均无（docs/technical-plans/20260827-...md:18-25）。skill-creator 的种子文件确实在 backend/agent_data/*/skills/skill-creator/scripts/（glob 实证）。
5. **experience_entries / agent_group_memory**：表与工具存在，但未接入个人层循环（M8）。

---

## 4. 未实现 / 计划中的部分

以下均**只有设计/调研文档，无实现代码**（grep 0 命中或文档自述状态）：

1. **Agent Maintainers 权限门控**：agent_maintainers 表、MaintainerService、resolve_file_modify_permission 均 **0 代码命中**。两份文档状态为「待实施」（docs/technical-plans/20260819-agent-maintainers-implementation-plan.md:4）与「设计草案（待确认后进入实施）」（...-permission-model.md:4）。这意味着「agent 在非维护人员驱动下能否改 workspace/skills」的治理边界**尚未落地**，当前 delete/modify 仍走既有 L1/L2/L3 审批（plan §1.5 已核对现状：durable 路径只有 delete_file 走 L3、write/edit/move 无门控）。
2. **Memory Consolidation Run（ADR-0007）**：**已废止**（docs/adr/0007-...md:3-6），「独立 scheduled 整合 run」被 ADR-0008 取代，无 system_role="memory_consolidation" 代码（grep 0 命中）。
3. **记忆整合层 G1**：LangMem 式 consolidate/update/delete、防 over/under-extraction——docs/technical-plans/20260828-self-evolution-best-practices.md:75-76 判为「最该做」，未实现。
4. **后台固化通道 G2**：...best-practices.md:78-79，A/B 而非替代，未实现。
5. **程序记忆固化 G3（自动技能沉淀）**：...best-practices.md:81-83「让 agent 把重复三次以上的成功流程沉淀为 skill 文件」，未实现（M6 只支持「人工/agent 手动 install_skill」，不支持自动沉淀 + 质量门禁）。
6. **记忆质量评估闭环 G4**：...best-practices.md:84-86，未实现。
7. **检索打分 G5**：...best-practices.md:87-89（importance×recency 分块注入），未实现。
8. **skill-creator eval runner（D2）**：docs/technical-plans/20260827-self-evolution-repair-spec.md:145-162 定「方案 A：OpenAI 兼容 runner」，未实现。
9. **tasks 错误码（D4）**：...repair-spec.md:173-177，与自进化主线弱相关，列为计划。

---

## 5. 文档-实现一致性核查结果

| 文档声明 | 代码核实 | 结论 |
|---|---|---|
| ADR-0005「门禁在 node_executor _tool/_model」 | node_executor.py:1147-1157（计数）、826-869（门禁） | ✅ 一致 |
| ADR-0005「memory_consolidation_skipped 事件 + f072 迁移」 | agent_run_event.py:36、checkpoint_side_effects.py:760、alembic/.../v1_11_4_f072_...py | ✅ 一致 |
| ADR-0008 B「门禁措辞扩 reflections」 | MEMORY_CONSOLIDATION_PROMPT 点名四节 + reflections.md（node_executor.py:51-66）；_MEMORY_MAINTENANCE 同步（agent_context.py:455-460） | ✅ 一致 |
| ADR-0008 A「reflections 注入 + per-agent 开关」 | _extract_reflections_injection（agent_context.py:38）+ _load_reflections_injection_enabled（agent_context.py:187）+ 注入点（agent_context.py:678） | ✅ 一致 |
| ADR-0008 G「心跳收敛 curiosity + 模板统一 + 迁移脚本」 | templates/HEARTBEAT.md:45-49 + scripts/migrate_legacy_heartbeat_template.py | ✅ 一致 |
| ADR-0004「白名单式 skill 版本对齐」 | skill_seeder.py:1048/1056/1148/1272 | ✅ 一致 |
| repair-spec D1「记忆维护义务回归静态提示词」 | _MEMORY_MAINTENANCE 在 agent_context.py:448、注入于 663-664 | ✅ 一致 |
| repair-spec D1b「HEARTBEAT 模板统一」 | app/templates/HEARTBEAT.md 与 agent_template/HEARTBEAT.md（读文实证内容一致） | ✅ 一致 |
| **agent-maintainers 两份 plan「门控已接三处执行点」** | agent_maintainers/MaintainerService/resolve_file_modify_permission **0 命中** | ❌ **文档-实现不一致：文档描述目标态，代码仍是旧 L1/L2/L3** |
| **ADR-0007「独立整合 run」** | 无 system_role="memory_consolidation" 代码 | ✅ 一致（文档已标「废止」，代码确实没有） |
| **best-practices「只人工维护 skills、无自动沉淀」** | install_skill/_load_skills_index 证实技能来自 agent 文件层，但无「自动沉淀」代码 | ✅ 一致（G3 确为缺口） |
| **best-practices「memory.md 只增不减不整合」** | _MEMORY_MAINTENANCE 只要求「原位合并」，无 consolidate/归档代码 | ✅ 一致 |
| **CONTEXT.md「Reflections Injection 进 dynamic 段、不破坏 static 缓存」** | 注入点位于 dynamic_parts（agent_context.py:678-698），static_parts 在 639-664 | ✅ 一致 |
| repair-spec「skill-creator eval 依赖 claude CLI/anthropic SDK 不可用」 | 种子脚本逐行复核：run_eval.py:73 subprocess 调 `claude` CLI；improve_description.py:14,220 / run_loop.py:18,79 `import anthropic` + `anthropic.Anthropic()`（SDK 自动读 ANTHROPIC_API_KEY） | ✅ 一致（Clawith 运行时无 claude CLI、无 ANTHROPIC_API_KEY，eval/improve 循环确实断） |

**最关键的文档-实现不一致点（2 个）**：
1. **agent-maintainers 权限门控**：两份 plan（20260819-*-implementation-plan.md / -permission-model.md）用「逐条核对代码」的确定性语气描述门控方案与三处接入，但目标符号在 backend/ 中 **0 命中**——文档状态写的是「待实施/设计草案」，但 §1.5 对「真实现状」的核对（write/edit/move 无门控、ACP check_tool_autonomy truthy bug、两份 _TOOL_AUTONOMY_MAP 矛盾）是**尚未被修复的现状描述**，读者极易误以为已落地。
2. **reflections 注入默认关闭**：ADR-0008 把 A 项写成「已接受」的 P0 动作，代码也确实实现了，但开关 context_inject_reflections_{agent_id} **缺省 false**（agent_context.py:187-210）。于是「reflections 循环接通」的 A 环节对未开开关的 agent 是**断开**的，文档正文（「每 run 上下文携带演化成果」）与代码默认行为之间存在语义落差——属于「已实现但默认未启用」的不一致。

---

## 6. 开放问题 / 风险

1. **自指修改的权限边界未定**：agent 可 write_file/edit_file 改 soul.md（自身指令）与 skills/（自身技能），autonomy_policy 里 modify_soul: L1（自动执行）。当前唯一的硬保护是 delete_file/move_file 拒绝碰 soul.md/tasks.json（builtin_tool_definitions.py:188,205），以及「治理层而非硬安全边界」的自述（permission-model.md:21-26：agent 手里有 shell，execute_code 可绕过一切文件门控）。**agent-maintainers 若想成为真正的治理门控，必须先回答：write/edit 到 soul.md/skills/ 的边界由谁判定、是否也纳入名单判定**——而该方案尚未实现。
2. **循环污染（memory-loop）风险**：ADR-0008 与 ADR-0005 反复强调「至多一轮」「条件义务不写成祈使目标句」正是为防「agent 把写记忆当成新任务、无限自我强化」的循环（docs/adr/0005-...md:28、docs/technical-plans/20260827-...md:109-114「R1 循环教训」）。但 reflections 是「agent 自观测内容，有幻觉风险」（docs/adr/0008-...md:97-98）——低信任声明 + 只注结论性内容只是缓解，一旦 reflections 被注入后又成为 agent 写 reflections 的素材，存在**自我引用放大**的隐患，目前无「来源/置信度」标注机制。
3. **context rot 无治理**：memory.md 只增不减（best-practices.md:50,67），读回又只截前 2K 字符（agent_context.py:574-577 的 _read_file_safe(..., 2000)），G1 整合层未做——记忆膨胀与「头部截断 vs 价值序相反」的张力（docs/adr/0008-...md:33-35）会在长期运行中持续劣化。
4. **记忆写入成功率依赖模型服从**：ADR-0005 后果自认「强制轮不能保证模型听话」（docs/adr/0005-...md:77-78）——门禁只保证「强制一次 + 留痕」，不保证「真的写了」；实测固化率约 1/3（best-practices.md:42）。
5. **评估缺失**：G4 记忆质量 benchmark 未做，因此「自进化是否在变好」目前只有 memory_consolidation_skipped 留痕事件，没有把它升级成反馈信号（best-practices.md:84-86）。
6. **skill 自动沉淀的风险未解决**：G3 明确「必须经过人工审核或严格 bench 验证才入 skills/，否则劣质技能污染后续所有 run」（best-practices.md:81-83），但该审核门禁本身未实现——与第 1 条（agent 可自写 skills/）叠加后，劣质/恶意技能有污染全部后续 run 的路径。

---

## 附：核心代码索引

| 机制 | 文件:行 | 符号 |
|---|---|---|
| Memory Maintenance | backend/app/services/agent_context.py:448 | _MEMORY_MAINTENANCE |
| 门禁 prompt | backend/app/services/agent_runtime/node_executor.py:51 | MEMORY_CONSOLIDATION_PROMPT |
| 门禁判定 | backend/app/services/agent_runtime/node_executor.py:826-869 | _model finish 分支 |
| 写计数 | backend/app/services/agent_runtime/node_executor.py:1147-1157 | _tool |
| lifecycle 字段 | backend/app/services/agent_runtime/state.py:133-139 | RuntimeLifecycle |
| skip 事件 | backend/app/services/agent_runtime/checkpoint_side_effects.py:747-770 | _record_lifecycle_events |
| 事件白名单 | backend/app/models/agent_run_event.py:36 | — |
| reflections 注入 | backend/app/services/agent_context.py:38,187,678 | _extract_reflections_injection / _load_reflections_injection_enabled |
| 技能目录 | backend/app/services/agent_context.py:119 | _load_skills_index |
| 装技能 | backend/app/services/agent_tools.py:5778,22777 | _install_skill |
| 技能版本对齐 | backend/app/services/skill_seeder.py:1056,1148,1272 | BUILTIN_SKILL_VERSION_SEED / _align_default_skill_files / push_default_skills_to_existing_agents |
| soul 注入 | backend/app/services/agent_context.py:562,636 | build_agent_context |
| soul 保护 | backend/app/services/agent_tools.py:2992、workspace_collaboration.py:752 | — |

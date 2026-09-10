# 生产级落地方案：G3 程序记忆沉淀（解挂 → 草稿 + 人工移动通道）

日期：2026-09-07
状态：**评审通过（有条件）** —— 7 角度全过，无阻断项；2 项已知风险（§6，均带缓解，已折入正文）
前置：`20260905-maintainer-gate-g3-g4-production-plan.md`（§4 = G3 既有设计；§4.3 = grill 决策 8「G3 挂起观察」）
       `20260902-self-evolution-gap-closure-plan.md`（§4 = G3 最小实现 = 提示词义务 + human-gate；G3-1 拍板 = 草稿写 `workspace/skill-drafts/` + 人工移动入 `skills/`）
       `20260828-self-evolution-best-practices.md`（程序记忆来源：Voyager 技能库 / LangMem procedural memory）

## 结论先行

**解挂成立、现在做 G3，但保持「最小提示词义务 + human-gate」的既有最小形态，不新增任何运行时机制。**

本方案的形状（与用户契约一致）：
1. **草稿**：HEARTBEAT 模板 Phase 3 追加「草稿」步（条件义务措辞）——agent 把「重复 ≥3 次的成功流程」写成 `workspace/skill-drafts/<name>.md`（**不落 `skills/`**，避免被 `_load_skills_index` 扫进目录表），草稿自带 `## 来源` 节引 reflections 条目。
2. **人工移动通道**：维护人员把草稿移入 `skills/<name>/SKILL.md`——Maintainer 门控（① 已落地）恰好保证只有维护人员驱动下才写得进 `skills/`，**零新代码**。
3. **HEARTBEAT 条件义务**：触发信号 = LLM 解释性「成功流程重复 ≥3 次」，非机器阈值（理由见 §3.3——这是与 G1「20K 阈值永不触」覆辙的本质区别）。
4. **P2 待审列表**：只读列出 `workspace/skill-drafts/`（复用既有 `fileApi.list_files` + `FileBrowser`），把「发现权交给人」。

最小改动清单（§3.4）：改 2 个模板文件 + 1 个迁移常量 + 2 处测试，**零运行时代码、零新工具、零新表、零新事件**。

---

## 0. 接地数据（2026-09-07 实测，非凭旧报告）

数据源：docker volume `clawith-agent_agentdata`（backend 容器 `clawith-agent-backend-1` 挂载 `/data/agents`，`STORAGE_LOCAL_ROOT=/data/agents`），只读 `mcp__docker__container_exec`。

| 维度 | 数据 | 结论 |
|---|---|---|
| 存量 agent 目录 | `/data/agents` 下 **24 个 agent 目录（23 个有 `memory/reflections.md`）** | 与 09-02「全库 Insights 个位数」时的基数一致，但内容已变 |
| 有真实 Insights 的 agent | **9 个** agent 的 `memory/reflections.md` 有真实 `- ` 条目（62bc9c81 / b05d3a82 / 475264c9 / 2ebdfee6 / ddc779e3 / 27d55a64 / 82dc9a8a / b1a73489 / 950a1943）；其余多为 4 行占位模板 | 触发前提的「数据池」已实质扩张 |
| 最活跃 agent | `950a1943`（mydome1 安卓项目）：`## Insights & Discoveries` 节 15–43 行，全文 **44 条 `- ` 行**（`grep -c "^- "` = 44） | 单 agent 已攒到「重复计数」可行的量级 |

**最活跃 agent 的真实条目逐条核验（摘录，证明「重复 ≥3 次成功流程」确实存在，而非环境噪声）**：

- git 交付收尾（commit→push→MR→`PUT /merge_requests/:iid/merge`）——**≥10 次**独立 dated 条目（09-01 MR !1 ebbd357 → 09-05 MR !6/!7 → 09-07 MR !8），每次都含「token 提取姿势」与「merge -s ours vs 真实 merge 决策」。
- `android_compile` 验证（`testDebugUnitTest` + `assembleDebug`）——**≥8 次**「✅ 已验证」。
- GitLab token 提取姿势——**≥5 次**，且带修正演进（sed 丢 `glpat-` 前缀 → `grep -o 'glpat-[A-Za-z0-9_-]*'`）。
- execute_code 沙箱幽灵执行判别法（落盘探针 / reflog 判别）——**≥6 次**。
- reflections 写入后「同 run 回读确认」——**≥4 次**。

**结论**：09-02 grill 决策 8 的挂起理由（「全库 reflections Insights 仅个位数条目，『≥3 次』前提不可达」）**已被真实数据推翻**——最活跃 agent 已有 ≥10 次重复的交付收尾流程。数据驱动解挂成立，无需重新设计触发信号（§3.3 诚实标注「≥3 次」的解释性本质与为何可接受）。

---

## 1. 参考资料对比（Phase 1，≥10 项目，含诚实负结论）

决策点拆解（G3 的三个关键决策）：

- **D1 沉淀通道形态**：草稿→人工移动（human-gate）vs 自动入技能库。
- **D2 触发信号**：「成功流程重复 ≥3 次」（LLM 解释性计数）vs 环境反馈（Voyager）vs 交互轨迹归纳（AWM）。
- **D3 程序记忆载体**：`SKILL.md` 文件目录（渐进披露）vs 系统提示自动改写（prompt optimizer）vs 自治子代理。

| # | 参考项目 | 位置 | 结论 / 偏离理由 |
|---|---|---|---|
| 1 | **Voyager**（arXiv 2305.16291） | 综述 quick-start（best-practices §2.4） | 技能库「code-as-policy」：成功程序自动入 library。**触发=单次任务成功（环境验证），无「重复 N 次」门槛**。→ 借鉴「成功流程沉淀为技能」的目标；**偏离**：Clawith 无可靠环境验证信号（`execute_code` 沙箱在 mydome1 上间歇性幽灵执行/不可用，见 §0 数据），故用「重复 ≥3 次」降噪 + human-gate 替代自动验证 |
| 2 | **LangMem** procedural memory | 本地 `UGit/langmem/` + best-practices §2.1 | 程序记忆是**独立类型**，通过 prompt optimizers 从交互反馈更新系统提示。→ 借鉴「程序记忆 ≠ 语义/情景记忆」的边界（对应 §3.3 的负向准则）；**偏离**：prompt optimizer 是自动改写系统提示，Clawith 不做自动改写（human-gate） |
| 3 | **Anthropic Agent Skills / skill-creator** | 本地 `UGit/skills/` + 20260905 plan §1 | 「草稿→评测→改进」渐进式质量门。→ 借鉴「草稿→评测→改进」的渐进披露 + 质量门思想；**偏离**：skill-creator 用 benchmark 评测改进，Clawith 本期用人工审核（G4 judge 未接，grill 决策 7） |
| 4 | **letta-code memory-v2 自治子代理** | 本地 `UGit/letta-code/` | reflection/recall 自治记忆子代理。→ 借鉴「记忆写入门禁 + 回顾」；**诚实负结论**：其自治子代理是完整新服务，Clawith 不新建（宪法 II 最小改动，zero-new-runtime） |
| 5 | **Mem0** | 本地 `UGit/mem0/` | 「写入轻、检索重」single-pass LLM 提取。→ **诚实负结论**：Mem0 做的是 semantic/episodic 提取，**无 procedural skill 沉淀机制**；「写入轻」对 Clawith 无直接可抄项 |
| 6 | **Andrej Karpathy skills** | 本地 `UGit/andrej-karpathy-skills/` | 个人技能库目录格式（`SKILL.md` + frontmatter）。→ 借鉴 `SKILL.md` 目录格式——正是 Clawith `_load_skills_index`（`agent_context.py::_load_skills_index`）已解析的格式 |
| 7 | **awesome-agent-skills / hermes-agent / langchain-skills** | 本地 `UGit/{awesome-agent-skills,hermes-agent,langchain-skills}/` | skills 生态目录格式 + `description`/triggers frontmatter。→ 借鉴「description + 触发条件」；Clawith 已有 `_parse_skill_frontmatter`（`agent_context.py::_parse_skill_frontmatter`），零新增 |
| 8 | **Agent Workflow Memory (AWM)**（arXiv 2409.07429） | best-practices §5 引用 | 从成功/失败轨迹**归纳 workflow 表示**（inductive）。→ 直接对应「成功流程沉淀」；**偏离**：AWM 是离线归纳 + 结构化 workflow 表示，Clawith 最小实现 = LLM 解释性计数 + human-gate，**不做结构化 workflow 归纳**（G3-2 拍板「不做自动语义聚类」） |
| 9 | **Reflexion / Self-Refine**（arXiv 2303.11366 / 2303.17651） | best-practices §5 | 反思回路生成「教训」。→ 借鉴「反思是触发源」（Clawith 的 reflections 即触发池）；**诚实负结论**：其产出是**语义教训**（对应 Clawith 经验库 `experience_entries`），非 **procedural 步骤**（对应 skill），故 G3 的边界必须划清（§3.3） |
| 10 | **12-factor-agents** | 本地 `UGit/12-factor-agents/` | ownership boundary 原则。→ 借鉴「所有权边界」：草稿所有权=workspace（agent 自主写）、成品所有权=skills（维护人员写），门控画线 |
| 11 | **skills-benchmarks** | 本地 `UGit/skills-benchmarks/` | skills 能力基准手法。→ 是 G4 评测口径，本期**不接**（grill 决策 7），仅作为 G3 未来质量回环的落点记录 |
| 12 | **Anthropic "Effective context engineering"** | best-practices §2.3 | context rot + note-taking + 渐进披露。→ 借鉴「渐进披露」（目录表只注 name+description，用时 `read_file` 读全文）——Clawith `_load_skills_index` 已实现，G3 **不改读侧** |

**诚实负结论（明示）**：参考清单里**没有任何项目提供「重复 ≥3 次 → 草稿 → 人工移动」的逐字可抄机制**。Voyager/AWM 的自动入技能库依赖「可靠的环境/执行验证信号」（Voyager 的 minecraft 环境反馈、AWM 的离线轨迹归纳），Clawith **缺该信号**（`execute_code` 沙箱不稳定），故用 **human-gate 替代自动验证**、用 **「重复 ≥3 次」降噪** 替代结构归纳。该组合是 Clawith 按自身约束（门控已落地、经验库已有 `propose_experience_draft` human-gate 先例）拼装，非抄自任一项。

---

## 2. 代码基线（2026-09-07 HEAD 实读重核，函数名定位、行号仅作约）

| 代码级事实 | 出处（已 read_file） | 对 G3 的含义 |
|---|---|---|
| HEARTBEAT 模板 Phase 3 **无草稿步** | `backend/app/templates/HEARTBEAT.md`（Phase 3 = 更新 reflections → 收敛探索日志 → reply） | G3 需在此追加「草稿」步（§3.4 ①） |
| 双模板完全一致，SHA `30283e1a…` | `app/templates/HEARTBEAT.md` 与 `agent_template/HEARTBEAT.md` `diff` 无差异，`shasum -a 256` 均 = `30283e1a06bc659e737134c903990f28cbf946ef765f019c060acba93110fdd9` | 改动必须同时改双模板（`test_both_heartbeat_templates_are_identical` 断言 byte 一致） |
| 新 agent 心跳模板落盘点 | `app/services/agent_manager.py:214-217`（`hb_template = Path(__file__).parent.parent / "templates" / "HEARTBEAT.md"`）+ `config.py:53 _default_agent_template_dir`（`agent_template/` 整目录 rglob 复制） | 双模板都要改：`app/templates/` 是新 agent 落盘点，`agent_template/` 是整目录复制 + 迁移脚本 current |
| 心跳指令装配 | `app/services/heartbeat.py::_build_heartbeat_instruction`（:62，读 `{agent_id}/HEARTBEAT.md` + `CUSTOM_HEARTBEAT_GUARDRAILS`） | 改模板即改心跳指令，零代码 |
| skills 目录表 | `app/services/agent_context.py::_load_skills_index`（:144，扫 `{agent_id}/skills/*/SKILL.md` 或 `skill.md`） | 草稿必须**不落 `skills/`**，否则被扫进目录表直接注入 |
| reflections 注入 | `agent_context.py::_extract_reflections_injection`（:46，只注入 Insights `- ` 条目 + Hypotheses ✅/❌，Insights 无数量上限、`max_chars=2000` 头截） | 「≥3 次」计数**不能依赖注入块**（截断 + 最新在前），必须依赖 heartbeat Phase 1「读全文 reflections.md」 |
| 模板迁移 | `app/scripts/migrate_legacy_heartbeat_template.py`：SHA-256 精确匹配；`LEGACY_HEARTBEAT_SHA256S` 现 5 个历史哈希（末位 `ed3de530`）；guard `if current_sha256 in legacy_set: raise`；`_current_template_bytes` 读 `agent_template/HEARTBEAT.md` | G3 改模板后需把**旧 SHA `30283e1a…` 加入 legacy set** + 跑 `--apply` 迁移存量 |
| 迁移测试钉住 current SHA | `tests/test_migrate_legacy_heartbeat_template.py::test_cli_defaults_to_dry_run_and_requires_apply_flag`（:300-302 断言 `_current_template_bytes()` SHA == `30283e1a…`） | 改模板后此断言**必断**，需更新为新 SHA |
| 模板一致性测试 | `tests/test_heartbeat_template_consistency.py`（`test_agent_template_heartbeat_drives_reflections_and_keeps_idle_guard` 等断言内容；`test_both_heartbeat_templates_are_identical`；`test_heartbeat_convergence_is_unconditional_even_when_idle` 断言 `HEARTBEAT_OK` 在 converge 步之后） | 需加草稿步断言；新草稿步必须排在 `HEARTBEAT_OK`（reply 步）之前 |
| 维护人员门控（G3 前置）已落地 | `app/services/maintainer_service.py::resolve_file_modify_permission`、`agent_runtime/tool_step_service.py::_maintainer_file_gate`、`models/agent.py` `agent_maintainers`、`is_maintainer`（详见 20260905 plan §3，已 implemented `aa539999` + 管理 API） | 「人工移动入 `skills/`」的写门控**已兜底**，G3 零新门控代码 |
| skills 双轨（须澄清） | DB `skills`/`skill_files`（`models/skill.py`，**全局 registry**：tenant 级、`/skills` API、ClawHub 导入、经 `agent_seeder.py`/`api/agents.py` 物化到 `{agent_id}/skills/<folder_name>/`）vs 文件目录（`_load_skills_index` 读文件、不读 DB） | **G3 沉淀的是「文件目录技能」**，不碰 DB registry。二者同名不同物，方案里显式区分 |
| heartbeat 终态投影 | `agent_runtime/heartbeat_completion.py::HeartbeatRuntimeCompletionHandler`（非 HEARTBEAT_OK 的 answer 写 `AgentActivityLog`，`summary=f"Heartbeat: {answer[:80]}"` **80 字符截断**；HEARTBEAT_OK **早退不写任何日志**）+ `HeartbeatSeedFocusHandler`（Next Cycle Seeds → Focus `source="heartbeat"`） | 「提示用户审核」**无直达用户通知通道**（oneshot 失败才发 `Notification`，且需 `triggered_by_user_id`）。落点须诚实设计（§3.4 ③） |
| 经验库 human-gate 先例 | `app/services/experience_retrieval.py`（`_HINT` 提示词 + `propose_experience_draft` 工具）+ `models/experience.py` `experience_entries`（draft→published→retired） | G3 与之并行但不同：**技能=procedural**（agent 本地文件目录、常驻目录表注入）、**经验=episodic/semantic**（团队级 DB、按需检索）——边界见 §3.3 |
| 前端复用点 | `frontend/src/components/FileBrowser.tsx` + `pages/agent-detail/tabs/SkillsTab.tsx`（已用 FileBrowser）+ `services/api.ts::fileApi.list_files`（`GET /agents/{agent_id}/files?path=`，后端 `api/files.py::list_files`） | P2 待审列表**零新组件**，复用 FileBrowser 指向 `workspace/skill-drafts/` |
| skill-drafts 现状 | 全库 `skill-drafts`/`skill_drafts` **零引用** | 无任何 draft 通道，需新建（仅模板 + 约定目录） |

---

## 3. 方案（最小改动）

### 3.1 解挂决策（数据驱动）

grill 决策 8 挂起的理由已不成立（§0）。**解挂，但保持最小形态**：不因数据到位就升级为「自动语义聚类 + 自动入 skills/」——那会重蹈 G1 原案「为臆想规模建机制」的覆辙，且违背 G3-1/G3-2 已拍板的「草稿 + 人工移动 + 不做聚类」。

### 3.2 通道设计（四件套）

**① 草稿**：`workspace/skill-drafts/<skill-name>.md`（agent-root-relative 路径，与现有模板的 `memory/…` 同规范）。不落 `skills/`——`_load_skills_index` 扫 `skills/` 会把草稿当正式技能注入目录表，污染后续所有 run（best-practices §2.5 release-engineering 护栏）。

**② 人工移动**：维护人员（`is_maintainer`）把 `workspace/skill-drafts/<name>.md` 移入 `skills/<name>/SKILL.md`。门控 `resolve_file_modify_permission` 已守卫 `skills/` 写（`GATED_DENIED` → 非维护人员被拒），**零新代码**。移动后 `_load_skills_index` 自动纳入目录表，下一次 run 起技能可用。

**③ 提示用户审核的落点（诚实）**：
- **尽力而为**：草稿写完后，heartbeat 总结（非 `HEARTBEAT_OK`）会进 `AgentActivityLog`——但 `summary` 被 `answer[:80]` 截断，且 `HEARTBEAT_OK` 时早退不写。故 activity 总结是「辅助提示」，**不是可靠通知**。
- **主发现面 = P2 待审列表**：只读列出 `workspace/skill-drafts/`（复用 `fileApi.list_files` + `FileBrowser`，零新组件）。这是「发现权交给人」的可靠落点。
- **不新增 Notification 通道**：heartbeat 无 `triggered_by_user_id`，新增通知 = 新机制，超最小实现（宪法 II）。

**④ 模板迁移**：改双模板 → 旧 SHA `30283e1a…` 加入 `LEGACY_HEARTBEAT_SHA256S` → 跑 `migrate_legacy_heartbeat_template.py --apply`（SHA 精确匹配，custom/current 不覆盖）。

### 3.3 触发信号设计（诚实处理两个已知风险）

**风险 a：「≥3 次」是 LLM 解释性计数，无机器信号。**

对比 G1 教训（「20K 阈值永不触」）：G1 的失败本质是**机器阈值门控自动动作**——阈值永不到达 = 机制白建、永不生效。G3 的「≥3 次」是**提示词启发的 LLM 判断，门控一个可选草稿 + 人工终审**：
- 假阳（误判重复，写出劣质草稿）→ 被 human-gate 拦截，代价 = 一个 `workspace/skill-drafts/` 文件，**不污染 skills/**。
- 假阴（漏判，不写草稿）→ 错过一次沉淀机会，**无污染、无副作用**。

两者代价都有界且非灾难，与 G1 的「自动改写 memory.md」有本质区别。故**保留 LLM 判断，不引入机器信号**（引入机器信号 = 给 free-text reflections 加结构化 workflow 实体 + 计数，等价于 G3-2 已否决的「自动语义聚类」，超最小实现）。**这是本方案唯一「接受而非消除」的风险**，缓解 = 草稿自带 `## 来源` 节（引具体 reflections 条目）→ 使「≥3 次」claim 人工可核验。

**风险 b：reflections 大量是「环境状态观察」而非「可复用流程」。**

用**负向准则**（写进模板，见 §3.4 ① 文本）划清边界：只有「可复用的 how-to 流程（有具体步骤/命令/决策规则）」才草稿；一次性环境观察/项目特定事实/一次性诊断结论 → 归 `memory/memory.md` 或经验库（`propose_experience_draft`），**不是 skills**。这正是 Reflexion/LangMem 的「语义教训 vs 程序记忆」边界（§1 #9/#2）。§0 已核验：最活跃 agent 的条目里，「git 交付收尾 / token 提取 / compile 验证 / 幽灵执行判别」是**流程**（可沉淀），「沙箱 .git 空壳根因已部署 / 网络恢复≠可交付」是**一次性观察**（不可沉淀）——负向准则能正确区分。

### 3.4 最小改动清单（零运行时代码）

| # | 文件 | 改动 |
|---|---|---|
| ① | `backend/app/templates/HEARTBEAT.md` + `backend/agent_template/HEARTBEAT.md`（**保持 byte 一致**） | Phase 3 追加「草稿」步（见下文本），reply 步 3→4 |
| ② | `backend/app/scripts/migrate_legacy_heartbeat_template.py` | `LEGACY_HEARTBEAT_SHA256S` 追加 `30283e1a06bc659e737134c903990f28cbf946ef765f019c060acba93110fdd9`（旧 current），并加注释 |
| ③ | `backend/tests/test_heartbeat_template_consistency.py` | 加草稿步内容断言（含 `workspace/skill-drafts`、`## 来源`、条件义务措辞、负向准则） |
| ④ | `backend/tests/test_migrate_legacy_heartbeat_template.py` | `test_cli_defaults_to_dry_run_and_requires_apply_flag` 的 `_current_template_bytes()` SHA 更新为新模板 SHA；`test_preunification_minimal_template_hash_is_in_legacy_set` 追加断言 `30283e1a…` 在 legacy set |

**草稿步模板文本（新增，插在「Converge your exploration log」步之后、reply 步之前）**：

```markdown
3. **Draft a reusable skill — conditional, only when a workflow has proven itself.**
   A "proven workflow" is a repeatable *procedure* (concrete steps, commands, or
   decision rules) that you have executed successfully on **at least 3 separate
   occasions**, each evidenced by its own dated entry in the Insights &
   Discoveries section of `memory/reflections.md`. Only act when such a workflow
   genuinely exists; most cycles there will be none.
   - Do NOT draft one-off environment observations, project-specific facts, or
     one-time diagnostic conclusions — those are knowledge for `memory/memory.md`,
     not reusable procedure.
   - If one qualifies, write a Markdown draft to
     `workspace/skill-drafts/<skill-name>.md` (never under `skills/`) containing:
     * a `## 来源` section listing the specific dated reflections entries that
       evidence the workflow repeating ≥3 times;
     * the repeatable steps, commands, or decision rules, written so a future run
       can follow them directly.
   - Do not move, copy, or write anything into `skills/` yourself — a human
     maintainer reviews drafts and moves them into place.
   - At most one new draft per heartbeat. If none qualifies, do nothing here.
   - In your final summary, mention any draft you created this cycle and any
     drafts still awaiting human review.
```

（措辞纪律：条件义务「Only act when such a workflow genuinely exists; most cycles there will be none.」，禁祈使目标句——R1 循环教训 / D1a；草稿写 `workspace/` 由 creator 隐式维护人员放行，heartbeat `actor_user_id` 空 → 回退 creator → `GATED_ALLOWED`，不冲突。）

### 3.5 范围外（明确不做）

- **不新增 `propose_skill_draft` 工具**（G3-1 已拍板：新公共工具需真实消费者，human-gate 已够）。
- **不新增 Notification 通道**（见 §3.2 ③）。
- **不接 judge/G4 评测**、不接 `skills-benchmarks`（grill 决策 7）。
- **不碰 DB `skills`/`skill_files` registry**（G3 是文件目录技能，与全局 registry 同名不同物，§2）。
- **不自动语义聚类、不结构化 workflow 实体**（G3-2 拍板 + §3.3 风险 a 的取舍）。

---

## 4. 七角度评审裁决（Phase 4，每条含负向探针）

### Q1 根因找得是否正确？

**裁决**：通过。

**正向依据**：本方案要解的「根因」是两层：①**解挂理由**（数据前提已变）——§0 docker 只读实测，9 agent 真实 Insights、最活跃 agent 44 条含 ≥10 次重复交付流程；②**G3 缺口的本质**（无草稿通道 + 无人工移动提示 + 模板无草稿步）——§2 代码基线：HEARTBEAT 模板 Phase 3 无草稿步、全库 `skill-drafts` 零引用、`_load_skills_index` 只扫 `skills/`、门控已落地。

**负向探针（反例测试）**：「若解挂理由成立，则应有 agent 的 Insights 含 ≥3 次重复成功流程」——我读了 `950a1943` 全文，git/MR 交付收尾流程独立 dated 条目 ≥10 次，**证实**。「若缺口本质是缺草稿通道，则全库应 0 个 skill-drafts 引用」——`grep -rn "skill-drafts\|skill_drafts"` 零命中，**证实**。「若触发信号设计错了（该用机器信号），则应能证明『≥3 次』在真实数据下不可 LLM 判定」——最活跃 agent 条目自带的日期 + ✅ 标记 + 流程名，LLM 可读可计数，对立假设**不成立**。

### Q2 根治方案是否正确？

**裁决**：通过。

**正向依据**：方案改的是 Q1 定的缺口本体——加模板草稿步（补「无草稿通道」）、人工移动 + P2 列表（补「无人工移动提示」），非止痛药。

**负向探针（删除测试）**：「删掉模板草稿步，agent 能否自动沉淀技能？」**不能**（现状零通道，全库无 skill-drafts）→ 是根治。「删掉人工移动通道（让人工直接写 skills/ 或让 agent 自动入 skills/），草稿能否安全生效？」自动入 skills/ = 劣质技能污染后续所有 run（best-practices §2.5 release-engineering 护栏明确反对）→ human-gate 是根治的必要部分，非可有可无。「删掉 P2 列表，人工能否可靠发现草稿？」不能（activity summary 80 字符截断 + HEARTBEAT_OK 早退）→ P2 列表必要。

### Q3 参考的资料是否正确？

**裁决**：通过。

**正向依据**：§1 引用均为「程序记忆/技能沉淀」**同类问题**（非同名 false friend）：Voyager（技能库）、LangMem（procedural memory）、AWM（workflow 归纳）、letta-code（记忆子代理）、skills 生态（SKILL.md 格式）；本地源码库已克隆（`UGit/{langmem,mem0,letta-code,ponytail,skills,skills-benchmarks,andrej-karpathy-skills,awesome-agent-skills,hermes-agent,langchain-skills}`）。

**负向探针（找引用错点）**：「Voyager 的触发信号是否被我误述成『重复 N 次』？」核 best-practices §2.4：Voyager 是「任务成功→入技能库」，无重复门槛——我的偏离是**刻意的**（Clawith 无可靠环境验证信号），已写进 §1 偏离理由，**无误**。「letta-code 已归档是否当第一依据？」只借「记忆门禁」思路、未依赖其实现细节，**低风险**。「AWM 是否可逐字可抄？」其结构化 workflow 归纳需离线归纳 + 结构化表示，Clawith 不做（G3-2），§1 已诚实标注偏离，**无误**。

### Q4 副作用与爆炸半径是否排查完？

**裁决**：通过。

**正向依据**（两个子检查）：
- ①**副作用面**：G3 是 **prompt-only**（零运行时代码/工具/表/事件）。唯一运行时影响 = agent 可能写 `workspace/skill-drafts/*.md`（多几个文件，不触发 `_load_skills_index`，不进目录表，零污染）。模板迁移 = SHA 精确匹配（legacy 才替换、custom/current 跳过，`migrate_legacy_heartbeat_template.py::_migrate_agent`）。
- ②**影响面**（消费者逐个过）：`_load_skills_index`（不扫 `workspace/`，不受影响）✓；门控（草稿写 `workspace/` 由 creator 放行，`skills/` 写仍需维护人员）✓；`_extract_reflections_injection`（不改）✓；`test_heartbeat_template_consistency`（需加断言）✓；`test_cli_defaults_to_dry_run_and_requires_apply_flag`（SHA 需更新）✓。

**负向探针（点名具体消费者/副作用）**：「漏掉的副作用：把旧 SHA `30283e1a…` 加进 legacy set，会不会触发 migrate 的 guard `current_sha256 in legacy_set: raise`？」guard 判的是**新** current SHA，我加的是**旧** SHA，两者必不同 → 不会误触发（但方案 §3.4 已显式提醒：新模板 SHA 绝不能等于任何 legacy SHA）→ **不受影响**。「漏掉的消费者：草稿写进 `workspace/` 会不会被 run-scoped 临时工作区/同步逻辑误清？」workspace 是持久 agent 存储（`agent_manager.py` 模板目录含 `workspace/`，非 run-scoped temp），草稿跨 run 持久——但这是「人工稍后审核」的前提，列为已知风险（§6 风险 2），实现时需真跑验证 → **受控**。

### Q5 这是最优且必要的方案吗？

**裁决**：通过。

**正向依据**（两个子检查）：
- ①**候选枚举（≥3，从 Ponytail 最低档起）**：A（更简单）= 只加模板草稿步、不做 P2 列表/迁移；B（当前）= 模板草稿步 + 人工移动 + 提示 + P2 列表 + 迁移；C（更彻底）= 自动语义聚类 + `propose_skill_draft` 工具 + 机器信号 + judge 评测。选 B（最低可行档），C 被 G3-1/G3-2/G4 grill 决策否决。
- ②**修已发生 vs 臆想**：这是**能力建设**（开启此前挂起的沉淀通道），非「已发生故障」——诚实定性为「按真实数据驱动解挂的增量能力」，非 P0 已损；故形态保持最小（不因数据到位就升级为自动机制）。

**负向探针（更简单一档能否解决）**：「试 A：只加模板草稿步、不做 P2 列表，人工能否发现草稿？」不能可靠（§3.2 ③：activity summary 截断 + 无列表视图，草稿会淹没）→ P2 列表必要，A 不成立。「试 C：自动入 skills/ 不 human-gate 是否更彻底？」风险爆炸（劣质技能污染所有 run），且 G3-2 已否决聚类 → C 不必要。「试『不做模板迁移、只改模板』一档？」存量 23 agent 仍持旧模板，草稿步对新 agent 生效、存量永不生效 → 迁移必要。

### Q6 是否已经有可复用的逻辑？

**裁决**：通过。

**正向依据**：G3 **零新运行时逻辑**，全部复用既有：
- 门控 `resolve_file_modify_permission`（`skills/` 写守卫已落地）——「人工移动」零新门控代码。
- `_load_skills_index` + `_parse_skill_frontmatter`（skills 目录表 + frontmatter 解析已存在）——草稿移动后自动纳入。
- `fileApi.list_files` + `FileBrowser`（P2 列表零新组件）。
- `migrate_legacy_heartbeat_template.py`（模板迁移复用）。
- 经验库 `propose_experience_draft`（human-gate 先例，边界见 §3.3）。

**负向探针**：「知识图谱/代码里是否已有『草稿→skills/』的自动移动逻辑？」`grep -rn "skill-drafts"` 零引用 → 无，但「人工移动」本身**不需要**新代码（门控已兜底 + `_load_skills_index` 已索引），复用即满足 → **无重复造轮**。「是否有等价『草稿表』可复用（如经验库 experience_entries）？」经验库是 DB 表 + 检索注入（episodic），技能是文件目录 + 目录表注入（procedural），**不可复用同一条通道**（§2 双轨已澄清）——复用不等于混用。

### Q7 会破坏 Clawith 的特性吗？

**裁决**：通过。

**正向依据**（宪法 C1–C6 + 红线）：
- C1 证据先行：§0 docker 只读实测。
- C2 最小改动：prompt-only，零运行时代码/工具/表/事件。
- C3 契约与状态所有权：草稿所有权 = workspace（agent 自主写、creator 放行），成品所有权 = skills（维护人员写），门控画线。
- C4 测试证行为：§5（模板内容断言 + 迁移 SHA 断言）。
- C5 保留既有工作：迁移 SHA 精确匹配，custom 模板不覆盖（`skipped_custom`）。
- C6 模块化与数据边界：草稿在 agent 本地 `workspace/`，无跨租户泄漏面。

红线：前缀缓存（HEARTBEAT.md 是 agent 存储文件，不进 provider 前缀缓存；改模板不碰 tool 缓存键）✓；checkpoint（无运行时改动）✓；多租户隔离（agent 本地目录）✓；WS 状态机（无）✓；exactly-once（无外部写）✓。

**负向探针（对红线逐一过）**：「草稿步会不会污染『每 run 注入的 reflections snapshot』？」不会——草稿写 `workspace/skill-drafts/`，`_extract_reflections_injection` 只读 `memory/reflections.md` → 不碰。「模板改动会不会让存量 9 个有 Insights 的 agent 心跳行为突变？」迁移后新模板对所有 agent 生效，但草稿步是**条件义务**（无 proven workflow 则 do nothing）→ 不强迫写；最坏 = agent 写了草稿落 workspace 待人工审 → 可接受。「新草稿步会不会与 `test_heartbeat_convergence_is_unconditional_even_when_idle` 的 `HEARTBEAT_OK > converge_pos` 断言冲突？」草稿步插在 converge 之后、reply（含 HEARTBEAT_OK）之前 → 仍满足 → 不冲突。

---

## 5. 测试 / 回滚 / 影响面

### 5.1 测试（TDD）

1. `test_heartbeat_template_consistency.py` 新增断言：双模板含 `workspace/skill-drafts`、`## 来源`、条件义务措辞（`only when a workflow has proven itself` / `most cycles there will be none`）、负向准则（`Do NOT draft one-off environment observations`）、「never under `skills/`」、`At most one new draft`。
2. `test_migrate_legacy_heartbeat_template.py::test_cli_defaults_to_dry_run_and_requires_apply_flag`：`_current_template_bytes()` SHA 更新为新模板 SHA。
3. `test_preunification_minimal_template_hash_is_in_legacy_set`：追加断言 `30283e1a…` 在 `LEGACY_HEARTBEAT_SHA256S`（防止未来再改模板时漏迁移）。
4. 迁移 dry-run 生产对账：`python -m app.scripts.migrate_legacy_heartbeat_template`（只读）确认 23 存量 agent 中 legacy 匹配数 = 预期（多数应命中 `30283e1a…`），再 `--apply`。

### 5.2 回滚

- 行为级：无开关（prompt-only）。代码级 revert 双模板 + 迁移常量 + 测试；`LEGACY_HEARTBEAT_SHA256S` 回滚后，已迁移 agent 的新模板 SHA 不在 legacy set，`migrate_legacy_heartbeat_template` 不再匹配（`skipped_custom`），**回滚不自动反向迁移**（与既有迁移语义一致：迁移是单向、SHA 精确、不碰 custom）。
- 草稿文件：`workspace/skill-drafts/` 是普通文件，人工删除即可，无迁移面。

### 5.3 影响面

- 契约变更：**HEARTBEAT.md 是模型可见契约**（`_build_heartbeat_instruction` 注入），新增草稿步 = 模型行为契约变更，需 release note 明示（与 20260905 plan §7「产品契约变更提醒」同规约）。
- 消费者：`_load_skills_index` / 门控 / reflections 注入均**不受影响**（§4 Q4 已过）。

---

## 6. 已知风险 + 缓解（评审后折入）

| # | 风险 | 定性 | 缓解 |
|---|---|---|---|
| 1 | 「≥3 次」是 LLM 解释性计数，无机器信号（§3.3 风险 a） | **接受**（代价有界） | 草稿自带 `## 来源` 节人工核验；假阳/假阴均不污染 `skills/`；若后续数据显示草稿产量异常（零产或滥产），再评估轻量结构信号（P2） |
| 2 | `workspace/skill-drafts/` 跨 run 持久性未端到端实测（§4 Q4） | **低风险，实现时补验** | `{agent_id}/workspace/` 持久性已被实证（`950a1943` 的 mydome1 项目/`GITLAB_GUIDE.md`/`协作约定.md` 跨多日多 run 存活于 `workspace/`）；但内存 `workspace-sync-conflict-root-cause` 记录的「run-scoped 临时工作区」是相邻机制，实现 Phase 5 时仍真跑一条 heartbeat，确认草稿跨 run 存活 + 不进 `_load_skills_index` 目录表 |

---

## 7. 执行序 + 已拍板汇总

```
Phase 5（实现另启，本方案到此为纸面交付）：
  P0 改双模板（app/templates + agent_template，byte 一致）→ 更新 test_heartbeat_template_consistency 断言
  → 旧 SHA 30283e1a… 加入 LEGACY_HEARTBEAT_SHA256S → 更新 test_migrate_legacy 两处断言
  → 跑 migrate_legacy_heartbeat_template --apply（先 dry-run 对账）
  → 端到端验证（§6 风险 2）
  → code-review 对照本方案复核 diff（Phase 5 铁律）
```

**沿用已拍板决策（不变）**：G3-1 草稿写 `workspace/skill-drafts/` + 人工移动（09-02 §7）；G3-2 不做自动语义聚类；grill 决策 7 G4 不接 judge；grill 决策 8 的「挂起」被本方案数据驱动**翻转**（其余 grill 决策 1-6 均属门控，已 implemented，与本方案无关）。

---

## 附：双源证据索引

- 数据源：`mcp__docker__container_exec`（`clawith-agent-backend-1`）——`ls /data/agents`（23 目录）、`cat /data/agents/950a1943-…/memory/reflections.md`（44 条 `- ` 行）、`grep -c "^- "`（=44）、`grep -n "^## "`（四节定位）。
- 代码源：`read_file` 抄录——`app/templates/HEARTBEAT.md`、`agent_template/HEARTBEAT.md`、`app/services/heartbeat.py`、`app/services/agent_context.py`、`app/scripts/migrate_legacy_heartbeat_template.py`、`app/services/agent_manager.py`、`app/models/skill.py`、`app/services/agent_runtime/heartbeat_completion.py`、`app/services/experience_retrieval.py`、`tests/test_heartbeat_template_consistency.py`、`tests/test_migrate_legacy_heartbeat_template.py`。

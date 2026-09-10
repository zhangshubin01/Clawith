# 生产级修复方案：技能沉淀生命周期收口（兑现 README「Self-Evolving → create new skills」）

> 触发：README §Self-Evolving Capabilities 宣称「create new skills for themselves or colleagues」，但代码里「技能沉淀」无生命周期——授权措辞错位、维护门控只拦用户不拦 agent、agent 已野生裸写 `skills/`（生产实证）。
> 本文是 clawith-fix-plan 全流程产物（Phase 1→4）。草稿通道与触发信号的既有设计复用 `docs/technical-plans/20260907-g3-skill-sedimentation-production-plan.md`（下称「G3 方案」），本文新增「授权措辞收口 + 门控语义澄清 + 野生沉淀实证 + 历史遗留处置」并修订根因。

## 结论先行

**裁决：有条件通过（2 项已知风险均带缓解，见 §6）。**

- 根因不是「G3 该不该解挂」，而是更基础的一层：**`skills/` 目录缺一个显式的「技能生命周期」（触发 → 草稿 → 审核 → 启用）**。证据是双源的——代码层面 `write_file` 授权「update skills in skills/」与维护门控（只拦「非维护人员用户」、不拦「agent 自主写」）语义错位；数据层面 agent「Android工程师」（`62bc9c81`）已在 2026-08 初**野生裸写 31 个 skill 目录**（自写 + 外部导入 + seed 大杂烩），门控 09-07 上线后授权措辞未收口。
- 最小改动 = **零运行时代码**：①收口 `write_file`/`edit_file` 工具描述的 skills/ 授权措辞；②草稿通道（G3 已设计）；③人工移动门控（复用已上线门控）；④HEARTBEAT 条件义务触发（G3 已设计）；⑤历史遗留 31 个野生 skill 不删、标记 `legacy 未审核`（P2 观察项）。
- 定性诚实：①③是「修已发生的错位/已发生的无规范」；④是「兑现 README 承诺」；⑤是「P2 观察项」，不是 P0 已损。

---

## 0. 接地数据（2026-09-07 实测，非凭旧报告）

**代码源（f-shubin 分支 `ab3e4b21`，read_file 核实）：**

| 出处 | 事实 |
|---|---|
| `builtin_tool_definitions.py:160` | `write_file` 描述：「…Can update memory/memory.md, create documents in workspace/, and **update skills in skills/ when the active workflow requires repair**.」 |
| `builtin_tool_definitions.py:232` | `edit_file` 参数示例：「…memory/memory.md, workspace/reports/report.md, **or skills/my-skill/SKILL.md**」 |
| 工具集 | **无独立 `create_skill`/`update_skill` 工具**——写 skill 的唯一通道就是 `write_file`/`edit_file` 直写 `skills/` |
| `agent_context.py:70 _load_skills_index` | 每次构建上下文扫 `{agent_id}/skills/*/SKILL.md`（或 `skill.md`）编目录表——写了即生效 |
| `maintainer_service.py:165 resolve_file_modify_permission` | 门控裁决序：`actor_agent_id` 非空→NOT_GATED；`actor_user_id` 空→**回退 `agent.creator_id`**→GATED_ALLOWED（creator 是隐式 maintainer）。即 **agent 自主写（heartbeat/trigger 场景 actor 为空）恒放行** |
| `main` 分支（`45fc701c`，08-25） | **无 `maintainer_service.py`**（门控是 08-25 之后 f-shubin 加的，`aa539999` 随 `0b95c42b` 上线），main 上 agent 直写 skills/ 完全裸奔 |

**数据源（PG 台账 + docker 卷，只读）：**

- `agent_tool_executions` 台账：`write_file`/`edit_file`/`move_file`/`delete_file` 且入参含 `skills/` 的共 **18 次**，主力 **agent `62bc9c81`（name=「Android工程师」）write 9 + edit 3 + delete 1**，集中在 **2026-08-03 ~ 08-05**（门控 09-07 上线之前）。
- docker 卷 `/data/agents/62bc9c81…/skills/` 现为 **31 个目录**，三类混杂：seed 默认（complex-task-executor / mcp-installer / skill-creator / vercel-full-stack-deploy…）+ **agent 自写**（`android-workflow`（5160 字符、含完整 frontmatter/references/evals）、`subagent-driven-development`、`dispatching-parallel-agents`、`SUPERPOWERS_SETUP.md`）+ **外部导入**（`pharaoh`（version 0.3.5 / homepage pharaoh.so / openclaw metadata）、`superpowers`、`superpowers-openclaw`、批量 `android-*` / `compose-*`）。
- 对照组：其余 agent 的 skills/ 多为 5–8 个 seed 目录；仅 `62bc9c81` 出现「自写 + 导入」大杂烩。

**这段数据推翻两个旧假设：**
1. README 的「create new skills」**不是没实现、而是被 agent 自发实现了**——只是这个自发实现「无门控、无审核、无触发规范」，是野生行为。
2. G3 方案「解挂决策」的论据从「Insights 数据量变了」升级为「**agent 已经在野生写 skill，缺的是收编**」——证据更强。

---

## 1. 根因（双源钉死）

**一句话：`skills/` 目录缺一个显式的「技能生命周期」状态机。**

它被同时当作两种东西，却没有衔接两者的机制：

- **系统配置面**：维护门控 `_classify_path`（`maintainer_service.py:58`）把 `skills/` 与 `workspace/` 一起归入 gated bucket，语义是「skills 是受控配置」。
- **agent 可写工作面**：`write_file`/`edit_file` 工具描述明确授权 agent 直写 `skills/`（「update skills in skills/」「skills/my-skill/SKILL.md」）。

两者之间缺的是「**何时该沉淀**（触发信号）+ **沉淀什么质量**（草稿→审核）+ **谁批准生效**（人工移动）」这三段。后果是三条可验证症状：

1. **门控上线前（8 月初）**：无任何门控，agent「Android工程师」野生直写 31 个 skill 目录——自写、外部导入、seed 全混在一个目录，被 `_load_skills_index` 扫进上下文。
2. **门控上线后（09-07）**：门控只拦「非维护人员**用户**」（`actor_user_id` 非 creator/maintainer → DENIED），**不拦 agent 自主写**（heartbeat/trigger 场景 `actor_user_id` 空 → 回退 creator → ALLOWED）。同时 `write_file` 描述仍写着「update skills in skills/」——授权措辞没随门控落地而收口，语义错位仍在。
3. **始终**：无触发信号，沉淀是「偶发野生」而非「可预期有质量」——README 的「自进化」既没被规范、也没被兑现。

**根因可证伪**：若「缺生命周期」是根因，则「补上 草稿通道 + 人工移动 + 触发信号 + 收口授权措辞」后，症状 1/2/3 应同时消失（agent 不再直写 skills/、沉淀走草稿受审核、触发可预期）；若只是「门控没拦 agent」，则只改门控应消除症状 2，但症状 1（历史 31 skill）与 3（无触发）仍在——故「缺生命周期」比「门控漏拦」更深一层。

---

## 2. 参考资料对比（Phase 1，≥10 项目，含诚实负结论）

沉淀机制维度的 12 项目对比已在 G3 方案 §1 完整做过（Voyager / LangMem / Anthropic Agent Skills·skill-creator / letta-code memory-v2 / Mem0 / andrej-karpathy-skills / awesome-agent-skills / AWM / Reflexion·Self-Refine / 12-factor-agents / skills-benchmarks / Anthropic Effective context engineering），结论不变：**无任何项目提供「重复≥3次→草稿→人工移动」逐字可抄机制**，是 Clawith 按自身约束拼装。本文只补「授权措辞 / 门控 / 创作态生命周期」几个维度的关键对照（这一轮新发现的问题域）：

> 本节 8 条结论均为**读真实源码 / 抓官方文档逐条核对**（本地路径 + 出处标注），非 README 摘要、非凭记忆。沉淀机制维度的 12 项目对比已在 G3 方案 §1 做过，结论不变：**无任何项目提供「重复≥3次→草稿→人工移动」逐字可抄机制**。本节补「授权措辞 / 门控 / 创作态生命周期」几个维度（本轮新发现的问题域）。

| 项目 | skill 目录的「写权限」怎么管 | 出处（已核对） | 对本方案的启示 |
|---|---|---|---|
| **Anthropic Agent Skills**（skill-creator） | skill 存**用户仓库**，agent 只读加载；创建靠开发者用 skill-creator 人工写 + 跑 eval（`run_eval.py` 等），**agent 不运行时写** | Clawith `backend/app/services/skill_creator_content.py` 头部注释明示「Based on anthropics/skills/tree/main/skills/skill-creator」，含 `scripts/run_eval.py`/`package_skill.py` 全套人工+评估通道 | 印证「skill 目录 = 配置面（只读），创建 = 人工 + 评估」是行业默认 |
| **Claude Code** | skill 是**用户创建的文件**（"Create a SKILL.md file with instructions, and Claude adds it to its toolkit" / "Create a skill when *you* keep pasting the same instructions"），**agent 不运行时自建**；其官方文档的 "Skill content lifecycle" 指 **context 生命周期**（渲染内容进出对话、compaction 重挂载、token 预算），**不是** skill 创作的 draft→review→publish 状态机 | 抓取 `https://code.claude.com/docs/en/skills`（curl，2026-09-07） | 连最成熟的 skill 系统也只管「加载/上下文」生命周期、**不管「创作」生命周期**（创作靠人工）——但 Claude Code 的「不管创作态」不代表全行业空白：OpenViking 有显式五态生命周期（见下） |
| **Voyager**（arXiv 2305.16291） | 技能库 agent 可写，但**单次成功即入库** + 用**环境验证信号**（任务是否可完成）兜底质量，**无重复 N 次门槛** | G3 方案 §1 已核对（best-practices §2.4 综述） | 『agent 写库』的例外**之一**（OpenViking 也是，见下），且靠环境信号兜底——Clawith 无此信号（`execute_code` 沙箱不稳定），故偏离用「≥3 次 + human-gate」（G3 已论证） |
| **LangMem**（procedural memory） | procedural memory 是**记忆内容的分类标签**（`_MEMORY_INSTRUCTIONS` 里 "semantic, procedural, and episodic memory" 三分类），记忆的 insert/update/delete 由 `MemoryManager`（LLM 提取器）管理，**agent 不直写文件**；不是「技能文件」的生命周期 | 本地 `UGit/langmem/src/langmem/knowledge/extraction.py:185`（`_MEMORY_INSTRUCTIONS`）+ `:86`（`Memory` schema 仅 `content` 字段）+ `:217`（`MemoryManager`） | 印证「沉淀应走框架管理的生命周期（增删改由框架仲裁）」而非「agent 直写目录」；修正：其「生命周期」是记忆增删改，非技能文件 |
| **Mem0** | **修正原「无 procedural skill」**：mem0 确有 `skills/` 目录，但全是 **vendor 发布的「如何使用 Mem0」手册**（reference 常驻 + pipeline 按需执行两类），经 `npx skills add` / `marketplace.json`（installation=AVAILABLE）安装，遵循 Anthropic skills 标准；**无 agent 运行时自主沉淀** | 本地 `UGit/mem0/skills/README.md`（两分类表）+ `UGit/mem0/marketplace.json`（plugin 安装元数据） | 恰恰印证「技能 = 供应商发布 + marketplace 安装」是行业默认（与 Clawith 的 ClawHub 导入同构），**没有**任何项目做 agent 自主沉淀 |
| **OpenClaw / pharaoh** | 第三方 skill 走 **marketplace 安装**（version/homepage/权限 metadata），与 agent 自写是两条独立通道 | 容器 `/data/agents/62bc9c81…/skills/pharaoh/` 实证（version 0.3.5 / homepage pharaoh.so / openclaw permissions metadata） | 印证「导入」与「自沉淀」应分道——Clawith 的 ClawHub 导入已在 DB registry 轨，文件目录轨应专用于「沉淀」 |
| **12-factor-agents** | 方法论文档（`content/` 全 markdown），**无技能生命周期代码机制**；但 factor-11 "Trigger from anywhere" 明确「Outer Loop Agents: triggered by non-humans, e.g. events, crons, outages」 | 本地 `UGit/12-factor-agents/content/factor-11-trigger-from-anywhere.md` | 修正原「无贡献」：贡献「非人类触发（事件/cron）」原则，直接支持 HEARTBEAT 条件义务触发设计 |
| **OpenViking**（session_skills） | skill 目录 **agent 运行时自进化可写**——session commit → case-driven training → session_skills，`SkillPolicyUpdater` 写 `SKILL.md`；**有显式五态生命周期** `PolicyStatus = draft/staging/production/deprecated/archived`，新生成默认 `draft`；但 **draft→production 无自动 promote、无显式 review API**（读侧对无 status 字段的 SKILL.md 兜底按 production） | 本地 `UGit/OpenViking/session/train/domain.py:23` + `components/skill_policy_updater.py:175` + `components/policy_updater.py:203` + `components/memory_store.py:92-96,172`（2026-09-07 深读） | **推翻「技能创作态生命周期是行业空白」**——OpenViking 是已知少见的显式实现（五态 + 新生成默认 draft）；但其「人工门控升级」半落地（无 promote API），Clawith 把「人工移动门控」做显式恰是补它缺的那半环 |

**诚实负结论（重申并修正）**：①没有任何一个参考项目把「agent 运行时直接写 skill 目录」当作默认设计——要么 agent 只读（Anthropic/Claude Code），要么有环境验证信号兜底（Voyager），要么由框架管理记忆增删改（LangMem，且那是记忆不是技能）。②skill **创作态**生命周期状态机**并非全行业空白**——之前的「没有」被 OpenViking 推翻：OpenViking 有显式五态 `PolicyStatus`（draft/staging/production/deprecated/archived，`domain.py:23`）+ 新生成默认 draft（`skill_policy_updater.py:175`）。但它**无自动 promote、无显式 review API**，draft→production 的人工门控升级是**隐式半落地**（读侧对无 status 字段的文件兜底按 production，`memory_store.py:92-96`）。所以更准确的结论：『显式五态 + 新生成默认 draft』已有先例可对标（OpenViking），而『draft→production 的人工门控升级』这一环行业里也是半落地——Clawith 方案把「人工移动门控」做显式，恰是补上那半环。方案仍按 Clawith 自身约束（门控已落地、经验库已有 `propose_experience_draft` human-gate 先例）拼装，但对标源从「无」修正为「OpenViking 半落地的五态」。

---

## 3. 方案（最小改动）

### 3.1 改动清单（零运行时代码）

| # | 改动 | 文件 | 性质 |
|---|---|---|---|
| **0** | **收口 skills/ 授权措辞**：`write_file` 描述删「update skills in skills/ when…repair」，改为「`skills/` 由维护人员管理，**只读**；沉淀可复用流程请写 `workspace/skill-drafts/<name>.md` 草稿（自带 `## 来源`），由维护人员审核后移入 `skills/`」；`edit_file` 示例删「skills/my-skill/SKILL.md」 | `builtin_tool_definitions.py:160,232` | 修已发生的错位 |
| **1** | **草稿通道**（G3 §3.4 已设计，复用）：草稿落 `workspace/skill-drafts/<name>.md`，不落 `skills/`（避开 `_load_skills_index` 目录表） | 提示词层，无代码 | 修已发生的无规范 |
| **2** | **人工移动门控**（复用已上线 `maintainer_service`）：维护人员把草稿移入 `skills/<name>/SKILL.md`，门控已兜底 | 零新代码 | 复用 |
| **3** | **触发信号**（G3 §3.4 已设计，复用）：双 HEARTBEAT 模板加「Draft a reusable skill」条件义务步（「≥3 次重复流程 → 写一份草稿」）+ `## 来源` + 负向准则 + never under skills/ + at most one draft | 双 `HEARTBEAT.md` + `migrate_legacy_heartbeat_template.py` + 2 处测试 | 兑现承诺 |
| **4** | **P2 待审列表**（G3 §3.4 已设计，复用）：复用 `fileApi.list_files` + `FileBrowser.tsx` | 前端复用 | 兑现承诺 |
| **5** | **历史遗留处置**：`62bc9c81` 的 31 个野生 skill **不删除**（可能有用，删除=破坏性），仅在方案交付后由维护人员人工标记/归档为 `legacy 未审核`，后续逐步走草稿通道复审 | 无代码，运维动作 | P2 观察项 |

### 3.2 关键设计决策

**D1「授权措辞收口」是第 0 项、最基础**：不改它，即使补了草稿通道 + 触发信号，`write_file` 描述仍在引导 agent 直写 `skills/`（「update skills in skills/」），与草稿通道互相矛盾。收口后，agent 的「写 skill」动作被**唯一**导向草稿路径，与门控语义（skills 受控）对齐。

**D2 门控语义澄清（不动代码）**：维护门控的职责是「**谁（用户）能驱动写**」，草稿通道的职责是「**沉淀质量**」——两者正交，不需要让门控去拦「agent 自主写」（那会把「自进化」的通道也堵死）。正确的收口是：agent 自主写 → 引导到草稿（非 skills/）→ 人工移动（维护人员）→ 启用。门控继续管「非维护人员用户借 agent 之手改 skills/」。

**D3 触发信号「≥3 次」= LLM 解释性计数，接受而非消除**（G3 §3.3 已裁决，沿用）：与 G1「20K 阈值永不触」的本质区别是——G1 是机器阈值门控自动动作（永不触发=白建），G3 是 LLM 判断门控可选草稿 + human-gate（假阳=一个 workspace 文件被拒、假阴=错过机会，均不污染 skills/）。

**D4 历史遗留不删**：31 个野生 skill 里 `android-workflow` 等质量不差（有完整 frontmatter/references/evals），删掉是破坏性且可能损失有价值知识。定性 P2 观察项，由维护人员逐步复审，不走自动脚本。

### 3.3 范围外（明确不做）

- 不做「自动语义聚类」（等价 G3-2 已否决：无环境验证信号，自动聚类=污染 skills/ 的自动通道）。
- 不做 DB registry 轨（`skills`/`skill_files`）的任何改动——G3 是**文件目录技能**，不碰 ClawHub 导入轨。
- 不动 `maintainer_service.py` 的门控裁决逻辑（语义已正确，只澄清职责）。
- 不做「机器触发信号」（如写日志计数 ≥3 自动触发）——与 D3 一致，触发交给 LLM 判断 + human-gate。
- 不删除、不迁移 `62bc9c81` 现有 31 个 skill（见 D4）。

---

## 4. 七角度评审裁决（Phase 4，每条含负向探针）

> 2026-09-07 复核：OpenViking 五态 + 多租户发现（§2 新增第 8 条）后，按 clawith-fix-plan Phase 4 重过 7 问——Q2/Q5/Q6 补 OpenViking 对标论证（Q5 新增候选 d「status 五态字段」并否决），其余维持通过。

### Q1 根因找得是否正确？——通过
- **正向依据**：根因「缺生命周期」能解释双源全部证据——①PG 台账 18 次 skills/ 写（8 月初野生写）②docker 卷 31 目录大杂烩 ③write_file 授权措辞仍在 ④门控只拦用户不拦 agent。且已追一层（「门控漏拦」是上一层症状，「无生命周期」才同时解释「历史野生写 + 授权错位 + 无触发」三症状）。
- **负向探针（反例测试）**：「若『缺生命周期』是根因，则只补『门控拦 agent』应仍留下症状 1（历史 31 skill 无人管）与 3（无触发）」。我对照了：`maintainer_service.py:197` 的 `actor == agent.creator_id → GATED_ALLOWED` 若改成「agent 自主写一律 DENIED」，确实只消除「未来 agent 直写」，**解释不了 8 月初已发生的 31 个 skill、也解释不了『何时该沉淀』**——故「缺生命周期」是比「门控漏拦」更深的根因，证实。

### Q2 根治方案是否正确？——通过
- **正向依据**：方案改的是根因本身——补「触发→草稿→审核→启用」四段 + 收口授权措辞，而不是「给门控加个 agent 判拒」的止痛药。且 OpenViking 的「draft→production 人工门控升级半落地」（§2）反向印证：把「人工移动门控」做**显式**正是行业缺的那一环，Clawith 方案补对了位置。
- **负向探针（删除测试）**：「把方案删掉，问『野生裸写/授权错位/无触发』会不会复发」——**会**：`write_file:160` 的「update skills in skills/」仍在，agent 下次仍会被引导直写；8 月初的野生写就是「无方案」时的实际后果（已实证复发过）。→ 方案是根治，非止痛。

### Q3 参考的资料是否正确？——通过（本轮逐条核对后修订）
- **正向依据**：8 条结论均读真实来源——Anthropic（`skill_creator_content.py` 移植源注释）、Claude Code（curl 抓官方 docs 原文）、Voyager（G3 best-practices §2.4）、LangMem（`extraction.py:185/86/217`）、Mem0（`skills/README.md` + `marketplace.json`）、OpenClaw/pharaoh（容器 metadata）、12-factor-agents（`factor-11` 原文）、OpenViking（`domain.py:23` + `skill_policy_updater.py:175` 五态），**非 README 摘要、非凭记忆**。
- **负向探针（本轮实做，三处纠正）**：「我最初凭记忆写了『Claude Code 用 `~/.claude/skills`』、『LangMem 提取→验证→激活生命周期』、『Mem0 无 procedural skill』；后来又凭记忆把『技能创作态生命周期』断言为『行业空白』」。逐条核对后纠正：①Claude Code 是用户创建文件 + context 生命周期（非创作态），已按 docs 原文改；②LangMem 的 procedural 是**记忆分类标签**（三分类），非技能文件生命周期，已改表述；③Mem0 **有** `skills/` 目录（vendor 发布的使用手册，marketplace 安装），但**无 agent 自主沉淀**，已把「无 procedural skill」改为「有目录但全是供应商手册」——这反而**更强**地印证「技能 = 供应商发布 + marketplace 安装」是行业默认；④OpenViking 有显式五态 `PolicyStatus`（`domain.py:23`）+ 新生成默认 draft，但无 promote API（人工门控半落地）——已把「技能创作态生命周期是行业空白」改为「OpenViking 是已知少见的显式实现，但其人工门控升级半落地」。无误后通过。

### Q4 副作用与爆炸半径是否排查完？——通过
- **正向依据**：①副作用面——改动全是提示词/文案层，无 exactly-once 外部写、无缓存、无连接/资源、无权限边界变更（门控不动）；②影响面——`write_file`/`edit_file` 描述改动只影响「模型看到的工具文案」，消费者是**模型**（非代码契约）；`_load_skills_index` 不扫 `workspace/skill-drafts/`（已核实只扫 `skills/`），草稿不进目录表。
- **负向探针**：「我特意找过方案会漏掉的一个消费者——`write_file` 描述的『skills/ 只读』会不会误伤『维护人员通过 API 移动 skill』这条已有路径？」核下来：维护人员移入 skills/ 走的是 `maintainer_service` 门控的 GATED_ALLOWED 分支 + `write_workspace_file`，**不受工具描述文案影响**（描述只约束模型行为，不约束 API）——不受影响。

### Q5 这是最优且必要的方案吗？——通过
- **正向依据**：①枚举过 ≥4 候选——a) 更简单「只改 write_file 描述收口」（能消除授权错位，但历史 31 skill 无人管、无触发，README 承诺仍落空）；b) 本方案「收口 + 草稿通道 + 触发 + 遗留处置」；c) 更彻底「引入机器触发信号 + 自动聚类 + DB registry 生命周期」（G3-2 已否决 + 宪法 II 投机式加固）；d) 对标 OpenViking「给 skill 文件加 status 五态 frontmatter 字段 + 读侧按 status 过滤」——否决，因 Clawith 的 `_load_skills_index` 只扫 `skills/` 目录，**目录位置已天然表达『草稿（skill-drafts/）vs 生效（skills/）』两态**，再加 status 字段是两套生命周期机制打架（冗余）；且 OpenViking 需要 status 字段是因为它所有 skill 同目录、只能靠字段区分，Clawith 无此约束；五态里的 staging/deprecated/archived 对 Clawith 两态需求是过度（宪法 II）。从 Ponytail 最低档起挑，选中 b；②「已发生 vs 臆想」诚实定性——①③是已发生（18 次写 + 31 目录实证），④是兑现承诺，⑤是 P2 观察项。
- **负向探针**：「我试过更简单的一档 a（只收口描述、不做草稿/触发）能否解决问题，结论：**不能**——它能消除『未来 agent 直写 skills/ 的误导』，但历史 31 个野生 skill 仍在被加载、且没有任何『何时沉淀』的机制，README 承诺仍落空；b 相对 a 多出的草稿/触发是 G3 已评审的最小增补，非多余。」

### Q6 是否已经有可复用的逻辑？——通过
- **正向依据**：草稿通道复用「经验库 human-gate 先例」`propose_experience_draft` + `experience_entries`（draft→published→retired）；人工移动复用已上线 `maintainer_service`；P2 列表复用 `FileBrowser.tsx` + `fileApi.list_files`；触发复用 G3 已设计的 HEARTBEAT 条件义务步。零新逻辑。且 Clawith 经验库 `experience_entries` 已是**自家三态生命周期**（draft→published→retired），比 OpenViking 的五态更贴合 Clawith 两态需求——复用自家先例，而非引入外部五态字段。
- **负向探针**：「我特意查过知识图谱/代码是否已有『skill 草稿』等价实现，结论：无——skill 只有直写（write_file）与导入（ClawHub/URL）两条，无 draft 态；但『草稿→审核』的等价模式已存在（经验库 experience_entries），故复用模式而非重复造。」

### Q7 会破坏 Clawith 的特性吗？——通过
- **正向依据**：逐条过宪法 C1–C6 + 工作区红线（durable run/checkpoint、多租户隔离、exactly-once、前缀缓存、WS 状态机、飞书通道）：改动全是提示词/文案 + 运维动作，不碰 checkpoint 语义、不碰 DB、不碰工具 schema 结构（只改 description 字符串）、不碰前缀缓存前缀、不碰多租户（每 agent 独立 skills/）。门控不动、`_load_skills_index` 不动。
- **负向探针**：「我试过把方案对每个红线过一遍——唯一可能擦边的是『工具 description 改动会否影响 DeepSeek 前缀缓存稳定性』。核下来：description 属 tools schema 文本，改动会改变该 tool 的序列化字节、导致**下一次该 tool 的缓存 miss**（一次性、非持续），这是任何 schema 文案改动的固有代价，不违反『前缀缓存稳定性』红线（该红线针对的是每步历史前缀，工具定义在 system 段，改一次 miss 一次后重新命中）。不碰。」

---

## 5. 测试 / 回滚 / 影响面

### 5.1 测试
- **改动 0（描述收口）**：`builtin_tool_definitions.py` 是纯数据模块，改 description 无逻辑测试；补 1 条断言——`write_file` 描述**含**「skill-drafts」且**不含**「update skills in skills/」（防止回退）。落点 `tests/`（与 G3 的 `test_heartbeat_template_consistency.py` 同套）。
- **改动 3（触发信号）**：G3 §5 已定——`test_heartbeat_template_consistency.py` 加草稿步断言 + `test_migrate_legacy_heartbeat_template.py` 更新钉住 SHA。沿用。

### 5.2 回滚
- 改动 0/3 均为文案/模板改动，回滚 = `git revert` 对应 commit；无迁移、无数据回滚。
- 历史遗留（改动 5）无代码，无回滚面。

### 5.3 影响面
- 改动 0 影响「模型看到的 write_file/edit_file 工具文案」→ 影响 agent 后续写 skill 的**行为**（导向草稿）。消费者=模型，非代码契约。
- 改动 3 影响 HEARTBEAT 提示词 → 影响 agent 在 heartbeat 时的可选草稿行为。已按「测试环境不灰度」红线（memory `clawith-workspace-facts`）一步全量。
- 爆炸半径：无 DB、无迁移、无工具 schema 结构变化（仅 description 文本）、无 checkpoint 变化。

---

## 6. 已知风险 + 缓解（评审后折入）

| # | 风险 | 定性 | 缓解 |
|---|---|---|---|
| R1 | 「≥3 次」触发 = LLM 解释性计数，无机器信号，可能不触发或误触发 | 接受（G3 已裁决） | 假阳=一个 workspace 草稿被维护人员拒；假阴=错过一次沉淀机会；两者均不污染 `skills/`。`## 来源` 节供人工核验触发依据 |
| R2 | `workspace/skill-drafts/` 跨 run 持久性 | 低（已实证） | G3 已实证 `{agent_id}/workspace/` 跨多日存活（mydome1 项目）；Phase 5 端到端补验草稿跨 run 存活 + 不进 `_load_skills_index` 目录表 |
| R3 | 历史 31 野生 skill 继续被加载，未及时复审 | P2 观察项 | 不自动删除；交付后维护人员逐步人工复审；不影响本方案核心（收口 + 通道）生效 |

---

## 7. 执行序 + 已拍板汇总

### Phase 5 实现序（另启会话）

1. 收口 `write_file`/`edit_file` 描述（改动 0）+ 补断言。
2. 改双 HEARTBEAT 模板（byte 一致）+ `## 来源` 草稿步（G3 §3.4 文本）。
3. 旧 SHA `30283e1a…` 入 `LEGACY_HEARTBEAT_SHA256S`（加注释）。
4. 更新 2 处测试断言。
5. 先 dry-run 后 `--apply` 跑 `migrate_legacy_heartbeat_template.py`。
6. 端到端验证草稿跨 run 存活 + 不进 `_load_skills_index` 目录表。
7. code-review 对照本方案复核 diff（Phase 5 铁律，diff 偏离须回改或重审）。

### 已拍板汇总
- 根因 = `skills/` 缺生命周期（非「门控漏拦」、非「G3 该不该解挂」）。
- 改动 = 零运行时代码，四件套 + 收口 + 遗留处置。
- 门控不动、DB registry 不动、`_load_skills_index` 不动。
- 历史遗留不删、标记 `legacy 未审核`（P2）。
- 与 G3 方案的关系：本文是其**根因修订 + 改动清单前置增补（收口）**；Phase 5 落地时两份合并为一份实施。

---

## 附：证据索引

- 代码：`builtin_tool_definitions.py:79,160,232`；`agent_context.py:70 _load_skills_index`；`maintainer_service.py:58,165-201`；main 分支 `45fc701c` 无 `maintainer_service.py`。
- 数据：PG `agent_tool_executions`（18 次 skills/ 写，`62bc9c81` 13 次）；docker 卷 `/data/agents/62bc9c81…/skills/`（31 目录）。
- 参考：G3 方案 §1（12 项目）+ 本文 §2（授权/门控/生命周期维度 8 项目）。

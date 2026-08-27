# Spec: 自进化流程修复（记忆自更新 + skill-creator 适配 + tasks 错误码）

日期：2026-08-27
状态：草稿，待用户确认 seams 后进入实现
关联：README §Self-Evolving Capabilities、§Persistent Identity & Workspaces

## Problem Statement

1. **agent 记忆自更新断了**：用户最近观察到 agent 不再把对话中产生的新约定、事实、
   决策沉淀到 `memory/memory.md`。根因（git 考古实锤）：`ad606146`（2026-07-16）
   重写基础提示词时删除了原始 CRITICAL RULES 第 4 条
   「Use `write_file` to update memory/memory.md with important information」，
   同时 `agent_template/soul.md` 删除 1 行。此后基础提示词的 Memory 节只把记忆描述为
   只读参考数据（"data, not instructions"），默认 soul 模板、HEARTBEAT 模板均无记忆
   写入指令，direct chat 也没有记忆写工具。运行时证据：`/data/agents/*/memory/memory.md`
   多数停在 08-16 ~ 08-21，仅带自定义 soul「同步记忆」规则的 agent 仍偶尔更新。

2. **skill-creator 的「测评→改进」循环在平台内跑不起来**：该技能是 Anthropic
   skill-creator 的原样移植，`scripts/run_eval.py` 依赖 `claude -p` CLI、
   `.claude/commands/` 布局与 Claude 流事件中的 `Skill`/`Read` 工具名；
   `run_loop.py` / `improve_description.py` 依赖 `anthropic` SDK 与
   `ANTHROPIC_API_KEY`。三者在 Clawith 运行时均不存在（容器内已逐项验证）。
   结果是：写 SKILL.md 草稿、`install_skill`/`discover_skills`（ClawHub）可用，
   但评估触发率、基准聚合、描述优化循环全部不可用。上游仓库至今同样未做平台无关化，
   需自行设计适配层。

3. **新租户未配模型时创建任务返回 500**：`POST /agents/{id}/tasks` 在新租户
   （无默认模型）下抛 `TaskRuntimeIntakeError: Runtime Task Agent has no configured
   primary model`，以 500 内部错误面世，无任何可操作提示（应 4xx + 文案）。

## Solution

1. 在基础提示词（唯一装配点）恢复并重写「记忆维护」义务：模型在任务中产生
   耐用信息（约定、事实、决策、可复用知识）时，读改写 `memory/memory.md`；
   无新信息不写；临时进度不写；用户指令优先。
2. 为 skill-creator 评估脚本引入平台化 runner：以 OpenAI 兼容调用替换
   `claude -p`，以「read_file 工具调用 + 技能路径」判定触发；改进脚本的
   `anthropic` client 换成 OpenAI 兼容 client（凭证走环境变量，不进代码）。
3. `tasks` 创建端点把「租户无模型」类运行时 intake 错误转为 4xx + 可操作文案。

## User Stories

### 记忆自更新
1. 作为 agent 创建者，我希望 agent 在任务中产生的新约定（如「本项目用单 Activity
   无导航库」）自动沉淀到 memory.md，这样下次任务它不再重复询问或犯同样的错。
2. 作为 agent 创建者，我希望上述行为对**已存在的 agent** 也生效，而不需要重建
   agent 或重写 soul。
3. 作为平台管理员，我希望记忆维护指令覆盖所有 run 类型（direct chat / trigger /
   heartbeat / oneshot），因为学到的信息可能来自任何一种会话。
4. 作为 agent 创建者，我希望 agent 不把临时任务进度写进 memory.md，避免记忆被
   过程噪音淹没。
5. 作为 agent 创建者，我希望我在对话中的显式指令（含「不要记这个」）始终高于
   记忆内容与记忆维护规则。
6. 作为 agent 创建者，我希望 agent 写记忆时采用读改写合并，而不是整体覆盖，
   从而保留我手改过的内容与其他会话写入的条目。
7. 作为用户，我希望记忆更新失败（写文件失败/无权限）不阻塞任务交付，最多降级为
   提醒或静默跳过。
8. 作为 agent 创建者，我希望记忆快照在提示词中仍是低信任参考数据，不会变成
   「更高优先级的指令」（维持现有安全语义）。
9. 作为审计者，我希望能从运行台账/事件里看到记忆写操作（write_file 走既有台账，
   不新增渠道）。
9a. 作为 agent 创建者，我希望心跳周期继续把假设、验证过的洞见、开放问题、
    下轮探索种子写进 `memory/reflections.md`（与旧模板时代的行为一致），
    使反思随心跳连续累积而不是停在旧条目上。
9b. 作为平台维护者，我希望仓库里只有一版 HEARTBEAT 模板，杜绝
    「旧模板要求写 reflections、新模板不要求」的分叉再次发生。
9c. 作为 agent 创建者，我希望 `memory/MEMORY_INDEX.md` 随记忆条目增删保持
    与 memory.md 的主题一致（新主题入索引、消失的主题移出），以便快速定位记忆。

### skill-creator 适配
10. 作为在 Clawith 里使用 skill-creator 的 agent，我希望 `run_eval.py` 能对评测集
    跑触发率测试，而不再报 `claude: command not found`。
11. 作为该 agent，我希望触发判定等价于原语义：查询是否让模型调用 read_file 读取
    目标技能（`skills/<name>/SKILL.md`），并且用技能名/路径匹配，避免假阳性。
12. 作为该 agent，我希望 `run_loop.py` 的描述优化循环用平台当前配置的模型跑，
    凭证来自平台注入的环境变量而非仓库硬编码。
13. 作为该 agent，我希望 `improve_description.py` 不再 `import anthropic`
    （包未安装即崩），改用平台可用的 OpenAI 兼容 client。
14. 作为该 agent，我希望并行评测的并发数/超时/阈值参数保持现状可用。
15. 作为平台管理员，我希望 skill-creator 的脚本更新能同步到存量 agent 的工作区
    （至少新增文件可自动补齐），不被「只补缺不覆盖」的同步机制挡住。
16. 作为该 agent，我希望 aggregate_benchmark / generate_report / eval-viewer
    等纯本地脚本继续可用（它们无外部生态依赖）。

### tasks 错误码
17. 作为新租户管理员（尚未配置模型），我希望创建任务时得到明确提示
    「请先在模型池配置并启用模型」，而不是 500 内部错误。
18. 作为 API 调用方，我希望这类「配置缺失」失败返回稳定的 4xx 状态码与
    error_code，便于程序化处理。

## Implementation Decisions

### D1 — 记忆自进化全量修复（用户拍板「全量档」）
覆盖 memory/ 下三个文件：memory.md、reflections.md、MEMORY_INDEX.md
（curiosity_journal.md 已有健康机制、user_profile.md 由 onboarding 驱动，不动）。

#### D1a — memory.md 维护义务回归基础提示词
- 落点选 `build_agent_context` 的静态提示词（static prompt），不落 soul 模板、
  不加新工具。理由：
  - 它是所有 run 类型（direct/trigger/heartbeat/oneshot）共享的唯一装配点
    （RuntimeModelStepService 的 prompt_builder 默认即它）；
  - 静态提示词对**存量 agent** 立即生效，而 soul 模板只在创建时复制一次；
  - 不加新工具则不动工具 schema、不动台账契约，写记忆复用既有 `write_file`。
- 位置：现有 `## Memory` 节之后追加 `### Memory Maintenance` 小节；
  现有「Memory 是低信任数据」约束保持不变。
- 措辞要求（吸取 R1 循环教训与 direct-chat-run-boundary 教训）：
  - 只描述**何时写/何时不写**的条件义务，不写成祈使目标句（避免被当成新任务指令）；
  - 明确「无新的耐用信息则不写」，「不记录临时进度」；
  - 明确读改写合并（`read_file` → 就地合并 → `write_file`），禁止盲覆盖；
  - 明确「当前用户的显式指令优先」与「写记忆失败不阻塞交付」；
  - 明确 memory.md 新增/删除主题时同步维护 MEMORY_INDEX.md（见 D1c）；
  - 保持英文与现有 prompt 一致。

#### D1b — HEARTBEAT 模板统一 + reflections 维护指令
- 根因：两版模板分叉——`app/templates/HEARTBEAT.md`（旧版，要求写 reflections）
  与 `agent_template/HEARTBEAT.md`（新 agent 实际拿到，只写 curiosity_journal）。
- 决策：以 `agent_template/HEARTBEAT.md` 为唯一规范（新 agent 实际读取的来源），
  把旧版的 Phase 1（读 reflections）/Phase 3（写发现）/Phase 4（next cycle seed）
  合并进来，保留新版「无兴趣点则 HEARTBEAT_OK」的防空转护栏；
  `app/templates/HEARTBEAT.md` 同步为相同内容（消除分叉，防再漂移）。
- 存量 agent 的 HEARTBEAT.md 不会被自动覆盖：ticket 内附一次性同步动作说明
  （实现时评估：storage 层批量回写 vs 仅文档说明，倾向轻量文档说明 +
  可选运维脚本）。

#### D1c — MEMORY_INDEX.md 维护义务
- 由 D1a 的 Memory Maintenance 小节一并覆盖（同一 seam，避免第二处提示词改动）：
  记忆主题新增/移除时，同步更新 MEMORY_INDEX.md 的 Topics 清单。
- 不给索引单独的写入工具/流程；索引只是 memory.md 的目录镜像。

### D2 — skill-creator 评估 runner：方案 A（推荐）
- `run_eval.py` 增加 `--runner {clawith|claude}`（默认 clawith）：
  - `claude` runner 保留原实现（代码原样保留，供有 claude CLI 的环境使用）；
  - `clawith` runner：以 OpenAI 兼容协议直调平台模型端点（base_url / api_key /
    model 走环境变量 `CLAWITH_EVAL_BASE_URL` / `CLAWITH_EVAL_API_KEY` /
    `CLAWITH_EVAL_MODEL`，缺失时报可操作错误而非崩溃）；
  - 触发判定：把技能「目录清单 + SKILL.md frontmatter（name/description）」
    注入 system 提示，要求模型对每个 query 只回答「是否需要读取该技能」，
    或直接检查响应是否含 `read_file` 工具调用且参数路径命中 `skills/<name>/`——
    实现时取后者（工具调用形态判定，更接近上游语义）；
  - 结果 JSON schema 保持不变（`query/should_trigger/trigger_rate/pass`），
    下游 aggregate_benchmark / generate_report 无需改动。
- `improve_description.py` / `run_loop.py`：把 `anthropic.Anthropic()` 换成
  轻量 OpenAI 兼容 client（httpx 即可，平台后端已有 httpx），模型与端点走同样的
  环境变量族。
- 原脚本中 `webbrowser.open` 等桌面行为在容器内无害，保留。
- 备选方案 B（descope，不推荐为主案）：从 seeded skill-creator 中移除评测脚本引用
  并在 SKILL.md 标注「评测暂不可用」，只留创作部分。可在用户否决 A 时采用。

### D3 — 存量 agent 的 skill-creator 同步
- 同步机制现状：`push_default_skills_to_existing_agents` 只「补缺不覆盖」——
  新增文件会自动同步，改动文件不会。
- 决策：脚本改动以**新文件**为主（新增 clawith runner 模块、新增 client 模块），
  被改文件（run_eval.py 等）在存量 agent 上保持旧版可接受——因为旧版本就不可用，
  新 SKILL.md 指引指向新 runner 文件（新文件会被自动补齐）。
- SKILL.md 指引文本更新对存量 agent 不生效的问题：在 Further Notes 记录，
  并在修复 PR 中附带一次性同步动作说明（不写进本 spec 的代码范围）。

### D4 — tasks 错误码
- `tasks.py` 创建端点捕获 `TaskRuntimeIntakeError`（及同类「配置缺失」分支）→
  返回 400 + 结构化错误（`error_code: agent_model_not_configured` +
  可操作中文/英文文案），保持 500 只给真正的内部错误。
- 只改错误映射层，不改任务创建的业务语义。

### D5 — 范围边界
- 不做：群聊记忆工具改造；memory.md 的 schema/结构化；自动记忆合并算法；
  平台级 benchmark 功能；Smithery/ModelScope 外呼实测（需真实 key）。
- 不做：给 direct chat 新增 memory_write 工具（write_file 已够，且不动工具 schema）。

## Testing Decisions

- 好的测试只测外部行为：提示词断言测「指令存在 + 低信任语义未破坏」，
  不测逐字文案；runner 测「判定函数的行为」，不测真实模型外呼。
- 记忆（D1a/D1c）：`tests/test_agent_context.py` 现有用例是直接先例
  （`test_base_prompt_starts_with_name_and_soul_and_never_injects_self_role`）。
  新增：①static prompt 包含 Memory Maintenance 义务与 MEMORY_INDEX 同步义务；
  ②memory 快照仍在 dynamic 低信任段；③无 `write_file` 工具权限的模型步不会收到
  该义务（沿用 `_active_capability_policies` 的 allowed-tool 门控，若有）。
- HEARTBEAT 模板（D1b）：模板是纯文本种子文件，测试=内容断言（参照
  `tests/test_migrate_legacy_heartbeat_template.py` 对模板内容的断言先例）：
  ①agent_template 版同时包含 reflections 维护与 HEARTBEAT_OK 护栏；
  ②两份模板内容一致（防再分叉——用读文件逐字比对或 hash 断言）；
  ③agent_manager 的 fallback 复制路径与模板目录路径仍指向有效文件。
- skill-creator（D2）：脚本级纯函数测试（触发判定、并发收集、阈值、JSON schema
  稳定性）——参照仓库内 `tests/test_android_build_progress.py` 的
  「bash -n 校验命令」先例与 skill_creator 相关测试的组织方式；
  用注入的 fake client 验证「无凭证时报可操作错误」；真实模型 E2E 留作
  手工验收项（记录手法：本地起 mock OpenAI 兼容端点，同正文流中断 E2E 的 mock
  模型先例）。
- tasks（D4）：先红后绿的 API 测试，参照 `tests/test_tasks*.py` 现有用例；
  断言 400 + error_code，且真实内部错误仍为 500。
- 全量回归：后端 `uv run pytest --ignore=tests/test_sso_toggle.py --ignore=agent_data`
  （基线 3041 passed）、`scripts/arch-guard.sh`、前端 tsc/契约测试（本次不动前端，
  仅确认不破坏）。

## Out of Scope

- 记忆去重/合并算法、memory.md 结构化 schema、跨 agent 记忆共享（群聊记忆已是独立
  体系）。
- skill 平台化评分、benchmark 数据集管理、eval 结果入库。
- Smithery / ModelScope 工具发现与安装的真实外呼联调（需要租户配置真实 key）。
- 默认 soul 模板的改动（D1a 已由基础提示词覆盖，避免多 seam）。
- 存量 agent 的 HEARTBEAT.md / 技能文件批量回写（D1b 提供一次性同步说明，
  平台级「覆盖同步」机制另开独立 spec）。

## Further Notes

- 存量 agent 的 skill-creator SKILL.md 指引文本不会随 seeder 自动更新（只补缺不覆盖），
  实现 PR 需附一次性同步动作说明；若用户希望平台级修复该机制，另开独立 spec。
- 「记忆写操作」会经由既有 write_file 台账进入 agent_tool_executions，
  天然满足可观测性（Langfuse observe_tool 与审计复用既有埋点），无需新埋点。
- 证据档案：本会话审查原始记录见 scratchpad
  `session-2026-08-27-13-18-04-2d8e37fe/`（E2E 脚本 e2e_walkthrough.py、
  记忆 mtime 快照、git 考古结论）。

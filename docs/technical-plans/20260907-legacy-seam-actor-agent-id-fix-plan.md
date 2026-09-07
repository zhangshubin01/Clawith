# S5「legacy seam 补 actor_agent_id」生产级修复方案

- **日期**：2026-09-07
- **待办出处**：`docs/technical-plans/20260905-maintainer-gate-g3-g4-production-plan.md` §9.2 S5（"legacy seam 补 actor_agent_id…若 legacy/ACP 链式路径复用于 a2a 文件写，会被误拒。低优先，记待办"）
- **当前部署**：e9f136bb（maintainer 门控 + agent_maintainers 管理 API 上线）；本仓 HEAD 696c573e（docs 追加），分支 `f-shubin-0806`
- **遵循流程**：clawith-fix-plan v2（Phase 1 参考对比 → Phase 2 双源定根因 → Phase 3 方案 → Phase 4 七角度评审）

---

## 结论（裁决）

**有条件通过 —— 定性 P2 预防（低优先待办，维持原 todo「记待办」定性）。**

- **本次交付 = Option C**：文档化 seam 不对称 + 精准触发条件 + 预置实现清单（零代码、零回归、零爆炸半径）。
- **否决 Option A**（2 行只改 `execute_tool` 签名）：化妆式修补，无任何调用方注入 `actor_agent_id`，删除测试下「a2a 规则在 legacy 路径不可达」这一根因仍在。
- **Option B（完整链式透传）作为升级路径已备好**，但不落地：其目标场景（legacy/ACP seam 复用于 a2a 文件写）当前零流量，落地属宪法 C6 禁止的投机式加固。

理由在 Phase 2（双源证据：a2a 文件写是**真实高频流量**，但**全部走 durable seam 且 0 误拒**）与 Phase 4（Q2 删除测试 / Q5 最优性）中展开。

---

## Phase 1：参考项目对比（关键决策点 → 谁怎么解决 → 结论）

**关键决策点**：多租户/多 Agent 平台如何把**「发起者（actor）身份」**贯穿到工具执行权限门控，尤其是 **agent-to-agent（a2a）vs 人驱动** 两类调用的身份传播如何区分？

> 复用既有研究报告：letta-code（`20260903-letta-code-study.md`）、bisheng（`20260905-bisheng-study.md`）、deepagents（`20260904-deepagents-in-action-study.md`）、orca（`20260905-orca-study.md`）、AgentTeams（`20260903-agentteams-study.md`）。以下逐项对齐本故障的决策点，非从零读源码。

| # | 参考项目（分层） | 同类机制 | 结论（对 a2a actor 身份传播） |
|---|---|---|---|
| 1 | **A2A 协议**（Agent 互操作，T1） | A2A 消息/Agent Card 携带**发起 agent 身份**，跨 agent 边界传播是协议第一类概念 | **正结论（最强）**：验证 Clawith 的 `actor_agent_id` 字段是正确模型——a2a 调用必须显式传播发起 agent 身份。但 A2A 只定义「身份传播」，**不定义**「如何按 actor 门控文件写」——门控规则是 Clawith 自有契约 |
| 2 | **dify**（多租户，T1） | 租户/用户级 RBAC + 应用编排权限 | **负结论**：权限主体是 user/tenant，**无 agent-as-actor 概念**（dify 是人面 workflow 平台，无自治 agent 间调用） |
| 3 | **bisheng**（OpenFGA 16 类型，T1） | OpenFGA 细粒度授权，subject 是 user/group/API key + owner>manager>editor>viewer 金字塔 | **负结论**：actor 恒是 user/API key，**无 agent 身份传播**；最接近的是其「owner/manager 主体金字塔」，Clawith 的 maintainer 模型（creator 隐式 + `agent_maintainers` 行）与之同构，但这是**人侧**模型，不解决 a2a |
| 4 | **letta-code**（memory-confinement/cross-agent-guard，T1） | 记忆子代理 fail-closed 沙箱 + 跨 agent 隔离（cross-agent-guard） | **负结论**：其「跨 agent 隔离」是**写域隔离**（子代理只写自记忆），非 actor 身份门控；且单用户 harness，无 a2a actor 身份线程 |
| 5 | **deepagents**（middleware 钩子，T0） | middleware（summarization/_message_eviction/_prompt_caching）把横切逻辑从工具路径解耦 | **部分正**：**「权限检查做成 middleware」的范式**可迁移（Clawith 的门控已做成 `resolve_file_modify_permission` 纯函数 + 两处 seam 调用，即此范式）。但 deepagents 单用户，无多租户 actor |
| 6 | **claude-agent-acp（ACP）**（T1） | permission-extension 模型，actor 是 IDE 里的人 | **正结论（反向）**：ACP 天然**人驱动**，其「actor」就是 human user、**无 agent-as-actor 语义**——这正是 Clawith ACP handler 传 `None` 的正确理由。本故障的「legacy/ACP 链式路径缺 actor_agent_id」在 ACP 侧**本就该缺** |
| 7 | **AgentTeams**（Manager-Worker，T1） | Manager→Worker 委派在 task 元数据携带**编排方身份**；凭据隔离=网关持密钥、Worker 仅 consumer token | **部分正**：委派时「谁让我干的」随 task 传播，与 `actor_agent_id` 语义同源；但它是编排平面元数据，不落到「文件写门控」粒度 |
| 8 | **orca**（runtimeFence 代际，T1） | runtimeFence 单调计数租约 + deathEvidence 仲裁——**所有权显式化、严格单调** | **部分正（方法论）**：核心教训是「调用所有权必须是**显式、单调、随调用传播**的字段」，Clawith 的 `actor_user_id`+`actor_agent_id` 双字段就是所有权显式化的落点。但 orca 无「a2a→放行」规则 |
| 9 | **openai-agents-python**（handoffs，T1） | agent handoff 传播 conversation context | **负结论**：handoff 是**单用户会话内**流转，非跨租户 a2a，无 actor_agent_id |
| 10 | **12-factor-agents**（方法论） | 「ownership boundary / 状态所有权」原则 | **正结论（方法论）**：seam 不对称（durable 传 `actor_agent_id`、legacy 不传）= 违反「所有权传播一致」。这一原则支撑**要么两 seam 对称、要么显式文档化不对称**，不能默认一致 |
| 11 | **OpenHands**（T1） | event-stream / 工具抽象 | **负结论**：单用户 dev agent，无 a2a actor 身份 |
| 12 | **conductor**（durable workflow，T2） | 持久化 workflow/task 上下文 + 发起身份经 auth 记录 | **部分负**：记录「谁发起」供 auth，但**无双字段 actor（user+agent）**，也无「agent 发起即放行」规则 |
| 13 | **langgraph human_in_the_loop**（T0） | interrupt/approval 携带 human actor | **负结论**：只有 human actor，无 agent-as-actor |

**核心诚实负结论**：**没有任何参考项目有「a2a `actor_agent_id` 非空 → `NOT_GATED`」逐字可抄的规则。** 双字段 actor 模型（`actor_user_id` + `actor_agent_id`）是 Clawith 特有设计；本修复由 Clawith **自身契约对称性**（durable seam 已正确传、legacy seam 缺）驱动，而非外部参照。唯一可正迁移的是两层**方法论**：①A2A「发起 agent 身份必须显式传播」（支撑字段设计）；②12-factor/orca「所有权字段必须显式且随调用一致传播」（支撑 seam 必须对称或显式文档化不对称）。

---

## Phase 2：双源定根因

### 源 1 —— 代码（全部 read_file 核实，2026-09-07）

门控函数已接受 `actor_agent_id`（规则 1）：

- `MaintainerService.resolve_file_modify_permission`（`app/services/maintainer_service.py:165-201`）
  - `:179-180`：`if actor_agent_id is not None: return FileModifyDecision.NOT_GATED`（a2a 规则，grill 决策 2）。
  - `FILE_MODIFY_TOOL_NAMES = frozenset({"delete_file","edit_file","write_file","move_file"})`（`:43`）。

**正确路径（durable seam）**：

- `ToolStepService._maintainer_file_gate`（`app/services/agent_runtime/tool_step_service.py:1923-1952`）
  - `:1950`：`actor_agent_id=context.actor_agent_id` ✅

**缺失路径（legacy seam）**：

- `execute_tool`（`app/services/agent_tools.py:6142-6149`）：签名 `(tool_name, arguments, agent_id, user_id, session_id="", on_output=None)`，**无 `actor_agent_id`**。
  - 门控调用 `:6194-6219` 只传 `actor_user_id=user_id`（`:6209`），**缺 `actor_agent_id`**。
- 完整 legacy/ACP 链式路径（均无 `actor_agent_id` 透传）：
  - `_process_tool_call`（`app/services/llm/caller.py:347-358`）→ 调 `execute_tool`（`:430-436`）。
  - `call_llm`（`caller.py:480-498`）→ 调 `_process_tool_call`（`:890-901`）。
  - `call_llm_with_failover`（`caller.py:916-935`）→ 两处调 `call_llm`（`:961-978`、`:1026-1035`）。
  - `call_agent_llm`（`caller.py:1056-1066`，legacy 入口，当前无调用方）→ 调 `call_llm_with_failover`（`:1100-1112`）。
  - ACP handler（`app/plugins/clawith_acp/acp_handler.py:789-801`）→ `call_llm_with_failover(user_id=self.user_id, agent_id=self.agent_id)`（ACP 是人在 IDE 驱动，天然无 actor_agent_id）。
  - ACP 链式分发 `_chained_execute_tool`（`app/plugins/clawith_acp/tool_hooks.py:38-68`，`:932` monkeypatch 替换 `agent_tools.execute_tool`）+ `_acp_aware_execute_tool`（`tool_hooks.py:940-947`）：两者签名 `(tool_name, args, agent_id, user_id, session_id="", on_output=None)`，**均无 `actor_agent_id`**。
  - `caller.py:60-61`：`execute_tool(*args, **kwargs)` 惰性 wrapper（透明透传 kwargs）。
- `execute_builtin_tool_outcome`（`agent_tools.py:5609`）四文件工具走 typed 分支，不落回 `execute_tool`；`execute_tool` 只被 `call_llm`(ACP) 与 unmigrated/dynamic 工具触达。
- **`app/services/agent_runtime/` 零引用** `execute_tool`/`call_llm`/`call_llm_with_failover`/`call_agent_llm`（grep 空结果）→ a2a 文件写只走 durable runtime。

### 源 2 —— 运行台账（PG 只读，`agent_run_commands` + `agent_tool_executions`，2026-09-07 实查）

> ⚠️ 此轮实测**纠正了此前调查的一处误判**：原记录「a2a 运行调用四文件工具 0 条」不实。正确数字如下——a2a 文件写是**真实高频流量**，但全部走 durable seam。

| 事实 | 数值 | 含义 |
|---|---|---|
| `agent_run_commands` 中 `actor_agent_id` 非空的命令 | **53**（34 start + 19 resume），全部 `actor_user_id` 亦非空 | a2a 运行真实存在，且双字段均有值 |
| 其中调用四文件工具的 distinct run | **20** | a2a 确实写文件，非零流量 |
| 四文件工具调用次数 | **433**（write_file 198 / edit_file 157 / move_file 60 / delete_file 18） | 高频 |
| 全平台 `result_metadata->>'error_code'='tool_permission_denied'` | **0**（门控部署至今） | durable seam 对 a2a 从未误拒 |
| `agent_tool_executions` 写入点 | 仅 `agent_runtime/tool_execution.py:1453` 一处实例化 | **该账本是 durable-only**；legacy `execute_tool` 走 `save_tool_call_log`→`ChatMessage(role="tool_call")`（`chat_session_service.py:395-442`），不写此表 |

**账本归属证据链**：433 次 a2a 文件写**全部**落在 `agent_tool_executions`（durable-only）→ 全部经 `_maintainer_file_gate`（传了 `actor_agent_id`）→ 0 次 `tool_permission_denied` → **durable seam 对 a2a 的 `NOT_GATED` 规则在生产中正确生效且被高频使用**。legacy 账本（`ChatMessage` role=tool_call）中无 a2a 文件写 → **legacy seam 的缺口当前不可达**。

### 根因（最深层）

**「a2a 规则（rule 1）在 legacy seam 不可达」是真实的，但它是「契约不对称」而非「live bug」。**

- 规则本身只在 durable seam 可达（`_maintainer_file_gate` 传了 `context.actor_agent_id`），且该 seam 承担了全部 a2a 文件写（433 次、0 误拒）。
- legacy seam（`execute_tool`→门控）不接受也不转发 `actor_agent_id`，且其**唯一现实调用方**（ACP 人驱动、unmigrated/dynamic 工具）**语义上就没有 agent-actor**——ACP 侧「缺 actor_agent_id」是正确的，不该补。
- **根因再追一层**：门控是 G3/G4 工作（e9f136bb）引入的，a2a 支持当时只接进 durable seam、legacy seam 显式留作待办（"若 legacy seam 复用于 a2a 再补参"）。此轮台账证明「a2a 文件写只走 durable」这一前提**至今成立**，故该待办仍未触发。

**可证伪表述**：「若 legacy seam 缺口是 live bug，则账本中应出现『经 legacy 路径的 a2a 文件写被拒』= `ChatMessage` 账本有 a2a 文件写 + `tool_permission_denied`>0；实测两者皆 0 → 缺口是 latent，非 live。」

---

## Phase 3：修复方案

### 候选枚举（Ponytail 阶梯从最低档起挑）

| 方案 | 改动 | 效果 | 裁决 |
|---|---|---|---|
| **Option C：文档化 + 精准触发条件** | 0 行代码（本文档 + 升级清单） | 把「trap」显式暴露给未来维护者，触发条件满足时按清单即插即用 | **✅ 本次交付** |
| **Option A：只改 `execute_tool` 签名 + 门控转发** | 2 行（加可选参 + 传参） | 签名对称，但**无调用方注入**，a2a 规则仍不可达（删除测试失败） | **❌ 否决**（化妆式修补） |
| **Option B：完整链式透传** | 6 函数加 `actor_agent_id: uuid.UUID \| None = None` 可选参 + 透传 + 2 回归测试 | a2a 规则在 legacy/ACP seam **端到端可达** | ⏸ 升级路径（触发条件满足时落地） |

### 推荐：Option C（本次交付，零代码）

**内容**：本文档即交付物——把 seam 不对称固化为可检索的契约记录，并写明精准触发条件与即插即用清单（见下）。

**精准触发条件**（满足任一即落地 Option B）：
1. 未来有任何代码把 **a2a / agent 间调用** 路由进 `execute_tool` / `call_llm` / `call_llm_with_failover` / `call_agent_llm`（而非 durable runtime）；
2. 或新增一个「agent 驱动的文件写」入口复用 legacy seam。

**理由**：
- 宪法 C2（最小改动）+ C6（禁止投机式加固）：修的是「臆想的风险」而非「已发生的故障」——删除本方案，观察到的故障（a2a 文件写被误拒）**仍不存在**（因为它从未发生过）。
- 诚实定性 P2 预防：零 live 症状、legacy seam 零 a2a 流量、ACP 是人为驱动。
- 与原始 todo 自述一致（"低优先，记待办"）。

### 升级路径：Option B（触发条件满足时落地，即插即用）

改 6 处签名 + 2 回归测试：

1. `execute_tool`（`agent_tools.py:6142`）：加 `actor_agent_id: uuid.UUID | None = None`，门控调用 `:6204` 补 `actor_agent_id=actor_agent_id`。
2. `_process_tool_call`（`caller.py:347`）：加同名可选参，调 `execute_tool`（`:430`）透传。
3. `call_llm`（`caller.py:480`）：加同名可选参，调 `_process_tool_call`（`:890`）透传。
4. `call_llm_with_failover`（`caller.py:916`）：加同名可选参，两处 `call_llm`（`:961`/`:1026`）透传。
5. `_chained_execute_tool` + `_acp_aware_execute_tool`（`tool_hooks.py:38`/`:940`）：加同名可选参并透传 base/兜底（否则未来注入时经 `caller.py:60` 的 `*args/**kwargs` wrapper 会打穿 monkeypatch → `TypeError`）。
6. （可选）`call_agent_llm`（`caller.py:1056`）：加同名可选参，透传 `call_llm_with_failover`。

**ACP handler 调用点（`acp_handler.py:789`）不改**：人驱动传 `None`（默认值），语义正确。

**回归测试（2 条，随 Option B 落地）**：
- seam 测试：`execute_tool` 把 `actor_agent_id` 转发给门控 —— 断言「a2a（`actor_agent_id` 非空）+ 非维护者 `actor_user_id` 的 workspace 写」**不被拒**（`NOT_GATED`），而非 `GATED_DENIED`。
- 透传测试：`_chained_execute_tool` 收到 `actor_agent_id` 时透传 base handler，不抛 `TypeError`。

**影响面 / 爆炸半径（Option B 时）**：
- 契约：6 个函数新增**可选**关键字参（默认 `None`）→ 对既有调用方**零破坏**（纯向后兼容）。
- 消费者：ACP handler、unmigrated/dynamic 工具、未来 a2a-over-legacy 入口。ACP 侧默认 `None` 行为不变。
- 爆炸半径：仅 legacy/ACP seam 的工具执行门控判定；durable seam 不动。`scripts/arch-guard.sh` + 全量 pytest 回归。

---

## Phase 4：七角度评审（裁决 → 正向依据 → 负向探针）

### Q1 根因是否正确？—— **通过**

- **正向依据**：根因解释双源**每一个**关键证据——①代码：`resolve_file_modify_permission` rule 1 已存在（`:179-180`），durable `:1950` 已传、legacy `:6209` 未传（源码）；②台账：433 次 a2a 文件写全落 durable-only 账本、0 次 `tool_permission_denied`（PG）；③`agent_runtime/` 零引用 legacy 链（grep 空）。已追到最深层：不是「漏传一个参数」而是「a2a 支持当初只接 durable seam，legacy 显式留待办且前提至今成立」。
- **负向探针（反例测试）**：「若根因是『legacy 缺口是 live bug』，则账本应出现经 legacy 路径的 a2a 文件写被拒 = `ChatMessage`(role=tool_call) 账本有 a2a 文件写 + `tool_permission_denied`>0；我对照了：`agent_tool_executions`(durable-only，仅 `tool_execution.py:1453` 实例化) 有 433 次、`tool_permission_denied`=0，legacy 账本零 a2a 文件写 → **证实缺口 latent、非 live**。」

### Q2 根治方案是否正确？—— **通过**（对 Option C 的「根治」语义）

- **正向依据**：本故障的「根因」不是 live fault 而是**契约不对称 trap**。对 trap 的「根治」= 把它显式化 + 给精准触发条件，而非给一个永不触发的路径写代码。Option C 正是此治疗。
- **负向探针（删除测试）**：「把 Option C（文档）删掉，问『故障会不会复发』——**不会**（因为 a2a 文件写从未走 legacy，无故障可复发）；但『trap 会不会重新隐身』——**会**，未来维护者看到 durable seam 传 `actor_agent_id`、legacy 不传，会误以为 legacy 有 bug 或两 seam 语义不一致，进而盲改。→ Option C 的价值是**防隐身**而非**防复发**，这正是 latent trap 的正确治疗。反之 Option A 删除后 trap 依旧不可达也不可见 → 否决。」

### Q3 参考资料是否正确？—— **通过**

- **正向依据**：引用的都是**同类问题**（多租户/多 Agent 的 actor 身份传播与权限门控），非同名/同栈 false friend；A2A/ACP 是协议级一手概念，bisheng/dify/letta-code/orca/AgentTeams 已出整库研究报告（读真实源码）；停更项目（OpenHands-CLI、plandex）未作第一依据。
- **负向探针**：「我找了一个可能引用错的点——**ACP 的『缺 actor_agent_id』**：若误当『ACP 也应补 agent 身份』则方向全错。核下来：ACP 是 IDE 人驱动，`acp_handler.py:789` 只传 `user_id=self.user_id` 是**正确语义**，agent-as-actor 在 ACP 不存在 → 方案明确『ACP handler 不改、传 None』，无误。」

### Q4 副作用与爆炸半径是否排查完？—— **通过**（本次 Option C 零改动，故零副作用；Option B 已标注）

- **正向依据**：①副作用面：Option C 无代码改动 → 无外部写/缓存/连接/权限边界变化。Option B（升级路径）只加**可选默认参数**，无 exactly-once 外部写改动、无缓存失效、无连接/资源变化，权限边界仅 legacy seam 的判定（且默认 `None` 行为不变）。②影响面：已过全部消费者——ACP handler（不改）、unmigrated/dynamic 工具（默认 None）、`caller.py:60` wrapper（`*args/**kwargs` 透明，但正是它会把 Option B 的新参打穿 monkeypatch，故清单第 5 条必须同步改 `_chained_execute_tool`）、`call_agent_llm`（无调用方）。`agent_runtime/` 零引用 → durable seam 不受影响。
- **负向探针**：「我特意找过方案会漏掉的一个消费者——**`caller.py:60` 的 `execute_tool(*args,**kwargs)` wrapper + ACP monkeypatch**：若只改 `execute_tool` 不改 `_chained_execute_tool`，未来注入 `actor_agent_id` 会经 wrapper 打穿 → `_chained_execute_tool` 无此参 → `TypeError`。核下来：**确实受影响** → Option B 清单第 5 条显式要求同步改 monkeypatch 链，已回改进方案。（这也佐证 Option A『2 行只改 execute_tool』不成立。）」

### Q5 这是最优且必要的方案吗？—— **通过**

- **正向依据**：①枚举了 ≥3 候选（A 更简单 / B 更彻底 / C 更轻），从 Ponytail 最低档（C=零代码）挑起，C 满足「诚实定性 + 防隐身」即停。②修的是「臆想的风险」而非「已发生的故障」——删除方案后观察到的故障仍不存在；已诚实定性 P2 预防、非 P0 已损（Q2 删除测试支持）。
- **负向探针**：「我试过用更简单一档（Option A，2 行）能否解决——**不能**：无调用方注入，删除测试下『a2a 规则在 legacy 路径不可达』仍存在，且给 monkeypatch 链埋 `TypeError` 隐患；Option A 是化妆式修补，故否决。更彻底一档（Option B，6 函数）**能**端到端解决，但目标场景零流量，属投机式加固（C6）。→ 结论：C 是当前最优必要解，B 是触发条件满足时的正确升级。」

### Q6 是否已有可复用逻辑？—— **通过**

- **正向依据**：门控逻辑已复用（`resolve_file_modify_permission` 是纯函数、durable/legacy 两 seam 已共用）；`actor_agent_id` 字段已在 `agent_run_commands`/`RuntimeContext` 存在；无「新增等价实现」的必要——Option B 只是把既有字段沿既有调用链透传。
- **负向探针**：「我查过知识图谱/代码是否已有『legacy 路径透传 actor_agent_id 的现成参数』——**无**（`call_llm`/`_process_tool_call`/`_chained_execute_tool` 签名均无此参，grep 确认）；也没有可复用 owner（如 `_PATH_CONVENTION_PARAMS` 类）承载 actor 字段。→ 结论：无现成逻辑，Option B 需手动透传，已纳入清单。」

### Q7 会破坏 Clawith 特性吗？—— **通过**

- **正向依据**：逐条过宪法 C1-C6 与红线——C1 证据先行（双源已核）；C2 最小改动（Option C 零代码）；C3 契约与状态所有权（未改任何契约/状态；Option B 只加可选参）；C4 测试证行为（Option B 带 2 回归测试）；C5 保留既有工作（不动 durable seam、不动 ACP handler）；C6 模块化与数据边界（未跨界）。红线：不碰 durable run/checkpoint、多租户隔离、exactly-once、前缀缓存、WS 状态机、飞书通道；DB 只读、容器未重启。
- **负向探针**：「我把方案对每个红线过一遍——尤其**门控判定语义**（会不会因加可选参把『人驱动』误判为『a2a 放行』导致越权）：核下来 Option B 默认 `None`、ACP 传 `None`，`actor_agent_id` 只在真 a2a 注入时非空，故**不会**把人为驱动误放行；且本次 Option C 无代码，红线零接触。→ 不碰。」

---

## 交付闭环说明

- 本次裁决为「**有条件通过（P2 预防，Option C 零代码交付）**」，无代码 diff，故 skill Phase 5（实现后跑 `code-review` 复核 diff）**不适用**——没有实现就没有「实现与方案一致性」要校验。
- 若未来触发条件满足、落地 Option B，届时**必须**补跑 `code-review`（Spec 轴对照本方案 Phase 3 清单 + Phase 4 裁决），方算该升级的交付闭环。

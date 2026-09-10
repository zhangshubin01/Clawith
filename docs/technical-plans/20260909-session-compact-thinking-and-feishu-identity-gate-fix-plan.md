# 2026-09-09 生产级修复方案：飞书场景两故障（Session Context 压缩开 thinking + 飞书身份门控拒绝）

> 方案类型：生产级修复方案（clawith-fix-plan v2）。两故障均来自同一飞书群诊断会话，双源证据（Langfuse span + 运行日志 + PG 台账 + 源码）已闭环。
> 当前部署 017d1e4a（alembic f078）；方案阶段**不动生产代码**，实现与部署见 Phase 5。

## 裁决（结论先行）

- **问题 A（飞书「准备中」20–30s）**：**评审通过**。根因是 Session Context 压缩调用漏传 `thinking_disabled=True`，DeepSeek thinking 默认开启且与 content 共享输出预算；修复 2 行 + 协议默认参数，完全复用 Thread Compact 已部署验收的同款机制（0c43ce61）。
- **问题 B（edit_file `tool_permission_denied` → `tool_config_failure_loop`）**：**有条件通过（列风险+缓解）**。门控本身是正确行为（不该被「修掉」）；根因是飞书身份未按 email 关联网页账号（飞书应用缺 `contact:user.email:readonly` scope → Contact API 不返回 email → 占位用户分裂）。**根治 = 补 email scope（配置，零代码）+ 存量清理（运维）**，另加 user_id/union_id 锚定加固与手工绑定兜底（P2 预防）+ 熔断文案 bug（代码）。全网检索见 B.0，风险与缓解见 B.4。

---

# Part A — Session Context 压缩调用开 DeepSeek thinking 致「准备中」20–30s

## A.1 参考对比（Phase 1，≥10 项目）

决策点：「辅助/压缩类的 LLM 调用，是否应显式关闭 reasoning/thinking，以省 token + 提速」。

| # | 参考项目 | 机制 / 结论 | 证据 |
|---|---|---|---|
| 1 | **Clawith 自身 Thread Compact**（`run_compactor.py`） | **已实现同款修复**：`RunCompactCompletionPort` 协议含 `thinking_disabled: bool = False`（`:288`），`_compact_batch` 调用点传 `thinking_disabled=True`（`:1294`）；底层 `complete_llm_once` 已有 `thinking_disabled` 参数（`single_step.py:137`）+ provider 守卫（`:158` 仅 deepseek 透传 `thinking={"type":"disabled"}`）。0c43ce61 部署后验收（issue 05）：reasoning 归零、耗时 7.8s/10.9s vs 旧 78–86s | 源码 + `.scratch/compaction-slimming/issues/05` |
| 2 | deepseek-harness（T1） | 压缩省 token 走**前缀缓存重放 + 确定性 tool-result-pruner**，compaction 包无 thinking/reasoning 开关（grep 无命中，诚实负结论） | 本地 `UGit/deepseek-harness` |
| 3 | deepagents（T0） | `summarization.py` middleware 用 `BaseChatModel`（默认 gpt-5.5）**默认参数**，无 thinking 控制——OpenAI 系 summary 默认不生成 reasoning，故无需开关（诚实负结论：这是 DeepSeek-provider 特有配置） | 本地 `UGit/deepagents/libs/deepagents/.../summarization.py` |
| 4 | OpenHands（T1） | condenser 已重构迁入 agent-server 后端；本地前端 clone 仅剩 `condensation-event.ts`/`condenser-settings.tsx`，无可读实现（诚实负结论：当前代码不可读） | 本地 `UGit/OpenHands`（HEAD c9b6ceac9） |
| 5 | letta-code（T1） | 记忆压缩走版本化自治子代理（memory-v2/reflection），无「关 reasoning」开关；fail-closed 沙箱是本故障无关的另一面 | `docs/technical-plans/20260903-letta-code-study.md` |
| 6 | jcode（T1） | 压缩三级阈值（80%软/95%硬/10%手动）+ 三模式 + 确定性护栏，不涉及 reasoning 控制（负结论） | `docs/technical-plans/20260905-jcode-study.md` |
| 7 | rtk（T2） | token 经济学 -60~90%，手段是结构化提示与裁剪，非 reasoning 开关（负结论） | `reference-projects.md` |
| 8 | LLMLingua（T3） | 确定性 prompt 压缩（小模型/无模型），无 LLM reasoning 概念（负结论） | 本地 `UGit/LLMLingua` |
| 9 | headroom（T0） | 上下文压缩层：compress 后再进 LLM，工具侧确定性压缩（负结论：无 reasoning 开关） | 本地 `UGit/headroom` |
| 10 | codex（T1） | OpenAI 系上下文管理，reasoning 与 output 分离计费，无此开关需求（负结论） | 本地 `UGit/codex` |
| 11 | DeepSeek-Reasonix（T3） | 围绕「前缀缓存稳定性」设计，关心 prompt 前缀而非输出侧 thinking（佐证：关 thinking 不碰前缀缓存） | 本地 `UGit/DeepSeek-Reasonix` |
| 12 | caveman（T3） | 省 65% 输出 token 的确定性技巧，无 reasoning 开关（负结论） | `reference-projects.md` |
| 13 | context_engineering / how_to_fix_your_context（langchain-ai） | 官方上下文工程方法论，无 DeepSeek thinking 开关 | 本地 `UGit/context_engineering` |

**对比结论**：「辅助压缩调用关 thinking」**不是任何参考项目的通用机制**——因为绝大多数项目用 OpenAI/Anthropic 模型，summary 默认不生成 reasoning。这恰是 **DeepSeek-provider 特有的配置盲区**（DeepSeek thinking 默认 `effort=high`，reasoning token 与 content 共享 `max_tokens`）。Clawith 已在 Thread Compact 路径（#1）用 `complete_llm_once.thinking_disabled` 解决了同一问题；本故障是**同一机制漏铺到 Session Context 路径**，唯一正确做法就是复用 #1。

## A.2 双源根因（Phase 2）

**症状链**：飞书消息 start 命令 → `langgraph_driver.execute` → `capture_run_inputs`（`context_builder.py:575`）→ `_rebuild_group_context_pack` → `session_context_compactor.compact()`（group 会话 cutoff 早于滚动 Session Context 时的 transient rebuild）→ 压缩 LLM 调用生成 DeepSeek thinking → 阻塞 20–30s 才进入主回复。

**证据（双源）**：

1. **Langfuse span（铁证）**：run `9de1ae20`（trace 8e4cf24e）首条 `llm` GENERATION = `commit_session_context` 压缩，06:17:29.138→06:17:56.041 = **26.9s**，usage `input=3615, output=1550, output_reasoning_tokens=3054`（reasoning ≈ 2× content，即输出预算大半被 thinking 吃掉），model deepseek-v4-flash。run `ae126000` 复现：压缩 06:23:03.260 = **37.42s**，2939 reasoning token。compaction 的 LLM 调用走 `observe_generation`（`complete_llm_once`），不落 `[RuntimeModelRequest]` 日志，故 backend 日志静默——**Langfuse span 是唯一时序证据**。
2. **源码（代码级事实，已 read_file 核对）**：
   - `backend/app/services/agent_runtime/session_context_compactor.py:100` `CompactCompletionPort` 协议**无** `thinking_disabled` 参数（对比 `run_compactor.py:288` `RunCompactCompletionPort` **有**）。
   - `session_context_compactor.py:361-376` `_complete_batch` 调 `self._completion(...)` 传 `tools=[_COMPACT_TOOL]`、`agent_id`、`supports_vision`，**未传 `thinking_disabled=True`**（对比 `run_compactor.py:1287-1295` `_compact_batch` **传了**）。
   - `session_context_compactor.py:274` `completion: CompactCompletionPort = complete_llm_once` —— 底层实现已支持 `thinking_disabled`，只是调用点没传。
   - `backend/app/services/llm/single_step.py:137` `complete_llm_once(thinking_disabled: bool = False)`、`:158` `provider_toggle = {"thinking": {"type": "disabled"}} if thinking_disabled and model.provider == "deepseek" else {}`。
   - 3. 探针验证（issue 05，同 provider + 同 `complete_llm_once` 通路）：`thinking={"type":"disabled"}` 被 DeepSeek 接受（HTTP 200）、reasoning 归零、output 2190→536（-75.5%）、耗时 18.0s→4.8s（-73%）、finish_reason=stop。

**根因**：`_complete_batch` 漏传 `thinking_disabled=True`（最深层因：DeepSeek thinking 默认开 + 共享输出预算，属 provider 配置盲区，非业务逻辑错误）。

**可证伪性**：若根因成立，则补传后压缩 span 的 `output_reasoning_tokens` 归零、耗时降到 ~5s 量级（issue 05 已在同通路上验证机制，session 路径待 Phase 5 部署后以 Langfuse span 复核）。

## A.3 修复方案（Phase 3，最小 2 行）

1. `backend/app/services/agent_runtime/session_context_compactor.py:100` `CompactCompletionPort.__call__` 加 `thinking_disabled: bool = False`（对齐 `run_compactor.py:288`）。
2. 同文件 `_complete_batch`（`:369-375`）的 `self._completion(...)` 调用加 `thinking_disabled=True`。

**回归测试**（新增 1 条断言，复用既有 spy 测试 `test_compact_accepts_only_the_commit_tool_and_sets_code_owned_watermark`，其 fake `complete(model_arg, messages, **kwargs)` 已收集 kwargs）：断言 `calls[0][2]["thinking_disabled"] is True`。既有 6 条 compactor 测试 fake 全用 `**kwargs`/`**_kwargs`，加默认协议参数不破坏。

**影响面**：
- 改的契约：`CompactCompletionPort` 加一个默认参数（`=False`），向后兼容；唯一实现 `complete_llm_once` 已支持；唯一消费者是 `LLMSessionContextCompactor`（`session_context_completion.py` 调的是 `compactor.compact()` 抽象，不触及 completion 端口签名）。
- 业务工具调用（reasoning_content 完整回传协议依赖 thinking）**不动**：`single_step.py:158` provider 守卫保证只有 `thinking_disabled=True 且 provider==deepseek` 才透传。

## A.4 七角度评审（Phase 4）

1. **根因正确？** — **通过**。正向：根因能解释双源全部证据——Langfuse span 的 `output_reasoning_tokens=3054/2939` + 26.9s/37.42s 延迟，均由「`_complete_batch` 未传 thinking_disabled → DeepSeek 默认开 thinking」一个机制解释；已追到最深层因（provider 默认开 thinking，非上层症状）。负向探针（反例测试）：「若根因是 X，则同一条 `complete_llm_once` 通路在传了 `thinking_disabled=True` 的 run_compactor 上 reasoning 应归零；我对照 issue 05 验收（mydome1 run 6b6dd377：reasoning 归零、耗时 7.8s/10.9s），证实——且源码 `single_step.py:158` 只有传参才透传 disabled，session 路径未传参，二者差异唯一。」
2. **根治方案正确？** — **通过**。正向：改的正是 Q1 根因（调用点漏传参数），非止痛药（不做「调大输出限额」之类）。负向探针（删除测试）：「删掉本方案，根因会不会复发——会：`_complete_batch` 仍不传参，DeepSeek thinking 仍默认开，20–30s + reasoning 成本照旧；issue 05 已证明不修时 40% 截断率 + 78–86s。」
3. **参考资料正确？** — **通过**。正向：引用均是同类「上下文压缩/摘要」问题；#1 是同代码库同 provider 的真实已部署实现（非 README 摘要）。负向探针：「我找一个可能引用错的点——deepseek-harness 是否真有『关 thinking』机制？核下来：其 compaction 包 grep thinking/reasoning 无命中，省 token 靠前缀缓存重放 + 确定性 pruner，非 reasoning 开关——我如实记为负结论，无误。」
4. **副作用与爆炸半径排查完？** — **通过**。①副作用面：无外部写/缓存失效/连接资源；仅改一个辅助调用点的输出侧 thinking 字段。②影响面：协议加默认参数向后兼容；fake 全 `**kwargs` 不破。负向探针：「我特意找会漏掉的消费者——`session_context_completion.py` 是否直接调 completion 端口？核下来：它调 `compactor.compact()`（抽象），默认 completion 端口只在 `LLMSessionContextCompactor` 内部（`:274`），不受签名变更影响。」
5. **最优且必要？** — **通过**。候选 ≥3：①更简单「不修、接受延迟」——不能，用户报的就是它且成本翻倍；②「调大输出限额」——治标，thinking 长度不可控（issue 05 已否决）；③「全局关 thinking/改默认 effort」——破坏业务 reasoning_content 协议，宪法禁止。本方案是最低一档且同库已验证。负向探针：「试过用更简单一档（仅加 thinking_disabled 到协议、不碰 provider 守卫）能否解决——不能，守卫是既有安全网，本方案本就只加参数+传参，已是极小化。」
6. **可复用逻辑？** — **通过**。正向：`complete_llm_once.thinking_disabled`（`single_step.py:137`）+ `run_compactor.py:288/1294` 已存在，纯复用。负向探针：「查过知识图谱/代码是否已有『session 关 thinking』逻辑——无；但底层守卫已有，只差 session 调用点传参，无新造。」
7. **破坏 Clawith 特性？** — **通过**。C2 最小改动、C4 测试证行为（spy 断言）。红线：reasoning_content 协议（业务调用不碰）、前缀缓存稳定性（thinking 是输出侧，不改 system+tools 前缀字节）、checkpoint 语义（不涉及）。负向探针：「把方案对『前缀缓存稳定性』红线过一遍——关 thinking 只改 completion 请求的 thinking 字段，不动 prompt 前缀，不切断 cache；DeepSeek-Reasonix 关注的是 prompt 前缀，输出侧 thinking 与其正交。不碰。」

**A 结论**：评审通过，可进入 Phase 5 实现。

---

# Part B — 飞书身份未关联网页账号 → edit_file 门控拒绝 → tool_config_failure_loop

## B.0 全网检索：飞书机器人集成最佳方案（2026-09-09 全网检索，Firecrawl）

应「全网搜索最佳飞书机器人集成方案」的要求，针对「跨通道身份关联」这一根治决策点，检索并交叉验证了飞书官方文档 + 同生态 Lark adapter + 企业 SSO 平台，结论如下（每条标注来源 URL）：

**飞书身份标识三 ID 语义（官方文档）** —— 这是根治方案的地基：
- `user_id`：用户在**租户内**的唯一标识，最稳定（不随应用变化）；需 `contact:user.employee_id:readonly` 权限才在通讯录 API 返回。
- `union_id`：用户在**同一开发商（developer）下**多应用间唯一，跨应用稳定（次稳）。
- `open_id`：用户在**单个应用内**唯一，同一用户在不同应用 `open_id` 不同（最不稳，仅 app-scoped）。
- 来源：`open.feishu.cn/document/platform-overveiw/basic-concepts/user-identity-introduction/introduction`、`.../open-id`。

**获取邮箱的精确 scope（官方权限列表）**：
- `contact:user.email:readonly`（「获取用户邮箱信息」，高级权限，App/用户身份）——「在调用获取用户信息相关接口时返回用户邮箱」。**本次故障的直接缺失项**。
- 配套：`contact:user.base:readonly`（获取用户基本信息）、`contact:user.employee_id:readonly`（获取 user_id/employee_id 字段）、`contact:user.phone:readonly`（获取手机号）、`contact:user.id:readonly`（通过手机号/邮箱反查用户 ID，清理脚本可用）。
- 来源：`open.feishu.cn/document/ukTMukTMukTM/uYTM5UjL2ETO14iNxkTN/scope-list`。

**同生态 Lark adapter 的已知坑（关键负向参考）**：`larksuite/openclaw-lark#348`——「同一飞书用户在不同机器人应用私聊中被识别为不同 open_id，导致 allowFrom 无法匹配身份」。佐证：**不能以 open_id 为唯一身份锚**（Clawith upstream = `dataelement/Clawith`，openclaw-lark 是其同生态 Lark adapter）。来源：`github.com/larksuite/openclaw-lark/issues/348`。

**邮箱可见性可控（失败模式）**：飞书管理后台「安全 > 成员权限 > 成员字段可见范围」允许管理员**隐藏成员邮箱** → email 匹配有结构性失败模式 → 必须有 `user_id`/`union_id` + 手工绑定兜底，不能只依赖 email。来源：`feishu.cn/hc/zh-CN/articles/360049067481`。

**企业 SSO 身份集成（行业正解）**：Coze（`docs.coze.cn/guides_configure_sso_from_feishu`）、Authing（`docs.authing.cn/v2/guides/connections/enterprise/lark-internal/`）、飞书官方（`feishu.cn/content/enterprise-sso-system-identity-integration`）都把**飞书作为 IdP**，用稳定 claim（email/user_id/union_id）关联平台账号——即「身份源连接」范式，而非「每次消息临时匹配」。Clawith 的 `login_or_register`（SSO 路径）与 `resolve_channel_user`（消息路径）均已实现 email 关联，缺的是 scope 配置 + 兜底绑定。

**结论**：根治 = **① 补 email scope（`contact:user.email:readonly`）使现有 email 关联生效** + **② 以 user_id/union_id 作稳定锚、open_id 仅兜底** + **③ 管理员手工绑定兜底（覆盖邮箱被隐藏/撞车）** + **④ 清理存量分裂身份**。详见 B.3。

## B.1 参考对比（Phase 1，≥10 项目）

决策点：「IM/SSO 身份如何关联网页账号，避免同一人分裂成两个身份；文件修改门控如何做」。

| # | 参考项目 | 机制 / 结论 | 证据 |
|---|---|---|---|
| 1 | **Clawith 自身 SSO 登录路径**（`feishu_service.login_or_register`） | **有 email 关联代码但未接线**：`Identity.email == fs_email` 精确匹配（`feishu_service.py:372`）+ tenant 内过滤（`:374`）。但**全仓无调用点**（`login_or_register`/`exchange_code_for_user` 均只有定义），且 `:388-389` 引用迁移 028 已删的 `user.external_id`/`user.feishu_user_id` 列（残留死代码，执行即 AttributeError）——故**不能当作已生效的 SSO 路径**，仅作「email 匹配范式」参考 | 源码 |
| 2 | **Clawith 自身消息路径**（`channel_user_service.resolve_channel_user`） | **已有 email/mobile 关联**：`sso_service.match_user_by_email`（`sso_service.py:29`，在 `channel_user_service.py:128` 调用）/`match_user_by_mobile`（`sso_service.py:86`）；失败才落 lazy registration 合成 `{username}@feishu.local`（`channel_user_service.py:451`） | 源码 |
| 3 | **Clawith 自身 maintainer 门控** | `resolve_file_modify_permission`（`maintainer_service.py:165-201`）：actor 非 creator 且非 maintainer 且 gated 路径 → `GATED_DENIED`。本故障门控是**正确拦截** | 源码 + `20260905/20260907` maintainer 计划 |
| 4 | bisheng（T1） | OpenFGA 细粒度授权（16 类型 + 权限金字塔）——授权模型参考，但**不解决身份映射**（subject 关联前置假设已登录正确身份） | `20260905-bisheng-study.md` |
| 5 | casdoor（T1） | Agent-first IAM/SSO（OIDC/SAML/MFA）：OIDC **email claim 作为身份锚**是标准做法——「按 email 关联」的行业正解 | 本地 `UGit/casdoor` |
| 6 | dify（T1） | 多租户 LLM 平台权限/应用编排，租户内账号模型（无 IM 身份映射专项） | `reference-projects.md` |
| 7 | LangBot（T1） | 飞书 IM + tenant RLS + entitlement + placement_generation；`tenant_scoped_listener` 把 adapter 回调绑 workspace 防串。身份映射靠平台 user_id（非 email） | `20260905-langbot-study.md` |
| 8 | AstrBot（T3） | 多 IM 平台 + 插件，身份按平台账号映射（负结论：无 email 关联专项可抄） | 本地 `UGit/AstrBot` |
| 9 | coze-studio（T3） | 企业 agent 平台，账号体系按平台（负结论） | `reference-projects.md` |
| 10 | FastGPT（T3） | 多租户知识库平台（负结论：无 IM 身份映射） | `reference-projects.md` |
| 11 | MaxKB（T3） | 企业 agent 平台（负结论：无 IM 身份映射专项） | `reference-projects.md` |
| 12 | MonkeyCode（T3） | 多租户协作平台（负结论：无 IM 身份映射专项） | `reference-projects.md` |
| 13 | AgentTeams（T1） | 凭据隔离=网关持真实密钥、Worker 仅 consumer token——授权/隔离参考，非身份映射 | `20260903-agentteams-study.md` |
| 14 | letta-code（T1） | cross-agent-guard 跨 agent 隔离（授权边界参考，非身份映射） | `20260903-letta-code-study.md` |

**对比结论**（结合 B.0 全网检索）：「按 email 关联网页账号」是 **SSO/IAM 行业标准**（casdoor OIDC email claim；Coze/Authing 飞书 IdP 集成；Clawith 自身 `login_or_register` 已实现）。Clawith 的消息路径 `resolve_channel_user` **也已实现 email 匹配**——本故障不是「缺关联代码」，而是**飞书应用未授予 `contact:user.email:readonly` → Contact API 不返回 email → `extra_info["email"]` 为空 → 匹配落空 → 占位用户分裂**（配置/数据问题，非代码问题）。门控的正确行为参考 bisheng OpenFGA（授权正确拦截不等于故障，故障在身份）。LangBot/AstrBot 用 open_id 作锚、不做 email 关联（负结论）——因为它们无独立网页账号体系，不面临「跨通道关联」；Clawith 是更复杂的一档，须以 email + user_id/union_id 双锚 + 手工绑定兜底。

## B.2 双源根因（Phase 2）

**症状链**：飞书用户 zhangshubin（网页账号 `16820bb9`）在飞书群 @Agent 让其改 NotesApp → agent 收到「S-3~S-6 等你拍板」后直接动手 edit_file ×4 → 全部 `tool_permission_denied` → `_trailing_config_failure_loop`（连续 3 次 `permission_denied` marker 命中，阈值 `_CONFIG_FAILURE_LOOP_THRESHOLD=3`/窗口 8）→ 熔断 `tool_config_failure_loop`，run 失败。

**证据（双源）**：

1. **PG 台账**：run `ae126000` actor `f50eb606`（email `feishu_58b9fb4g@feishu.local`、`registration_source=feishu`、member）≠ agent creator `16820bb9`（zhangshubin，web，org_admin）。`agent_maintainers` 表空。飞书 identity provider `cef3ae2b` `config={}`（无 email 字段映射）。工具入参权威来源 `agent_tool_executions.sanitized_arguments`（Langfuse `observe_tool` 有意不落工具入参）。
2. **运行日志/源码（代码级事实，已 read_file 核对）**：
   - 门控：`maintainer_service.py:165-201` `resolve_file_modify_permission`——`actor_agent_id=None`（用户 actor）→ 非 group_scoped → `tool_name in FILE_MODIFY_TOOL_NAMES`（`{delete_file,edit_file,write_file,move_file}`）→ bucket=gated → `actor(f50eb606) != creator(16820bb9)` 且非 maintainer → `GATED_DENIED`。这次调用**没带 `workspace_scope`**，不是 group-scoped。
   - 拒绝错误码：`tool_step_service.py:1925` `_maintainer_file_gate`（`:1970-1982` GATED_DENIED 分支）返回 `error_code="tool_permission_denied"`，summary「You are not a maintainer of this agent…」。
   - 熔断：`model_step_service.py:471-475` `_CONFIG_FAILURE_CODE_MARKERS` 含 `"permission_denied"` → 命中 config-class；`:2595-2614` 熔断文案**写死**「（例如在飞书开放平台控制台为应用开通相应 API 权限）」——对 maintainer 门控场景是误导（且 web 渠道也走这条文案，见 B.4 Q4）。
   - 身份链路：`api/feishu.py:459-505` 消息路径确实调 Contact API（`get_contact_user_cached`）取 sender `email`/`enterprise_email` 入 `extra_info`（`:475-476`）；`channel_user_service.py:124-128` 有 email 则调 `sso_service.match_user_by_email`（`sso_service.py:29`）关联、无则 `:189-192` `_create_channel_user` 合成占位邮箱 `:451`。
3. **存量脚本 + SSO 残留死代码**：`cleanup_duplicate_feishu_users.py` 存在，但引用的 `users.feishu_user_id` 列已在迁移 `028_refactor_user_system_phase2.py:32` 删除（脚本过期）。**同源残留**：`feishu_service.py:388-389` `login_or_register` 仍写 `user.external_id = user_id` + `user.feishu_user_id = user_id`，但 `User` 模型（`user.py:51-117`）已无这两个字段（迁移 028 删列）——SSO 路径 `user_id` 非空即 `AttributeError`；因 `login_or_register`/`exchange_code_for_user` 全仓无调用点（未接线），该残留从未暴露。
4. **Contact API 实测（2026-09-09，真实 app 凭证 `cli_aac5e0d96f795eed` + open_id 调 `GET /open-apis/contact/v3/users/{open_id}?user_id_type=open_id`）**：返回 `{name:"David-AndroidTeamLeader-张淑宾", open_id:"ou_1bf04a0302f9c80e4bd8b6ce4ccf0190", union_id:"on_cab85d9bdcf53a24fffea442a182e24c", user_id:"58b9fb4g"}`，**无 `email`/`enterprise_email` 字段**。三点：①`user_id=58b9fb4g` 证实 DB `external_id` 是真实 user_id 非截断 bug；②`union_id` 现（无需 email scope）就能拿到，但 DB `org_members.unionid=None`（该 OrgMember 创建时未取到 union_id 的历史缺失）；③**email 字段当前不返回，无法确证「补 scope 后 email == zhangshubin@guidefuture.com」**——此假设基于名字拼音（张淑宾=zhangshubin）+ 域名（guidefuture.com）吻合，是强推断但未实锤。

**根因（分层）**：飞书应用缺 contact email 权限 scope → Contact API 返回 user info 无 email → `extra_info["email"]` 空 → `resolve_channel_user` email 匹配落空 → 合成占位邮箱 `feishu_58b9fb4g@feishu.local` 创建独立用户 `f50eb606` → 门控判定非 creator/非 maintainer → 拒绝。**最深层因是「飞书应用未授予 email scope」（配置层）**，门控拦截只是正确后果，不是 bug。

**可证伪性**：若根因成立，则补 email scope 后，同一飞书用户下次发消息会经 `sso_service.match_user_by_email` 关联到 `16820bb9`，门控放行（源码 `channel_user_service.py:128` 调用具备该能力）。**前置假设（须实测）**：飞书侧该用户补 scope 后返回的 email == `zhangshubin@guidefuture.com`；若飞书邮箱是别的值或被隐藏，email 匹配仍落空，改走层 3 手工绑定（用实测已得的 `union_id=on_cab85d9...` 显式绑定）。此假设当前未验证。

## B.3 修复方案（Phase 3，四层根治：scope 配置 → 身份锚定 → 手工绑定兜底 → 存量清理）

### 层 1（根治，配置，零代码）— 补 email scope 使现有关联生效
飞书自建应用在开放平台申请并发布权限，使 Contact API 返回真实 email：
- **必加**：`contact:user.email:readonly`（获取用户邮箱）——本次故障直接缺失项。
- **配套加**：`contact:user.base:readonly`（基本信息，含 user_id）、`contact:user.employee_id:readonly`（user_id/employee_id 字段）、`contact:user.phone:readonly`（手机号，作第二关联键）。
- 效果：`api/feishu.py` 的 `extra_info["email"]` 非空 → `resolve_channel_user` 现有 `sso_service.match_user_by_email`（`sso_service.py:29`，在 `channel_user_service.py:128` 调用）自动把飞书身份关联到网页账号。**零代码**。
- **生效边界（关键）**：只对**尚无 OrgMember 的新飞书用户**生效。`resolve_channel_user` Step 2 `_find_org_member` 先按 open_id/unionid/user_id 查 OrgMember，Step 3 命中且已 link 到 User 即**直接返回**（`channel_user_service.py:114-121`），Step 4 的 email 匹配（`:124-128`）不会执行——**存量占位用户 `f50eb606` 补 scope 后也不会自动合并**，必须靠层 4 清理脚本。这是层 4 必要性的机制依据。
- **前置验证（必须，否则层 1 可能空转）**：补 scope 并发布通过后，**先实测一次 Contact API**（app `cli_aac5e0d96f795eed` + open_id `ou_1bf04a...`），确认返回的 `email` 字段 == `zhangshubin@guidefuture.com`。若不等（飞书邮箱是别的值/被隐藏），email 自动关联失败，改走层 3 手工绑定（用实测已得的 `union_id=on_cab85d9...`）。实测证据见 B.2 证据 4。

**为什么主锚是 email，而非 open_id/union_id**（决策依据，回应「为什么非要邮箱」）：
- **现有代码已把 email 当统一键**：飞书侧与网页侧是两套身份体系，Clawith 已有两条关联路径都以 email 为锚——消息路径 `resolve_channel_user` 调 `sso_service.match_user_by_email`（`sso_service.py:29`）+ SSO 路径 `login_or_register` 的 `Identity.email == fs_email`（`feishu_service.py:372`，该函数未接线，见 B.2 证据 3）。补 email scope 不是引入新机制，而是**喂饱已写好的 email 匹配逻辑**，让飞书身份第一次就拼成同一个网页账号——这正是它能做到「零代码根治」的原因。
- **open_id 不能当唯一锚**：open_id 仅单应用内唯一、跨应用不同（B.0 `openclaw-lark#348` 已证同用户在不同 bot 应用被识别为不同 open_id），拿它当锚会在多应用场景下必然身份分裂。
- **union_id 更稳但需改代码**：union_id 同开发商跨应用稳定，理论上是更优锚，但现有匹配键就是 email，改用 union_id 当主锚要改关联代码；email 是现成路径、零改动。
- **email 的已知边界（诚实）**：飞书管理员可在「成员字段可见范围」隐藏邮箱 → 即便授 scope 也可能返回空，email 匹配有结构性失败模式。故 email 只作**让主路径先跑通的零代码根治**，不是唯一防线——这正是层 2（user_id/union_id 排序）与层 3（手工绑定）存在的理由。

### 层 2（身份锚定加固，代码，P2 预防性）— user_id/union_id 优先于 open_id
当前 `_find_org_member`（`channel_user_service.py:284-294`）对 feishu 的 `unionid/open_id/external_id` 是**平权 OR**（docstring `:262` 写「unionid first」但实现未排序，二者矛盾）。按 B.0 的 ID 语义与 openclaw-lark#348 坑，应显式按 `external_id(user_id) > union_id > open_id` 排序匹配，避免多 bot app 场景下 open_id 漂移导致的身份分裂。**定性为 P2 预防**（非本次故障根因，本次根因是层 1 的 email 缺失）；实现时须：①修正 docstring `:262` 为 `user_id > union_id > open_id`；②协调既有 `limit(1) + order_by(linked desc, synced_at asc)` 去重（`:319-332`，注释记载「duplicate shells 导致每次消息新建用户」的历史根因），改排序不得破坏它；③加回归测试（构造 user_id 与 open_id 各命中不同 OrgMember 的用例，断言命中 user_id）。

### 层 3（手工绑定兜底，代码/产品，覆盖邮箱不可用）— 管理员显式绑定飞书身份
B.0 指出邮箱可被管理员隐藏（`成员字段可见范围`）→ email 关联有结构性失败模式。兜底：提供**管理员把某飞书身份（user_id/union_id）显式绑定到平台账号**的管理操作（对齐 Coze/Authing「身份源连接」范式）。最小形态：复用现有 maintainer 管理 API 的 admin-only 治理权限，加一个「channel 身份绑定」端点/脚本（`OrgMember.user_id = 平台 user_id` 的显式写）。**可选、P2**；若时间紧，先以运维脚本（层 4 清理脚本）覆盖，UI 化后置。**注意**：B.2 证据 4 实测已拿到 `union_id=on_cab85d9bdcf53a24fffea442a182e24c`（无需 email scope），故本层兜底可直接用该 union_id 绑定，不必依赖 email scope 生效。

### 层 4（存量清理 + 立即解封，运维，非代码，需用户批准后执行）
- 立即解封：把飞书占位用户 `f50eb606` 加为 agent `62bc9c81` 的 maintainer：`POST /agents/{agent_id}/maintainers`（`agents.py:1103` `add_agent_maintainer`，admin-only；zhangshubin 是 org_admin 可调）。爆炸半径=单 agent 文件权限，可回退。
- 存量合并：复核 `cleanup_duplicate_feishu_users.py`（其引用的 `users.feishu_user_id` 列已被迁移 `028` 删除，主体应改用 OrgMember.external_id + `batch_get_id` 反查），把 `f50eb606` 合并到 `16820bb9`。**执行前非生产库试跑 + 备份**。

### 代码 bug（误导文案，最小改动，独立于上述四层）
`backend/app/services/agent_runtime/model_step_service.py:2595-2614` 的 `tool_config_failure_loop` 文案按 `error_code` 分支：
- `error_code == "tool_permission_denied"` → 「当前账号不是该 Agent 的创建者或维护者，无权修改其工作区文件；请联系 Agent 创建者将你添加为维护者，或改用创建者账号后再试」。
- 其他 config-class（`not_configured`/`credentials_unavailable`）→ 保留「配置错误需人工修复」，去掉飞书专属误导句（或按渠道注入正确提示）。

**回归测试**：
- 层 1/4：无代码，运维验证清单（补 scope 后：`extra_info["email"]` 非空、飞书消息 resolve 到 `16820bb9`）。
- 层 2（若实现）：`_find_org_member` 排序单测——user_id 与 open_id 各命中不同 OrgMember 时断言命中 user_id。
- 文案 bug：单测构造 `loop=("edit_file","tool_permission_denied",4)` 触发熔断，断言返回文案含「维护者」不含「飞书开放平台」。

**影响面**：
- 层 1：零代码爆炸半径；副作用=新飞书用户按 email 自动关联（期望行为）。**风险**：email 撞车（多飞书账号同 email）→ 缓解：层 3 手工绑定 + 清理脚本核对 email 唯一性，撞车走人工。
- 层 2（若实现）：改 `_find_org_member` 匹配排序，消费者是所有渠道的 OrgMember 解析；需全渠道回归（feishu/dingtalk/wecom 各自优先级不变，仅 feishu 显式化）。爆炸半径=身份解析，需谨慎。
- 层 3（若实现）：新增绑定端点，复用 admin 治理权限，爆炸半径=身份关联写入，可审计。
- 层 4：改 `agent_maintainers` 一行 + 用户合并；爆炸半径=单 agent 权限 + 单用户身份；可回退。
- 文案 bug：纯字符串分支，零运行时爆炸半径；唯一消费者是 `_prepare_messages` 熔断返回（web/飞书/所有渠道共用）。

## B.4 七角度评审（Phase 4）

1. **根因正确？** — **通过**。正向：根因解释双源全部证据（PG 台账：actor 占位身份 ≠ creator、maintainers 空、provider config={}；源码：email 匹配有但依赖 `extra_info.email`，占位邮箱在 `:451` 合成）；已追到最深层因（配置层 email scope 缺失，非门控代码 bug）。负向探针（反例测试）：「若根因是『email 数据缺失』而非『缺关联代码』，则身份路径应已有 email 匹配能力；我对照源码：`resolve_channel_user` 在 `:128` 调 `sso_service.match_user_by_email`（`sso_service.py:29`）——证实消息路径已有关联逻辑，根因是数据（email 未返回）非代码。同时核实 `login_or_register:372` 也有 email 匹配，但发现该函数全仓无调用点（未接线）且含已删列残留死代码——不能作『已生效路径』论据，已在 B.1 #1、B.2 证据 3 修正。」
2. **根治方案正确？** — **通过（有条件）**。正向：**层 1（补 email scope）改的是 Q1 最深层因**，不是止痛药；**不取消门控**（门控是正确的安全行为）。层 2（锚定排序）、层 3（手工绑定）**明确定性为 P2 预防**，不是本次故障根因——避免投机式加固（宪法 II）。负向探针（删除测试）：「删掉层 1，根因会不会再发作——会：飞书应用仍不返回 email，任何新飞书用户都分裂成占位身份、门控持续拒绝。层 1 是根治。**但已分裂的存量 `f50eb606` 不会因补 scope 自动合并**——此为已知风险，由层 4 清理脚本覆盖（缓解措施已列入方案）。」
3. **参考资料正确？** — **通过**。正向：引用均是同类「IM/SSO 身份映射 + 授权」问题；casdoor email claim、Clawith 自身两条 email 匹配路径是真源码；B.0 每条结论标注来源 URL（飞书官方 scope 列表 / ID 概念 / openclaw-lark#348 / Coze·Authing SSO）。负向探针：「我找一个可能引用错的点——`contact:user.email:readonly` 是否真是『获取用户邮箱』且为高级权限？核下来：飞书官方 scope-list 页原文『获取用户邮箱信息 · 高级 · contact:user.email:readonly』，无误。另一个可能错点——openclaw-lark#348 说 open_id 跨应用不同，我核飞书官方 ID 概念页『同一个用户在不同应用中的 Open ID 不同』，一致。」
4. **副作用与爆炸半径排查完？** — **通过**。①副作用面：无 exactly-once 外部写/缓存/连接；层 1 的「email 撞车」是唯一副作用，已列缓解（层 3 手工绑定 + 清理核对唯一性）。②影响面：四层逐一过——层 1 无代码、层 2 改身份解析排序（全渠道回归）、层 3 新增绑定端点、层 4 单表单行、文案纯字符串。负向探针：「我特意找会漏掉的消费者——`tool_config_failure_loop` 文案是否被 web 渠道复用？核下来：`_trailing_config_failure_loop` 在 `model_step_service` 统一触发不分渠道，web 用户 maintainer 拒绝也会看到飞书专属文案——这正是文案 bug 的爆炸半径，已纳入修复。」
5. **最优且必要？** — **通过**。候选 ≥3：①更简单「只运维加 maintainer」——解当前 agent 当前用户，不解决「任何飞书用户都会分裂」的结构问题；②本方案（层 1 根治 + 层 2/3 P2 预防 + 层 4 存量 + 文案 bug）；③更彻底「引入 OpenFGA 细粒度授权」——宪法 II 禁止投机式加固，门控已够用。负向探针：「试过更简单一档（只加 maintainer + 补 email scope，不做层 2/3）能否根治——能根治本次故障，层 2/3 是覆盖『邮箱被隐藏/多 bot 应用』的 P2 预防；我把层 2/3 显式标为 P2、不冒充 P0 已损，符合『修已发生故障 vs 臆想风险』的定性纪律。」
6. **可复用逻辑？** — **通过**。正向：email 关联已两处存在（`login_or_register`/`resolve_channel_user`），不新写；`cleanup_duplicate_feishu_users.py` 已存在（需复核）；`batch_get_id`（`feishu_service.py:534/574`）已实现「email/mobile 反查飞书 ID」，清理脚本可直接复用。负向探针：「查过是否有等价『身份合并/关联』逻辑——有：两处 email 匹配 + `batch_get_id` 反查 + 清理脚本，无新造，只需补配置 + 复核脚本过期性。」
7. **破坏 Clawith 特性？** — **通过**。红线：多租户隔离（`match_user_by_email(db, email, tenant_id)` 带 tenant_id，不跨租户）、门控安全（不取消，只让身份正确解析）、checkpoint/WS/前缀缓存（均不涉及）。负向探针：「把方案对『多租户隔离』红线过一遍——`channel_user_service.py:128` 与 `feishu_service.py:374` 都带 tenant_id 过滤，不会把 A 租户飞书用户关联到 B 租户账号；但 email 撞车是残留边界风险，已列缓解。」

**B 结论**：有条件通过。**根治 = 层 1（补 email scope，配置层）+ 层 4（存量清理，运维）**；层 2/3 为 P2 预防；文案 bug 独立修复。**已知风险 + 缓解**：
- **【前置风险，最需先验证】飞书侧 email 值未验证**：方案假设补 scope 后飞书返回 `zhangshubin@guidefuture.com`，但 email 字段当前不返回、无法实锤（B.2 证据 4）；若飞书邮箱是别的值/被隐藏，email 自动关联失败 → 缓解：层 1 补 scope 后**先实测 email 值**，不等则走层 3（用实测已得的 `union_id=on_cab85d9...` 绑定）。
1. 存量占位身份 `f50eb606` 不因补 scope 自动合并（`_find_org_member` 命中已 link 的 OrgMember 后 Step 3 直接返回，email 匹配被短路）→ 层 4 清理脚本覆盖（先非生产库试跑 + 备份）。
2. email 撞车（多飞书账号同 email）→ 层 3 手工绑定 + 清理脚本核对唯一性，撞车走人工，不自动关联。
3. 邮箱可被管理员隐藏（`成员字段可见范围`）→ email 关联有结构性失败模式，由层 3 手工绑定兜底（P2）。
4. 门控拒绝本身是正确行为，**任何修复都不得以「取消门控」为代价** → 方案明确「不取消门控」。

---

## Phase 5 实现闭环（待实现，实现完成必须跑，否则不算闭环）

方案被实现为 diff 后，须用 `code-review`（Spec 轴对照本方案 + Standards 轴对照仓库规范）复核实际 diff：无偏离、无夹带范围外改动、无「评审时未提的机制」。当前阶段未动生产代码，实现与部署走 `clawith-prod-deploy` 规范。

**实现清单（预估）**：
- A：2 处改 + 1 条测试断言（`session_context_compactor.py`）。
- B 文案 bug：1 处文案分支 + 1 条单测（`model_step_service.py`）。
- B 残留死代码（新发现）：删除 `feishu_service.py:388-389` 的 `user.external_id = user_id` + `user.feishu_user_id = user_id`（迁移 028 已删列；SSO 未接线故未暴露，若未来接线 SSO 须先清）。
- B 层 2（P2，可选）：`_find_org_member` feishu 排序显式化 + 排序单测（`channel_user_service.py`）。
- B 层 3（P2，可选）：channel 身份绑定端点/脚本（复用 admin 治理权限）。
- B 层 1/4：非代码，配置 + 运维执行（需用户明确批准后，按安全纪律执行 DB/配置操作）。

**验收项（部署后）**：
- A：Langfuse span 复核 session 压缩调用 `output_reasoning_tokens` 归零、耗时降至 ~5s 量级（验收方法见记忆 `compaction-acceptance-evidence-methods`）。
- B：飞书用户发消息后 resolve 到 `16820bb9`（PG `users`/`org_members` 台账）、edit_file 不再 `tool_permission_denied`；`tool_config_failure_loop` 文案按 error_code 分支正确；`extra_info["email"]` 非空（scope 已生效）。

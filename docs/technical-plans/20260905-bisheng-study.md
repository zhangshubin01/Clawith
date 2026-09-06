# bisheng（dataelement/bisheng）整库源码研究报告

日期：2026-09-05
状态：**完成**（分析基于本地仓库 `/Users/shubinzhang/Documents/UGit/bisheng` HEAD `2456ec1`，`--depth 1` 浅克隆；Clawith 对照基于 `/Users/shubinzhang/Documents/agent/Clawith` 工作树）
定位：参考资料研究，非实现方案。对照 Clawith 的多租户隔离、token_tracker 计量、skills 渐进披露、审批流与 LangGraph 运行时。

## 0. 项目概览

- **是什么**：bisheng（`dataelement/bisheng`，Python，~10k★）——**企业级 LLM 应用 DevOps 平台**（README 首句 *"an open LLM application devops platform, focusing on enterprise scenarios"*）。定位一句话：让企业用户以**可视化流程图 + 助手编排 + 知识库 RAG**的方式，把「文档审核、固定版式报告生成、多 Agent 协作、工单助手、会议纪要、简历筛选」等企业场景落地为可部署应用。
- **三层架构**：① `src/backend`（FastAPI + SQLModel + LangChain 1.x + **LangGraph 1.x**，`bisheng/` 主包 37 个领域模块 + `bisheng_langchain/` 14 个 LangChain 封装子包）；② `src/frontend`（pnpm monorepo：`client/` + `platform/` + `packages/`，React 工作台 + 流程画布）；③ `docker/`（docker-compose 11 组件：mysql/redis/openfga×2/backend/backend_worker(celery)/frontend/elasticsearch/etcd/minio/milvus）。
- **核心结论**：bisheng 的「企业级」不是口号，而是三块硬机制：**OpenFGA 细粒度授权**（Google Zanzibar 式，16 类型 + 权限金字塔 owner>manager>editor>viewer + 跨租户共享）、**SQLAlchemy 事件自动租户隔离**（ContextVar 驱动，do_orm_execute/before_flush 自动注入 WHERE tenant_id）、**LangGraph 只当「拓扑调度器」**（真正的执行态放自研 `GraphState` sidecar，人机协同靠 `interrupt_before + continue_run`）。这三点是它最值得 Clawith 借鉴的部分——**不是可视化画布本身，不是 LangChain 的 chain/tool 封装，而是「多租户数据隔离」与「租约/授权/计量」这类平台底座原语**。
- **对标关系**：bisheng 与 Clawith **同属 dataelement org**（Clawith 上游 remote 即 `dataelement/Clawith`），是姊妹项目。**关键更正**：任务背景称 bisheng 为「LangChain 栈（非 LangGraph）」，但 HEAD `2456ec1` 实际**已用 LangGraph 1.x**（`bisheng/workflow/graph/graph_engine.py:6` `from langgraph.graph import StateGraph`，`pyproject.toml` 依赖 `langgraph>=1.2,<2.0`）。两者真正的分野不在「LangChain vs LangGraph」，而在**产品定位**：bisheng = **无代码/低代码的 LLM 应用编排 + RAG + 助手**（面向「搭应用」的 DevOps）；Clawith = **自治 Agent 运行时**（run 生命周期、上下文压缩、token 计量、自进化记忆，面向「跑 agent」）。技术栈高度同源（都是 LangChain 模型层 + LangGraph 编排层 + FastAPI/SQLModel/SQLAlchemy + PG/Redis），但演进出了两套不同的抽象。

---

## 1. 多租户与租户隔离

bisheng 的租户隔离是「**三件套**」，且每一件都打磨到能写进事故复盘的程度：

### 1.1 ContextVar 租户上下文 + SQLAlchemy 事件自动隔离

- **请求级 ContextVar**：`current_tenant_id: ContextVar[int | None]`（`bisheng/core/context/tenant.py:44-47`），由 HTTP 中间件（JWT cookie）、WebSocket 中间件、Celery `task_prerun` 信号三处写入（`tenant.py:1-9` 模块 docstring）。`get_current_tenant_id()` 带 **F019 管理视角覆盖**优先级：先看 `_admin_scope_tenant_id`（超级管理员「切换管理视角」），再回落到 JWT 的 leaf tenant（`tenant.py:84-97`）。
- **SQLAlchemy 事件自动注入 WHERE**：`register_tenant_filter_events()`（`bisheng/core/database/tenant_filter.py:143-237`）注册两个全局 Session 事件：
  - `do_orm_execute`（`:158-198`）：拦截所有 SELECT，凡涉及「有 `tenant_id` 列的表」自动追加 `WHERE tenant_id = X` 或 `tenant_id IN (...)`；
  - `before_flush`（`:200-234`）：新对象 INSERT 前自动回填 `tenant_id`，且**若对象显式值 ≠ 当前上下文值会记 warning**（防御「v2.5 default=1 泄漏 bug」复发）。
- **可见集合 IN-list**：`visible_tenant_ids`（`tenant.py:58-61`）——Root 用户见 `{1}`，Child 用户见 `{leaf_id, 1}`（能看到 Root 共享资源）。`strict_tenant_filter()`（`tenant.py:167-179`）强制「仅叶子租户相等过滤」，用于配额/资源计数这类 IN-list 会「多算」的场景。
- **bypass 逃生门（受控）**：`bypass_tenant_filter()`（`tenant.py:105-115`）与 `bypass_tenant_filter_if()`（`tenant.py:118-137`）——前者给系统管理员跨租户查询，后者给「凭 capability（分享令牌）而非租户成员资格」授权的读路径，docstring 明确写了「两点必须同时成立才安全：只放宽读 + 后续仍跑 owner/capability 校验」。
- **防「静默泄漏」的模型全量导入**：`_TENANT_AWARE_MODEL_MODULES`（`tenant_filter.py:39-102`）硬编码 40+ 个 ORM 模块强制 import，`_discover_tenant_aware_tables()`（`:127-140`）从 `SQLModel.metadata` 自动发现所有带 `tenant_id` 列的表——**注释直接点名了 v2.5 的教训**：「knowledgefile / flowversion / roleaccess / userrole 等模型没被 import 链覆盖，导致子租户资源持续写到 root」。
- **已知边界**：`build_tenant_filter_clause()`（`tenant_filter.py:240-277`）的 docstring 记录了一个真实漏洞形态——`select(sub.c.id) FROM (select … UNION ALL …) AS sub` 会隐藏内层表，自动过滤器看不见、不注入，需要手动挂 clause。

### 1.2 OpenFGA 细粒度授权（Zanzibar 式）

- **16 类型授权模型**：`AUTHORIZATION_MODEL`（`bisheng/core/openfga/authorization_model.py:167-298`）定义 `user / system / tenant / department / user_group / knowledge_space / knowledge_library / folder / knowledge_file / channel / workflow / assistant / tool / dashboard / llm_server / llm_model`（模块 docstring `:1-8`）。
- **权限金字塔**：`_standard_resource_type()`（`:46-164`）为每个资源统一生成 `owner > manager(can_manage) > editor(can_edit) > viewer(can_read)` 四层，`can_delete` 分层（顶层仅 owner，层级资源 owner 或父级 can_manage）。
- **层级资源**：`folder`/`knowledge_file` 带 `parent` 关系（`:283-286`），`manager/editor/viewer` 通过 `tupleToUserset(parent → can_*)` 向上继承（`:76-82`）。
- **跨租户共享**：每个资源有 `shared_with: [tenant]` 关系，`viewer` 扩展 `tupleToUserset(shared_with, member)`（`:104-133`）——Root 把资源分享给 Child 时写入 `{resource}#shared_with → tenant:{child}`，Child 的 member 即可 view。租户自身用 `shared_to`（Root→Child 显式分享标记）+ 双层 admin 模型（`tenant#parent` 有意移除，父租户关系只存 MySQL，`:188-217`）。
- **配套工程**：`openfga-migrate` + `openfga` 两个容器（`docker/docker-compose.yml`），MySQL 作为 OpenFGA datastore，`CHECK_QUERY_CACHE_TTL=30s` 等缓存调优。

### 1.3 对 Clawith

- Clawith 的租户隔离是**显式 FK + 应用层校验**，没有自动注入：`Tenant` 模型（`backend/app/models/tenant.py`，UUID 主键，含 `default_message_limit/default_max_agents/default_max_llm_calls_per_day/min_heartbeat_interval_minutes` 等租户级配额），`TenantSetting`（`backend/app/models/tenant_setting.py`，per-tenant key-value），权限靠 `backend/app/core/permissions.py` 里显式 `agent.tenant_id == user.tenant_id`（`:30-36`、`:204-206`、`:292`）逐处校验。
- **对照结论**：这是 Clawith 最值得深思的一块。bisheng 的**「ORM 事件自动注入 WHERE tenant_id」**把「忘了过滤」从「每次写查询都要记得」变成「默认就有、漏了才显式 bypass」——**隔离默认开启、跨租户是逃生门而非常态**。Clawith 目前靠 `permissions.py` 逐点校验 + 模型 `tenant_id` 外键，**存在「新增一条查询忘写过滤 → 静默跨租户」的同类风险**（bisheng 的 `_TENANT_AWARE_MODEL_MODULES` 正是为这类事故打的补丁）。**bisheng 的 `strict_tenant_filter` / `visible_tenant_ids` IN-list / 双层 admin 视角**，对 Clawith 的「租户配额统计是否多算」同样有直接参考价值。OpenFGA 整套（权限金字塔 + 层级继承 + 跨租户共享）是 Clawith 目前没有的细粒度授权能力——Clawith 的 `approval_requests` 是「操作审批」，bisheng 的 OpenFGA 是「资源级读写授权」，二者正交，可评估引入后者作为 RBAC 之上的资源授权层。

---

## 2. 知识库与 RAG 检索链

### 2.1 双向量库 + 权限校验检索

- **Milvus + Elasticsearch 双向量库**：`KnowledgeRag`（`bisheng/knowledge/domain/knowledge_rag.py:22`）的 `get_multi_knowledge_vectorstore()`（`:265-312`）按 `knowledge_id` 同时初始化 Milvus（向量）与 ES（关键字/全文）两个 store，返回 `{knowledge_id: {knowledge, milvus, es}}`；工厂分 `MilvusFactory` / `ElasticsearchFactory`（`bisheng/knowledge/rag/`）。
- **检索前权限校验**：`_aget_usable_knowledge()`（`:27-55`）在取向量库**之前**先用 `KnowledgePermissionService.get_knowledge_action_map_async(login_user, ids, ["use"])` 过滤出当前用户有 `use` 权限的知识库——**权限不过关的知识库根本不会进检索链**，而不是检索完再过滤（后者会泄漏存在性）。
- **Root 共享知识跨租户检索**：`aexpand_with_root_shared()`（`:68-119`）把「Root 已分享（`is_shared=1`）的知识 id」追加到 Child 的检索 id 列表，实现「Child 自己的集合 + Root 分享的集合」并集跨库检索；内部用 `bypass_tenant_filter()` 直接查 `knowledge WHERE tenant_id=1 AND is_shared=1`（`:121-141`）。
- **文件管线**：`bisheng/knowledge/rag/` 下 `base_file_pipeline.py` / `knowledge_file_pipeline.py` / `preview_file_pipeline.py` / `temp_file_pipeline.py` / `version_filter.py`——文档解析走独立管线（`pymupdf`、`easyofd`、`pptx2md` 等，README 强调「高精度文档解析模型」是卖点）。

### 2.2 对 Clawith

- Clawith 当前**没有通用 RAG 知识库**——检索靠 agent 工具（`agent_tool_executions`）或外部数据源，知识以 skills（`Skill`+`SkillFile`，`backend/app/models/skill.py`）和自进化记忆（`focus`/`experience`/`experience_reference` 模型）形态存在，非「文档向量库」形态。
- **对照结论**：若 Clawith 未来要做「企业知识库检索」，「**检索前按权限过滤 knowledge_ids 再建 store**」的顺序（先鉴权后检索）是必须抄的——它能避免两类 bug：① 无权知识库被检索到（越权）② 无权知识库的**存在性**被时序侧信道泄漏。bisheng 的「Milvus 向量 + ES 全文**双通道**」也值得参考（Clawith 若做检索，向量召回之外补一路关键字召回能显著改善「数字/编号/专有名词」类查询）。

---

## 3. 应用/工作流编排（assistant → flow 的抽象）

### 3.1 一切皆 Flow

- **`FlowType` 六态**（`bisheng/database/models/flow.py:33-39`）：`ASSISTANT=5 / WORKFLOW=10 / WORKSTATION=15 / LINSIGHT=20 / CHANNEL_ARTICLE=25 / KNOWLEDGE_SPACE=30`。**assistant、workflow、workstation、linsight（灵感模式）都是同一个 `Flow` 表的一行**，靠 `flow_type` 区分，图数据塞在 `Flow.data` 的 JSON 列（`flow.py:98-100`）。这是「assistant→flow」抽象的落点：**助手只是 flow 的一个子类型**，共享同一套 tenant/权限/分享/版本机制（`flow_version` 表）。
- **助手 = LLM + tools + prompt + agent_executor 类型**：`AssistantBase`（`bisheng/database/models/assistant.py:15-70`）含 `system_prompt/prompt/model_name/temperature/max_token`；`AssistantLinkBase`（`:70-...`）是关联表，把 `assistant_id → tool_id / flow_id(技能) / knowledge_id` 多对多连起来——**助手的能力（工具/知识/子流程）都是外挂链接，而非内嵌**。
- **助手运行体**：`ConfigurableAssistant(RunnableBinding)`（`bisheng_langchain/gpts/assistant.py:17-53`）从 `bisheng_langchain.gpts.agent_types.{agent_executor_type}` 动态 import executor（如 `get_react_agent_executor`），`BishengAssistant`（`:56-127`）从 YAML 读配置拼装 LLM + tools + prompt。这是**老的 LangChain agent executor 模式**（ReAct 等），与 Clawith 的 LangGraph 自主 agent 是两代东西。

### 3.2 LangGraph 只当「拓扑调度器」，状态在自研 sidecar

这是 bisheng 最巧妙的架构决策：

- **`TempState` 是个空壳**：`class TempState(TypedDict): flag: Annotated[bool, operator.and_]`（`bisheng/workflow/graph/graph_engine.py:22-24`）——**LangGraph 的原生 state channel 完全不承载业务状态**，只用来驱动节点拓扑流转。
- **真实状态在 `GraphState` sidecar**（`bisheng/workflow/graph/graph_state.py:8-15`）：一个 pydantic 对象，持 `history_memory`（`ConversationBufferWindowMemory` 对话历史）+ `variables_pool: {node_id: {key: value}}` 全局变量池。节点读写走 `get_variable_by_str('node_id.key#index')`（`:56-81`）。
- **节点抽象**：`NodeType` 13 种（`bisheng/workflow/common/node.py:8-25`）——`start/end/input/output/agent/code/condition/llm/qa_retriever/rag/report/tool/knowledge_retriever/note`，`NodeFactory`（`bisheng/workflow/nodes/node_manage.py:33-43`）按 type 实例化。`BaseNode`（`bisheng/workflow/nodes/base.py:20-83`）统一 `run/arun/handle_input/route_node/stop` 契约，`run()`（`:208-248`）先查 `stop_flag` 与 `max_steps` 熔断，执行后把结果写回 `graph_state.variables_pool`，首尾发 `on_node_start/on_node_end` 回调。
- **人机协同（HITL）**：`build_nodes()` 里 `self.graph_builder.compile(checkpointer=MemorySaver(), interrupt_before=interrupt_nodes)`（`graph_engine.py:287`）——`input/output` 节点前挂起；用户提交后 `continue_run(data)`（`:333-365`）把输入塞回节点 `handle_input()` 再 `_run(None)` 续跑。`judge_status()`（`:367-393`）通过 `graph.get_state().next` 判断是「跑完」还是「等待输入」。`WorkflowStatus` 六态（`bisheng/workflow/common/workflow.py:4-10`）：`WAITING/RUNNING/SUCCESS/FAILED/INPUT/INPUT_OVER`。
- **并行/循环/批处理**：fan-in 节点「等所有前驱跑完」由 `parse_fan_in_node()`（`graph_engine.py:139-184`）用**层级（node_level）+ 互斥分支判定**推导；README 卖点「画个圈就是循环、对齐就是并行」其实落在这套 node_level 标记 + conditional edge 机制上。

### 3.3 对 Clawith

- Clawith 直接**原生使用 LangGraph**：checkpoint 存执行态（`AsyncPostgresSaver`），状态是 LangGraph 的 TypedDict channel，事件走 `agent_run_events` 流；多 run 靠 `agent_runs`/`agent_run_commands` 台账 + checkpoint，而非 bisheng 的「自研 sidecar + MemorySaver」。
- **对照结论**：两者 LangGraph 用法分处两个极端——Clawith 是「**把 LangGraph 当运行时内核**」（状态/checkpoint/持久化全托管），bisheng 是「**把 LangGraph 当纯 DAG 调度器**」（状态全在自己手里，checkpointer 只用于 interrupt 语义）。bisheng 的 `MemorySaver` + sidecar 意味着**进程挂了状态就丢**（靠外层持久化兜底），Clawith 的 `AsyncPostgresSaver` 意味着**进程挂了可跨进程恢复**。**Clawith 更硬、bisheng 更轻**。可借鉴的不是「退回 sidecar」，而是 bisheng 的两点设计：① **`FlowType` 统一应用抽象**——assistant/工作流/灵感模式共用一张表、共享 tenant/权限/版本，Clawith 若未来加「可视化流程」可复用这一「一切皆 Flow」思路；② **`max_steps` 节点级熔断 + `stop_flag` 协作式停止**（`base.py:213-216`）比 Clawith 的图级 `recursion_limit` 更细粒度。

---

## 4. 模型接入层

### 4.1 LLMServer / LLMModel 双层 + 五类运行时封装

- **模型 = 服务商 + 模型实例**：`LLMServer`（模型服务商，存 `config` 含 api_key/base_url）与 `LLMModel`（具体模型，`model_type` 区分 LLM/EMBEDDING/ASR/TTS/RERANK）。`LLMService`（`bisheng/llm/domain/services/llm.py:172`）是统一入口，`get_bisheng_llm/embedding/asr/tts/rerank`（`:1216-1242`）各自返回 LangChain 的 `BaseChatModel/Embeddings/BaseDocumentCompressor` 封装（`BishengLLM/BishengEmbedding/...`，`bisheng/llm/domain/llm/`）。
- **租户级系统模型配置（F022）**：`TenantSystemModelConfigDao` + 5 类 typed config（`KnowledgeLLMConfig/AssistantLLMConfig/EvaluationLLMConfig/WorkflowLLMConfig/WorkbenchModelConfig`），按 `ConfigKeyEnum` 存每租户默认模型；`_resolve_tenant_id()`（`:77-96`）优先级「显式参数 > admin-scope ContextVar > Root 兜底」，Celery worker 漏传 tenant_id 时**记 warning 而非静默错配**（INV-T18）。
- **模型探针**：`test_model_status()`（`:733-758`）用一个真实小调用（`ainvoke("hello")`，30s 超时）验证模型可用性，成功/失败都写 status；`_invoke_model_probe()`（`:760-781`）按 model_type 发最小的探测调用。`add_llm_server`（`:616-697`）会逐模型初始化、失败的模型回滚删除。
- **跨模块调用鉴权**：`get_model_for_call()`（`:538-555`）——模型不在当前租户可见范围时，bypass 重查并走 `aget_shared_server_ids_for_leaf()` 判断「Root 是否分享给本叶子」，不通过抛 19802。
- **凭证卫生**：`strip_config_whitespace()`（`:187-200`）去 `*_key/*_secret/*_base/*_url` 字段首尾空白（防 "Bearer sk-xxx " 挂 header）；`_llm_api_key_hash()`（`:99-107`）只把 key 的 sha256 前 16 位写审计日志。

### 4.2 对 Clawith

- Clawith 的模型层是 `backend/app/services/llm/`（`caller.py` 统一 LLM 调用 + failover、`model_resolution.py` 的 `active_agent_model_candidates`、`client.py` 自定义 httpx 客户端），模型存 `llm` 模型 + `agent_credential`，**租户默认模型**靠 `Tenant.default_model_id`（`tenant.py`）单字段。
- **对照结论**：bisheng 的「**LLMServer(服务商) / LLMModel(实例) 双层 + per-tenant typed config + 模型探针**」对 Clawith 有两点可借鉴：① **模型可用性探针**——Clawith 目前靠运行时 failover（`caller.py`），缺「预先探测 + 状态落库」的治理视图，bisheng 的 `test_model_status` 是现成的样板；② **API key 指纹审计**（只记 sha256 前 16 位）是 Clawith 审计日志（`audit.py`）可抄的脱敏手法。

---

## 5. 技能/工具挂载

### 5.1 工具注册表 + MCP 客户端

- **工具 = `tool_key` 注册表**：`load_tools()`（`bisheng_langchain/gpts/load_tools.py:164-213`）按名字分派到 5 张表：`_BASE_TOOLS`（get_current_time/calculator/arxiv，`:50-54`）、`_LLM_TOOLS`、`_EXTRA_LLM_TOOLS`、`_EXTRA_PARAM_TOOLS`（dalle/bing_search/code_interpreter/bisheng_rag/sql_agent/web_search，`:116-129`）、`_API_TOOLS`（含 LocalFileTool 的 list_files/read_text_file 等 7 个文件操作工具，`:133-141`）。工具元数据存 `t_gpts_tools` 表（`get_tool_table()` 直连 MySQL，`:221-260`）。
- **MCP 客户端**：`ClientManager`（`bisheng/mcp_manage/manager.py:10-60`）支持 `SSE/STDIO/STREAMABLE` 三种传输（`clients/sse.py`/`stdio.py`/`streamable.py`），`parse_mcp_client_type()`（`:12-30`）从 `mcpServers` JSON 自动判型（有 `command` 判 stdio，否则 sse）。
- **AGL 内置技能**：`bisheng/linsight/builtin_skills/` 下 `bisheng-docx/pptx/xlsx` 三个内置技能——这是 bisheng 主推的 **AGL（Agent Guidance Language）** 通用 Agent 的技能形态，把领域专家的偏好/经验/业务逻辑嵌入 agent（README Feature 1）。linsight 的 worker 用 **Redis 队列 park-and-release**（`bisheng/linsight/worker.py:41-67`）：`resume=True` 的条目携带用户答复、`lpush` 到队头抢跑；`continue_question`（F035 多轮）作为新 HumanMessage 喂进**同一个 `thread_id = session_version_id`** 延续上下文。

### 5.2 对 Clawith

- Clawith 的 skills 是 `Skill`+`SkillFile` 全局注册表（`backend/app/models/skill.py`，`folder_name` 存目录、`SKILL.md` 全文存 DB，`tenant_id` 可空表示「全局 vs 租户级」），`_load_skills_index()` 注入 context 走渐进披露（`agent_context.py`）；工具走 `tool.py` 模型 + `agent_tool_executions` 台账；Clawith 无 MCP 客户端、无 bisheng 那种「可视化流程里的 tool 节点」。
- **对照结论**：三者对比——bisheng 的 `tool_key` 注册表、Clawith 的 skills 索引、orca 的 SKILL.md stub 是三种「能力挂载」范式。**bisheng 的 MCP `ClientManager`（SSE/STDIO/STREAMABLE 三传输 + 自动判型）**对 Clawith 最直接可迁移：Clawith 若要接外部 MCP 工具，这套「从 `mcpServers` JSON 判型 → 建客户端」是现成样板（本 Agent 自己的 MCP 就是 `system_config.yaml` 的 `tools.mcpServers`，与 bisheng 的 `mcpServers` JSON 结构同源）。**linsight 的「多轮继续 = 同一 thread 新消息」**与 Clawith 的 run 续跑语义一致，可对照验证。

---

## 6. 部署形态（docker/组件化）

- **11 组件**（`docker/docker-compose.yml`）：`mysql`（8.0，含 healthcheck 预建 `openfga` 库）、`openfga-migrate` + `openfga`（v1.15.1 版本钉死，distroless 无 /bin/sh 故不写 HEALTHCHECK，靠 `service_started`）、`redis`（7.0.4）、`backend`（`entrypoint.sh api`，端口 7860，挂 `config.yaml`）、`backend_worker`（`entrypoint.sh worker` 跑 **Celery** 异步任务）、`frontend`（nginx，3001）、`elasticsearch`（8.12.0）、`etcd` + `minio` + `milvus`（standalone 2.5.10 三件套）。
- **API/worker 分离**：`backend`（FastAPI 同步 API）与 `backend_worker`（Celery worker 处理耗时任务）是同一镜像两个 entrypoint——长任务（文档解析、向量化、工作流异步执行）从 API 进程剥离。这与 Clawith 的 `RuntimeCommandDaemon`（命令消费循环）理念同源，但 bisheng 用成熟的 Celery 而非自研 daemon。
- **可观测**：OpenFGA 开 `CHECK_QUERY_CACHE/ITERATOR_CACHE` 调优、`METRICS_ENABLE_RPC_HISTOGRAMS`；`docs/observability/` 有专门文档目录。

### 6.1 对 Clawith

- Clawith 是**单仓库单后端镜像**（FastAPI 长连 + `RuntimeCommandDaemon` 领命令 + bwrap 沙箱），组件数比 bisheng 少（PG/Redis/Langfuse），但**没有 Celery**——异步执行靠 LangGraph 图 + 后台 service（task executor/scheduler/heartbeat）。
- **对照结论**：bisheng 的「**Celery 把耗时任务从 API 进程剥离**」是成熟做法，Clawith 的自研 daemon 更贴合「命令租约 + 心跳」语义（不需要换）。真正可借鉴的是 **`openfga` 作为独立授权服务的部署范式**（配 migrate 一次性任务 + datastore 复用 MySQL + 缓存调优）——若 Clawith 引入细粒度授权，照这个 compose 就能落地。

---

## 7. 计量与可观测（token_tracker 直接对标）

bisheng 的 token 计量与 Clawith 的 token_tracker 是**同一命题的两种记账模型**，值得单列：

- **bisheng：逐调用事件日志（event log）**。`LLMTokenTracker.record_usage()`（`bisheng/llm/domain/services/token_tracker.py:37-71`）**每笔 LLM 调用写一行 `llm_token_log`**，`tenant_id = get_current_tenant_id()`（叶子租户，非模型租户——AC-09 语义）；**无租户上下文时拒绝落库**（抛 `TenantContextMissingError`，`:52-54`）。`record_usage_sync()`（`:73-96`）对 FGA/DB 抖动「记 warning 返回 None」不阻断用户调用，但对「缺上下文」仍 raise（那是框架 bug 要暴露）。配套 `ModelCallLogger` 写 `llm_call_log`（成功/失败 + 时延 + endpoint）。挂载点 `LLMUsageCallbackHandler.on_llm_end`（`bisheng/workflow/callback/llm_usage_callback.py:72-132`）：LangChain callback 里**并发**发 token + call 两笔 INSERT，并「hop 到共享 bridge loop」防「sync invoke 的 throwaway loop 毒化全局 async 连接池」（`:109-117` docstring + `run_on_bridge_loop`）。
- **Clawith：逐 agent 滚动计数 + 日汇总（aggregate）**。`record_token_usage()`（`backend/app/services/token_tracker.py:258-347`）把 token 累加到 `Agent.tokens_used_today/month/total` + `cache_read/creation/miss` 各维度，再 `on_conflict_do_update` upsert 进 `DailyTokenUsage`（`agent_id+date` 唯一，带 `tenant_id`，`:326-347`）。
- **对照结论**：bisheng「**逐调用事件行**」可做任意维度事后聚合（按模型/按会话/按租户/按时延），代价是行数随调用量线性涨；Clawith「**逐 agent 滚动计数 + 日粒**」省空间、读快，但**丢失了「单笔调用」的可追溯性**（想知道某次调用花了多少 token 得回 Langfuse/LLM usage）。**两者互补**：Clawith 若要在「计量精确性」与「可追溯性」间补一条「关键调用的事件日志」（如超长调用、cache_miss 异常调用），bisheng 的 `llm_token_log + llm_call_log` 双表 + callback 挂载是现成范本；bisheng 的「**缺租户上下文即拒绝落库**」原则（宁可暴露 bug 不静默错配）也值得 Clawith 的 `record_token_usage` 借鉴（当前 `agent` 查不到就静默跳过 `:295`）。

---

## 8. Clawith 侧对照汇总（工具核实）

| bisheng 机制 | bisheng 文件:行 | Clawith 对标 | Clawith 文件:行 |
|---|---|---|---|
| ContextVar 租户上下文 + SQLAlchemy 事件自动注入 WHERE | `tenant.py:44-115`、`tenant_filter.py:143-234` | 显式 tenant_id FK + `permissions.py` 逐点校验 | `models/tenant.py`、`core/permissions.py:30-36` |
| `visible_tenant_ids` IN-list + `strict_tenant_filter` | `tenant.py:58-61`、`tenant.py:167-179` | 无（单租户视角） | — |
| OpenFGA 16 类型 + 权限金字塔 + 跨租户共享 | `authorization_model.py:167-298` | 无细粒度资源授权（仅 approval 审批流） | — |
| 检索前按权限过滤 knowledge_ids | `knowledge_rag.py:27-55` | 无通用 RAG | — |
| 一切皆 Flow（FlowType 六态 + Flow.data JSON） | `models/flow.py:33-39`、`:98-100` | agent/run 分离，无统一「应用」抽象 | `models/agent.py` |
| LangGraph 只当拓扑调度器 + GraphState sidecar + MemorySaver | `graph_engine.py:22-24`、`:287` | LangGraph 原生 checkpoint（AsyncPostgresSaver） | — |
| HITL interrupt_before + continue_run | `graph_engine.py:287`、`:333-365` | run 生命周期 + 命令续跑 | `agent_runtime/` |
| 节点级 max_steps 熔断 + stop_flag | `nodes/base.py:213-216`、`:253-254` | 图级 recursion_limit | — |
| LLMServer/LLMModel 双层 + 模型探针 | `services/llm.py:616-781` | llm 模型 + `caller.py` failover | `services/llm/caller.py` |
| 逐调用 token 事件日志（llm_token_log + llm_call_log） | `token_tracker.py:37-96`、`llm_usage_callback.py:72-132` | 逐 agent 滚动计数 + DailyTokenUsage 日汇总 | `services/token_tracker.py:258-347` |
| 工具 tool_key 注册表 + MCP ClientManager | `load_tools.py:164-213`、`mcp_manage/manager.py:10-60` | skill 索引 + tool 台账，无 MCP 客户端 | `models/skill.py` |
| API/Celery worker 分离 | `docker-compose.yml`（backend + backend_worker） | RuntimeCommandDaemon 自研消费循环 | `services/agent_runtime/` |

---

## 9. 可迁移点 → Clawith 映射

| # | bisheng 机制（文件） | Clawith 对标点 | 可借鉴要点 |
|---|---|---|---|
| 1 | SQLAlchemy 事件自动租户隔离（`tenant_filter.py:143-234`） | 显式 FK + permissions.py 逐点校验 | 隔离默认开启、跨租户才 bypass；新增查询「忘了过滤」不再静默泄漏 |
| 2 | `_TENANT_AWARE_MODEL_MODULES` 强制 import 防静默泄漏（`tenant_filter.py:39-102`） | 无 | 模型未入 import 链 → 静默写错租户的教训，值得做成启动自检 |
| 3 | `strict_tenant_filter` / `visible_tenant_ids` 区分「多算/精确」（`tenant.py:58-61`、`:167-179`） | 租户配额统计 | 配额计数用严格相等，避免 IN-list 把 Root 资源算到 Child |
| 4 | OpenFGA 权限金字塔 + 层级继承 + 跨租户共享（`authorization_model.py:46-298`） | approval_requests 审批流 | 资源级读写授权与操作审批正交，可评估作为 RBAC 之上的授权层 |
| 5 | 检索前按权限过滤 knowledge_ids（`knowledge_rag.py:27-55`） | 无通用 RAG | 先鉴权后检索，杜绝越权读取 + 存在性侧信道 |
| 6 | Milvus + ES 双通道检索（`knowledge_rag.py:265-312`） | 无 | 向量召回 + 关键字召回的互补召回 |
| 7 | 模型探针 test_model_status + 状态落库（`services/llm.py:733-781`） | caller.py 运行时 failover | 预探测 + 治理视图，补运行时 failover 的盲区 |
| 8 | API key sha256 指纹入审计（`services/llm.py:99-107`） | audit.py | 只记指纹不记明文，审计可追溯且不泄密 |
| 9 | 逐调用 token 事件日志（`token_tracker.py:37-96`、`llm_usage_callback.py:72-132`） | 逐 agent 滚动计数 | 关键调用补事件行，补「单笔调用可追溯性」；缺租户上下文即拒绝落库 |
| 10 | MCP ClientManager（SSE/STDIO/STREAMABLE + 自动判型）（`mcp_manage/manager.py:10-60`） | 无 MCP 客户端 | 接外部 MCP 工具的现成样板 |
| 11 | 一切皆 Flow 的应用抽象（`models/flow.py:33-39`） | agent/run 分离 | 未来加「可视化流程」时复用统一应用抽象（共享 tenant/权限/版本） |
| 12 | 节点级 max_steps + stop_flag 协作式停止（`nodes/base.py:208-254`） | 图级 recursion_limit | 更细粒度的执行熔断与停止 |

---

## 10. 局限 / 不可照搬点（诚实记录）

- **产品定位分水岭**：bisheng 是「**无代码搭应用**」的 DevOps 平台（画布 + 助手 + RAG），Clawith 是「**跑自治 agent**」的运行时（run 生命周期 + 压缩 + 计量 + 自进化）。bisheng 的**可视化画布、LangChain agent_executor（ReAct 旧范式）、文档解析管线**是「应用建设」能力，与 Clawith 的「agent 运行」诉求**方向不同，不能照搬**——Clawith 缺的是 bisheng 的「企业底座」（租户隔离/细粒度授权/模型治理），而不是它的「搭应用」层。
- **LangGraph 用法相反（不可搬）**：bisheng 的「`TempState` 空壳 + `GraphState` sidecar + `MemorySaver`」意味着**状态不跨进程持久、进程死即丢**（靠外层 Celery/队列兜底）。Clawith 的「原生 checkpoint + `AsyncPostgresSaver`」是**跨进程可恢复**的更强语义——**这条不要学 bisheng 退回 sidecar**；bisheng 该方案是「轻量无代码工作流」的取舍，不是「生产 agent 运行时」的答案。
- **LangChain 旧栈包袱**：`bisheng_langchain/gpts/`（agent_types/ReAct executor、`langchain-classic` 依赖、YAML 驱动助手）是**上一代 LangChain agent 范式**，与 Clawith 的 LangGraph 自主 agent + 工具调用台账不是一代东西，工具挂载的「`tool_key` 注册表 + YAML 拼装」这类模式**只作参考，不照搬**。
- **本次未深入**：`src/frontend/`（`client/` + `platform/` 画布交互层，未逐文件读）、`bisheng/finetune/`（模型微调）、`bisheng/workstation/`（工作台）、`bisheng/channel/`（飞书/企微等渠道）、`bisheng_langchain/gpts/tools/` 全部 54 个工具实现细节、`docs/architecture/` 与 `docs/PRD/` 的设计文档（本次只据代码 + 注释推断，未逐篇读 PRD）。
- **浅克隆限制**：HEAD `2456ec1`（`--depth 1`）无完整历史，无法追踪「为何选 ContextVar 事件注入而非每查询显式过滤」「为何 OpenFGA 而非自研 RBAC」等设计决策的演进，仅能从代码注释里的 F0xx/INV-Txx 事故编号反推（代码里大量 `F013/F017/F019/F022/F041/INV-T18/AC-09` 等 feature/incident 编号，说明它有一套 `features/` 目录 + 需求/事故驱动开发流程，`features/v3.0.0-beta1/` 目录印证）。
- **行号精度**：所有 bisheng/Clawith 行号均经 `read_file` 核实（截至 2026-09-05 工作树）；bisheng 上游仍在快速迭代（README 已到 v3.0.0-beta1），行号随版本漂移。

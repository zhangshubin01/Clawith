# LangGraph BigTool 工具检索源码研究报告

日期：2026-09-03
状态：**完成**（分析基于本地仓库 `/Users/shubinzhang/Documents/UGit/langgraph-bigtool` HEAD `0bb7f9227d349afa4d4207c6630e800658c80894`，浅克隆、仅 1 个 commit，与 origin/main 同步）
定位：参考资料研究，非实现方案。对照 Clawith 工具 schema 规模（实测 59–214 个、约 90% 未使用，工具 schema 为 DeepSeek 前缀缓存 cache-HIT 部分，动态块不切断工具缓存，见 [[deepseek-cache-tool-schema-facts]]）与 DeepSeek 前缀缓存。

## 0. 项目概览

- **是什么**：LangChain 官方 `langgraph-bigtool`——为 LangGraph agent 接入「成百上千个工具」的最小库。核心理念：agent 不一次性拿到全部工具 schema，而是**先只绑定一个检索工具，按需从注册表检索出少量工具再绑定**。
- **体量**：极小。核心源码仅 3 个 Python 文件：`langgraph_bigtool/graph.py`（178 行）、`tools.py`（84 行）、`utils.py`（39 行），版本 `0.0.3`，仅依赖 `langgraph>=0.3.0`（`pyproject.toml`）。README 约 214 行，quickstart 用 `math` 库全部约 50 个函数演示。
- **检索底座**：复用 LangGraph 长程 memory Store（`BaseStore`）及其内建**语义检索索引**（embedding），而非自建 BM25/向量库。
- **与 Clawith 的对标关系**：两者痛点同构——工具太多撑爆上下文/前缀缓存。区别：bigtool 是「运行时按需检索」的**单 agent ReAct 图**（无多 agent 协作），Clawith 是「全量绑定 + 字节稳定排序优化前缀缓存」的多租户平台。看的是 bigtool 的**按需收缩思路**，但要评估其与 Clawith 前缀缓存策略的冲突（见 §5、§7）。

## 1. 架构总览：单 ReAct 图 + `select_tools` 检索节点（并非多 agent）

⚠️ **先纠偏**：任务假设存在「tool builder agent / tool retriever agent 多 agent 分工」，本仓库**不存在**。全库 grep `builder agent`/`retriever agent`/`tool builder`/`subagent` 零命中。这是一个**单 agent 图**，检索动作由一个普通 function node（非 LLM 子代理）完成：

`create_agent()`（`langgraph_bigtool/graph.py:45`）构建 `StateGraph`，共 3 个节点、4 条边：

| 节点 | 实现 | 职责 |
|---|---|---|
| `agent` | `call_model`/`acall_model`（`graph.py:85`/`91`） | ReAct 主循环，`llm.bind_tools([retrieve_tools, *selected_tools])` 调用模型 |
| `select_tools` | `select_tools`/`aselect_tools`（`graph.py:101`/`115`） | 执行检索函数，把结果写回 state 的 `selected_tool_ids` |
| `tools` | `ToolNode`（`graph.py:99`） | 执行已选工具的实参，标准工具执行 |

路由 `should_continue`（`graph.py:129-148`）：末条消息无 `tool_calls` → `END`；否则对每个 call，若 `call["name"] == retrieve_tools.name` 用 `Send("select_tools", ...)` 扇出到检索节点，否则 `Send("tools", ...)` 扇出到工具执行（并用 `ToolNode._inject_tool_args` 注入 store）。边：`tools→agent`、`select_tools→agent`（`graph.py:175-176`）。

关键 state 扩展（`graph.py:21-22`）：

```python
class State(MessagesState):
    selected_tool_ids: Annotated[list[str], _add_new]
```

`_add_new`（`graph.py:16-18`）是 reducer：**只增不减地累积**已选工具 ID，跨步去重。这意味着一个 run 内工具集单调增长（见 §5 局限）。

## 2. 工具目录构建：LangGraph Store 语义索引（非 BM25/混合）

目录即 `tool_registry`（`dict[uuid → tool]`）+ Store 索引条目。两条路径：

- **注册表**：调用方建 `{str(uuid.uuid4()): tool}`（README:73-78），`create_agent(llm, tool_registry)` 接收。
- **索引**：对每个 `tool_id` 执行 `store.put(("tools",), tool_id, {"description": f"{tool.name}: {tool.description}"})`（README:91-98）。Store 索引配置（README:84-90）：

```python
store = InMemoryStore(index={
    "embed": embeddings,   # openai:text-embedding-3-small
    "dims": 1536,
    "fields": ["description"],   # 只索引 description 字段
})
```

**结论**：默认检索是纯 **embedding 语义搜索**（`store.search` 对 `fields=["description"]` 做向量近邻），无 BM25、无混合检索。`namespace_prefix=("tools",)`（`graph.py:51`）限定命名空间，`filter`（`graph.py:50`）支持按 key-value 过滤。唯一的「非语义」扩展点是自定义检索函数（§4），可借此接 BM25/分类/图谱等任意逻辑——README 明示「不一定非得语义搜索」（README:175-194）。

## 3. 检索触发时机：模型驱动、按需、增量

- **起点**：agent 首步**只绑定 `retrieve_tools` 一个工具**（`graph.py:87` 的 `[retrieve_tools, *selected_tools]`，初始 `selected_tools=[]`）。首请求 tools 字段极小。
- **触发**：不是预取、不是每步都检。**由模型决定何时**调 `retrieve_tools(query=...)`——当它需要尚未绑定的工具时。默认检索函数 `retrieve_tools`/`aretrieve_tools`（`langgraph_bigtool/tools.py:22-48`）对 `store.search(namespace_prefix, query=query, limit=limit, filter=filter)` 取 `limit`（默认 **2**，`graph.py:49`）个，返回 `[result.key for result in results]`（tools.py:34）。
- **增量累积**：每轮检索出的工具通过 `_add_new` 并入 `selected_tool_ids`，下一轮 `bind_tools` 即携带「`retrieve_tools` + 迄今所有已选工具」，直到任务完成不再调检索。

即：`limit` 是**每次检索的步长上限**，而非工具总数上限——多轮检索会逐步逼近更大工具集。

## 4. 检索结果如何注入 prompt：bind_tools 绑 schema + ToolMessage 只回名字

两条独立通道，务必区分：

1. **`ToolMessage(f"Available tools: {tool_names}", tool_call_id=...)`**（`_format_selected_tools`，`graph.py:25-42`，正文在第 38 行）：只把**工具名列表**作为普通文本消息喂给模型，充当「本轮可见工具清单」的提示。**不是 schema**。
2. **真正的 schema 注入走 `llm.bind_tools([retrieve_tools, *selected_tools])`**（`graph.py:87`）：被选中的工具以**完整 OpenAI function schema（含 parameters）**进入下一次请求的 `tools` 字段（LangChain `bind_tools` 语义），这才是模型实际可调用的部分。

因此 bigtool 的上下文收缩只作用于 `tools` 字段：首步 1 个 schema，之后逐步 +`limit` 个，而非一次塞 50/200 个。`select_tools` 节点（`graph.py:101-113`）内部：把 store 注入检索函数的 `store` 实参（`get_store_arg`，`tools.py:66-84`，靠 `Annotated[BaseStore, InjectedStore]` 类型标记识别注入参数名），对每个 `tool_call` 执行 `retrieve_tools.invoke(kwargs)`，结果按 `tool_call["id"]` 分组回写。

自定义检索（README:145-173）：传 `retrieve_tools_function`/`retrieve_tools_coroutine` 覆盖默认语义搜索；函数返回 `list[str]`（工具 ID）。函数参数用类型标注（含 `Literal`）即可让 LLM 决定检索实参形状（README:195-197）——例如按 `category: Literal["billing","service"]` 分类返回。

## 5. 目录更新与缓存策略：库内几乎为零

- **无跨 run 缓存**：检索结果不落盘；`selected_tool_ids` 只存在于单次图执行的内存/checkpoint state，run 结束即弃。
- **目录更新**：完全由外部负责——调用方 `store.put`（README:91-98）增删条目；库内无失效/刷新/版本机制。
- **唯一的「缓存」是 reducer 去重**：`_add_new` 保证同一工具不重复绑定（`test_duplicate_tools` 断言 `bind_tools` 收到的工具名无重复，`tests/unit_tests/test_end_to_end.py:340-403`）。
- Store 后端：README 支持 `InMemoryStore` 与 `PostgresStore`（`langgraph.store.postgres.PostgresStore`），语义检索的向量索引由 Store 层实现，库本身不碰。

## 6. 对上下文 / token 成本 / 前缀缓存的影响（Clawith 最关注的一节）

**bigtool 侧**：
- 上下文：首步 tools 字段 ≈ 1 个检索工具 schema；每轮 +`limit` 个全 schema。对 50 工具场景，模型不再一次性收到 50 个 schema，而是按需 1→3→5…递增。
- **代价是前缀缓存的字节不稳定**：`selected_tool_ids` 只增不减（§1 `_add_new`），所以**每一轮 `bind_tools` 的 tools 字段都在变**（严格说工具集改变时变）。在 OpenAI/DeepSeek 前缀缓存里，`tools` 字段位于 messages 之前的稳定前缀区，工具集一变，前缀从该处起全部失效 → **每个检索轮都 cache MISS**。这正是 bigtool「为省上下文而牺牲前缀缓存」的结构性取舍。
- 无工具集收缩/淘汰：长任务里工具集单调膨胀，最坏退化为接近全量。

**对照 Clawith（以下均已用 codebase-memory + read_file 核实，非凭记忆）**：

Clawith 走的是**与 bigtool 相反**的优化方向——「全量绑定 + 字节稳定排序保前缀缓存」：

- 工具 schema 构造：`get_agent_tools_for_llm(agent_id)`（`backend/app/services/agent_tools.py:1016`）从 DB（`Tool` + `AgentTool`）加载 enabled 工具，拼 OpenAI function 格式 `{"type":"function","function":{"name","description","parameters"}}`（`agent_tools.py:1141-1148`），经 `_canonicalize_llm_tool`（1149）规范化；重名去重守卫（1150-1166，防 LLM 400「Tool names must be unique」）。`get_runtime_agent_tools_for_llm`（`agent_tools.py:1422`）再叠加 execute_code 超时 schema patch、动态 MCP 绑定、typed-outcome 门控。
- **字节稳定排序是刻意设计**：`order_by(Tool.name)` 处注释明写「Stable name ordering keeps the tool-schema prefix byte-identical across calls — a requirement for provider KV-cache hits」（`agent_tools.py:1078-1081`）。
- 工具注入请求：`caller.py:535-545` 解析 `tools_for_llm`（过滤 `finish` 控制工具），`caller.py:685` 以 `tools=tools_for_llm` 传入 `client.stream`；`client.py:650` 落 `payload["tools"]=tools` + `tool_choice:"auto"`（652）。
- prompt 前缀缓存布局：`_prompt_messages`（`model_step_service.py:1299`）按 `[system(静态)] [history] [稳定块A] [轮内块B] [末条控制消息]` 组装，**动态块 A 是 cache break 边界**（`prefix_cache_break=True`）；`_messages_to_openai_payload`（`client.py:675`）给 system 块与边界消息打 `cache_control:{"type":"ephemeral"}`。docstring 明言「provider prefix cache keeps hitting a byte-stable prefix」。
- 解析缓存（注意：是**解析结果**的进程内 TTL 缓存，非检索）：`agent_tools_cache.py` 的 `cached_runtime_agent_tools`（`:61`）TTL 30s（`TOOL_RESOLUTION_TTL_SECONDS=30.0`，`:35`）+ in-flight 去重 + 写路径 `invalidate_agent_tool_resolution`（`:89`）。其 docstring 印证「每次 model/tool step 都重解析同一套约 60 个 tool schema」——这是 Clawith 现有痛点。

**净结论**：Clawith 当前「tools 字段是全量、但字节稳定 → 前缀 cache-HIT」；bigtool 是「tools 字段按需收缩、但字节逐轮变化 → 前缀 cache-MISS」。二者**不可兼得**，迁移必须做混合设计（见 §7）。

## 7. 可迁移点 → Clawith 映射

| # | bigtool 机制（文件） | Clawith 对标点 | 可借鉴要点 |
|---|---|---|---|
| 1 | 起始只绑 `retrieve_tools`，按需增量绑工具（`graph.py:85-97`、`tools.py:22-48`） | `get_agent_tools_for_llm` 全量返回 59–214 个 schema（`agent_tools.py:1016`）→ 撑大 `tools` 字段 | **核心思路**：把「全量绑定」改为「核心集常驻 + 长尾按需检索」。但注意与 #4 的缓存冲突 |
| 2 | 目录 = registry dict + Store 语义索引，只索引 `description` 字段（README:84-98） | 现有 `Tool.description` 已存在，可作为检索向量来源 | 用现有 description 建索引即可，无需额外元数据；可复用 Clawith 已有的 embedding/向量设施 |
| 3 | 检索函数可自定义（README:145-197，`retrieve_tools_function`） | Clawith 无工具检索，但有 `_allowed_tool_names`（`caller.py:307`）按名字过滤 | 检索后端可后接 BM25/分类/知识图谱，不锁定语义搜索；与「分类→工具」的现有 UI 同步 |
| 4 | **前缀缓存冲突**：`_add_new` 单调累积 → tools 字段逐轮变 → 前缀 cache-MISS（`graph.py:16-22`） | Clawith 靠 `order_by(Tool.name)` 保字节稳定 + `prefix_cache_break` 动态块隔离（`agent_tools.py:1078-1081`、`model_step_service.py:1299`） | **最关键的一条**：直接抄 bigtool 会击穿 DeepSeek 前缀缓存。迁移须保「核心工具集字节稳定常驻（cache-HIT 前缀）+ 长尾检索结果放轮内动态块（本就 cache-MISS）」 |
| 5 | `limit=2` 小步长 + reducer 去重（`graph.py:49`、`tools.py:34`） | 无对应；Clawith 每轮全量 | 检索步长与「本轮可见工具」语义可借鉴，但要加**淘汰/上限**防膨胀（bigtool 缺失） |
| 6 | 检索解析结果进程内 TTL 缓存（Clawith 已有 `agent_tools_cache.py:35/61/89`） | bigtool 无缓存，Clawith 反而领先 | 若引入检索，可沿用现有 TTL+in-flight+写失效缓存模式缓存「检索→工具集」结果 |
| 7 | `ToolMessage("Available tools: [...]")` 只回名字提示（`graph.py:25-42`） | Clawith finish 协议已有「tool round 只提示当前 schema 内工具」测试（`test_finish_protocol.py:957-976`） | 「可见工具清单」作为文本提示、schema 走 `tools` 字段的两通道分离，可直接沿用 |

## 8. 局限（诚实记录）

- **体量与成熟度**：0.0.3、178 行核心、单 commit 浅克隆；无 eviction/上限、无跨 run 缓存、无目录失效机制、无 BM25/混合/重排、无工具级安全策略。是「教学级参考实现」，非生产级检索系统。benchmark 类关联工作仅列在 README「Related work」（Toolshed、Graph RAG-Tool Fusion 等论文），库内无评估。
- **无多 agent**：任务预期的「tool builder / tool retriever agent」在本仓库不存在，`select_tools` 是无 LLM 的普通 function node。
- **前缀缓存是被忽略的维度**：bigtool 全程不提 prefix cache，其「逐轮变工具集」设计与 Clawith 的缓存优化直接冲突，不可直接照搬。
- **只索引 description**：检索质量受 description 质量制约；对 Clawith 长尾工具（约 90% 未用）未必能靠 description 精确召回。
- **Clawith 侧遗留**：`scripts/measure_tool_schema.py` 已失效——它 import 的 `app.services.agent_runtime.agent_tool_runtime.resolve_agent_tools` 已不存在（该文件已删，grep 零命中），现行入口是 `agent_tools.get_agent_tools_for_llm`（`agent_tools.py:1016`）。若要复测工具 schema 规模，应改用此函数。

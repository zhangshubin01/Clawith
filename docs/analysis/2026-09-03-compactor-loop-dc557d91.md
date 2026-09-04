# compactor 失忆循环实况：run dc557d91 的 62 分钟观察

- 观察日期：2026-09-03（10:06Z run 自然结束后取样）
- 观察对象：run `dc557d91`（agent 950a1943「Android 工程师 07」，direct chat 会话 b67d1138）
- 触发指令：「[Attachment: memory/memory.md] 根据你推荐的执行待办」
- 运行环境：部署 8b6f86c6（含 b47ee6c6 R3 注入补丁）；model=deepseek-v4-flash；`model_turn_limit=10000`
- 数据源：Langfuse events_full（trace `2a41a974c27f68968085367d316b38d5`）+ 后端日志 `[LLM-CacheFp]` / `[Token Cache]` 指纹
- 用途：第 2/3 层 compactor 指代消解方案的事实输入（本文件是观察记录，不是方案）

## 1. 时间线与规模

| 项 | 值 |
|---|---|
| 起止 | 09:04:04Z → 10:06:04Z（62 分钟） |
| 模型步 | 86 步（step 0→86） |
| llm GENERATION | 118 次（含 compact 摘要与 completion gate） |
| read_file | 199 次 / 仅 11 个文件 |
| edit_file | 14 次（11 次 java/com、3 次 strings.xml，最后编辑 10:04:00） |
| execute_code（编译验证） | 15 次 |
| node:compact SPAN | 89 次（节点每步经过，真实压缩按下方指纹计） |

## 2. 循环铁证

### 2.1 compact 频率：每 3–7 分钟一次

`[Token Cache] Low hit rate … ratio=100%`（压缩后第一步全 miss 的指纹）出现于：
09:36:36、09:40:04、09:44:31、09:47:25、09:53:09、10:04:11 —— **62 分钟内 ≥6 次真实压缩**。

### 2.2 压缩后重建出完全相同的起点

`[LLM-CacheFp]` 的 prefix 哈希（消息前缀指纹）在三次压缩后**完全相同**：

| step | 时刻 | tokens | prefix |
|---|---|---|---|
| 53 | 09:38:13 | 10675 | `697aef1a1281` |
| 59 | 09:44:31 | 10663 | `697aef1a1281` |
| 83 | 10:04:11 | 10467 | `697aef1a1281` |

压缩 → 摘要 → 重建出的消息序列与 6–20 步前一致 → 模型从同一状态出发，做同样的事。

### 2.3 工具调用序列跨轮重复

step 54 与 step 60 的 tool 哈希序列完全相同（`t:352a4ef09064 → t:025dcbd52d8d → t:ce910ef46c3f`），step 84 再次重复同一序列——压缩后模型重发同一批 read_file。

### 2.4 失忆自证

09:47:28（已运行 43 分钟、60+ 步）的主图输出：
「收到，按推荐顺序执行剩余待办。**先读取当前代码与文档状态，确认从哪里开始**。」

### 2.5 终局摘要自认「还在起点」

run 结束时（10:06:36）的最后一条 llm 是 compactor 摘要（8097 字符）。摘要本身质量不低（含文件行号、P1 三项、Next Step 顺序），但「Current Work」段写道：
**「All three P1 code fixes remain unapplied (authorized but not yet executed in visible history)」**
——跑了 86 步、改了 14 次文件之后，模型仍认定三项 P1 修复一个都没做。

### 2.6 摘要内部指代失真

同一份摘要内部自相矛盾：Git log 段写最新 commit 为 `9087bee`，而「Current Work」段写「`8087bee` is the latest commit」——**把 commit 哈希抄错一位**。这正是第 2/3 层「指代消解」要解决的真实实例：摘要中的数字/标识符会漂移，而下游照抄执行。

## 3. 重复劳动量化

read_file 199 次只覆盖 11 个文件，同一文件被反复读：

| 文件 | 次数 | 时间跨度 |
|---|---|---|
| Calculator.kt | 36+ | 09:09:04 → 10:06 |
| CalculatorReducer.kt | 28 | 全程 |
| CalculatorReducerTest.kt | 19 | 全程 |
| README.md | 15+ | 09:09 → 09:57 |
| memory/清单.md | 11 | 09:04:56 → 09:58:05（82 行全文） |
| CalculatorViewModel/UiState/MainActivity.kt | 各 15 | 全程 |
| strings.xml / CalculatorTest.kt | 各 14 | 全程 |

**R3 注入副作用（新发现）**：注入块标注「完整内容见 memory/清单.md」→ 模型全程读了 11 次清单全文（82 行）。清单内容每步注入上下文 → 加速膨胀 → 压缩更频繁。这为「note 改 run 内首步注入」待办提供了新的论据（每步注入的截断标注诱导全文重读）。

## 4. 环境抖动（次要因素）

- DeepSeek `RemoteProtocolError` 重试 6 次/15 分钟（09:41:30、09:43:11、09:45:48、09:49:52、09:51:15、09:55:14），均 attempt 1/4 重试成功——增加延迟，与循环正交。
- 非压缩步的 Token Cache miss 仍达 51–56%（input 3.2 万时 miss 1.7 万）——每步工具结果插入使前缀不断变化，缓存收益低。

## 5. 循环传染：下一个 run 重复同样路径

dc557d91 释放 lane 后，8ef42390（用户 09:07:47 发的「执行 [P1·文档] README 与代码脱节」）于 10:06:46 启动。其 step 1 的 tool 哈希序列 `t:ce910ef46c3f → t:352a4ef09064 → t:025dcbd52d8d` **与 dc557d91 各循环轮开头的 read_file 三连完全相同**——继承同一 thread 上下文后，接棒 run 从相同的读取路径开始。若其任务同样宽泛，预计重演相同循环（观察项，见记忆）。

## 6. 对第 2/3 层方案的事实输入

1. **压缩触发阈值与增长速率**：上下文每 3–7 分钟涨到压缩线（~3.5–4 万 token 附近触发），单步增量 4–6K token（工具结果 + 注入块 + 重复读取结果）。
2. **摘要质量不差，差在「进度锚点」**：摘要记录了文件细节与 Next Step，但「已完成动作」（edit_file 了哪个文件、哪次 execute_code 通过）没有被摘要携带——模型重建后把已做的事当作未做。**compactor 输入/输出缺「工具执行结果」这一维**（或执行结果在摘要输入中已被丢弃，待第 2/3 层确认机制细节）。
3. **指代失真**：commit 哈希抄错（9087bee→8087bee）；摘要内的数字/标识符需要消解与校验。
4. **R3 注入块加重循环**：清单全文 11 次重读 + 每步注入块计入前缀 → 压缩更频（副作用，非根因）。
5. **传染性**：循环状态通过 thread 上下文传给下一个 run（8ef42390 正在复现）。

## 7. 关联

- 待办：第 2/3 层 compactor 指代消解（优先序 1→3→2→4/5，两步走：方案+评审）。
- 先例：`docs/analysis/2026-09-02-opening-loop-number-hallucination.md`（开场白数字幻觉，R3 由来）。
- 相关记忆：`clawith-workspace-facts` 第 2/3 层待办、`p0-memory-loop-verification`（Langfuse 4K 截断真相）。

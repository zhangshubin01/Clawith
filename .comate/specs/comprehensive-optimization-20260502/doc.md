# 通义灵码 × Clawith 综合优化方案

> 基于 27 条原则全面审查 + 响应流畅度分析 + 依赖库升级分析
> 日期：2026-05-02

---

## 一、问题总览

共发现 **18 个问题**，按修复方和优先级分类：

| 优先级 | 数量 | 插件端 | 后端 | 配置/运维 |
|--------|------|--------|------|-----------|
| P0 | 4 | 3 | 1 | 0 |
| P1 | 5 | 3 | 2 | 0 |
| P2 | 5 | 3 | 1 | 1 |
| P3 | 4 | 1 | 1 | 2 |

---

## 二、P0：必须立即修复（影响核心体验/正确性）

### P0-1 🔴 MarkdownStreamPanel 全量重解析 O(n²) 退化

- **分端：** 插件端
- **位置：** `MarkdownStreamPanel.java:156` — `parseBlock(this.buffer.toString())`
- **问题：** 每次收到新 chunk 都对整个累积 buffer 重新运行正则，n 个 chunk = O(n²)。长回复数百个 chunk 时性能急剧恶化
- **修复方案：** 增量解析 — 记录上次解析结束位置 `lastParseOffset`，仅对新增部分 `buffer.substring(lastParseOffset)` 运行正则匹配，在 chunk 边界处合并相邻同类型 block
- **工作量：** 中（约 40 行改动）

### P0-2 🔴 MarkdownBlock 热路径 MD5 哈希计算

- **分端：** 插件端
- **位置：** `MarkdownBlock` 构造器 — `Md5Util.encode(content.getBytes())` 作为标识符
- **问题：** MD5 密码学哈希在流式热路径每次 chunk 更新时被重复调用，仅用于字符串相等性比较，完全不必要的 CPU 开销
- **修复方案：** 替换为 `Objects.hash(content)` 或直接用 content 的前 N 字符 + 长度组合作为轻量标识符
- **工作量：** 小（约 5 行改动）

### P0-3 🔴 toolCall markdown 格式不一致

- **分端：** 后端
- **位置：** Clawith 后端 `jsonrpc_router.py` / `tool_hooks.py` 中 toolCall 生成逻辑
- **问题：** 后端生成 `toolCall::name::id::status::` 4 段格式，插件 `MarkdownStreamPanel.MATCHER_PATTERN` (14 groups) 解析 3 段格式 `toolCall::name::id::status`。格式不一致导致 toolCall 块可能解析失败
- **修复方案：** 后端统一为 3 段格式，去掉末尾多余的 `::`
- **工作量：** 小（约 3 行改动）

### P0-4 🔴 extra.context recent_tool_result 对话记忆持久化缺失

- **分端：** 后端
- **位置：** Clawith 后端 `jsonrpc_router.py` — `_build_lsp4j_ide_prompt()`
- **问题：** `recent_tool_result` contextType 仅注入当前请求的 prompt 中，不持久化到数据库。多轮对话时历史工具结果丢失，智能体无法跨轮复用
- **修复方案：** 在 `chat/finish` 回调中将工具调用结果写入对话记忆存储（如 `chat_messages` 表或独立的 `tool_results` 表），后续请求自动附带历史工具结果
- **工作量：** 中（约 50 行新增代码）

---

## 三、P1：应当尽快修复（影响流畅度/可靠性）

### P1-1 🟡 LanguageClientImpl 冗余 supplyAsync + invokeLater 双包装

- **分端：** 插件端
- **位置：** `LanguageClientImpl.java` — `toolCallSync()`, `syncAllSnapshots()`, `syncWorkspaceFile()`, `chatDeleteNotification()`, `doFiltering()`, `pushCustomCommand()`, `networkRecover()` 等 7+ 个方法
- **问题：** 外层 `CompletableFuture.supplyAsync(() -> { SwingUtilities.invokeLater(...); return null; })` 完全多余。`toolCallSync` 路径造成 4 次线程切换：commonPool → EDT → consumer → EDT
- **修复方案：** 去掉外层 `supplyAsync`，直接 `SwingUtilities.invokeLater()`
- **工作量：** 小（约 20 行删除）

### P1-2 🟡 每次 chunk 双重 revalidate + repaint

- **分端：** 插件端
- **位置：** `MarkdownStreamPanel.renderComponent():196` + `BaseChatPanel.pushGenerate():443`
- **问题：** 每次 chunk 触发两次布局传递和一次强制重绘。稳定流式响应（每秒多个 chunk）造成显著 EDT 累积开销
- **修复方案：** `renderComponent()` 中移除 `this.revalidate()`，统一由 `pushGenerate()` 的 `invokeLater` 处理一次布局+重绘
- **工作量：** 小（约 3 行删除）

### P1-3 🟡 ThreadUtil 线程池参数不合理

- **分端：** 插件端
- **位置：** `ThreadUtil.java:28`
- **问题：** `ArrayBlockingQueue(128)` 相对 `maxPoolSize`(约 112) 过小；`CallerRunsPolicy` 在队列满后可能让 EDT 同步执行任务
- **修复方案：** 队列扩容至 1024；拒绝策略改为 `DiscardOldestPolicy` + 日志告警，避免 EDT 阻塞
- **工作量：** 小（约 3 行改动）

### P1-4 🟡 ChatToolEventProcessor 锁竞争 + 3 秒尾部延迟

- **分端：** 插件端
- **位置：** `ChatToolEventProcessor.java:163-226`
- **问题：** 消费者线程持锁循环 `await(500ms)`；REQUEST_FINISHED 条件竞争导致请求末尾不必要 3 秒超时
- **修复方案：** 
  1. 消费者线程不再持锁等待，改用 `CompletableFuture` 回调模式 — 面板注册后 `complete()` future，消费者 `get(3s, SECONDS)` 等待
  2. 消费者退出条件从检查队列头部改为检查当前事件状态 + 超时双重判断
- **工作量：** 中（约 35 行改动）

### P1-5 🟡 search_replace 语义降级

- **分端：** 后端
- **位置：** Clawith 后端 `tool_hooks.py` — `_lsp4j_aware_execute_tool()`
- **问题：** `search_replace` 从 Go 后端的正则批量替换降级为单文件简单文本替换。跨文件正则批量搜索替换功能不可用
- **修复方案：** 评估是否需要保持与 Go 后端一致的 `search_replace` 语义。如需，在后端实现基于 `grep_code` + `replace_text_by_path` 的组合调用模式
- **工作量：** 中（约 40 行新增代码）

---

## 四、P2：推荐修复（改善体验/可维护性）

### P2-1 🟠 流式模式下 link 转换未跳过

- **分端：** 后端
- **位置：** Clawith 后端的 markdown 后处理逻辑
- **问题：** 后端在流式模式下也转换链接语法 `[text](url)`，可能产生不完整的链接语法
- **修复方案：** 流式 chunk 生成时跳过链接转换，仅在 `chat/finish` 最终响应时做完整转换
- **工作量：** 小（约 5 行条件判断）

### P2-2 🟠 热路径过度日志

- **分端：** 插件端
- **位置：** `BaseChatPanel.pushGenerate()` 每个 chunk 15+ `log.info()`；`ChatToolEventProcessor` 每个方法入口/出口记录；`MarkdownStreamPanel.append()` 每个 chunk 记录
- **问题：** 流式响应中高频日志产生字符串拼接开销
- **修复方案：** 
  1. 高频路径改用 `log.debug()` 
  2. 或加 `if (log.isDebugEnabled())` 守卫
  3. `SLOW RENDER DETECTED` 告警加节流（如每 10 秒最多一次）
- **工作量：** 小（约 15 行改动）

### P2-3 🟠 run_mode is null WARN

- **分端：** 插件端
- **位置：** `RunInTerminalToolContextProvider.java`
- **问题：** 部分终端命令缺少 run_mode 导致日志告警
- **修复方案：** 检查 `RunTerminalToolHandlerV2` 中 run_mode 设置逻辑，确保所有调用路径都正确设置 run_mode（`readonly` / `build` / `default`）
- **工作量：** 小（约 5 行改动）

### P2-4 🟠 CODE_EDIT_BLOCK 仅限部分 chatTask 场景

- **分端：** 后端
- **位置：** Clawith 后端流式输出生成逻辑
- **问题：** CODE_EDIT_BLOCK 格式生成仅限 `FREE_INPUT` 和 `PRE_CONTEXT` chatTask 类型。其他场景（如 `CODE_REVIEW`、`UNIT_TEST`）也应支持
- **修复方案：** 扩展 CODE_EDIT_BLOCK 的 chatTask 支持范围，或将格式判断与 chatTask 解耦
- **工作量：** 小（约 10 行改动）

### P2-5 🟠 DynamicBundle 日志噪音 32.6%

- **分端：** 运维/配置
- **位置：** IDE 配置
- **问题：** `#com.intellij.DynamicBundle` 在日志中占 32.6%
- **修复方案：** 用户在 IDE 中 Help → Diagnostic Tools → Debug Log Settings → 添加 `#com.intellij.DynamicBundle` 关闭
- **工作量：** 用户手动操作

---

## 五、P3：跟踪关注（长期优化）

### P3-1 🔵 依赖库升级

| 依赖 | 当前 | 目标 | 收益 |
|------|------|------|------|
| Kotlin | 2.3.20 | 2.3.21 | patch bug fix |
| Gradle Wrapper | 9.4.0 | 9.4.1 | bug fix |
| Jsoup | 1.21.1 | 1.22.2 | HTTP/2 + 安全 |
| Tomcat Embed | 9.0.113 | 9.0.117 | 安全修复累积 |
| LSP4J | 0.12.0 | 1.0.0 | 需专项评估 |
| JGit | 6.10.1 | 7.6.0 | 需专项评估 |

### P3-2 🔵 FlexMark 长期替代方案

- **分端：** 插件端
- **问题：** FlexMark 0.64.8 2023 年停更，长期存在安全/兼容风险
- **建议：** 调研活跃维护的 Markdown 解析库（如 commonmark-java）作为替代方案

### P3-3 🔵 `InlineChatPanel` 内联对话路径验证

- **分端：** 插件端
- **问题：** 内联对话（编辑器内直接触发）可能使用不同的上下文注入路径，`recent_tool_result` 注入可能未覆盖
- **建议：** 追踪验证 `InlineChatPanel` 的 `extra.context` 注入逻辑

### P3-4 🔵 多模态支持（截图/UI 布局）

- **分端：** 后端
- **问题：** 后端不支持接收并处理 IDE 本地插件传来的截图或 UI 布局数据
- **建议：** 在 `_build_lsp4j_ide_prompt` 中添加图片附件处理逻辑，支持 Vision 能力的 AI 基于截图完成 UI 调试

---

## 六、修复计划（按实施顺序）

### 第一批：插件端流畅度（预计 2-3 小时）

```
P0-1 MarkdownStreamPanel 增量解析        ← 影响最大
P0-2 MD5 标识符替换                      ← 简单
P1-1 去除冗余 supplyAsync 包装           ← 简单
P1-2 合并重复 revalidate                ← 简单
P1-3 ThreadUtil 参数调整                ← 简单
```

### 第二批：插件端可靠性（预计 1-2 小时）

```
P1-4 ChatToolEventProcessor 锁优化      ← 中等
P2-2 热路径日志降级                      ← 简单
P2-3 run_mode is null 修复              ← 简单
```

### 第三批：后端修复（预计 2-3 小时）

```
P0-3 toolCall 格式统一                   ← 简单
P0-4 对话记忆持久化                      ← 中等
P1-5 search_replace 语义对齐             ← 中等
P2-1 流式模式链接转换跳过                 ← 简单
P2-4 CODE_EDIT_BLOCK 范围扩展            ← 简单
```

### 第四批：长期优化（持续跟踪）

```
P3-1 依赖库升级（Kotlin/Gradle/Jsoup/Tomcat 先做）
P2-5 DynamicBundle 噪音（用户手动配置）
P3-2 FlexMark 替代方案调研
P3-3 InlineChatPanel 验证
P3-4 多模态支持
```

---

## 七、附录：27 条原则审查速查表

| # | 原则 | 状态 | 备注 |
|---|------|------|------|
| 1 | 中文注释 | ✅ | P0 修复均已添加 |
| 2 | 调试日志 | ✅ | 全链路 [Clawith]/[LSP]/[CHAT_SERVICE] 标签 |
| 3 | 最佳实践 | ✅ | WebSocket 心跳、Caffeine 缓存、异步 Future |
| 4 | 官方文档 | ✅ | IntelliJ Platform SDK / LSP4J 规范 |
| 5 | 代码精简 | ✅ | 最小变更原则 |
| 6 | 代码复用 | ✅ | ToolHandler 基类、Caffeine 依赖复用 |
| 7 | 风格一致 | ✅ | try/catch + ToolInvokeResponse 模式 |
| 8 | 智能体影响 | ✅ | 无负面影响，extra.context 增强上下文 |
| 9 | Web UI 可见 | ✅ | chat/ask → chat/answer 完整链路 |
| 10 | 对话记忆 | ⚠️ | P0-4 待修复 |
| 11 | 插件源码 | ✅ | /Users/shubinzhang/Downloads/demo-new |
| 12 | 运行日志 | ✅ | .intellijPlatform/sandbox/demo/IC-2025.1.1/log/idea.log |
| 13 | 后端源码 | ✅ | /Users/shubinzhang/Documents/UGit/Clawith |
| 14 | 方案验证 | ✅ | ToolInvokeProcessor switch 注册 |
| 15 | Diff 能力 | ✅ | InlineDiffManagerImpl |
| 16 | 任务规划 | ✅ | add_tasks/todo_write |
| 17 | 改代码能力 | ✅ | replace_text_by_path/create_file/delete_file |
| 18 | 本地工具 | ✅ | 14 IDE 工具完整注册 |
| 19 | 修改能力适配 | ⚠️ | P1-5 search_replace 语义待修复 |
| 20 | 功能适配 | ⚠️ | P3-3 InlineChatPanel 未验证 |
| 21 | 真实查代码 | ✅ | 追踪完整调用链 |
| 22 | 功能完整性 | ✅ | 14 IDE 工具全覆盖 |
| 23 | 日志问题 | ⚠️ | P2-3 run_mode null, P2-5 DynamicBundle |
| 24 | Markdown 适配 | ⚠️ | P0-3 toolCall 格式, P2-1 link 转换, P2-4 CODE_EDIT_BLOCK |
| 25 | 文档更新 | ✅ | 本文档 |
| 26 | 流畅度优化 | ⚠️ | P0-1/P0-2/P1-1/P1-2/P1-3/P1-4 待修复 |
| 27 | 依赖升级 | ⚠️ | P3-1 待执行 |

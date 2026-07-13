# Clawith 群聊

这个目录是 Clawith 群聊功能的本地工作区。

当前文档：

- [prd.md](prd.md)：群聊 v1 产品开发基线，记录已确定规则、边界和后续规划。
- [technical-design.md](technical-design.md)：群聊 v1 终版技术设计，包含技术建模、上下文构造、LangGraph 主 Runtime、群 workspace 和落地分期。
- [chat-model-refactor.md](chat-model-refactor.md)：统一 `chat_sessions`、`chat_messages` 和群聊天数据模型的规范来源。
- [context-compression.md](context-compression.md)：群上下文压缩与加载策略调研记录；最终实现以技术设计为准。
- [agent-mention-queue-research.md](agent-mention-queue-research.md)：Agent 被 @ 后的异步执行与队列处理历史调研；最终实现以技术设计为准。

固定开发顺序：Schema/迁移与依赖基线 → Single 后端 → Group 后端 → 后端整体验证与旧循环清理 → 前端统一更新。

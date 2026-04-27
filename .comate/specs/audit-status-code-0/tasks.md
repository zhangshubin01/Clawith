# 审计 statusCode 及修复工具调用链路

- [ ] Task 1: 修复异常场景 statusCode 映射
    - 1.1: 修改 `_send_chat_finish` 增加 `status_code` 参数（默认 200）
    - 1.2: 修改 `_handle_chat_ask` 异常分支映射 statusCode（Cancelled→200, Timeout→408, Exception→500）
    - 1.3: 同步修改 `finish_reason` 逻辑（error 时 reason="error"）

- [ ] Task 2: 诊断 tool/invoke 工具调用链路
    - 2.1: 在 `_lsp4j_aware_get_tools` 添加日志，确认 IDE 工具是否注册到 LLM
    - 2.2: 在 `_lsp4j_aware_execute_tool` 添加日志，确认工具调用路由路径
    - 2.3: 部署重启，检查日志中是否有工具注册和调用的记录
    - 2.4: 根据日志定位具体断裂点并修复

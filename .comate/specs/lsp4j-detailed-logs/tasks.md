# 添加 LSP4J 完整调用链日志

> 验证结论：doc.md v3.1 设计完整，19 条需求全覆盖。代码审计确认约 36 处缺失，其中 31 处新增日志 + 10 处前缀统一。
> **关键新增（v3）：`caller.py` 的 `from ... import` 绑定 bug 必须先修复，否则 LSP4J 工具桥完全失效，所有工具日志不会被触发。**

- [x] Task 0: 修复 caller.py 的 `from ... import` 绑定 bug（前置修复，P0-Critical）
    - 0.1: `caller.py:26` 导入改为 `from app.services import agent_tools`
    - 0.2: `caller.py:258/844` 调用改为 `await agent_tools.execute_tool(...)`
    - 0.3: `caller.py:343/767` 工具获取改为 `agent_tools.get_agent_tools_for_llm` / `agent_tools.AGENT_TOOLS`
    - 0.4: 验证：触发 `read_file` 工具调用，检查日志是否出现 `[LSP4J-TOOL] execute_tool: name=read_file lsp4j_ws=True`

- [x] Task 1: 修复 router.py 日志前缀统一（共 10 处）
    - 1.1: `_resolve_agent_override` 4 处 `LSP4J` → `[LSP4J-LIFE]`（65/72/77/83 行）
    - 1.2: `LSP4J WS auth failed` → `[LSP4J-LIFE] WS auth failed`（116 行）
    - 1.3: `LSP4J WebSocket auth error` → `[LSP4J-LIFE] WebSocket auth error`（120 行）
    - 1.4: `LSP4J WS connected` → `[LSP4J-LIFE] WS connected`（132 行）
    - 1.5: `LSP4J WS disconnected` → `[LSP4J-LIFE] WS disconnected`（160 行）
    - 1.6: `LSP4J WS error` → `[LSP4J-LIFE] WS error`（162 行）
    - 1.7: `LSP4J WS cleanup done` → `[LSP4J-LIFE] WS cleanup done`（179 行）

- [x] Task 2: 添加回复流程日志（流程 A #13）
    - 2.1: `_handle_chat_ask` 空消息拒绝日志 (A1)
    - 2.2: `_handle_chat_ask` 并发拒绝日志 (A2)
    - 2.3: `_handle_chat_ask` 历史加载日志 (A3)
    - 2.4: `on_chunk` 首次 chunk info 日志 + 后续 chunk debug 日志 (A4)
    - 2.5: `on_chunk` 取消日志 (A5)
    - 2.6: `_send_chat_answer` 回答发送日志 (A6)

- [x] Task 3: 添加 diff 流程日志（流程 B #14）
    - 3.1: `_handle_code_change_apply` 入口日志 (B1)
    - 3.2: `_handle_code_change_apply` 空内容警告 (B2)
    - 3.3: `_handle_code_change_apply` finish 通知日志 (B3)

- [x] Task 4: 添加任务规划日志（流程 C #15）
    - 4.1: `_send_process_step_callback` 步骤推送日志 (C1)
    - 4.2: `_handle_step_process_confirm` 确认日志 (C2)

- [x] Task 5: 添加改代码/本地工具日志（流程 D/E #16/#17）
    - 5.1: `_handle_response` 未匹配响应日志 (D1)
    - 5.2: `_handle_response` 错误响应日志 (D2)
    - 5.3: `_handle_response` 成功日志 (D3)
    - 5.4: `_handle_response` 失败日志 (D4)
    - 5.5: `_handle_response` Future 已完成日志 (D5)
    - 5.6: `_handle_tool_invoke_result` 结果日志 (D6)
    - 5.7: `_handle_tool_invoke_result` Future 缺失日志 (D7)
    - 5.8: `_handle_tool_call_approve` 审批审计日志 (D8)

- [x] Task 6: 添加生命周期日志（流程 F）
    - 6.1: `_handle_initialize` 日志 (F1)
    - 6.2: `_handle_shutdown` 日志 (F2)
    - 6.3: `_handle_exit` 日志 (F3)
    - 6.4: `_handle_chat_stop` 日志 (F4)
    - 6.5: `cleanup` 清理详情日志 (F5)
    - 6.6: `_resolve_model_by_key` 找到/未找到日志 (F7-F8)

- [x] Task 7: 添加记忆持久化日志（流程 G #9/#10）
    - 7.1: `_load_lsp4j_history_from_db` 成功日志 (G1)
    - 7.2: `_load_lsp4j_history_from_db` 拒绝日志保持 warning（安全事件不改级别）(G2)
    - 7.3: `_persist_lsp4j_chat_turn` 持久化成功日志 (G3)
    - 7.4: `_persist_lsp4j_chat_turn` 前端 WebSocket 通知日志（`send_to_session` 调用后，1133行为失败日志）(G4)

- [x] Task 8: 添加工具钩子日志（流程 H）
    - 8.1: `install_lsp4j_tool_hooks` 重复调用日志 (H1)
    - 8.2: `_lsp4j_aware_execute_tool` 异常日志（只加日志不吞异常）(H2)

- [x] Task 9: 部署重启并验证
    - 9.1: 运行 restart.sh 部署
    - 9.2: 检查日志中所有新增日志关键字是否出现
    - 9.3: 验证 5 个功能流日志完整性
    - 9.4: **验证 Task 0：触发 read_file 工具调用，确认 `[LSP4J-TOOL] execute_tool` 日志出现**
    - 9.5: 验证 requestId→Project 回退：检查工具调用是否成功映射到正确的 IDE Project

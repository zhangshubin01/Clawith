# LSP4J 详细诊断日志 — 实施总结

## 完成概览

10 个任务全部完成，涉及 4 个文件修改，共约 35 处变更。

## 修改文件清单

### 1. `backend/app/services/llm/caller.py` — 前置修复（P0-Critical）

**问题**：`from app.services.agent_tools import execute_tool, get_agent_tools_for_llm, AGENT_TOOLS` 在模块加载时绑定局部名称，导致后续 monkey-patch 的 LSP4J/ACP 工具钩子无法被 `call_llm` 调用到。

**修复**：
| 行号 | 修改 |
|------|------|
| 26 | `from app.services.agent_tools import ...` → `from app.services import agent_tools` |
| 258 | `await execute_tool(...)` → `await agent_tools.execute_tool(...)` |
| 343 | `await get_agent_tools_for_llm(...) if agent_id else AGENT_TOOLS` → `await agent_tools.get_agent_tools_for_llm(...) if agent_id else agent_tools.AGENT_TOOLS` |
| 767 | `await get_agent_tools_for_llm(agent_id)` → `await agent_tools.get_agent_tools_for_llm(agent_id)` |
| 844 | `await execute_tool(...)` → `await agent_tools.execute_tool(...)` |

**注意**：doc.md 原本只记录了 258/343 行，实施时发现 767/844 行（`call_llm_with_tools` 函数）存在同类 bug，一并修复。

### 2. `backend/app/plugins/clawith_lsp4j/router.py` — 前缀统一

10 处日志统一为 `[LSP4J-LIFE]` 前缀：
- `_resolve_agent_override` 4 处 warning（65/72/77/83 行）
- WS auth failed/error（116/120 行）
- WS connected/disconnected/error/cleanup（132/160/162/179 行）

### 3. `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` — 核心日志

新增约 25 处日志：

| 流程 | 日志 | 前缀 | 级别 |
|------|------|------|------|
| A1 | 空消息拒绝 | `[LSP4J]` | warning |
| A2 | 并发拒绝 | `[LSP4J]` | warning |
| A3 | 历史加载完成 | `[LSP4J]` | debug |
| A4 | on_chunk 首次/后续 | `[LSP4J]` | info/debug |
| A5 | on_chunk 取消 | `[LSP4J]` | info |
| A6 | chat/answer 发送 | `[LSP4J]` | debug |
| B1 | codeChange/apply 入口 | `[LSP4J]` | info |
| B2 | codeChange/apply 空 | `[LSP4J]` | warning |
| B3 | apply/finish 通知 | `[LSP4J]` | debug |
| C1 | process_step_callback | `[LSP4J]` | info |
| C2 | stepProcessConfirm | `[LSP4J]` | info |
| D1 | 未匹配响应 | `[LSP4J-TOOL]` | warning |
| D2 | 工具响应错误 | `[LSP4J-TOOL]` | warning |
| D3 | 工具执行成功 | `[LSP4J-TOOL]` | info |
| D4 | 工具执行失败 | `[LSP4J-TOOL]` | warning |
| D5 | Future 已完成 | `[LSP4J-TOOL]` | debug |
| D6 | invokeResult 结果 | `[LSP4J-TOOL]` | info |
| D7 | invokeResult 无 Future | `[LSP4J-TOOL]` | warning |
| D8 | 工具审批 | `[LSP4J-TOOL]` | info |
| F1 | initialize | `[LSP4J-LIFE]` | info |
| F2 | shutdown | `[LSP4J-LIFE]` | info |
| F3 | exit | `[LSP4J-LIFE]` | info |
| F4 | chat/stop | `[LSP4J]` | info |
| F5 | cleanup 清理详情 | `[LSP4J-LIFE]` | info |
| F7 | 模型查找成功 | `[LSP4J]` | debug |
| F8 | 模型查找失败 | `[LSP4J]` | warning |
| G1 | 历史加载成功 | `[LSP4J]` | debug |
| G3 | 持久化成功 | `[LSP4J]` | debug |
| G4 | 前端通知已发送 | `[LSP4J]` | debug |

### 4. `backend/app/plugins/clawith_lsp4j/tool_hooks.py` — 钩子增强

| 修改 | 说明 |
|------|------|
| H1 | `_installed` 为 True 时记录 debug 日志 |
| H2 | LSP4J 路径加 try/except/raise，异常记录 logger.exception 后继续传播 |
| — | 安装完成日志前缀统一为 `[LSP4J-TOOL]` |

## 代码审查结果

代码审查通过：
- 全部 30 处新增日志确认存在，前缀和级别正确
- caller.py 导入修改正确且必要（支持 monkey-patch 架构）
- 无语法错误，全部模块导入验证通过

## 已知遗留

1. **旧日志前缀不一致**：jsonrpc_router.py 中约 13 处旧日志使用 `LSP4J:` 或裸 `LSP4J` 前缀，未在此次修改中统一（属于已有技术债）
2. **运行时验证**：`[LSP4J-LIFE]`/`[LSP4J-TOOL]` 前缀日志需要 IDE 插件连接后才会产生，当前部署验证确认服务启动正常
3. **requestId→Project 回退**：需要实际 IDE 插件连接后验证工具调用是否正确映射到 IDE Project

## 部署状态

服务已通过 `restart.sh --source` 重新部署并启动成功（Backend + Frontend + Proxy 均正常）。

# Clawith ACP 后端插件 (`/ws/acp`)

生产路径：**IDE acp-plugin → WebSocket `/ws/acp` → `acp_handler.py` → `call_llm_with_failover`**。

## 架构

- JSON-RPC 2.0 + Bearer JWT
- Session 持久化：`acp_session.py`（`source_channel=acp`，与 Web UI 隔离）
- LLM：`call_llm_with_failover` + `build_agent_context`（soul/memory/skills/MCP 在后端）
- IDE 工具：`tool_bridge` + `tool_hooks`

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `ACP_FEATURES` | `document,nes,providers` | 逗号分隔；空=关闭全部扩展 |
| `ACP_NES_ENABLED` | `0` | NES 后端未接入时保持 0 |
| `ACP_LLM_TIMEOUT_SECONDS` | `600` | prompt 超时 |

## 能力矩阵

| 能力 | 状态 | 说明 |
|------|------|------|
| session/prompt | ✅ | 主对话路径 |
| session/cancel | ✅ | 取消 LLM + terminal |
| document/* | ✅ | `acp_document.py` 内存 store + prompt 注入 |
| providers/list\|set | ✅ | 映射 `_clawith/list_agents` / `set_agent` |
| nes/* | ⚠️ | 占位，返回空 suggestions |
| elicitation | ⚠️ | SDK 端到端不完整，不声明 capability |
| `_clawith/*` | ✅ | 智能体切换、工具结果 |

## 日志前缀

`[ACP]`、`[ACP-DOC]`、`[ACP-NES]`、`[ACP-PERF]`、`[ACP-CONN]`

## 测试

```bash
cd Clawith/backend && pytest tests/plugins/test_clawith_acp.py -v
docker logs <container> 2>&1 | grep '\[ACP\]'
```

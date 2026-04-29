# 移除 OpenViking 集成

## 需求场景

OpenViking 语义记忆功能在项目中处于休眠状态：
- `search_memory()` 从未被任何模块调用（语义检索功能实际不可用）
- `openviking_watcher.py` 中的 `start_watcher()` 从未在应用启动时被调用
- 唯一的运行时集成是 `write_file` 工具写入 `memory.md` 时触发 `index_memory_file()` 索引

移除 OpenViking 可减少代码复杂度和维护负担。

## 受影响文件

### 删除文件（2 个）

| 文件 | 行数 | 说明 |
|------|------|------|
| `backend/app/services/openviking_client.py` | 287 | HTTP 客户端，定义 `is_available()`, `search_memory()`, `index_memory_file()` 等 |
| `backend/app/services/openviking_watcher.py` | 71 | 基于 watchdog 的文件监视器 |

### 修改文件（5 个）

| 文件 | 位置 | 变更 |
|------|------|------|
| `backend/app/services/agent_tools.py` | lines 2154-2162 | 移除 `if "memory" in path` 块（`index_memory_file` 调用） |
| `.mcp.json` | lines 3-12 | 移除 `"openviking"` mcpServers 条目 |
| `README.md` | lines 290-306 | 移除 "Semantic Memory (OpenViking)" 章节 |
| `README_zh-CN.md` | lines 260-276 | 移除中文版 OpenViking 章节 |
| `clawith_acp/ACP_DELIVERY_ABC.md` | line 76 | 移除 `OpenViking retrieval unavailable` 提及 |

### 不受影响的文件

- `.env` / `.env.example` — 无 OPENVIKING 环境变量
- `docker-compose.yml` — 无 openviking 服务
- `pyproject.toml` / `requirements.txt` — 无 openviking 专用依赖（httpx、watchdog 为共享依赖）
- `backend/app/main.py` — 未调用 `start_watcher()`
- `backend/app/services/llm/caller.py` — 未调用 `search_memory()`

## 边界条件

1. **watchdog 依赖**：仅被 `openviking_watcher.py` 使用。移除后 watchdog 成为死代码，但它是常见包，暂不从 requirements 中移除（避免破坏其他潜在用途）
2. **graphify-out/ 输出**：包含 openviking 节点，删除源文件后需运行 graphify rebuild 自动更新
3. **环境变量**：`OPENVIKING_URL` 等 4 个变量仅被 `openviking_client.py` 读取，删除文件后自动失效

## 预期结果

1. 项目中无 OpenViking 相关代码和配置
2. `write_file` 工具写入 `memory.md` 时不再触发索引（正常写入不受影响）
3. MCP 服务器列表中无 openviking 条目
4. 文档中无 OpenViking 章节

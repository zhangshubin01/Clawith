# Clawith 后端 LSP4J 适配差异清单（对插件）

> 对照：`LanguageServer.java` / `LanguageClient.java`、插件模型类、`jsonrpc_router.py`  
> 详细逐方法表：`docs/plugin-analysis/15-complete-method-by-method-gap-analysis.md`  
> 本文档：**执行计划后**的摘要与仍开放项（随 `git` 版本变化，以代码为准）。

## 已加强或已实现（相对早期 STUB 描述）

| Wire method | 说明 |
|-------------|------|
| `snapshot/listBySession` | 返回 `ListSnapshotsResult`，数据来自 `WorkspaceFileService`。 |
| `snapshot/operate` | 内存状态 + 可选 `snapshot/syncAll`。 |
| `chat/listAllSessions` | `ide_lsp4j` 会话 DB 列表。 |
| `chat/getSessionById` | DB 消息组装为 `REPLY_TASK` 记录。 |
| `config/queryModels` | 占位 Map，满足模型下拉。 |
| `ping` | `PingResult.success = true`。 |
| 工具编辑后 | `workingSpaceFile/sync` 后再发 `snapshot/syncAll`。 |
| P0 静默忽略 | `settings/change`、`statistics/*`、多条 `config/update*`、`window/workDoneProgress/cancel` 等见 `jsonrpc_router._dispatch`。 |

## 仍为 STUB 或弱实现（需产品决策）

| 区域 | 方法示例 | 建议 |
|------|-----------|------|
| Auth | `auth/login`、`auth/status`、… | Clawith 模式返回固定「已连接」或对接真实账号体系。 |
| Chat 清理 | `chat/deleteSessionById`、`clearAllSessions`、`deleteChatById` | DB 删除 + 与前端 Web UI 一致。 |
| 配置 | `config/getGlobal`、`ide/update` | 若插件强依赖，返回最小合法 JSON。 |
| 工具历史 | `tool/call/results` | 内存环形缓冲或 DB。 |
| Session | `session/getCurrent` | 返回 `_session_id` + 元数据。 |
| Snippet / KB / model BYOK | 多条 stub | 低优先级可继续空成功。 |

## 协议字段

`15-complete-method-by-method-gap-analysis.md` 第三节：**69 个推送字段**与 Java getter 一致；后端改字段须同步该表与插件 POJO。

## 插件独占修改（非 Clawith 仓库）

- `ToolTypeEnum`：`write_file` 等别名。  
- `DiffContentUnwrapper`、Markdown 表格 CSS、Clawith 设置页等 — 见 `demo-new/docs/` 各修复说明。

## 验证建议

1. IDE `idea.log` 无 `-32601`。  
2. 一轮 chat + 读文件 + 写文件 + Accept：快照列表与文件状态正确。  
3. 心跳无 `PingResult` NPE。

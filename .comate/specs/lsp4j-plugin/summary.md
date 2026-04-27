# 通义灵码 LSP4J 插件实现总结

## 完成状态

所有 8 个任务已完成，Python 后端通过语法检查和导入链测试，Java 端 5 个文件已修改。

## 新建文件（Python 后端）

| 文件 | 行数 | 说明 |
|------|------|------|
| `backend/app/plugins/clawith_lsp4j/plugin.json` | 9 | 插件元数据 |
| `backend/app/plugins/clawith_lsp4j/__init__.py` | 41 | ClawithLsp4jPlugin 子类 + register() |
| `backend/app/plugins/clawith_lsp4j/context.py` | 45 | 6 个 ContextVar + _active_routers |
| `backend/app/plugins/clawith_lsp4j/lsp_protocol.py` | 119 | LSP Base Protocol 解析器（字节级操作） |
| `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` | 445 | JSON-RPC 路由器 + call_llm + 持久化 |
| `backend/app/plugins/clawith_lsp4j/router.py` | 138 | WebSocket 端点 + 认证 + _resolve_agent_override |
| `backend/app/plugins/clawith_lsp4j/tool_hooks.py` | 219 | 工具钩子（执行路径 + 注册路径） |

## 修改文件（Java 插件端）

| 文件 | 修改内容 |
|------|----------|
| `GlobalConfig.java` | +4 字段（clawithBackendUrl, clawithApiKey, clawithAgentId, useClawithBackend）+ getter/setter + equals/hashCode/toString |
| `LingmaUrls.java` | +1 枚举值 CLAWITH_SERVICE_URL |
| `ConfigMainForm.java` | +7 UI 字段 + initGlobalConfigPanel 初始化 + updateLingmaGlobalConfig 持久化 + updateClawithConfigPanelVisibility 可见性控制 |
| `CosySetting.java` | +4 字段 + getter/setter（Clawith 配置持久化到 cosy_setting.xml） |
| `LanguageWebSocketService.java` | +1 方法 createClawithService(Project, String url, String apiKey, String agentId)，URL 含 agent_id + token 查询参数 |

## 关键设计决策

1. **独立工具名称体系**：`_LSP4J_IDE_TOOL_NAMES` 使用插件原生名称（read_file, save_file 等），不复用 ACP 的 `ide_` 前缀名称，因为插件 `ToolInvokeProcessor` 不识别 `ide_` 前缀。

2. **双路径补丁**：`install_lsp4j_tool_hooks()` 同时补丁执行路径（`_lsp4j_aware_execute_tool`）和注册路径（`_lsp4j_aware_get_tools`），确保 LLM 能看到 IDE 工具且调用路由正确。

3. **协议字段严格匹配**：所有 JSON-RPC 消息字段名基于插件源码验证（text 不是 content，name 不是 tool，parameters 不是 arguments，step 不是 thinking）。

4. **钩子安装时机**：LSP4J 在 `register()` 中安装钩子（晚于 ACP 的模块级安装），保证获取到 ACP 已安装的引用。

5. **LSP 协议解析器**：按字节操作，支持中文多字节字符、粘包处理、分帧解析。

## 测试验证

- Python 语法检查：6 个文件全部通过
- 导入链测试：所有模块导入正常
- LSP 协议解析器测试：基本解析、中文消息、粘包、分帧消息全部通过
- 工具名称验证：8 个工具名与定义完全匹配，无 `ide_` 前缀
- 插件实例验证：ClawithLsp4jPlugin 正确创建

## 修复记录

- **lsp_protocol.py regex 修复**：`_CONTENT_LENGTH_RE` 原始模式 `rb"^Content-Length:\s*(\d+)\r\n"` 中 `\r\n` 导致 header line 匹配失败（split 已移除行尾 `\r\n`），修正为 `rb"^Content-Length:\s*(\d+)"`。

## 后续工作

- Java 端 UI 布局：ConfigMainForm.java 中的 UI 字段需要配合 IntelliJ GUI Designer 的 .form 文件绑定（目前仅声明了字段，需要在 .form 文件中添加对应的 Swing 组件）
- 端到端集成测试：需要启动 Clawith 后端并使用修改后的通义灵码插件实际连接验证
- 权限审批 UI：当前 MVP 阶段 `tool/callApprove` 自动批准，后续可增加审批界面

# LSP4J 灵码插件全量兼容 — 实施总结

## 完成概览

全部 10 个任务已完成，涉及 3 个文件的修改：

| 文件 | 修改行数 | 说明 |
|---|---|---|
| `backend/app/services/llm/caller.py` | ~30 行 | cancel_event 透传（Task 1） |
| `backend/app/services/llm/utils.py` | ~8 行 | BYOK _runtime_api_key 优先读取（Task 5） |
| `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` | ~300 行 | 核心协议兼容实现（Task 2-9） |

## 各 Task 实施详情

### Task 1：call_llm cancel_event 透传
- `call_llm` 签名新增 `cancel_event: asyncio.Event | None = None`
- 工具循环入口、readonly 并行前、write 串行前均插入 cancel 检查
- `call_llm_with_failover` 同步透传 cancel_event
- `client.stream()` 透传 cancel_event（已有 `**kwargs` 支持）

### Task 2：ChatAskParam dataclass 与参数解析
- 定义 17 字段 ChatAskParam dataclass（严格匹配 ChatAskParam.java）
- `_handle_chat_ask` 使用 ChatAskParam 替代原始 dict 取值
- 保存 `_stream_mode` 和 `_current_session_type` 到实例属性
- `_send_chat_answer` 增加 `extra: Map<String, String>` 字段（含 sessionType）
- `on_chunk` 回调增加 `_stream_mode` 判断

### Task 3：ide_prompt 构建与注入
- `_CHAT_TASK_HINTS` 12 项映射（基于 ChatTaskEnum.java 实际枚举值）
- `_build_lsp4j_ide_prompt()` 处理 chatTask/codeLanguage/mode/extra.context/shellType/pluginPayloadConfig
- 2000 字符 token 预算控制（P2-1）
- 计算 `supports_vision` 并传入 call_llm

### Task 4：client_type 标记与 chat/stop 响应修复
- ChatSession 创建增加 `client_type="ide_plugin"`
- `_handle_chat_stop` 添加 `await self._send_response(msg_id, {})` 修复 P1-6
- P2-4 TODO 注释

### Task 5：BYOK 与模型配置
- `_handle_chat_ask` 实现 customModel 处理：创建 transient LLMModel + `_runtime_api_key` + isVl→supports_vision
- `utils.py:get_model_api_key` 优先读取 `_runtime_api_key`（BYOK 场景，不入库）
- 模块级 `_resolve_model_by_key()` 函数：按 UUID 或 model 名称查数据库
- 模型选择优先级：customModel > extra.modelConfig.key > 默认

### Task 6：METHOD_MAP 补齐所有缺失方法
- `_handle_stub` 通用存根处理器（返回 `{}`）
- `_METHOD_MAP.update()` 追加 17 个方法（含 chat/ 13 + tool/ 1 + agents/ 1 + textDocument/ 2）
- 保留原有 `tool/invokeResult` 等条目不被覆盖
- `textDocument/inlineEdit` 和 `textDocument/editPredict` 存根（P2-3）

### Task 7：step callback 与确认处理
- `_send_process_step_callback` 方法（ChatProcessStepCallbackParams 7 字段）
- call_llm 调用前发送 step_start，完成后发送 step_end
- on_tool_call 回调中发送 doing/done 步骤状态
- `_handle_step_process_confirm` 返回 StepProcessConfirmResult

### Task 8：codeChange/apply 完整实现
- 返回完整 ChatCodeChangeApplyResult（9 个字段：applyId, projectPath, filePath, applyCode, requestId, sessionId, extra, sessionType, mode）
- 发送 `chat/codeChange/apply/finish` 通知
- MVP 策略：codeEdit → applyCode（无 merge 逻辑）

### Task 9：代码质量与边界补充
- 所有新增函数已有中文 docstring 和关键注释（9.1）
- shellType 和 pluginPayloadConfig 已在 _build_lsp4j_ide_prompt 中处理（9.2）
- inlineEditParams 相关方法（textDocument/inlineEdit + textDocument/editPredict）已注册存根（9.3）
- METHOD_MAP 完整性验证：7 原始 + 17 扩展 = 24 条目，无遗漏（9.4）
- docstring "17 个字段" 已正确（9.5）
- ChatSession P2-4 TODO 已标注（9.6）

### Task 10：部署验证与回归测试
- 3 个修改文件 Python 语法检查全部通过
- `get_model_api_key` BYOK 逻辑单元测试通过
- graphify 知识图已更新（3987 nodes, 15408 edges, 156 communities）
- 10.1-10.5 运行时验证需实际部署后人工测试

## 关键设计决策

1. **METHOD_MAP 使用 .update() 追加**：确保已有条目（tool/invokeResult）不被覆盖
2. **BYOK 密钥通过 `_runtime_api_key` 注入**：不入库、不写日志、GC 后释放
3. **chat/answer 等通知模式**：虽是 @JsonRequest 但发送为 notification，避免 LSP4J 阻塞
4. **codeChange/apply MVP**：直接返回 codeEdit 作为 applyCode，后续可增强三方 merge
5. **step callback 嵌入 on_tool_call**：与现有 chat/think 回调并行推送，互不干扰

## 待后续增强项

- P2-4：ChatSession project_path / current_file / open_files 字段利用
- codeChange/apply 三方 merge 逻辑
- textDocument/inlineEdit 和 textDocument/editPredict 完整实现
- step callback 的 manual_confirm 确认流程增强

# Clawith 死代码审查报告

- **日期**：2026-08-16
- **分支**：`f-shubin-0806`（HEAD `2682044c`）
- **范围**：backend/app、backend/tests、backend/alembic、frontend/src、frontend/tests、scripts（排除 agent_data、node_modules、锁文件）
- **性质**：仅审查，未删除任何代码。

## 1. 方法与数据管线

以 codebase-memory 知识图谱导出为基础，配合本地脚本做差集与全库按名复核：

1. 全量函数/方法 8384 个（函数 5719 + 方法 2665）减去图中所有引用边目标
   （CALLS / USAGE / DECORATES / IMPORTS / CALL_REFERENCE / OVERRIDE / HANDLES）
   → 图判定死代码 3751 个。
2. 剔除 backend.tests（2887）与 backend.alembic（147）→ **生产代码死候选 717 个**。
3. 对 717 个候选做全库按名扫描（短名 + 类名，覆盖源码目录）：
   - 仅定义处出现（total == 1）：172 个
   - 同文件引用：103 个；跨文件引用：436 个（多为 JSX/注册表/多态误报）
4. 对 172 个做二层验证（下划线变体、子串、类名外部引用、测试引用、`getattr(x, 'str')`
   字符串调度——已确认全库零存在），最终三分类：
   - **硬候选 89 项 / 1564 行**、**类活方法无引用 51 项 / 1796 行**、**按名引用疑似误报 32 项 / 961 行**。

**已知系统性误报源**（图形图不捕获，已在分类后人工复核修正）：
- 属性调用形式的装饰器：`@router.websocket(...)`、`@event.listens_for(...)`；
- pydantic `@field_validator` / `@model_validator`；
- 同文件 `partial(Handler, ...)` 注册（http.server 框架约定）；
- 字符串/字典注册表（如 ACP 工具名注册、DAO 按名导出）。

## 2. 结论分级

### A0. 整文件可删（最高置信）

| 文件 | 行数 | 依据 |
|---|---|---|
| `backend/app/services/tool_stream.py` | 205 | 全库零 import；文件内仅 3 处自身 logger 字符串。其中 `subscribe_tool_output`/`publish_tool_output`（34 行）亦在死方法清单内，整文件删除一并覆盖 |
| `backend/app/dao/agent_run_dao.py` | 201 | `agent_run_dao` 实例全库零使用（11 个方法 155 行全部无引用，生产与测试均无）；无事件监听器等副作用。删除时需一并清理：`dao/__init__.py` 第 6、29 行的导入与导出，以及同样零引用的 `backend/app/dao/agent_run_event_dao.py`（仅一个 re-export 链） |

> **修正勘误**：审查中期曾将 4 个沙箱后端文件（`local/docker_backend.py` 213 行、
> `api/judge0_backend.py` 196 行、`remote/self_hosted_backend.py` 201 行、
> `remote/aio_sandbox_backend.py` 192 行）列为整文件死代码——**该结论有误，已撤销**。
> 复核发现 `services/sandbox/registry.py::_register_builtin_backends()` 静态导入并注册全部
> 8 个后端（含这 4 个），且 `SandboxConfig.from_dict()` 接受租户配置的 `sandbox_type`
> 字符串（全局还有环境变量 `SANDBOX_TYPE` 可切换）。它们属于**配置门控的功能开关**，
> 生产默认配置（subprocess / e2b / android-build）虽不触达，但不构成死代码。
> 若产品明确决定下线 docker / judge0 / self-hosted / aio-sandbox，需连同 `SandboxType`
> 枚举、registry 注册表、各 `__init__.py` 导出一起删除，建议先由 PM 确认。

### A. 高置信可删（81 项 / 1405 行）

判定标准：短名与类名在全库任何形式下零外部出现（git grep -w 逐一验证）、无
`getattr` 字符串调度。已从机械清单剔除 6 个复核确认为误报的项（见 §4）。

| 文件 | 行数 | 死代码项（行数） |
|---|---|---|
| `backend/app/services/dingtalk_stream.py` | 147 | `_send_dingtalk_media_message` (96)、`_upload_dingtalk_media` (51) |
| `backend/app/services/llm/context_compressor.py` | 147 | `_dedup_file_tool_results` (71)、`_dedup_list_tool_results` (64)、`_get_ctx_guard_ratios` (12) |
| `backend/app/services/okr_reporting.py` | 41 | `list_company_members` (41) |
| `backend/app/services/sso_service.py` | 48 | `auto_associate_tenant` (40)、`add_domain_hint` (8) |
| `backend/app/services/llm/compression_config.py` | 65 | `pre_round_budget_post_fold` (39)、`layer1_compress_threshold_ratio` (10)、`read_lifecycle_config_from_settings` (10)、`_normalized_exclude_tools` (3)、`is_tier2_lossless_only` (3) |
| `backend/app/services/llm/truncate_caps.py` | 37 | `apply_cross_session_read_hints` (37) |
| `backend/app/services/audit_logger.py` | 84 | `write_identity_audit_log` (34)、`write_role_audit_log` (30)、`write_tenant_audit_log` (20) |
| `backend/app/services/focus_service.py` | 32 | `render_focus_context` (32) |
| `backend/app/services/agent_tools.py` | 233 | `_agentbay_find_installed_app_match` (31)、`_feishu_append_doc` (26)、`_get_feishu_token` (26)、`_feishu_read_doc` (24)、`_feishu_create_doc` (23)、`_get_agent_owner_info` (21)、`_search_bing` (17)、`_search_google` (17)、`_get_scoped_agentbay_client` (14)、`_agentbay_visible_apps_note` (10)、`_feishu_contacts_refresh` (9)、`_search_tavily` (7)、`_materialize_storage_workspace` (5)、`_agentbay_uncertain_start_error` (3) |
| `backend/app/services/quota_guard.py` | 33 | `check_agent_llm_quota` (30)、`get_agent_expiry_reply` (3) |
| `backend/app/api/agentbay_control.py` | 29 | `_cdp_exec` (29) |
| `backend/app/services/llm/tool_trim.py` | 74 | `_dispatch_guarded_result` (27)、`_dispatch_guarded` (26)、`_effective_tool_budget` (11)、`_hard_head_tail` (10) |
| `backend/app/plugins/clawith_acp/tool_bridge.py` | 37 | `_list_files_local` (27)、`_is_project_file` (10) |
| `backend/app/api/enterprise.py` | 36 | `normalize_oauth2_config` (26)、`from_config_dict` (10) |
| `backend/app/dao/chat_session_dao.py` | 93 | `get_or_create_primary_direct` (25)、`list_by_group` (23)、`list_for_user` (20)、`touch_last_message_at` (14)、`find_by_external_conv_id` (11) |
| `backend/app/dao/chat_message_dao.py` | 36 | `list_by_conversation` (17)、`get_last_by_conversation` (12)、`bulk_create` (7) |
| `backend/app/services/chat_session_service.py` | 16 | `get_primary_platform_session` (16) |
| `backend/app/api/agents.py` | 15 | `_agents_to_out` (15) |
| `backend/app/services/credential_crypto.py` | 25 | `decrypt_credential` (15)、`encrypt_credential` (7)、`is_encrypted` (3) |
| `backend/app/services/resource_discovery.py` | 14 | `refresh_atlassian_rovo_api_key` (14) |
| `backend/app/dao/agent_access_dao.py` | 39 | `list_active_admin_user_ids_by_tenant` (13)、`list_custom_permission_user_ids` (11)、`list_active_user_ids_by_tenant` (10)、`get_org_member` (5) |
| `backend/app/services/agentbay_client.py` | 12 | `cleanup_agentbay_sessions` (12) |
| `backend/app/services/agentbay_live.py` | 12 | `detect_agentbay_env` (12) |
| `backend/app/services/llm/content_router.py` | 10 | `_retrieve_tool_available` (10) |
| `backend/app/services/tool_config.py` | 10 | `delete_tenant_tool_config` (10) |
| `backend/app/api/wecom.py` | 9 | `_encrypt_msg` (9) |
| `backend/app/services/token_tracker.py` | 9 | `extract_usage_tokens` (9) |
| `backend/app/services/dingtalk_token.py` | 8 | `get_corp_token` (8) |
| `backend/app/api/files.py` | 13 | `_safe_path` (7)、`_enterprise_info_dir` (3)、`_enterprise_kb_dir` (3) |
| `backend/app/services/onboarding.py` | 7 | `mark_onboarded` (7) |
| `backend/app/services/storage_runtime/s3.py` | 6 | `_is_header_parsing_error` (6) |
| `backend/app/plugins/clawith_acp/acp_features.py` | 6 | `enabled_features` (6) |
| `backend/app/services/heartbeat.py` | 6 | `start_heartbeat` (6) |
| `backend/app/services/email_verification_service.py` | 4 | `build_email_verification_url` (4) |
| `frontend/src/utils/theme.ts` | 3 | `darken` (3) |
| `backend/app/core/permissions.py` | 5 | `is_company_visible_agent` (3)、`_non_private_mode` (2) |
| `backend/app/plugins/clawith_acp/turn_budget.py` | 4 | `default_llm_call_timeout_seconds` (2)、`default_tool_timeout_seconds` (2) |

备注：`agent_tools.py` 中 14 项为旧工具分发器时代的私有实现（新路径
`execute_builtin_tool_outcome` 已改用 `_*_outcome` 版本），删除风险最低。
`tool_stream.py` 中的两个函数已归入 A0。

### B. 类活但方法无引用——需人工确认（39 项 / 1635 行，另 9 项归 A0）

类本身仍被使用，以下方法零引用；删除风险低，但建议每条删除前再跑一次
`git grep -w` 终验（框架反射/动态调用不在图谱视野内）：

| 文件 | 行数 | 候选死方法（行数） |
|---|---|---|
| `frontend/src/pages/agent-detail/AgentDetailPage.tsx` | 736 | `RelationshipEditor` (695)、`parseFocusItems` (41) |
| `backend/app/services/feishu_service.py` | 209 | `login_or_register` (132)、`exchange_code_for_user` (30)、`create_approval_instance` (15)、`query_approval_instances` (13)、`append_feishu_doc_blocks` (10)、`get_approval_instance` (9) |
| `frontend/src/pages/enterprise-settings/tabs/OrgTab.tsx` | 109 | `SsoStatus` (109) |
| `frontend/src/pages/enterprise-settings/tabs/SkillsTab.tsx` | 91 | `BroadcastSection` (91) |
| `frontend/src/pages/AdminCompanies.tsx` | 102 | `EditCompanyModal` (91)、`insertVariable` (11) |
| `backend/app/services/mcp_client.py` | 44 | `_sse_connect` (44) |
| `backend/app/services/auth_registry.py` | 104 | `update_provider` (41)、`create_provider` (34)、`delete_provider` (26)、`clear_all_cache` (3) |
| `backend/app/dao/agent_dao.py` | 84 | `upsert_permission` (28)、`get_with_models` (17)、`delete_permission` (16)、`update_last_active` (12)、`count_active` (11) |
| `backend/app/services/org_sync_adapter.py` | 21 | `_resolve_platform_user` (21) |
| `backend/app/dao/group_dao.py` | 50 | `list_groups_for_participant` (20)、`list_members` (15)、`remove_member` (15) |
| `backend/app/services/agentbay_client.py` | 19 | `get_live_url` (19) |
| `frontend/src/pages/EnterpriseSettings.tsx` | 24 | `saveJinaKey` (14)、`clearJinaKey` (10) |
| `backend/app/schemas/schemas.py` | 11 | `serialize_extra_config` (11) |
| `backend/app/services/storage_runtime/s3.py` | 6 | `_put_succeeded` (6) |
| `frontend/src/components/WorkspaceOperationPanel.tsx` | 8 | `parentDir` (5)、`isPreviewable` (3) |
| `backend/app/services/agent_runtime/card_stream_bridge.py` | 9 | `force_flush` (4)、`_push_tool_updates` (3)、`mark_flushed` (2) |
| `backend/app/plugins/clawith_acp/turn_budget.py` | 6 | `check_compute_or_raise` (3)、`check_workflow_or_raise` (3) |
| `backend/app/services/sandbox/local/subprocess_backend.py` | 2 | `_venv_python` (2) |

已归入 A0、不在此重复列出：`agent_run_dao.py` 的 9 个方法（144 行）。

### C. 按名引用——疑似误报 / 低优先级（31 项 / 952 行，另 1 项归 A0）

图谱判定无引用，但按名扫描发现外部同名引用，多为注册表、JSX 或测试引用，
**默认不建议删除**：

| 文件 | 行数 | 项 | 引用性质 |
|---|---|---|---|
| `backend/app/services/agent_tools.py` | 694 | `_plaza_add_comment` (145)、`_search_files` (82)、`_plaza_create_post` (76)、`_find_files` (63)、`_edit_file` (60)、`_read_file` (56)、`_list_files` (54)、`_plaza_get_new_posts` (52)、`_write_file` (31)、`_delete_file` (23)、`_execute_via_smithery_connect` (20)、`_execute_code_legacy` (18)、`_query_text_match_rank` (12)、`_department_name` (2) | `_read_file` 等注册于 acp_routes.py；`_execute_code_legacy`、`_execute_via_smithery_connect` 仅测试引用 |
| `backend/app/services/feishu_service.py` | 78 | `resolve_open_id` (39)、`resolve_user_id` (39) | 同名/变体引用，待逐条确认 |
| `backend/app/services/mcp_client.py` | 36 | `call_tool` (36) | 同名引用 |
| `backend/app/dao/group_dao.py` | 38 | `add_member` (27)、`get_member` (11) | DAO 按名导出 |
| `backend/app/dao/chat_message_dao.py` | 27 | `create_message` (27) | 仅测试引用（低优先级候选） |
| `backend/app/plugins/clawith_acp/list_dedup.py` | 26 | `agent_debug_log` (26) | 待确认 |
| `backend/app/services/resource_discovery.py` | 16 | `import_mcp_direct` (14)、`search_smithery` (2) | 同名引用 |
| `backend/app/services/sandbox/local/android_build_metrics.py` | 12 | `record_build` (12) | 指标注册表 |
| `backend/app/services/group_file_service.py` | 7 | `_entry_version` (7) | 待确认 |
| `backend/app/dao/agent_access_dao.py` | 5 | `get_user` (5) | DAO 按名导出 |
| `backend/app/plugins/clawith_acp/acp_handler.py` | 4 | `_send_json` (4) | 同名引用 |
| `backend/app/services/agent_runtime/tool_execution.py` | 3 | `execution_policy` (3) | 待确认 |
| `backend/app/api/tools.py` | 2 | `_get_sensitive_keys` (2) | 同名引用 |
| `backend/app/services/trigger_daemon.py` | 4 | `_mark_trigger_skipped` (2)、`_should_skip_non_workday` (2) | 待确认 |

已归入 A0：`agent_run_dao.py` 的 `get_run` (9)。

## 3. 统计摘要

| 分级 | 项数 | 行数 | 建议 |
|---|---|---|---|
| A0 整文件 | 2 个文件（+1 个 re-export 文件 + 2 行导出） | 406+ | 可直接删 |
| A 高置信 | 81 | 1405 | 可直接删（逐条 git grep -w 已验） |
| B 需人工确认 | 39（另有 9 项归 A0） | 1635 | 大概率可删，删除前终验 |
| C 疑似误报 | 31（另有 1 项归 A0） | 952 | 默认不动，低优先级复核 |

合计若全部落实：约 **3400+ 行**生产死代码（不含 C）。

## 4. 已识别并剔除的误报（不要删）

| 项 | 位置 | 误报原因 |
|---|---|---|
| `do_GET` / `do_POST` | `backend/app/services/skill_creator_files/eval-viewer__generate_review.py` | 同文件 `partial(ReviewHandler, ...)` 注册，http.server 框架约定 |
| `acp_endpoint` | `backend/app/plugins/clawith_acp/router.py` | `@router.websocket` 装饰器注册，图为捕获不到 |
| `websocket_chat` | `backend/app/api/websocket.py` | 同上，`@router.websocket("/ws/chat/{agent_id}")` 且挂载于 main.py |
| `_inject_tenant_on_insert` | `backend/app/dao/base.py` | `@event.listens_for` SQLAlchemy 监听器 |
| `_claim_renewal_precedes_expiry` / `_blank_optional_runtime_values` / `_nonempty_runtime_identifiers` | `backend/app/config.py` | pydantic `@field_validator` / `@model_validator` |
| `send_to_session` | `backend/app/api/websocket.py` | 活动 ws manager 的公开方法，仅定义处出现——归入 B 级，删除前需确认无外部消费者 |

## 5. 操作建议与后续工作

1. **本次未做任何删除**。若要落地，建议顺序：A0 → A → B（每条 `git grep -w <名>`
   终验后删）→ 全量后端 pytest + 前端 build 回归。
2. 删除 `agent_run_dao.py` 时务必同步清理 `dao/__init__.py`（6/29 行）与
   `agent_run_event_dao.py` 的 re-export，避免 import 断裂。
3. 未覆盖的分析方向（可作后续）：同文件引用类（103 项）抽查、SIMILAR_TO 近似重复
   代码检测、C 类中仅测试引用项的处置决策。
4. 图谱对装饰器/字符串注册天然失明，任何删除决定建议配合文本级 grep 终验，
   不要仅依据图判定。

---
*数据来源：codebase-memory 项目 `Users-shubinzhang-Documents-agent-Clawith` 图谱导出 +
全库 git grep 复核。中间产物与脚本位于本次会话 scratchpad 的 `analysis/` 目录。*

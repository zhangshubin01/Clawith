# Graph Report - .  (2026-05-09)

## Corpus Check
- 415 files · ~747,220 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 5371 nodes · 22148 edges · 205 communities detected
- Extraction: 33% EXTRACTED · 67% INFERRED · 0% AMBIGUOUS · INFERRED: 14812 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## God Nodes (most connected - your core abstractions)
1. `User` - 1110 edges
2. `Agent` - 909 edges
3. `ChatMessage` - 499 edges
4. `IdentityProvider` - 499 edges
5. `LLMModel` - 489 edges
6. `Tenant` - 487 edges
7. `OrgMember` - 472 edges
8. `ChannelConfig` - 452 edges
9. `ChatSession` - 449 edges
10. `Participant` - 388 edges

## Surprising Connections (you probably didn't know these)
- `Generate a fake tool result of given length to simulate accumulated context.` --uses--> `LLMModel`  [INFERRED]
  test_llm_context_size.py → backend/app/models/llm.py
- `Build messages simulating N rounds of tool calls with accumulated results.` --uses--> `LLMModel`  [INFERRED]
  test_llm_context_size.py → backend/app/models/llm.py
- `Test model latency with given number of simulated tool rounds.` --uses--> `LLMModel`  [INFERRED]
  test_llm_context_size.py → backend/app/models/llm.py
- `Test a model's streaming latency.` --uses--> `LLMModel`  [INFERRED]
  test_llm_latency.py → backend/app/models/llm.py
- `Clawith ACP Thin Client — IDE 侧瘦客户端（JetBrains Agent Client Protocol）  通过 WebSock` --uses--> `AgentSideConnection`  [INFERRED]
  clawith_acp/server.py → backend/app/plugins/clawith_acp/connection.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.02
Nodes (435): ensure_access_granted_platform_relationships(), Helpers that keep access permissions and relationship prerequisites aligned., Ensure private/custom platform users are in the agent's human network.      Plat, DailyTokenUsage, CompanyCreateRequest, CompanyCreateResponse, CompanyStats, create_company() (+427 more)

### Community 1 - "Community 1"
Cohesion: 0.01
Nodes (297): Agent, AgentManager, Agent lifecycle manager — Docker container management for OpenClaw Gateway insta, Generate openclaw.json config for the agent container., Start an OpenClaw Gateway Docker container for the agent.          Returns conta, Stop the agent's Docker container., Manage OpenClaw Gateway Docker containers for digital employees., Stop and remove the agent's Docker container. (+289 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (411): Build rich system prompt context for agents.  Loads soul, memory, skills summary, Parse YAML frontmatter from a skill .md file.      Returns (name, description)., Load skill index with LRU cache + mtime invalidation., Build skills index table from skills/ directory.      Supports two formats:, Build a rich system prompt incorporating agent's full context.      Reads from w, Return the canonical persistent workspace path for an agent., Read a file with LRU cache + mtime invalidation.      Only caches static files (, Read a file, return empty string if missing. Truncate if too long. (+403 more)

### Community 3 - "Community 3"
Cohesion: 0.01
Nodes (141): fetchJson(), handleCreate(), handleNotificationBarToggle(), handleSendTestEmail(), handleToggle(), handleToggleSetting(), loadCompanies(), saveEmailConfig() (+133 more)

### Community 4 - "Community 4"
Cohesion: 0.01
Nodes (234): _cdp_exec(), ClickRequest, control_click(), control_current_url(), control_drag(), control_lock(), control_press_keys(), control_screenshot() (+226 more)

### Community 5 - "Community 5"
Cohesion: 0.04
Nodes (197): authorize(), bind_identity(), change_password(), check_duplicate(), forgot_password(), get_email_hint(), get_me(), get_my_tenants() (+189 more)

### Community 6 - "Community 6"
Cohesion: 0.02
Nodes (217): _agent_base_dir(), agent_import_from_clawhub(), agent_import_from_url(), ClawhubImportBody, delete_enterprise_file(), delete_file(), _detect_csv_delimiter(), download_file() (+209 more)

### Community 7 - "Community 7"
Cohesion: 0.02
Nodes (219): _agent_has_any_channel(), _agent_has_feishu(), _agentbay_app_field(), _agentbay_browser_click(), _agentbay_browser_extract(), _agentbay_browser_login(), _agentbay_browser_navigate(), _agentbay_browser_observe() (+211 more)

### Community 8 - "Community 8"
Cohesion: 0.02
Nodes (114): ABC, AboutCommand, about command - display version information about Clawith ACP., AcpCommand, AgentsCommand, AioSandboxBackend, aio-sandbox backend.      Connects to aio-sandbox (https://github.com/agent-infr, Check if aio-sandbox service is available. (+106 more)

### Community 9 - "Community 9"
Cohesion: 0.02
Nodes (140): _allowed_tool_names(), call_agent_llm(), call_agent_llm_with_tools(), call_llm(), call_llm_with_failover(), _check_tool_requires_args(), _convert_messages_for_vision(), FailoverGuard (+132 more)

### Community 10 - "Community 10"
Cohesion: 0.01
Nodes (167): Initial schema — create all tables for fresh deployments.  env.py already import, get_agent_activity(), get_conversation_messages(), list_conversations(), AgentActivityLog, Activity log model for tracking agent actions., Records every action taken by a digital employee., Rolled up token consumption per agent per day for time-series analytics. (+159 more)

### Community 11 - "Community 11"
Cohesion: 0.04
Nodes (120): create_template(), delegate_task(), DelegateRequest, delete_template(), get_agent_metrics(), get_template(), handover_agent(), HandoverRequest (+112 more)

### Community 12 - "Community 12"
Cohesion: 0.04
Nodes (62): AgentSideConnection, ndJson stream connection over WebSocket for ACP.  This module handles the ndJson, ACP Agent-side connection wrapping ndJson stream over WebSocket., Read one line from stdin, parse as JSON., Send one JSON message as a line., Close the connection., RPC: ask IDE to read a text file., RPC: ask IDE to write a text file. (+54 more)

### Community 13 - "Community 13"
Cohesion: 0.04
Nodes (55): extract_config_schema(), extract_skill_metadata(), Extract metadata from Superpowers SKILL.md.      Superpowers skills can have YAM, Convert Superpowers skill to Clawith Skill create/update dict., Extract JSON Schema for configuration from metadata., to_clawith_skill(), Client for interacting with Superpowers Marketplace git repository., Check if the marketplace repo is already cloned. (+47 more)

### Community 14 - "Community 14"
Cohesion: 0.04
Nodes (47): AgentBayClient, cleanup_agentbay_sessions(), get_agentbay_api_key_for_agent(), get_agentbay_client_for_agent(), _inject_credentials(), _is_plausible_agentbay_api_key(), AgentBay API client using official SDK.  This module provides a client wrapper a, Navigate browser to URL using SDK.          The AgentBay SDK default navigation (+39 more)

### Community 15 - "Community 15"
Cohesion: 0.05
Nodes (62): AuditAction, Helper to write audit log entries from background services., Write audit log for role-related events.      Args:         action: Role action, Standard audit action types., Write audit log for tenant-related events.      Args:         action: Tenant act, Internal method to write audit log., Write a single audit log entry using raw SQL.      Uses raw SQL to avoid ORM for, Write audit log for identity-related events.      Args:         action: Identity (+54 more)

### Community 16 - "Community 16"
Cohesion: 0.07
Nodes (30): _AsyncSessionCtx, _FakeAsyncSessionFactory, _FakeDb, _load_thin_server_module(), patch_acp_async_session(), Unit / integration-style tests for the clawith-acp WebSocket bridge (no real IDE, Inject a fake async_session factory; restore after test., Must not hit DB when history already present. (+22 more)

### Community 17 - "Community 17"
Cohesion: 0.09
Nodes (32): DummyResult, _make_agent(), _make_participant(), _make_tenant(), Tests for async A2A msg_type differentiation (notify/consult/task_delegate).  Va, notify msg_type should return immediately without calling LLM., task_delegate should create a focus item and an on_message trigger., consult msg_type should call LLM synchronously and return reply. (+24 more)

### Community 18 - "Community 18"
Cohesion: 0.07
Nodes (41): _apply_category_filter(), broadcast_notification(), BroadcastRequest, get_unread_count(), list_notifications(), mark_all_read(), mark_read(), Notification model — notifications for users and agents. (+33 more)

### Community 19 - "Community 19"
Cohesion: 0.08
Nodes (12): client(), FakeAsyncSessionFactory, FakeQuery, FakeScalarResult, FakeSession, FakeSkill, QueryField, RaiseOnInstanceAccess (+4 more)

### Community 20 - "Community 20"
Cohesion: 0.11
Nodes (31): _agent_visible_tool_clause(), create_tool(), _decrypt_sensitive_fields(), delete_agent_tool(), delete_category_config(), delete_tool(), _encrypt_sensitive_fields(), get_agent_tool_config() (+23 more)

### Community 21 - "Community 21"
Cohesion: 0.08
Nodes (25): DingTalkStreamManager, download_dingtalk_media(), _download_file(), _fire_and_forget(), _get_media_download_url(), _handle_media_and_dispatch(), _process_media_message(), DingTalk Stream Connection Manager.  Manages WebSocket-based Stream connections (+17 more)

### Community 22 - "Community 22"
Cohesion: 0.08
Nodes (8): BaseOrgSyncAdapter, _DummyAdapter, _FakeDB, _SyncAdapterWithFailure, test_sync_org_structure_skips_reconcile_after_member_failure(), test_validate_member_identifiers_allows_wecom_without_unionid(), test_validate_member_identifiers_rejects_unionid_equal_to_external_id(), test_validate_member_identifiers_requires_unionid_for_feishu()

### Community 23 - "Community 23"
Cohesion: 0.15
Nodes (28): _bucket_items(), _build_company_daily_content(), _build_company_rollup_content(), _contains_risk(), _dedupe_preserve_order(), _default_report_headings(), _extract_section_lines(), generate_company_daily_report() (+20 more)

### Community 24 - "Community 24"
Cohesion: 0.12
Nodes (18): DummyResult, _make_identity(), _make_login_data(), _make_user(), Unit tests for the authentication API (app/api/auth.py)., Login with a nonexistent user returns 401., Login with wrong password returns 401., Login with a disabled account returns 403. (+10 more)

### Community 25 - "Community 25"
Cohesion: 0.09
Nodes (22): Compatibility exports for HTML document conversion helpers., generate_html(), main(), Generate HTML report from loop output data. If auto_refresh is True, adds a meta, improve_description(), main(), Call Claude to improve the description based on eval results., find_project_root() (+14 more)

### Community 26 - "Community 26"
Cohesion: 0.13
Nodes (16): build_wechat_headers(), _extract_wechat_text(), _process_wechat_message(), random_wechat_uin(), WeChat iLink Bot long-poll manager and client helpers., Manage WeChat iLink long-poll workers per agent., Raised when the remote iLink session has expired., Generate X-WECHAT-UIN according to the protocol spec. (+8 more)

### Community 27 - "Community 27"
Cohesion: 0.09
Nodes (23): create_access_token(), decode_access_token(), decrypt_data(), encrypt_data(), get_authenticated_user(), get_current_admin(), get_current_user(), hash_password() (+15 more)

### Community 28 - "Community 28"
Cohesion: 0.11
Nodes (22): force_ipv4(), _ipv4_getaddrinfo(), Core email utilities for SMTP operations and network compatibility., Wrapper that forces AF_INET (IPv4) to avoid IPv6 failures in Docker., Context manager that forces all socket connections to use IPv4.      Docker cont, Synchronously send an email via SMTP with IPv4 enforcement.      Three connectio, send_smtp_email(), _decode_header_value() (+14 more)

### Community 29 - "Community 29"
Cohesion: 0.11
Nodes (13): _build_wecom_conv_id(), _disable_wecom_sdk_proxy(), _process_wecom_stream_message(), WeCom (企业微信) AI Bot WebSocket Long Connection Manager.  Uses the wecom-aibot-sdk, Force the WeCom SDK websocket path to bypass system proxies., Stop a running WebSocket client for an agent., Start WebSocket clients for all configured WeCom agents with bot credentials., Return status of all active WebSocket clients. (+5 more)

### Community 30 - "Community 30"
Cohesion: 0.13
Nodes (18): BaseHTTPRequestHandler, build_run(), embed_file(), find_runs(), _find_runs_recursive(), generate_html(), get_mime_type(), _kill_port() (+10 more)

### Community 31 - "Community 31"
Cohesion: 0.12
Nodes (8): DummyResult, make_agent(), make_user(), _NestedTransaction, RecordingDB, TaskCleanupDB, test_archive_agent_task_history_writes_json_snapshot(), test_delete_agent_cleans_remaining_foreign_key_rows()

### Community 32 - "Community 32"
Cohesion: 0.12
Nodes (11): FeishuWSManager, _make_no_proxy_connect(), Feishu WebSocket Long Connection Manager., Handle im.message.receive_v1 events from Feishu WebSocket asynchronously., Spawns a WebSocket client fully asynchronously inside FastAPI's loop., Return a drop-in replacement for websockets.connect that forces proxy=None., Stops an actively running WebSocket client for an agent., Start WS clients for all configured Feishu agents. (+3 more)

### Community 33 - "Community 33"
Cohesion: 0.12
Nodes (11): LSP4J 插件上下文变量定义。  将 ContextVar 集中在独立模块中，避免 router.py 与 tool_hooks.py 之间的循环导入。 所有, LSP4J IDE 工具常量定义。  定义插件端可识别的工具名称、名称映射及显示名称， 供 JSON-RPC 路由、tool hooks 及工具调用同步使用。, _extract_file_path(), install_lsp4j_tool_hooks(), _is_agent_internal_path(), LSP4J 工具调用钩子 — 执行路径 + 注册路径。  扩展 ACP 已安装的 _custom_execute_tool 和 _custom_get_tool, 从工具参数中提取文件路径。      LLM 可能使用不同的参数名调用同一工具：       - filePath  (camelCase) — 插件原生协议, 判断路径是否属于 Agent 内部文件（应在本地执行，不路由到 IDE）。 (+3 more)

### Community 34 - "Community 34"
Cohesion: 0.16
Nodes (13): android_module_tier(), collect_android_values_xml_hits(), contains_han(), filename_keyword_for_search_file(), is_extension_only_language_glob(), is_unusable_natural_language_file_query(), longest_latin_identifier(), LSP4J 本地检索纯函数（无 DB / 异步依赖），供 jsonrpc_router 与单测复用。 (+5 more)

### Community 35 - "Community 35"
Cohesion: 0.18
Nodes (17): _clean_cell(), _extract_docx(), _extract_pdf(), _extract_pptx(), extract_text(), _extract_xlsx(), _markdown_table(), needs_extraction() (+9 more)

### Community 36 - "Community 36"
Cohesion: 0.12
Nodes (9): Send a JSON-RPC request via Streamable HTTP transport., Send a JSON-RPC request via SSE transport.          Opens a fresh SSE connection, Auto-detect transport and send request.          Strategy: If transport is alrea, Fetch available tools from the MCP server., Execute a tool on the MCP server., Build request headers with proper MCP and auth headers., Parse response — handles both JSON and SSE (text/event-stream) formats., Extract the last JSON-RPC result from an SSE stream. (+1 more)

### Community 37 - "Community 37"
Cohesion: 0.22
Nodes (17): _check_new_agent_messages(), _cleanup_stale_invoke_cache(), _evaluate_trigger(), _extract_json_path(), _handle_okr_collection_trigger(), _handle_okr_report_trigger(), _invoke_agent_for_triggers(), _is_private_url() (+9 more)

### Community 38 - "Community 38"
Cohesion: 0.15
Nodes (17): compress_bytes_to_base64(), compress_screenshot_to_base64(), _draw_coordinate_grid(), pop_temp_screenshot(), _prune_expired_cache(), Vision injection utilities for AgentBay screenshot tools.  Architecture: "Epheme, Overlay a light desktop-coordinate grid for LLM-only screenshot analysis., Compress raw image bytes to a base64 JPEG data URL.      Resizes to _MAX_WIDTH ( (+9 more)

### Community 39 - "Community 39"
Cohesion: 0.17
Nodes (7): DummyResult, RecordingDB, test_create_session_returns_web_session_shape(), test_creator_can_list_all_sessions(), test_creator_can_view_other_users_session_messages(), test_org_admin_can_list_all_sessions(), test_org_admin_can_view_other_users_session_messages()

### Community 40 - "Community 40"
Cohesion: 0.18
Nodes (16): _load_from(), _make_valid_plugin(), Calling load_plugins twice must not register routes twice., Plugin that exports a non-ClawithPlugin 'plugin' attribute must be skipped., Create a minimal valid plugin directory in tmp_path., Run load_plugins but point _PLUGINS_DIR at tmp_path., Empty plugins directory must not raise., A valid plugin with plugin.json and __init__.py registers its routes. (+8 more)

### Community 41 - "Community 41"
Cohesion: 0.26
Nodes (7): DummyResult, make_channel(), make_user(), RecordingDB, test_delete_wecom_channel_stops_runtime_client(), test_get_wecom_channel_marks_webhook_mode_disconnected(), test_get_wecom_channel_reports_runtime_websocket_status()

### Community 42 - "Community 42"
Cohesion: 0.16
Nodes (8): EmailVerificationService, Email verification token lifecycle helpers., Email verification token lifecycle helpers., Hash a raw verification token before persistence or lookup., Create a new 6-digit email verification code and store in Redis., Build the user-facing verification URL. Note: now uses 6-digit code., Load a valid verification code from Redis and mark it used (by deleting)., Send an email verification code using the configured template.

### Community 43 - "Community 43"
Cohesion: 0.31
Nodes (7): DummyResult, _make_agent(), RecordingDB, test_custom_boundary_follow_up_keeps_tools_enabled(), test_custom_follow_up_keeps_tools_enabled(), test_first_contact_is_the_only_tool_free_greeting_turn(), test_template_follow_up_keeps_tools_enabled()

### Community 44 - "Community 44"
Cohesion: 0.33
Nodes (12): _build_okr_snapshot(), collect_all_focus_updates(), _compute_period(), _format_monthly_report_body(), _format_report_body(), generate_daily_report(), generate_monthly_report(), generate_weekly_report() (+4 more)

### Community 45 - "Community 45"
Cohesion: 0.15
Nodes (12): TOOL_DEFINITIONS must have list_agents and call_agent with required fields., http_list_agents should return formatted agent list., http_list_agents should handle empty list gracefully., http_call_agent should return reply with session_id appended., http_call_agent should raise ValueError on 404., http_call_agent should raise ValueError when agent_id is empty., test_http_call_agent_404(), test_http_call_agent_no_agent_id() (+4 more)

### Community 46 - "Community 46"
Cohesion: 0.23
Nodes (6): _extract_message_text(), WhatsApp Cloud API channel routes., _send_whatsapp_messages(), _split_text(), _verify_signature(), whatsapp_event_webhook()

### Community 47 - "Community 47"
Cohesion: 0.23
Nodes (11): download_dingtalk_media(), get_dingtalk_access_token(), DingTalk service for sending messages via Open API., Unified message sending method.          Default behavior is sending via Robot O, Download a media file from DingTalk using a downloadCode.      Convenience wrapp, Send single chat messages via Robot using modern v1.0 API (RECOMMENDED)., Get DingTalk access_token using app_id and app_secret.      API: https://open.di, Send a work notification (工作通知).          API: https://open.dingtalk.com/documen (+3 more)

### Community 48 - "Community 48"
Cohesion: 0.27
Nodes (11): _ensure_smithery_connection(), _get_modelscope_api_token(), _get_smithery_api_key(), import_mcp_direct(), import_mcp_from_smithery(), refresh_atlassian_rovo_api_key(), _search_modelscope_api(), search_registries() (+3 more)

### Community 49 - "Community 49"
Cohesion: 0.24
Nodes (11): aggregate_results(), calculate_stats(), generate_benchmark(), generate_markdown(), load_run_results(), main(), Aggregate run results into summary statistics.      Returns run_summary with sta, Generate complete benchmark.json from run results. (+3 more)

### Community 50 - "Community 50"
Cohesion: 0.23
Nodes (4): _FakeAsyncClient, _FakeResponse, test_patch_message_raises_when_business_code_nonzero(), test_send_message_raises_when_business_code_nonzero()

### Community 51 - "Community 51"
Cohesion: 0.17
Nodes (11): Basic smoke tests for the clawith_acp plugin. These tests verify that the main m, Test importing the connection module., Test importing the file_system_service module., Test importing the types module., Test importing the errors module., Test importing the router module., test_import_connection(), test_import_errors() (+3 more)

### Community 52 - "Community 52"
Cohesion: 0.18
Nodes (0): 

### Community 53 - "Community 53"
Cohesion: 0.22
Nodes (9): compress_image_if_needed(), compress_image_if_needed_async(), process_ide_image(), process_ide_image_async(), Vision handler for IDEA plugin integration., Async entry point for callers in asyncio contexts., Compress image if it exceeds the size threshold.          Args:         base64_d, Process Base64 image data from IDEA plugin.          Args:         base64_data: (+1 more)

### Community 54 - "Community 54"
Cohesion: 0.2
Nodes (9): cleanup_pending_calls(), Tool call handler for IDEA plugin integration., Send a tool call request to the connected IDE plugin., Wait for the IDEA plugin to return a tool result., Resolve a pending tool call with the result from the IDEA plugin., Clean up all pending tool calls (e.g., when WebSocket disconnects)., resolve_ide_tool_result(), send_ide_tool_request() (+1 more)

### Community 55 - "Community 55"
Cohesion: 0.29
Nodes (7): _build_qrcode_headers(), create_wechat_qrcode(), get_wechat_qrcode_image(), get_wechat_qrcode_status(), WeChat iLink Bot channel API routes., _route_tag(), _validate_qrcode_proxy_url()

### Community 56 - "Community 56"
Cohesion: 0.2
Nodes (6): Services for managing IDEA plugin session context., Manages IDEA plugin session context information., Update IDEA plugin session context., Get session context for building prompts., Get the latest IDE context for an agent's most recent session., SessionContextManager

### Community 57 - "Community 57"
Cohesion: 0.24
Nodes (6): PlatformService, Platform-wide service for URL resolution and host type detection., Check if a host is an IP address (IPv4)., Resolve the platform's public base URL with priority lookup.                  Pr, Generate the SSO base URL for a tenant based on IP/Domain logic., Service to handle platform-wide settings and URL resolution.

### Community 58 - "Community 58"
Cohesion: 0.24
Nodes (9): _agent_workspace(), _load_skills_index(), _parse_skill_frontmatter(), Build rich system prompt context for agents.  Loads soul, memory, skills summary, Return the canonical persistent workspace path for an agent., Read a file, return empty string if missing. Truncate if too long., Parse YAML frontmatter from a skill .md file.      Returns (name, description)., Load skill index (name + description) from skills/ directory.      Supports two (+1 more)

### Community 59 - "Community 59"
Cohesion: 0.31
Nodes (8): _get_okr_agent(), hook_new_agent(), hook_new_org_member(), Hook to automatically bind new users and company-visible agents to the OKR Agent, When a new OrgMember is created or bound, bind them to the system OKR Agent if i, Bind all existing active platform users in a tenant to its OKR Agent.      hook_, When a new company-visible agent is created, bind to OKR Agent., sync_okr_agent_platform_members()

### Community 60 - "Community 60"
Cohesion: 0.44
Nodes (8): _append_seed_marker(), _ensure_okr_tool_rows_exist(), patch_existing_okr_agent(), seed_default_agents(), seed_okr_agent(), seed_okr_agent_for_tenant(), _seed_okr_triggers(), _sync_okr_triggers_with_settings()

### Community 61 - "Community 61"
Cohesion: 0.28
Nodes (7): main(), package_skill(), Check if a path should be excluded from packaging., Package a skill folder into a .skill file.      Args:         skill_path: Path t, should_exclude(), Basic validation of a skill, validate_skill()

### Community 62 - "Community 62"
Cohesion: 0.29
Nodes (7): close_redis(), get_redis(), publish_event(), Redis Pub/Sub events for enterprise info sync., Get or create the Redis client., Publish an event to a Redis Pub/Sub channel., Close the Redis connection.

### Community 63 - "Community 63"
Cohesion: 0.36
Nodes (6): configure_atlassian_channel(), get_atlassian_api_key_for_agent(), get_atlassian_channel(), _serialize(), _sync_atlassian_tools_for_agent(), test_atlassian_channel()

### Community 64 - "Community 64"
Cohesion: 0.25
Nodes (7): detect_agentbay_env(), get_browser_snapshot(), get_desktop_screenshot(), AgentBay live preview helpers.  Provides utility functions for fetching live pre, Get a base64-encoded screenshot of an agent's active computer session.      Uses, Get a base64-encoded screenshot of an agent's active browser session.      Retur, Detect which AgentBay environment a tool belongs to.      Returns 'desktop', 'br

### Community 65 - "Community 65"
Cohesion: 0.57
Nodes (7): _agent_workspace(), build_agent_context(), _build_skills_index(), _load_skills_index(), _parse_skill_frontmatter(), _read_file_cached(), _read_file_safe()

### Community 66 - "Community 66"
Cohesion: 0.25
Nodes (0): 

### Community 67 - "Community 67"
Cohesion: 0.25
Nodes (1): search_input_utils 回归：不加载 jsonrpc_router，避免 DB/异步副作用。

### Community 68 - "Community 68"
Cohesion: 0.39
Nodes (6): _has_column(), _has_index(), _has_table(), _has_unique_constraint(), Add IDE plugin fields to chat_sessions  Revision ID: 29f3f8de3ca0 Revises: add_u, upgrade()

### Community 69 - "Community 69"
Cohesion: 0.48
Nodes (5): cleanup(), runTests(), sleep(), startServer(), waitForServer()

### Community 70 - "Community 70"
Cohesion: 0.33
Nodes (6): _agent_base_dir(), list_pages(), Public pages API — serves published HTML without authentication., Serve a published HTML page. No authentication required., List published pages for an agent., render_page()

### Community 71 - "Community 71"
Cohesion: 0.33
Nodes (2): get_google_provider_base_url(), get_google_redirect_uri()

### Community 72 - "Community 72"
Cohesion: 0.52
Nodes (6): _get_agent_reply(), _is_reminder_due(), _parse_schedule(), _send_supervision_reminder(), start_supervision_reminder(), _supervision_tick()

### Community 73 - "Community 73"
Cohesion: 0.43
Nodes (6): chrome_executable(), collect_browser_layout(), is_complex_css_paint(), is_translucent_css_color(), Shared Chrome rendering helpers for document conversion., Return a local Chrome/Chromium executable path if one is available.

### Community 74 - "Community 74"
Cohesion: 0.43
Nodes (5): _has_column(), _has_index(), _has_table(), add open_files column to chat_session  Revision ID: 25811072c8fd Revises: 45681b, upgrade()

### Community 75 - "Community 75"
Cohesion: 0.53
Nodes (4): combineGraphs(), extractDotBlocks(), main(), renderToSvg()

### Community 76 - "Community 76"
Cohesion: 0.33
Nodes (5): 测试 toolCall markdown 块格式。, 测试完整的 MATCHER_PATTERN 对 toolCall 的匹配。, 验证插件的正则表达式是否能匹配 toolCall 格式。, test_full_matcher_pattern(), test_toolcall_regex_match()

### Community 77 - "Community 77"
Cohesion: 0.4
Nodes (5): extract_text(), File upload API for chat — saves files to agent workspace and extracts text., Upload a file for chat context. Saves to agent workspace/uploads/ and returns ex, Extract text content from a file., upload_file()

### Community 78 - "Community 78"
Cohesion: 0.4
Nodes (5): get_skill_creator_files(), _load_file(), Content for the skill-creator builtin skill.  Based on: https://github.com/anthr, Return list of {path, content} for all skill-creator files., Load a file from the skill_creator_files directory.

### Community 79 - "Community 79"
Cohesion: 0.33
Nodes (5): add_thinking_reaction(), DingTalk emotion reaction service — "thinking" indicator on user messages., Add "🤔思考中" reaction to a user message. Fire-and-forget, never raises., Recall "🤔思考中" reaction with retry (0ms, 1500ms, 5000ms). Fire-and-forget., recall_thinking_reaction()

### Community 80 - "Community 80"
Cohesion: 0.4
Nodes (5): get_wecom_access_token(), WeCom (Enterprise WeChat) service for sending messages via Open API., Send a text message to a WeCom user.      API: https://developer.work.weixin.qq., Get WeCom access_token using corp_id and secret.      API: https://developer.wor, send_wecom_message()

### Community 81 - "Community 81"
Cohesion: 0.4
Nodes (5): ensure_primary_platform_session(), get_primary_platform_session(), Helpers for first-party chat session selection and creation., Return the current primary first-party session for a user+agent pair, if any., Return a guaranteed primary platform session for a given user+agent pair.      T

### Community 82 - "Community 82"
Cohesion: 0.33
Nodes (0): 

### Community 83 - "Community 83"
Cohesion: 0.33
Nodes (0): 

### Community 84 - "Community 84"
Cohesion: 0.33
Nodes (0): 

### Community 85 - "Community 85"
Cohesion: 0.5
Nodes (4): format_lsp_message(), LSP4J 插件端到端测试脚本。  模拟通义灵码 IDE 插件的 WebSocket 连接行为， 验证 Clawith LSP4J 后端的完整功能链路。  使用, 格式化 LSP Base Protocol 消息, test_lsp4j()

### Community 86 - "Community 86"
Cohesion: 0.4
Nodes (1): 测试 _convert_file_paths_to_links 方法的表格保护功能。

### Community 87 - "Community 87"
Cohesion: 0.6
Nodes (3): google_workspace_callback(), _handle_google_admin_sync_callback(), _handle_google_sso_callback()

### Community 88 - "Community 88"
Cohesion: 0.7
Nodes (4): _agent_request_message(), _cleanup_legacy_daily_reply_triggers(), _human_request_message(), trigger_daily_collection_for_tenant()

### Community 89 - "Community 89"
Cohesion: 0.5
Nodes (3): extract_code_diffs(), 代码 Diff 提取器 — 从 LLM 响应中解析带文件路径的代码块。  用于 IDEA 插件集成场景，将 LLM 返回的代码块转换为结构化的 diff 数据。, 从 LLM 响应内容中提取带文件路径的代码块。      按优先级依次尝试两种匹配策略：     1. 显式路径格式: ```<lang>:<path> — 语

### Community 90 - "Community 90"
Cohesion: 0.5
Nodes (0): 

### Community 91 - "Community 91"
Cohesion: 0.5
Nodes (2): 精确验证 toolCall 正则匹配行为。, TestToolCallRegex

### Community 92 - "Community 92"
Cohesion: 0.5
Nodes (3): find_or_create_channel_session(), Shared helper: find-or-create ChatSession by external channel conv_id.  Used by, Find an existing ChatSession by (agent_id, external_conv_id), or create one.

### Community 93 - "Community 93"
Cohesion: 0.5
Nodes (3): Notification service — unified entry point for sending in-app notifications., Create and persist a notification for a user or an agent.      Args:         db:, send_notification()

### Community 94 - "Community 94"
Cohesion: 0.5
Nodes (3): is_non_workday(), Business calendar helpers for scheduled OKR work.  The first layer is intentiona, Return True when a date should be skipped for business reporting.

### Community 95 - "Community 95"
Cohesion: 0.5
Nodes (0): 

### Community 96 - "Community 96"
Cohesion: 0.5
Nodes (1): Add is_system column to agents table, and agent_triggers.is_system.  Also adds i

### Community 97 - "Community 97"
Cohesion: 0.5
Nodes (1): Ensure channel_type_enum contains all channel values used by the app.  Revision

### Community 98 - "Community 98"
Cohesion: 0.5
Nodes (1): Add source to tools and backfill data  Revision ID: add_tool_source Revises: add

### Community 99 - "Community 99"
Cohesion: 0.5
Nodes (1): Unified column fix for missing fields across main tables.  Revision ID: 20260313

### Community 100 - "Community 100"
Cohesion: 0.5
Nodes (1): Merge merge_okr_api_key and add_workspace_revisions heads.  Revision ID: merge_w

### Community 101 - "Community 101"
Cohesion: 0.5
Nodes (1): Increase api_key_encrypted column length to support Minimax API keys.  Revision

### Community 102 - "Community 102"
Cohesion: 0.5
Nodes (1): Add workspace file revision and edit lock tables.  Revision ID: add_workspace_re

### Community 103 - "Community 103"
Cohesion: 0.5
Nodes (1): Merge heads after main merge  Revision ID: 5fe287d9d58b Revises: fd6e34661d12, r

### Community 104 - "Community 104"
Cohesion: 0.5
Nodes (1): Merge OKR tables branch with llm_request_timeout branch.  This merge migration r

### Community 105 - "Community 105"
Cohesion: 0.5
Nodes (1): add whatsapp channel support  Revision ID: add_whatsapp_channel_support Revises:

### Community 106 - "Community 106"
Cohesion: 0.5
Nodes (1): Add phase tracking to agent/user onboarding.  Revision ID: add_onboarding_phase

### Community 107 - "Community 107"
Cohesion: 0.5
Nodes (1): Add chat_sessions table and update existing chat_messages conversation_ids.

### Community 108 - "Community 108"
Cohesion: 0.5
Nodes (1): Add relationship access metadata.  Revision ID: add_relationship_access_metadata

### Community 109 - "Community 109"
Cohesion: 0.5
Nodes (1): add llm temperature  Revision ID: add_llm_temperature Revises:  Create Date: 202

### Community 110 - "Community 110"
Cohesion: 0.5
Nodes (1): Add agent token usage and context fields to agents table.  Revision ID: add_agen

### Community 111 - "Community 111"
Cohesion: 0.5
Nodes (1): Add api_key_hash column to users table for user-level API key support.  Revision

### Community 112 - "Community 112"
Cohesion: 0.5
Nodes (1): merge: merge main and feature heads  Revision ID: eba6ac4d8a55 Revises: 6c3ec1ce

### Community 113 - "Community 113"
Cohesion: 0.5
Nodes (1): merge add_onboarding_phase and add_token_cache_usage_fields  Revision ID: 6c3ec1

### Community 114 - "Community 114"
Cohesion: 0.5
Nodes (1): merge remaining alembic heads  Revision ID: 87ff921e8e6f Revises: 5fe287d9d58b,

### Community 115 - "Community 115"
Cohesion: 0.5
Nodes (1): Add Tenant.default_model_id + backfill per-tenant to earliest enabled model.  Re

### Community 116 - "Community 116"
Cohesion: 0.5
Nodes (1): Add source and installed_by_agent_id to agent_tools  Revision ID: add_agent_tool

### Community 117 - "Community 117"
Cohesion: 0.5
Nodes (1): Add default_mcp_servers to agent templates.  Revision ID: add_default_mcp_server

### Community 118 - "Community 118"
Cohesion: 0.5
Nodes (1): Add usage quota fields to users, agents, and tenants tables.  Idempotent — uses

### Community 119 - "Community 119"
Cohesion: 0.5
Nodes (1): Multi-tenant registration: add tenant_id to invitation_codes, delete historical

### Community 120 - "Community 120"
Cohesion: 0.5
Nodes (1): Add name_translit fields to OrgMember  Revision ID: be48e94fa052 Revises: add_da

### Community 121 - "Community 121"
Cohesion: 0.5
Nodes (1): merge heads  Revision ID: 45681b72317e Revises: 29f3f8de3ca0, f1a2b3c4d5e6 Creat

### Community 122 - "Community 122"
Cohesion: 0.5
Nodes (1): Add agent_triggers table for Pulse engine.  Revision ID: add_agent_triggers

### Community 123 - "Community 123"
Cohesion: 0.5
Nodes (1): add ide_plugin_configs  Revision ID: add_ide_plugin_configs Revises: user_refact

### Community 124 - "Community 124"
Cohesion: 0.5
Nodes (1): Add explicit agent access policy fields.  Revision ID: add_agent_access_policy R

### Community 125 - "Community 125"
Cohesion: 0.5
Nodes (1): Add wechat to channel_type_enum.  Revision ID: add_wechat_channel_support Revise

### Community 126 - "Community 126"
Cohesion: 0.5
Nodes (1): Add a2a_async_enabled column to tenants table.  Revision ID: f1a2b3c4d5e6 Revise

### Community 127 - "Community 127"
Cohesion: 0.5
Nodes (1): Add tenant_id to llm_models table for per-company model pools.  Revision ID: add

### Community 128 - "Community 128"
Cohesion: 0.5
Nodes (1): Add consolidated OKR reporting and scheduling schema updates.  Revision ID: add_

### Community 129 - "Community 129"
Cohesion: 0.5
Nodes (1): User system refactor - unified migration.  Revision ID: user_refactor_v1 Revises

### Community 130 - "Community 130"
Cohesion: 0.5
Nodes (1): add_group_chat_fields_to_chat_sessions  Add is_group and group_name columns to c

### Community 131 - "Community 131"
Cohesion: 0.5
Nodes (1): merge heads  Revision ID: fd6e34661d12 Revises: 25811072c8fd, increase_api_key_l

### Community 132 - "Community 132"
Cohesion: 0.5
Nodes (1): Per-(user, agent) onboarding junction table + drop legacy bootstrapped flag.  Re

### Community 133 - "Community 133"
Cohesion: 0.5
Nodes (1): Add tenant_id to skills table for per-company skill scoping.  Revision ID: add_s

### Community 134 - "Community 134"
Cohesion: 0.5
Nodes (1): add entrypoint missing columns  Revision ID: df3da9cf3b27 Revises: multi_tenant_

### Community 135 - "Community 135"
Cohesion: 0.5
Nodes (1): add llm request_timeout  Revision ID: d9cbd43b62e5 Revises: 440261f5594f Create

### Community 136 - "Community 136"
Cohesion: 0.5
Nodes (1): Add Microsoft Teams support to im_provider and channel_type enums.

### Community 137 - "Community 137"
Cohesion: 0.5
Nodes (1): Add structured agent focus items.  Revision ID: add_agent_focus_items Revises: w

### Community 138 - "Community 138"
Cohesion: 0.5
Nodes (1): Merge remaining release heads after PR #494.  Revision ID: merge_pr494_heads Rev

### Community 139 - "Community 139"
Cohesion: 0.5
Nodes (1): Add participants table, extend chat_sessions and chat_messages, migrate messages

### Community 140 - "Community 140"
Cohesion: 0.5
Nodes (1): Refactor user system to global Identities - Phase 2 (Consolidated & Idempotent)

### Community 141 - "Community 141"
Cohesion: 0.5
Nodes (1): add published_pages table  Revision ID: add_published_pages Revises: df3da9cf3b2

### Community 142 - "Community 142"
Cohesion: 0.5
Nodes (1): Add invitation_codes table.  This is an idempotent migration — uses CREATE TABLE

### Community 143 - "Community 143"
Cohesion: 0.5
Nodes (1): Merge okr_agent_id migration and increase_api_key_length migration heads.  Revis

### Community 144 - "Community 144"
Cohesion: 0.5
Nodes (1): Add OKR system tables.  Creates six tables for the OKR feature:   okr_objectives

### Community 145 - "Community 145"
Cohesion: 0.5
Nodes (1): Add primary first-party chat sessions and per-session read tracking.  Revision I

### Community 146 - "Community 146"
Cohesion: 0.5
Nodes (1): Rename web identity provider display name to Platform.  Revision ID: web_provide

### Community 147 - "Community 147"
Cohesion: 0.5
Nodes (1): Add okr_agent_id to okr_settings; add unique partial index on system agents.  Tw

### Community 148 - "Community 148"
Cohesion: 0.5
Nodes (1): merge add_ide_plugin_configs head  Revision ID: 482391030754 Revises: 87ff921e8e

### Community 149 - "Community 149"
Cohesion: 0.5
Nodes (1): Add sso_login_enabled to identity_providers  Revision ID: add_sso_login_enabled

### Community 150 - "Community 150"
Cohesion: 0.5
Nodes (1): Add agentbay and atlassian to channel_type_enum.  Revision ID: add_agentbay_enum

### Community 151 - "Community 151"
Cohesion: 0.5
Nodes (1): Add agent_id and sender_name to notifications table.  Revision ID: add_notificat

### Community 152 - "Community 152"
Cohesion: 0.5
Nodes (1): Add bootstrap_content + capability_bullets to agent templates.  Revision ID: add

### Community 153 - "Community 153"
Cohesion: 0.5
Nodes (1): Set default agent quotas to permanent TTL and higher daily LLM calls.  Revision

### Community 154 - "Community 154"
Cohesion: 0.67
Nodes (2): Run pytest tests in the specified directory, run_tests()

### Community 155 - "Community 155"
Cohesion: 0.67
Nodes (2): check_logs(), Check if the new logging is working correctly.

### Community 156 - "Community 156"
Cohesion: 0.67
Nodes (0): 

### Community 157 - "Community 157"
Cohesion: 0.67
Nodes (1): HTML to PDF conversion service.

### Community 158 - "Community 158"
Cohesion: 0.67
Nodes (1): Editable PPTX rendering implementation for HTML inputs.

### Community 159 - "Community 159"
Cohesion: 0.67
Nodes (1): HTML to PPTX conversion service.

### Community 160 - "Community 160"
Cohesion: 0.67
Nodes (0): 

### Community 161 - "Community 161"
Cohesion: 0.67
Nodes (0): 

### Community 162 - "Community 162"
Cohesion: 1.0
Nodes (0): 

### Community 163 - "Community 163"
Cohesion: 1.0
Nodes (0): 

### Community 164 - "Community 164"
Cohesion: 1.0
Nodes (0): 

### Community 165 - "Community 165"
Cohesion: 1.0
Nodes (0): 

### Community 166 - "Community 166"
Cohesion: 1.0
Nodes (0): 

### Community 167 - "Community 167"
Cohesion: 1.0
Nodes (0): 

### Community 168 - "Community 168"
Cohesion: 1.0
Nodes (0): 

### Community 169 - "Community 169"
Cohesion: 1.0
Nodes (0): 

### Community 170 - "Community 170"
Cohesion: 1.0
Nodes (0): 

### Community 171 - "Community 171"
Cohesion: 1.0
Nodes (0): 

### Community 172 - "Community 172"
Cohesion: 1.0
Nodes (0): 

### Community 173 - "Community 173"
Cohesion: 1.0
Nodes (0): 

### Community 174 - "Community 174"
Cohesion: 1.0
Nodes (0): 

### Community 175 - "Community 175"
Cohesion: 1.0
Nodes (0): 

### Community 176 - "Community 176"
Cohesion: 1.0
Nodes (0): 

### Community 177 - "Community 177"
Cohesion: 1.0
Nodes (1): 向 FastAPI app 注册路由、启动钩子等。

### Community 178 - "Community 178"
Cohesion: 1.0
Nodes (1): Command name (e.g., "about").

### Community 179 - "Community 179"
Cohesion: 1.0
Nodes (1): Brief description for help output.

### Community 180 - "Community 180"
Cohesion: 1.0
Nodes (1): Alternative names for this command.

### Community 181 - "Community 181"
Cohesion: 1.0
Nodes (1): Nested subcommands (for "extensions list" style).

### Community 182 - "Community 182"
Cohesion: 1.0
Nodes (1): Execute the command with given arguments.

### Community 183 - "Community 183"
Cohesion: 1.0
Nodes (1): 计算两段内容之间的 DiffInfo（行级 + 字符级）。          纯计算函数，不访问共享状态，可在锁内外安全调用。

### Community 184 - "Community 184"
Cohesion: 1.0
Nodes (1): 将 JSON-RPC 消息格式化为 LSP Base Protocol 文本帧。          注意：Content-Length 必须是 UTF-8 编码

### Community 185 - "Community 185"
Cohesion: 1.0
Nodes (1): 从 header 块中解析 Content-Length 值。          Args:             header_block: header

### Community 186 - "Community 186"
Cohesion: 1.0
Nodes (0): 

### Community 187 - "Community 187"
Cohesion: 1.0
Nodes (1): Send a completion request and return the full response.

### Community 188 - "Community 188"
Cohesion: 1.0
Nodes (1): Send a streaming request and return the aggregated response.          Implementa

### Community 189 - "Community 189"
Cohesion: 1.0
Nodes (1): 从 dict 构建 SandboxConfig，支持字段级 fallback。          Args:             config: 工具配置

### Community 190 - "Community 190"
Cohesion: 1.0
Nodes (1): Backend name for identification.

### Community 191 - "Community 191"
Cohesion: 1.0
Nodes (1): Backend name for identification.

### Community 192 - "Community 192"
Cohesion: 1.0
Nodes (1): Execute code in the sandbox.

### Community 193 - "Community 193"
Cohesion: 1.0
Nodes (1): Check if the sandbox backend is healthy.

### Community 194 - "Community 194"
Cohesion: 1.0
Nodes (1): Get the capabilities of this sandbox backend.

### Community 195 - "Community 195"
Cohesion: 1.0
Nodes (1): Enqueue a new permission request for later approval.

### Community 196 - "Community 196"
Cohesion: 1.0
Nodes (1): Get all pending permission requests for a session.

### Community 197 - "Community 197"
Cohesion: 1.0
Nodes (1): Get a specific pending permission request by ID.

### Community 198 - "Community 198"
Cohesion: 1.0
Nodes (1): Process a permission decision (grant/deny).

### Community 199 - "Community 199"
Cohesion: 1.0
Nodes (1): Wait for a decision on a permission request.          Returns True if granted, F

### Community 200 - "Community 200"
Cohesion: 1.0
Nodes (1): Clear all pending requests for a session.

### Community 201 - "Community 201"
Cohesion: 1.0
Nodes (1): Count pending requests for a session.

### Community 202 - "Community 202"
Cohesion: 1.0
Nodes (1): Read content from a text file.

### Community 203 - "Community 203"
Cohesion: 1.0
Nodes (1): Write content to a text file.

### Community 204 - "Community 204"
Cohesion: 1.0
Nodes (0): 

## Knowledge Gaps
- **452 isolated node(s):** `Run pytest tests in the specified directory`, `LSP4J 插件端到端测试脚本。  模拟通义灵码 IDE 插件的 WebSocket 连接行为， 验证 Clawith LSP4J 后端的完整功能链路。  使用`, `格式化 LSP Base Protocol 消息`, `Check if the new logging is working correctly.`, `O3: Log IDE-provided MCP servers so we know what's available (not yet forwarded` (+447 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 162`** (2 nodes): `test_async.py`, `test_manager()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 163`** (2 nodes): `PermissionModal.tsx`, `computeDiff()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 164`** (2 nodes): `update_schema.py`, `main()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 165`** (2 nodes): `remove_old_tool.py`, `run()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 166`** (2 nodes): `ws-protocol.test.js`, `runTests()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 167`** (1 nodes): `check_circular_imports.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 168`** (1 nodes): `verify_superpowers_tests.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 169`** (1 nodes): `merge_graphify.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 170`** (1 nodes): `vite.config.ts`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 171`** (1 nodes): `vite-env.d.ts`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 172`** (1 nodes): `qrcode.d.ts`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 173`** (1 nodes): `setup-win.ps1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 174`** (1 nodes): `run-win.ps1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 175`** (1 nodes): `test_import.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 176`** (1 nodes): `test_adapter_direct.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 177`** (1 nodes): `向 FastAPI app 注册路由、启动钩子等。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 178`** (1 nodes): `Command name (e.g., "about").`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 179`** (1 nodes): `Brief description for help output.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 180`** (1 nodes): `Alternative names for this command.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 181`** (1 nodes): `Nested subcommands (for "extensions list" style).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 182`** (1 nodes): `Execute the command with given arguments.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 183`** (1 nodes): `计算两段内容之间的 DiffInfo（行级 + 字符级）。          纯计算函数，不访问共享状态，可在锁内外安全调用。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 184`** (1 nodes): `将 JSON-RPC 消息格式化为 LSP Base Protocol 文本帧。          注意：Content-Length 必须是 UTF-8 编码`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 185`** (1 nodes): `从 header 块中解析 Content-Length 值。          Args:             header_block: header`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 186`** (1 nodes): `scripts____init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 187`** (1 nodes): `Send a completion request and return the full response.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 188`** (1 nodes): `Send a streaming request and return the aggregated response.          Implementa`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 189`** (1 nodes): `从 dict 构建 SandboxConfig，支持字段级 fallback。          Args:             config: 工具配置`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 190`** (1 nodes): `Backend name for identification.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 191`** (1 nodes): `Backend name for identification.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 192`** (1 nodes): `Execute code in the sandbox.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 193`** (1 nodes): `Check if the sandbox backend is healthy.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 194`** (1 nodes): `Get the capabilities of this sandbox backend.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 195`** (1 nodes): `Enqueue a new permission request for later approval.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 196`** (1 nodes): `Get all pending permission requests for a session.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 197`** (1 nodes): `Get a specific pending permission request by ID.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 198`** (1 nodes): `Process a permission decision (grant/deny).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 199`** (1 nodes): `Wait for a decision on a permission request.          Returns True if granted, F`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 200`** (1 nodes): `Clear all pending requests for a session.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 201`** (1 nodes): `Count pending requests for a session.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 202`** (1 nodes): `Read content from a text file.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 203`** (1 nodes): `Write content to a text file.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 204`** (1 nodes): `merge_semantic.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `User` connect `Community 0` to `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 8`, `Community 9`, `Community 10`, `Community 11`, `Community 13`, `Community 18`, `Community 23`, `Community 27`, `Community 31`, `Community 41`, `Community 46`, `Community 55`, `Community 70`, `Community 77`?**
  _High betweenness centrality (0.217) - this node is a cross-community bridge._
- **Why does `Agent` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 8`, `Community 9`, `Community 10`, `Community 11`, `Community 13`, `Community 46`, `Community 18`, `Community 23`, `Community 26`, `Community 59`, `Community 29`, `Community 31`?**
  _High betweenness centrality (0.148) - this node is a cross-community bridge._
- **Why does `ChannelConfig` connect `Community 2` to `Community 0`, `Community 1`, `Community 32`, `Community 4`, `Community 5`, `Community 8`, `Community 41`, `Community 10`, `Community 11`, `Community 46`, `Community 14`, `Community 21`, `Community 55`, `Community 26`, `Community 29`?**
  _High betweenness centrality (0.052) - this node is a cross-community bridge._
- **Are the 1107 inferred relationships involving `User` (e.g. with `Seed data script — creates initial admin user and built-in templates.` and `Create tables and seed initial data.`) actually correct?**
  _`User` has 1107 INFERRED edges - model-reasoned connections that need verification._
- **Are the 906 inferred relationships involving `Agent` (e.g. with `Seed data script — creates initial admin user and built-in templates.` and `Create tables and seed initial data.`) actually correct?**
  _`Agent` has 906 INFERRED edges - model-reasoned connections that need verification._
- **Are the 496 inferred relationships involving `ChatMessage` (e.g. with `LSP4J WebSocket 端点 + 认证。  提供 WebSocket 端点供通义灵码 IDE 插件连接。 URL 格式：ws://{host}/api/` and `Outbound message with schema version for forward-compatible clients.`) actually correct?**
  _`ChatMessage` has 496 INFERRED edges - model-reasoned connections that need verification._
- **Are the 496 inferred relationships involving `IdentityProvider` (e.g. with `Base` and `Backfill department paths from the department tree and refresh member paths.  Us`) actually correct?**
  _`IdentityProvider` has 496 INFERRED edges - model-reasoned connections that need verification._
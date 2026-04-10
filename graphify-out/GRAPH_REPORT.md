# Graph Report - .  (2026-04-10)

## Corpus Check
- Large corpus: 293 files · ~368,887 words. Semantic extraction will be expensive (many Claude tokens). Consider running on a subfolder, or use --no-semantic to run AST-only.

## Summary
- 3247 nodes · 13517 edges · 104 communities detected
- Extraction: 32% EXTRACTED · 68% INFERRED · 0% AMBIGUOUS · INFERRED: 9213 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## God Nodes (most connected - your core abstractions)
1. `User` - 794 edges
2. `Agent` - 549 edges
3. `IdentityProvider` - 376 edges
4. `ChannelConfig` - 326 edges
5. `OrgMember` - 310 edges
6. `SystemSetting` - 300 edges
7. `LLMModel` - 287 edges
8. `Identity` - 277 edges
9. `ChatMessage` - 267 edges
10. `Tool` - 233 edges

## Surprising Connections (you probably didn't know these)
- `Global Skill registry model.` --uses--> `Base`  [INFERRED]
  backend/app/models/skill.py → backend/app/database.py
- `A globally registered skill definition.` --uses--> `Base`  [INFERRED]
  backend/app/models/skill.py → backend/app/database.py
- `A file within a skill folder (e.g. SKILL.md, scripts/helper.py).` --uses--> `Base`  [INFERRED]
  backend/app/models/skill.py → backend/app/database.py
- `A company/organization that uses the platform.` --uses--> `Base`  [INFERRED]
  backend/app/models/tenant.py → backend/app/database.py
- `Rolled up token consumption per agent per day for time-series analytics.` --uses--> `Base`  [INFERRED]
  backend/app/models/activity_log.py → backend/app/database.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (366): get_agent_activity(), get_conversation_messages(), list_conversations(), Activity log API — view agent work history., Get messages for a specific conversation., Get recent activity logs for an agent., List all conversation partners for this agent (web users + other agents)., Agent (+358 more)

### Community 1 - "Community 1"
Cohesion: 0.03
Nodes (287): ApprovalRequest, EnterpriseInfo, ExternalUserInfo, Generic OAuth/SSO authentication provider framework.  This module provides a bas, Get or create IdentityProvider record., Find user by legacy provider-specific fields (if any)., Update existing user with new info from provider., Create new user from external identity. (+279 more)

### Community 2 - "Community 2"
Cohesion: 0.03
Nodes (286): DailyTokenUsage, Rolled up token consumption per agent per day for time-series analytics., CompanyCreateRequest, CompanyCreateResponse, CompanyStats, create_company(), get_enhanced_metrics(), get_platform_leaderboards() (+278 more)

### Community 3 - "Community 3"
Cohesion: 0.01
Nodes (88): fetchJson(), handleCreate(), handleSendTestEmail(), handleToggle(), handleToggleSetting(), loadCompanies(), saveEmailConfig(), saveEmailTemplates() (+80 more)

### Community 4 - "Community 4"
Cohesion: 0.02
Nodes (125): AioSandboxBackend, aio-sandbox backend.      Connects to aio-sandbox (https://github.com/agent-infr, Check if aio-sandbox service is available., Execute code using aio-sandbox., AuditAction, Helper to write audit log entries from background services., Write audit log for role-related events.      Args:         action: Role action, Write audit log for tenant-related events.      Args:         action: Tenant act (+117 more)

### Community 5 - "Community 5"
Cohesion: 0.02
Nodes (114): BaseAuthProvider, DingTalkAuthProvider, FeishuAuthProvider, MicrosoftTeamsAuthProvider, WeComAuthProvider, AuthProviderRegistry, Authentication provider registry and factory.  This module provides a centralize, Create a new identity provider.          Args:             db: Database session (+106 more)

### Community 6 - "Community 6"
Cohesion: 0.03
Nodes (80): ABC, AnthropicClient, chat_complete(), chat_stream(), create_llm_client(), GeminiClient, get_max_tokens(), get_provider_base_url() (+72 more)

### Community 7 - "Community 7"
Cohesion: 0.04
Nodes (133): _agent_has_any_channel(), _agent_has_feishu(), _agentbay_browser_click(), _agentbay_browser_extract(), _agentbay_browser_login(), _agentbay_browser_navigate(), _agentbay_browser_observe(), _agentbay_browser_screenshot() (+125 more)

### Community 8 - "Community 8"
Cohesion: 0.02
Nodes (107): AgentActivityLog, Activity log model for tracking agent actions., Records every action taken by a digital employee., log_activity(), Activity logger — simple async function to record agent actions., Record an agent activity. Fire-and-forget, never raises., Audit log, approval request, chat message, and enterprise info models., Audit trail for all operations. (+99 more)

### Community 9 - "Community 9"
Cohesion: 0.04
Nodes (116): _agent_base_dir(), agent_import_from_clawhub(), agent_import_from_url(), ClawhubImportBody, delete_enterprise_file(), delete_file(), download_file(), _enterprise_info_dir() (+108 more)

### Community 10 - "Community 10"
Cohesion: 0.04
Nodes (44): AgentBayClient, AgentBaySession, cleanup_agentbay_sessions(), get_agentbay_api_key_for_agent(), get_agentbay_client_for_agent(), _inject_credentials(), AgentBay API client using official SDK.  This module provides a client wrapper a, Navigate browser to URL using SDK.          The AgentBay SDK default navigation (+36 more)

### Community 11 - "Community 11"
Cohesion: 0.05
Nodes (59): _cdp_exec(), ClickRequest, control_click(), control_current_url(), control_drag(), control_lock(), control_press_keys(), control_screenshot() (+51 more)

### Community 12 - "Community 12"
Cohesion: 0.05
Nodes (35): Agent, Digital Employee (Agent) models., _acp_block_to_dict(), _check_server_schema(), ClawithThinClientAgent, _cloud_msg(), _configure_thin_client_logging(), _infer_tool_kind() (+27 more)

### Community 13 - "Community 13"
Cohesion: 0.07
Nodes (30): _AsyncSessionCtx, _FakeAsyncSessionFactory, _FakeDb, _load_thin_server_module(), patch_acp_async_session(), Unit / integration-style tests for the clawith-acp WebSocket bridge (no real IDE, Inject a fake async_session factory; restore after test., Must not hit DB when history already present. (+22 more)

### Community 14 - "Community 14"
Cohesion: 0.08
Nodes (13): list, client(), FakeAsyncSessionFactory, FakeQuery, FakeScalarResult, FakeSession, FakeSkill, QueryField (+5 more)

### Community 15 - "Community 15"
Cohesion: 0.11
Nodes (27): create_tool(), _decrypt_sensitive_fields(), delete_agent_tool(), delete_category_config(), delete_tool(), _encrypt_sensitive_fields(), get_agent_tool_config(), get_agent_tools() (+19 more)

### Community 16 - "Community 16"
Cohesion: 0.11
Nodes (22): FileSystemEventHandler, _agent_headers(), _fire_extract(), _get_client(), index_memory_file(), _invalidate_availability_cache(), is_available(), OpenViking HTTP client for semantic memory retrieval.  Provides optional integra (+14 more)

### Community 17 - "Community 17"
Cohesion: 0.11
Nodes (25): deliver_broadcast_emails(), get_email_templates(), _load_templates_from_db(), System-owned outbound email service.  Supports both: 1. Platform-level configura, Send email with provided config., Send a password reset email using the configured template.      Args:         to, Send a company invitation email using the configured template.      Args:, Deliver broadcast emails while isolating per-recipient failures. (+17 more)

### Community 18 - "Community 18"
Cohesion: 0.1
Nodes (21): generate_html(), main(), Generate HTML report from loop output data. If auto_refresh is True, adds a meta, improve_description(), main(), Call Claude to improve the description based on eval results., find_project_root(), main() (+13 more)

### Community 19 - "Community 19"
Cohesion: 0.09
Nodes (23): create_access_token(), decode_access_token(), decrypt_data(), encrypt_data(), get_authenticated_user(), get_current_admin(), get_current_user(), hash_password() (+15 more)

### Community 20 - "Community 20"
Cohesion: 0.11
Nodes (22): force_ipv4(), _ipv4_getaddrinfo(), Core email utilities for SMTP operations and network compatibility., Wrapper that forces AF_INET (IPv4) to avoid IPv6 failures in Docker., Context manager that forces all socket connections to use IPv4.      Docker cont, Synchronously send an email via SMTP with IPv4 enforcement.      Three connectio, send_smtp_email(), _decode_header_value() (+14 more)

### Community 21 - "Community 21"
Cohesion: 0.13
Nodes (18): BaseHTTPRequestHandler, build_run(), embed_file(), find_runs(), _find_runs_recursive(), generate_html(), get_mime_type(), _kill_port() (+10 more)

### Community 22 - "Community 22"
Cohesion: 0.12
Nodes (8): DummyResult, make_agent(), make_user(), _NestedTransaction, RecordingDB, TaskCleanupDB, test_archive_agent_task_history_writes_json_snapshot(), test_delete_agent_cleans_remaining_foreign_key_rows()

### Community 23 - "Community 23"
Cohesion: 0.12
Nodes (10): AgentManager, Agent lifecycle manager — Docker container management for OpenClaw Gateway insta, Generate openclaw.json config for the agent container., Start an OpenClaw Gateway Docker container for the agent.          Returns conta, Stop the agent's Docker container., Manage OpenClaw Gateway Docker containers for digital employees., Stop and remove the agent's Docker container., Archive agent files to a backup location and return the archive directory. (+2 more)

### Community 24 - "Community 24"
Cohesion: 0.12
Nodes (11): FeishuWSManager, _make_no_proxy_connect(), Feishu WebSocket Long Connection Manager., Create an event dispatcher for a specific agent., Handle im.message.receive_v1 events from Feishu WebSocket asynchronously., Spawns a WebSocket client fully asynchronously inside FastAPI's loop., Stops an actively running WebSocket client for an agent., Start WS clients for all configured Feishu agents. (+3 more)

### Community 25 - "Community 25"
Cohesion: 0.2
Nodes (15): _acp_await_client_permission(), _acp_verbose(), acp_websocket(), _acp_ws_envelope(), _build_acp_user_turn_from_ws(), _custom_execute_tool(), _hydrate_if_needed_acp(), install_acp_tool_hooks() (+7 more)

### Community 26 - "Community 26"
Cohesion: 0.12
Nodes (9): Send a JSON-RPC request via Streamable HTTP transport., Send a JSON-RPC request via SSE transport.          Opens a fresh SSE connection, Auto-detect transport and send request.          Strategy: If transport is alrea, Fetch available tools from the MCP server., Execute a tool on the MCP server., Build request headers with proper MCP and auth headers., Parse response — handles both JSON and SSE (text/event-stream) formats., Extract the last JSON-RPC result from an SSE stream. (+1 more)

### Community 27 - "Community 27"
Cohesion: 0.14
Nodes (16): generate_user_api_key(), get_user_api_key_status(), _hash_user_key(), list_users(), Update a user's quota settings (admin only)., Generate or regenerate a personal API key.      The raw key is returned only onc, Revoke the current personal API key., Return whether the user has an active API key. (+8 more)

### Community 28 - "Community 28"
Cohesion: 0.17
Nodes (15): _extract_docx(), _extract_pdf(), _extract_pptx(), extract_text(), _extract_xlsx(), needs_extraction(), Extract text from common office file formats.  Supports: PDF, DOCX, XLSX, PPTX,, Extract text from XLSX using openpyxl. (+7 more)

### Community 29 - "Community 29"
Cohesion: 0.17
Nodes (15): compress_bytes_to_base64(), compress_screenshot_to_base64(), pop_temp_screenshot(), _prune_expired_cache(), Vision injection utilities for AgentBay screenshot tools.  Architecture: "Epheme, Compress raw image bytes to a base64 JPEG data URL.      Resizes to _MAX_WIDTH (, Read a screenshot file, compress it, and return a base64 data URL.      Used onl, Try to extract a screenshot from a tool result and build a vision content array. (+7 more)

### Community 30 - "Community 30"
Cohesion: 0.17
Nodes (5): DummyResult, RecordingDB, test_create_session_returns_web_session_shape(), test_org_admin_can_list_all_sessions(), test_org_admin_can_view_other_users_session_messages()

### Community 31 - "Community 31"
Cohesion: 0.15
Nodes (12): run_async_migrations(), run_migrations_offline(), run_migrations_online(), configure_logging(), get_trace_id(), intercept_standard_logging(), Centralized logging configuration using loguru., Get current trace ID from context. (+4 more)

### Community 32 - "Community 32"
Cohesion: 0.15
Nodes (8): DingTalkStreamManager, DingTalk Stream Connection Manager.  Manages WebSocket-based Stream connections, Stop a running Stream client for an agent., Start Stream clients for all configured DingTalk agents., Return status of all active Stream clients., Manages DingTalk Stream clients for all agents., Start a DingTalk Stream client for a specific agent., Run the DingTalk Stream client in a blocking thread.

### Community 33 - "Community 33"
Cohesion: 0.16
Nodes (8): EmailVerificationService, Email verification token lifecycle helpers., Send an email verification code using the configured template., Email verification token lifecycle helpers., Hash a raw verification token before persistence or lookup., Create a new 6-digit email verification code and store in Redis., Build the user-facing verification URL. Note: now uses 6-digit code., Load a valid verification code from Redis and mark it used (by deleting).

### Community 34 - "Community 34"
Cohesion: 0.15
Nodes (12): TOOL_DEFINITIONS must have list_agents and call_agent with required fields., http_list_agents should return formatted agent list., http_list_agents should handle empty list gracefully., http_call_agent should return reply with session_id appended., http_call_agent should raise ValueError on 404., http_call_agent should raise ValueError when agent_id is empty., test_http_call_agent_404(), test_http_call_agent_no_agent_id() (+4 more)

### Community 35 - "Community 35"
Cohesion: 0.27
Nodes (11): _ensure_smithery_connection(), _get_modelscope_api_token(), _get_smithery_api_key(), import_mcp_direct(), import_mcp_from_smithery(), refresh_atlassian_rovo_api_key(), _search_modelscope_api(), search_registries() (+3 more)

### Community 36 - "Community 36"
Cohesion: 0.24
Nodes (11): aggregate_results(), calculate_stats(), generate_benchmark(), generate_markdown(), load_run_results(), main(), Aggregate run results into summary statistics.      Returns run_summary with sta, Generate complete benchmark.json from run results. (+3 more)

### Community 37 - "Community 37"
Cohesion: 0.23
Nodes (4): _FakeAsyncClient, _FakeResponse, test_patch_message_raises_when_business_code_nonzero(), test_send_message_raises_when_business_code_nonzero()

### Community 38 - "Community 38"
Cohesion: 0.35
Nodes (10): generate_agent_api_key(), generate_api_key(), _get_agent_by_key(), get_setup_guide(), _hash_key(), heartbeat(), poll_messages(), report_result() (+2 more)

### Community 39 - "Community 39"
Cohesion: 0.22
Nodes (9): delete_trigger(), list_agent_triggers(), Triggers REST API — CRUD endpoints for the Aware page frontend., Delete a trigger entirely., List all triggers for an agent., Update a trigger (from frontend management UI)., TriggerResponse, TriggerUpdate (+1 more)

### Community 40 - "Community 40"
Cohesion: 0.36
Nodes (8): _can_view_all_agent_chat_sessions(), create_session(), delete_session(), get_session_messages(), list_sessions(), rename_session(), SessionOut, _split_inline_tools()

### Community 41 - "Community 41"
Cohesion: 0.22
Nodes (6): EnterpriseSyncService, Enterprise information synchronization service.  Uses Redis Pub/Sub to notify on, Synchronize enterprise information to all online Agent containers., Update enterprise info in database and notify all agents., Pull enterprise info from DB and write to agent's enterprise_info/ directory., Sync enterprise info to all running agents. Returns count.

### Community 42 - "Community 42"
Cohesion: 0.24
Nodes (6): PlatformService, Platform-wide service for URL resolution and host type detection., Service to handle platform-wide settings and URL resolution., Check if a host is an IP address (IPv4)., Resolve the platform's public base URL with priority lookup.                  Pr, Generate the SSO base URL for a tenant based on IP/Domain logic.

### Community 43 - "Community 43"
Cohesion: 0.29
Nodes (9): get_dingtalk_access_token(), DingTalk service for sending messages via Open API., Unified message sending method.          Default behavior is sending via Robot O, Send single chat messages via Robot using modern v1.0 API (RECOMMENDED)., Get DingTalk access_token using app_id and app_secret.      API: https://open.di, Send a work notification (工作通知).          API: https://open.dingtalk.com/documen, send_dingtalk_corp_conversation(), send_dingtalk_message() (+1 more)

### Community 44 - "Community 44"
Cohesion: 0.36
Nodes (9): _check_new_agent_messages(), _evaluate_trigger(), _extract_json_path(), _invoke_agent_for_triggers(), _is_private_url(), _poll_check(), # NOTE: trigger state (last_fired_at, fire_count, auto-disable), start_trigger_daemon() (+1 more)

### Community 45 - "Community 45"
Cohesion: 0.31
Nodes (2): _process_wecom_stream_message(), WeComStreamManager

### Community 46 - "Community 46"
Cohesion: 0.28
Nodes (7): main(), package_skill(), Check if a path should be excluded from packaging., Package a skill folder into a .skill file.      Args:         skill_path: Path t, should_exclude(), Basic validation of a skill, validate_skill()

### Community 47 - "Community 47"
Cohesion: 0.29
Nodes (7): close_redis(), get_redis(), publish_event(), Redis Pub/Sub events for enterprise info sync., Get or create the Redis client., Publish an event to a Redis Pub/Sub channel., Close the Redis connection.

### Community 48 - "Community 48"
Cohesion: 0.36
Nodes (6): _completion_id(), list_models(), _oai_chunk_role(), _oai_response(), openai_chat_completions(), _resolve_agent()

### Community 49 - "Community 49"
Cohesion: 0.39
Nodes (7): _dispatch(), _err(), _execute_tool(), mcp_handler(), mcp_sse_connect(), mcp_sse_messages(), _ok()

### Community 50 - "Community 50"
Cohesion: 0.36
Nodes (6): configure_atlassian_channel(), get_atlassian_api_key_for_agent(), get_atlassian_channel(), _serialize(), _sync_atlassian_tools_for_agent(), test_atlassian_channel()

### Community 51 - "Community 51"
Cohesion: 0.25
Nodes (7): detect_agentbay_env(), get_browser_snapshot(), get_desktop_screenshot(), AgentBay live preview helpers.  Provides utility functions for fetching live pre, Get a base64-encoded screenshot of an agent's active computer session.      Uses, Get a base64-encoded screenshot of an agent's active browser session.      Retur, Detect which AgentBay environment a tool belongs to.      Returns 'desktop', 'br

### Community 52 - "Community 52"
Cohesion: 0.33
Nodes (6): _agent_base_dir(), list_pages(), Public pages API — serves published HTML without authentication., Serve a published HTML page. No authentication required., List published pages for an agent., render_page()

### Community 53 - "Community 53"
Cohesion: 0.52
Nodes (6): _get_agent_reply(), _is_reminder_due(), _parse_schedule(), _send_supervision_reminder(), start_supervision_reminder(), _supervision_tick()

### Community 54 - "Community 54"
Cohesion: 0.4
Nodes (5): extract_text(), File upload API for chat — saves files to agent workspace and extracts text., Upload a file for chat context. Saves to agent workspace/uploads/ and returns ex, Extract text content from a file., upload_file()

### Community 55 - "Community 55"
Cohesion: 0.4
Nodes (5): get_skill_creator_files(), _load_file(), Content for the skill-creator builtin skill.  Based on: https://github.com/anthr, Return list of {path, content} for all skill-creator files., Load a file from the skill_creator_files directory.

### Community 56 - "Community 56"
Cohesion: 0.67
Nodes (5): _agent_workspace(), build_agent_context(), _load_skills_index(), _parse_skill_frontmatter(), _read_file_safe()

### Community 57 - "Community 57"
Cohesion: 0.4
Nodes (5): get_wecom_access_token(), WeCom (Enterprise WeChat) service for sending messages via Open API., Send a text message to a WeCom user.      API: https://developer.work.weixin.qq., Get WeCom access_token using corp_id and secret.      API: https://developer.wor, send_wecom_message()

### Community 58 - "Community 58"
Cohesion: 0.5
Nodes (0): 

### Community 59 - "Community 59"
Cohesion: 0.5
Nodes (3): find_or_create_channel_session(), Shared helper: find-or-create ChatSession by external channel conv_id.  Used by, Find an existing ChatSession by (agent_id, external_conv_id), or create one.

### Community 60 - "Community 60"
Cohesion: 0.83
Nodes (3): execute_task(), _log_error(), _restore_supervision_status()

### Community 61 - "Community 61"
Cohesion: 0.5
Nodes (1): Add source to tools and backfill data  Revision ID: add_tool_source Revises: add

### Community 62 - "Community 62"
Cohesion: 0.5
Nodes (1): Unified column fix for missing fields across main tables.  Revision ID: 20260313

### Community 63 - "Community 63"
Cohesion: 0.5
Nodes (1): Add chat_sessions table and update existing chat_messages conversation_ids.

### Community 64 - "Community 64"
Cohesion: 0.5
Nodes (1): add llm temperature  Revision ID: add_llm_temperature Revises:  Create Date: 202

### Community 65 - "Community 65"
Cohesion: 0.5
Nodes (1): Add agent token usage and context fields to agents table.  Revision ID: add_agen

### Community 66 - "Community 66"
Cohesion: 0.5
Nodes (1): Add api_key_hash column to users table for user-level API key support.  Revision

### Community 67 - "Community 67"
Cohesion: 0.5
Nodes (1): Add source and installed_by_agent_id to agent_tools  Revision ID: add_agent_tool

### Community 68 - "Community 68"
Cohesion: 0.5
Nodes (1): Add usage quota fields to users, agents, and tenants tables.  Idempotent — uses

### Community 69 - "Community 69"
Cohesion: 0.5
Nodes (1): Multi-tenant registration: add tenant_id to invitation_codes, delete historical

### Community 70 - "Community 70"
Cohesion: 0.5
Nodes (1): Add name_translit fields to OrgMember  Revision ID: be48e94fa052 Revises: add_da

### Community 71 - "Community 71"
Cohesion: 0.5
Nodes (1): Add agent_triggers table for Pulse engine.  Revision ID: add_agent_triggers

### Community 72 - "Community 72"
Cohesion: 0.5
Nodes (1): Add tenant_id to llm_models table for per-company model pools.  Revision ID: add

### Community 73 - "Community 73"
Cohesion: 0.5
Nodes (1): User system refactor - unified migration.  Revision ID: user_refactor_v1 Revises

### Community 74 - "Community 74"
Cohesion: 0.5
Nodes (1): add_group_chat_fields_to_chat_sessions  Add is_group and group_name columns to c

### Community 75 - "Community 75"
Cohesion: 0.5
Nodes (1): Add tenant_id to skills table for per-company skill scoping.  Revision ID: add_s

### Community 76 - "Community 76"
Cohesion: 0.5
Nodes (1): add entrypoint missing columns  Revision ID: df3da9cf3b27 Revises: multi_tenant_

### Community 77 - "Community 77"
Cohesion: 0.5
Nodes (1): add llm request_timeout  Revision ID: d9cbd43b62e5 Revises: 440261f5594f Create

### Community 78 - "Community 78"
Cohesion: 0.5
Nodes (1): Add Microsoft Teams support to im_provider and channel_type enums.

### Community 79 - "Community 79"
Cohesion: 0.5
Nodes (1): Add participants table, extend chat_sessions and chat_messages, migrate messages

### Community 80 - "Community 80"
Cohesion: 0.5
Nodes (1): Refactor user system to global Identities - Phase 2 (Consolidated & Idempotent)

### Community 81 - "Community 81"
Cohesion: 0.5
Nodes (1): add published_pages table  Revision ID: add_published_pages Revises: df3da9cf3b2

### Community 82 - "Community 82"
Cohesion: 0.5
Nodes (1): Add invitation_codes table.  This is an idempotent migration — uses CREATE TABLE

### Community 83 - "Community 83"
Cohesion: 0.5
Nodes (1): Add sso_login_enabled to identity_providers  Revision ID: add_sso_login_enabled

### Community 84 - "Community 84"
Cohesion: 0.5
Nodes (1): Add agentbay and atlassian to channel_type_enum.  Revision ID: add_agentbay_enum

### Community 85 - "Community 85"
Cohesion: 0.5
Nodes (1): Add agent_id and sender_name to notifications table.  Revision ID: add_notificat

### Community 86 - "Community 86"
Cohesion: 1.0
Nodes (0): 

### Community 87 - "Community 87"
Cohesion: 1.0
Nodes (0): 

### Community 88 - "Community 88"
Cohesion: 1.0
Nodes (0): 

### Community 89 - "Community 89"
Cohesion: 1.0
Nodes (0): 

### Community 90 - "Community 90"
Cohesion: 1.0
Nodes (0): 

### Community 91 - "Community 91"
Cohesion: 1.0
Nodes (1): 向 FastAPI app 注册路由、启动钩子等。

### Community 92 - "Community 92"
Cohesion: 1.0
Nodes (1): Send a completion request and return the full response.

### Community 93 - "Community 93"
Cohesion: 1.0
Nodes (1): Send a streaming request and return the aggregated response.          Implementa

### Community 94 - "Community 94"
Cohesion: 1.0
Nodes (0): 

### Community 95 - "Community 95"
Cohesion: 1.0
Nodes (1): 从 dict 构建 SandboxConfig，支持字段级 fallback。          Args:             config: 工具配置

### Community 96 - "Community 96"
Cohesion: 1.0
Nodes (1): Backend name for identification.

### Community 97 - "Community 97"
Cohesion: 1.0
Nodes (1): Backend name for identification.

### Community 98 - "Community 98"
Cohesion: 1.0
Nodes (1): Execute code in the sandbox.

### Community 99 - "Community 99"
Cohesion: 1.0
Nodes (1): Check if the sandbox backend is healthy.

### Community 100 - "Community 100"
Cohesion: 1.0
Nodes (1): Get the capabilities of this sandbox backend.

### Community 101 - "Community 101"
Cohesion: 1.0
Nodes (1): O3: Log IDE-provided MCP servers so we know what's available (not yet forwarded

### Community 102 - "Community 102"
Cohesion: 1.0
Nodes (0): 

### Community 103 - "Community 103"
Cohesion: 1.0
Nodes (0): 

## Knowledge Gaps
- **273 isolated node(s):** `Clawith ACP Thin Client — IDE 侧瘦客户端（JetBrains Agent Client Protocol）  通过 WebSock`, `Database connection and session management.`, `SQLAlchemy declarative base.`, `Dependency for getting async database sessions.`, `Centralized logging configuration using loguru.` (+268 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 86`** (2 nodes): `update_schema.py`, `main()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 87`** (2 nodes): `remove_old_tool.py`, `run()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 88`** (1 nodes): `vite.config.ts`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 89`** (1 nodes): `vite-env.d.ts`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 90`** (1 nodes): `index.ts`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 91`** (1 nodes): `向 FastAPI app 注册路由、启动钩子等。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 92`** (1 nodes): `Send a completion request and return the full response.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 93`** (1 nodes): `Send a streaming request and return the aggregated response.          Implementa`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 94`** (1 nodes): `scripts____init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 95`** (1 nodes): `从 dict 构建 SandboxConfig，支持字段级 fallback。          Args:             config: 工具配置`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 96`** (1 nodes): `Backend name for identification.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 97`** (1 nodes): `Backend name for identification.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 98`** (1 nodes): `Execute code in the sandbox.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 99`** (1 nodes): `Check if the sandbox backend is healthy.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 100`** (1 nodes): `Get the capabilities of this sandbox backend.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 101`** (1 nodes): `O3: Log IDE-provided MCP servers so we know what's available (not yet forwarded`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 102`** (1 nodes): `setup-win.ps1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 103`** (1 nodes): `run-win.ps1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `User` connect `Community 1` to `Community 0`, `Community 2`, `Community 4`, `Community 5`, `Community 8`, `Community 9`, `Community 40`, `Community 11`, `Community 12`, `Community 19`, `Community 52`, `Community 54`, `Community 23`, `Community 22`, `Community 27`?**
  _High betweenness centrality (0.261) - this node is a cross-community bridge._
- **Why does `Agent` connect `Community 0` to `Community 1`, `Community 2`, `Community 4`, `Community 5`, `Community 8`, `Community 40`, `Community 41`, `Community 9`, `Community 12`, `Community 45`, `Community 22`, `Community 23`, `Community 27`?**
  _High betweenness centrality (0.137) - this node is a cross-community bridge._
- **Why does `SandboxConfig` connect `Community 0` to `Community 2`, `Community 4`?**
  _High betweenness centrality (0.061) - this node is a cross-community bridge._
- **Are the 791 inferred relationships involving `User` (e.g. with `Seed data script — creates initial admin user and built-in templates.` and `Create tables and seed initial data.`) actually correct?**
  _`User` has 791 INFERRED edges - model-reasoned connections that need verification._
- **Are the 546 inferred relationships involving `Agent` (e.g. with `Seed data script — creates initial admin user and built-in templates.` and `Create tables and seed initial data.`) actually correct?**
  _`Agent` has 546 INFERRED edges - model-reasoned connections that need verification._
- **Are the 373 inferred relationships involving `IdentityProvider` (e.g. with `Base` and `Create a new SSO scan session for QR code login.`) actually correct?**
  _`IdentityProvider` has 373 INFERRED edges - model-reasoned connections that need verification._
- **Are the 323 inferred relationships involving `ChannelConfig` (e.g. with `Seed data script — creates initial admin user and built-in templates.` and `Create tables and seed initial data.`) actually correct?**
  _`ChannelConfig` has 323 INFERRED edges - model-reasoned connections that need verification._
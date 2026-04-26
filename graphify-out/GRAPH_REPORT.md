# Graph Report - .  (2026-04-26)

## Corpus Check
- 319 files · ~541,979 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 4003 nodes · 15493 edges · 159 communities detected
- Extraction: 34% EXTRACTED · 66% INFERRED · 0% AMBIGUOUS · INFERRED: 10185 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## God Nodes (most connected - your core abstractions)
1. `User` - 843 edges
2. `Agent` - 640 edges
3. `IdentityProvider` - 394 edges
4. `LLMModel` - 357 edges
5. `ChannelConfig` - 342 edges
6. `OrgMember` - 327 edges
7. `Tenant` - 324 edges
8. `ChatMessage` - 317 edges
9. `SystemSetting` - 314 edges
10. `ChatSession` - 285 edges

## Surprising Connections (you probably didn't know these)
- `Clawith ACP Thin Client — IDE 侧瘦客户端（JetBrains Agent Client Protocol）  通过 WebSock` --uses--> `AgentSideConnection`  [INFERRED]
  clawith_acp/server.py → backend/app/plugins/clawith_acp/connection.py
- `Optional verbose logs for TC-14 style debugging (chunk + session_update trail).` --uses--> `AgentSideConnection`  [INFERRED]
  clawith_acp/server.py → backend/app/plugins/clawith_acp/connection.py
- `Log JSON-RPC traffic + errors to debug NDJSON (no secrets / no result bodies).` --uses--> `AgentSideConnection`  [INFERRED]
  clawith_acp/server.py → backend/app/plugins/clawith_acp/connection.py
- `Map ACP ImageContentBlock `uri` (file:// or absolute path) to a readable local f` --uses--> `AgentSideConnection`  [INFERRED]
  clawith_acp/server.py → backend/app/plugins/clawith_acp/connection.py
- `Return (mime_type, base64_without_data_prefix). Empty if no pixels to send.` --uses--> `AgentSideConnection`  [INFERRED]
  clawith_acp/server.py → backend/app/plugins/clawith_acp/connection.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.02
Nodes (342): DailyTokenUsage, CompanyCreateRequest, CompanyCreateResponse, CompanyStats, create_company(), get_enhanced_metrics(), get_platform_leaderboards(), get_platform_settings() (+334 more)

### Community 1 - "Community 1"
Cohesion: 0.04
Nodes (406): get_agent_activity(), get_conversation_messages(), list_conversations(), Activity log API — view agent work history., Get messages for a specific conversation., Get recent activity logs for an agent., List all conversation partners for this agent (web users + other agents)., Agent (+398 more)

### Community 2 - "Community 2"
Cohesion: 0.01
Nodes (108): fetchJson(), handleCreate(), handleSendTestEmail(), handleToggle(), handleToggleSetting(), loadCompanies(), saveEmailConfig(), saveEmailTemplates() (+100 more)

### Community 3 - "Community 3"
Cohesion: 0.04
Nodes (225): authorize(), bind_identity(), change_password(), check_duplicate(), forgot_password(), get_email_hint(), get_me(), get_my_tenants() (+217 more)

### Community 4 - "Community 4"
Cohesion: 0.02
Nodes (185): AgentActivityLog, Activity log model for tracking agent actions., Records every action taken by a digital employee., Rolled up token consumption per agent per day for time-series analytics., log_activity(), Activity logger — simple async function to record agent actions., Record an agent activity. Fire-and-forget, never raises., create_template() (+177 more)

### Community 5 - "Community 5"
Cohesion: 0.02
Nodes (171): extract_config_schema(), extract_skill_metadata(), Extract metadata from Superpowers SKILL.md.      Superpowers skills can have YAM, Convert Superpowers skill to Clawith Skill create/update dict., Extract JSON Schema for configuration from metadata., to_clawith_skill(), _agent_base_dir(), agent_import_from_clawhub() (+163 more)

### Community 6 - "Community 6"
Cohesion: 0.02
Nodes (121): call_agent_llm(), call_agent_llm_with_tools(), call_llm(), call_llm_with_failover(), _check_tool_requires_args(), _convert_messages_for_vision(), FailoverGuard, _get_agent_config() (+113 more)

### Community 7 - "Community 7"
Cohesion: 0.03
Nodes (107): AioSandboxBackend, aio-sandbox backend.      Connects to aio-sandbox (https://github.com/agent-infr, Check if aio-sandbox service is available., Execute code using aio-sandbox., BaseSandboxBackend, ExecutionResult, Result of code execution in a sandbox., Format execution result for user display. (+99 more)

### Community 8 - "Community 8"
Cohesion: 0.04
Nodes (145): _agent_has_any_channel(), _agent_has_feishu(), _agentbay_browser_click(), _agentbay_browser_extract(), _agentbay_browser_login(), _agentbay_browser_navigate(), _agentbay_browser_observe(), _agentbay_browser_screenshot() (+137 more)

### Community 9 - "Community 9"
Cohesion: 0.04
Nodes (63): AgentSideConnection, ndJson stream connection over WebSocket for ACP.  This module handles the ndJson, ACP Agent-side connection wrapping ndJson stream over WebSocket., Read one line from stdin, parse as JSON., Send one JSON message as a line., Close the connection., RPC: ask IDE to read a text file., RPC: ask IDE to write a text file. (+55 more)

### Community 10 - "Community 10"
Cohesion: 0.03
Nodes (56): AboutCommand, about command - display version information about Clawith ACP., AgentsCommand, agents command - list available agents for current tenant/user., Base, AcpCommand, ClawithPlugin, CommandContext (+48 more)

### Community 11 - "Community 11"
Cohesion: 0.04
Nodes (35): LSP4J 插件上下文变量定义。  将 ContextVar 集中在独立模块中，避免 router.py 与 tool_hooks.py 之间的循环导入。 所有, _build_lsp4j_ide_prompt(), invoke_lsp4j_tool(), JSONRPCRouter, _load_lsp4j_history_from_db(), _persist_lsp4j_chat_turn(), _resolve_model_by_key(), _parse_content_length() (+27 more)

### Community 12 - "Community 12"
Cohesion: 0.05
Nodes (29): ABC, BaseAuthProvider, DingTalkAuthProvider, FeishuAuthProvider, MicrosoftTeamsAuthProvider, WeComAuthProvider, AuthProviderRegistry, Authentication provider registry and factory.  This module provides a centralize (+21 more)

### Community 13 - "Community 13"
Cohesion: 0.04
Nodes (42): AgentBayClient, cleanup_agentbay_sessions(), get_agentbay_api_key_for_agent(), get_agentbay_client_for_agent(), _inject_credentials(), AgentBay API client using official SDK.  This module provides a client wrapper a, Navigate browser to URL using SDK.          The AgentBay SDK default navigation, Take a screenshot of the current browser page without navigating.          Use t (+34 more)

### Community 14 - "Community 14"
Cohesion: 0.05
Nodes (59): _cdp_exec(), ClickRequest, control_click(), control_current_url(), control_drag(), control_lock(), control_press_keys(), control_screenshot() (+51 more)

### Community 15 - "Community 15"
Cohesion: 0.05
Nodes (55): _execute_heartbeat(), _heartbeat_tick(), _is_in_active_hours(), Heartbeat service — proactive agent awareness loop.  Periodically triggers agent, Execute a single heartbeat for an agent.      Uses three short DB transactions t, One heartbeat tick: find agents due for heartbeat., Start the background heartbeat loop. Call from FastAPI startup., Check if current time is within the agent's active hours.      Format: "HH:MM-HH (+47 more)

### Community 16 - "Community 16"
Cohesion: 0.07
Nodes (30): _AsyncSessionCtx, _FakeAsyncSessionFactory, _FakeDb, _load_thin_server_module(), patch_acp_async_session(), Unit / integration-style tests for the clawith-acp WebSocket bridge (no real IDE, Inject a fake async_session factory; restore after test., Must not hit DB when history already present. (+22 more)

### Community 17 - "Community 17"
Cohesion: 0.09
Nodes (32): DummyResult, _make_agent(), _make_participant(), _make_tenant(), Tests for async A2A msg_type differentiation (notify/consult/task_delegate).  Va, notify msg_type should return immediately without calling LLM., task_delegate should create a focus item and an on_message trigger., consult msg_type should call LLM synchronously and return reply. (+24 more)

### Community 18 - "Community 18"
Cohesion: 0.08
Nodes (12): client(), FakeAsyncSessionFactory, FakeQuery, FakeScalarResult, FakeSession, FakeSkill, QueryField, RaiseOnInstanceAccess (+4 more)

### Community 19 - "Community 19"
Cohesion: 0.1
Nodes (25): _agent_headers(), _fire_extract(), _get_client(), index_all_skills(), index_enterprise_info(), index_memory_file(), _invalidate_availability_cache(), is_available() (+17 more)

### Community 20 - "Community 20"
Cohesion: 0.12
Nodes (18): DummyResult, _make_identity(), _make_login_data(), _make_user(), Unit tests for the authentication API (app/api/auth.py)., Login with a nonexistent user returns 401., Login with wrong password returns 401., Login with a disabled account returns 403. (+10 more)

### Community 21 - "Community 21"
Cohesion: 0.1
Nodes (21): generate_html(), main(), Generate HTML report from loop output data. If auto_refresh is True, adds a meta, improve_description(), main(), Call Claude to improve the description based on eval results., find_project_root(), main() (+13 more)

### Community 22 - "Community 22"
Cohesion: 0.09
Nodes (23): create_access_token(), decode_access_token(), decrypt_data(), encrypt_data(), get_authenticated_user(), get_current_admin(), get_current_user(), hash_password() (+15 more)

### Community 23 - "Community 23"
Cohesion: 0.11
Nodes (22): force_ipv4(), _ipv4_getaddrinfo(), Core email utilities for SMTP operations and network compatibility., Wrapper that forces AF_INET (IPv4) to avoid IPv6 failures in Docker., Context manager that forces all socket connections to use IPv4.      Docker cont, Synchronously send an email via SMTP with IPv4 enforcement.      Three connectio, send_smtp_email(), _decode_header_value() (+14 more)

### Community 24 - "Community 24"
Cohesion: 0.11
Nodes (7): _DummyAdapter, _FakeDB, _SyncAdapterWithFailure, test_sync_org_structure_skips_reconcile_after_member_failure(), test_validate_member_identifiers_allows_wecom_without_unionid(), test_validate_member_identifiers_rejects_unionid_equal_to_external_id(), test_validate_member_identifiers_requires_unionid_for_feishu()

### Community 25 - "Community 25"
Cohesion: 0.12
Nodes (8): DummyResult, make_agent(), make_user(), _NestedTransaction, RecordingDB, TaskCleanupDB, test_archive_agent_task_history_writes_json_snapshot(), test_delete_agent_cleans_remaining_foreign_key_rows()

### Community 26 - "Community 26"
Cohesion: 0.14
Nodes (17): build_run(), embed_file(), find_runs(), _find_runs_recursive(), generate_html(), get_mime_type(), _kill_port(), load_previous_iteration() (+9 more)

### Community 27 - "Community 27"
Cohesion: 0.12
Nodes (10): AgentManager, Agent lifecycle manager — Docker container management for OpenClaw Gateway insta, Generate openclaw.json config for the agent container., Start an OpenClaw Gateway Docker container for the agent.          Returns conta, Stop the agent's Docker container., Manage OpenClaw Gateway Docker containers for digital employees., Stop and remove the agent's Docker container., Archive agent files to a backup location and return the archive directory. (+2 more)

### Community 28 - "Community 28"
Cohesion: 0.11
Nodes (12): FeishuWSManager, _make_no_proxy_connect(), Feishu WebSocket Long Connection Manager., Recursively convert lark-oapi SDK objects to dictionaries for downstream process, Handle im.message.receive_v1 events from Feishu WebSocket asynchronously., Spawns a WebSocket client fully asynchronously inside FastAPI's loop., Return a drop-in replacement for websockets.connect that forces proxy=None., Stops an actively running WebSocket client for an agent. (+4 more)

### Community 29 - "Community 29"
Cohesion: 0.27
Nodes (19): TaskCreate, TaskLogCreate, TaskLogOut, TaskOut, TaskUpdate, add_task_log(), create_task(), _enrich_task_out() (+11 more)

### Community 30 - "Community 30"
Cohesion: 0.12
Nodes (9): Send a JSON-RPC request via Streamable HTTP transport., Send a JSON-RPC request via SSE transport.          Opens a fresh SSE connection, Auto-detect transport and send request.          Strategy: If transport is alrea, Fetch available tools from the MCP server., Execute a tool on the MCP server., Build request headers with proper MCP and auth headers., Parse response — handles both JSON and SSE (text/event-stream) formats., Extract the last JSON-RPC result from an SSE stream. (+1 more)

### Community 31 - "Community 31"
Cohesion: 0.17
Nodes (7): DummyResult, RecordingDB, test_create_session_returns_web_session_shape(), test_creator_can_list_all_sessions(), test_creator_can_view_other_users_session_messages(), test_org_admin_can_list_all_sessions(), test_org_admin_can_view_other_users_session_messages()

### Community 32 - "Community 32"
Cohesion: 0.14
Nodes (16): generate_user_api_key(), get_user_api_key_status(), _hash_user_key(), list_users(), Update a user's quota settings (admin only)., Generate or regenerate a personal API key.      The raw key is returned only onc, Revoke the current personal API key., Return whether the user has an active API key. (+8 more)

### Community 33 - "Community 33"
Cohesion: 0.17
Nodes (15): _extract_docx(), _extract_pdf(), _extract_pptx(), extract_text(), _extract_xlsx(), needs_extraction(), Extract text from common office file formats.  Supports: PDF, DOCX, XLSX, PPTX,, Extract text from XLSX using openpyxl. (+7 more)

### Community 34 - "Community 34"
Cohesion: 0.17
Nodes (15): compress_bytes_to_base64(), compress_screenshot_to_base64(), pop_temp_screenshot(), _prune_expired_cache(), Vision injection utilities for AgentBay screenshot tools.  Architecture: "Epheme, Compress raw image bytes to a base64 JPEG data URL.      Resizes to _MAX_WIDTH (, Read a screenshot file, compress it, and return a base64 data URL.      Used onl, Try to extract a screenshot from a tool result and build a vision content array. (+7 more)

### Community 35 - "Community 35"
Cohesion: 0.15
Nodes (11): configure_discord_channel(), discord_interaction_webhook(), Discord Bot Channel API routes (slash command interactions)., Register /ask global slash command with Discord API., Verify Discord ed25519 signature., Send follow-up message(s) to Discord Interactions, chunked at 2000 chars., Handle Discord Interaction webhooks (PING + slash commands)., Configure Discord bot for an agent.      Gateway mode fields: bot_token (+ conne (+3 more)

### Community 36 - "Community 36"
Cohesion: 0.15
Nodes (8): DingTalkStreamManager, DingTalk Stream Connection Manager.  Manages WebSocket-based Stream connections, Stop a running Stream client for an agent., Start Stream clients for all configured DingTalk agents., Return status of all active Stream clients., Manages DingTalk Stream clients for all agents., Start a DingTalk Stream client for a specific agent., Run the DingTalk Stream client in a blocking thread.

### Community 37 - "Community 37"
Cohesion: 0.26
Nodes (7): DummyResult, make_channel(), make_user(), RecordingDB, test_delete_wecom_channel_stops_runtime_client(), test_get_wecom_channel_marks_webhook_mode_disconnected(), test_get_wecom_channel_reports_runtime_websocket_status()

### Community 38 - "Community 38"
Cohesion: 0.2
Nodes (4): _build_wecom_conv_id(), _disable_wecom_sdk_proxy(), _process_wecom_stream_message(), WeComStreamManager

### Community 39 - "Community 39"
Cohesion: 0.19
Nodes (13): AuditAction, Helper to write audit log entries from background services., Write audit log for role-related events.      Args:         action: Role action, Write audit log for tenant-related events.      Args:         action: Tenant act, Standard audit action types., Internal method to write audit log., Write a single audit log entry using raw SQL.      Uses raw SQL to avoid ORM for, Write audit log for identity-related events.      Args:         action: Identity (+5 more)

### Community 40 - "Community 40"
Cohesion: 0.16
Nodes (8): EmailVerificationService, Email verification token lifecycle helpers., Send an email verification code using the configured template., Email verification token lifecycle helpers., Hash a raw verification token before persistence or lookup., Create a new 6-digit email verification code and store in Redis., Build the user-facing verification URL. Note: now uses 6-digit code., Load a valid verification code from Redis and mark it used (by deleting).

### Community 41 - "Community 41"
Cohesion: 0.18
Nodes (9): configure_slack_channel(), Slack Bot Channel API routes., Verify Slack's HMAC-SHA256 request signature., Send text to Slack, splitting into SLACK_MSG_LIMIT chunks if needed., Handle Slack Event API callbacks., Configure Slack bot for an agent. Fields: bot_token, signing_secret., _send_slack_messages(), slack_event_webhook() (+1 more)

### Community 42 - "Community 42"
Cohesion: 0.15
Nodes (12): TOOL_DEFINITIONS must have list_agents and call_agent with required fields., http_list_agents should return formatted agent list., http_list_agents should handle empty list gracefully., http_call_agent should return reply with session_id appended., http_call_agent should raise ValueError on 404., http_call_agent should raise ValueError when agent_id is empty., test_http_call_agent_404(), test_http_call_agent_no_agent_id() (+4 more)

### Community 43 - "Community 43"
Cohesion: 0.3
Nodes (11): _check_new_agent_messages(), _cleanup_stale_invoke_cache(), _evaluate_trigger(), _extract_json_path(), _invoke_agent_for_triggers(), _is_private_url(), _poll_check(), # NOTE: trigger state (last_fired_at, fire_count, auto-disable) (+3 more)

### Community 44 - "Community 44"
Cohesion: 0.27
Nodes (11): _ensure_smithery_connection(), _get_modelscope_api_token(), _get_smithery_api_key(), import_mcp_direct(), import_mcp_from_smithery(), refresh_atlassian_rovo_api_key(), _search_modelscope_api(), search_registries() (+3 more)

### Community 45 - "Community 45"
Cohesion: 0.24
Nodes (11): aggregate_results(), calculate_stats(), generate_benchmark(), generate_markdown(), load_run_results(), main(), Aggregate run results into summary statistics.      Returns run_summary with sta, Generate complete benchmark.json from run results. (+3 more)

### Community 46 - "Community 46"
Cohesion: 0.23
Nodes (4): _FakeAsyncClient, _FakeResponse, test_patch_message_raises_when_business_code_nonzero(), test_send_message_raises_when_business_code_nonzero()

### Community 47 - "Community 47"
Cohesion: 0.17
Nodes (11): Basic smoke tests for the clawith_acp plugin. These tests verify that the main m, Test importing the connection module., Test importing the file_system_service module., Test importing the types module., Test importing the errors module., Test importing the router module., test_import_connection(), test_import_errors() (+3 more)

### Community 48 - "Community 48"
Cohesion: 0.18
Nodes (0): 

### Community 49 - "Community 49"
Cohesion: 0.2
Nodes (9): cleanup_pending_calls(), Tool call handler for IDEA plugin integration., Send a tool call request to the connected IDE plugin., Wait for the IDEA plugin to return a tool result., Resolve a pending tool call with the result from the IDEA plugin., Clean up all pending tool calls (e.g., when WebSocket disconnects)., resolve_ide_tool_result(), send_ide_tool_request() (+1 more)

### Community 50 - "Community 50"
Cohesion: 0.22
Nodes (9): delete_trigger(), list_agent_triggers(), Triggers REST API — CRUD endpoints for the Aware page frontend., Delete a trigger entirely., List all triggers for an agent., Update a trigger (from frontend management UI)., TriggerResponse, TriggerUpdate (+1 more)

### Community 51 - "Community 51"
Cohesion: 0.2
Nodes (6): Services for managing IDEA plugin session context., Manages IDEA plugin session context information., Update IDEA plugin session context., Get session context for building prompts., Get the latest IDE context for an agent's most recent session., SessionContextManager

### Community 52 - "Community 52"
Cohesion: 0.22
Nodes (6): EnterpriseSyncService, Enterprise information synchronization service.  Uses Redis Pub/Sub to notify on, Synchronize enterprise information to all online Agent containers., Update enterprise info in database and notify all agents., Pull enterprise info from DB and write to agent's enterprise_info/ directory., Sync enterprise info to all running agents. Returns count.

### Community 53 - "Community 53"
Cohesion: 0.24
Nodes (6): PlatformService, Platform-wide service for URL resolution and host type detection., Service to handle platform-wide settings and URL resolution., Check if a host is an IP address (IPv4)., Resolve the platform's public base URL with priority lookup.                  Pr, Generate the SSO base URL for a tenant based on IP/Domain logic.

### Community 54 - "Community 54"
Cohesion: 0.29
Nodes (9): get_dingtalk_access_token(), DingTalk service for sending messages via Open API., Unified message sending method.          Default behavior is sending via Robot O, Send single chat messages via Robot using modern v1.0 API (RECOMMENDED)., Get DingTalk access_token using app_id and app_secret.      API: https://open.di, Send a work notification (工作通知).          API: https://open.dingtalk.com/documen, send_dingtalk_corp_conversation(), send_dingtalk_message() (+1 more)

### Community 55 - "Community 55"
Cohesion: 0.24
Nodes (9): _agent_workspace(), _load_skills_index(), _parse_skill_frontmatter(), Build rich system prompt context for agents.  Loads soul, memory, skills summary, Return the canonical persistent workspace path for an agent., Read a file, return empty string if missing. Truncate if too long., Parse YAML frontmatter from a skill .md file.      Returns (name, description)., Load skill index (name + description) from skills/ directory.      Supports two (+1 more)

### Community 56 - "Community 56"
Cohesion: 0.42
Nodes (8): _can_view_all_agent_chat_sessions(), create_session(), delete_session(), get_session_messages(), list_sessions(), rename_session(), SessionOut, _split_inline_tools()

### Community 57 - "Community 57"
Cohesion: 0.28
Nodes (7): main(), package_skill(), Check if a path should be excluded from packaging., Package a skill folder into a .skill file.      Args:         skill_path: Path t, should_exclude(), Basic validation of a skill, validate_skill()

### Community 58 - "Community 58"
Cohesion: 0.29
Nodes (7): close_redis(), get_redis(), publish_event(), Redis Pub/Sub events for enterprise info sync., Get or create the Redis client., Publish an event to a Redis Pub/Sub channel., Close the Redis connection.

### Community 59 - "Community 59"
Cohesion: 0.36
Nodes (6): _completion_id(), list_models(), _oai_chunk_role(), _oai_response(), openai_chat_completions(), _resolve_agent()

### Community 60 - "Community 60"
Cohesion: 0.39
Nodes (7): _dispatch(), _err(), _execute_tool(), mcp_handler(), mcp_sse_connect(), mcp_sse_messages(), _ok()

### Community 61 - "Community 61"
Cohesion: 0.36
Nodes (6): configure_atlassian_channel(), get_atlassian_api_key_for_agent(), get_atlassian_channel(), _serialize(), _sync_atlassian_tools_for_agent(), test_atlassian_channel()

### Community 62 - "Community 62"
Cohesion: 0.25
Nodes (7): get_agent_timezone(), get_agent_timezone_sync(), now_in_timezone(), Timezone utilities for resolving agent and tenant timezones., Resolve effective timezone for an agent.      Priority: agent.timezone → tenant., Synchronous version — when agent and tenant objects are already loaded.      Pri, Get current datetime in the given timezone.

### Community 63 - "Community 63"
Cohesion: 0.25
Nodes (7): detect_agentbay_env(), get_browser_snapshot(), get_desktop_screenshot(), AgentBay live preview helpers.  Provides utility functions for fetching live pre, Get a base64-encoded screenshot of an agent's active computer session.      Uses, Get a base64-encoded screenshot of an agent's active browser session.      Retur, Detect which AgentBay environment a tool belongs to.      Returns 'desktop', 'br

### Community 64 - "Community 64"
Cohesion: 0.57
Nodes (7): _agent_workspace(), build_agent_context(), _build_skills_index(), _load_skills_index(), _parse_skill_frontmatter(), _read_file_cached(), _read_file_safe()

### Community 65 - "Community 65"
Cohesion: 0.25
Nodes (0): 

### Community 66 - "Community 66"
Cohesion: 0.25
Nodes (0): 

### Community 67 - "Community 67"
Cohesion: 0.48
Nodes (5): cleanup(), runTests(), sleep(), startServer(), waitForServer()

### Community 68 - "Community 68"
Cohesion: 0.33
Nodes (6): _agent_base_dir(), list_pages(), Public pages API — serves published HTML without authentication., Serve a published HTML page. No authentication required., List published pages for an agent., render_page()

### Community 69 - "Community 69"
Cohesion: 0.52
Nodes (6): _get_agent_reply(), _is_reminder_due(), _parse_schedule(), _send_supervision_reminder(), start_supervision_reminder(), _supervision_tick()

### Community 70 - "Community 70"
Cohesion: 0.4
Nodes (5): compress_image_if_needed(), process_ide_image(), Vision handler for IDEA plugin integration., Compress image if it exceeds the size threshold.          Args:         base64_d, Process Base64 image data from IDEA plugin.          Args:         base64_data:

### Community 71 - "Community 71"
Cohesion: 0.53
Nodes (4): combineGraphs(), extractDotBlocks(), main(), renderToSvg()

### Community 72 - "Community 72"
Cohesion: 0.4
Nodes (5): extract_text(), File upload API for chat — saves files to agent workspace and extracts text., Upload a file for chat context. Saves to agent workspace/uploads/ and returns ex, Extract text content from a file., upload_file()

### Community 73 - "Community 73"
Cohesion: 0.4
Nodes (5): get_skill_creator_files(), _load_file(), Content for the skill-creator builtin skill.  Based on: https://github.com/anthr, Return list of {path, content} for all skill-creator files., Load a file from the skill_creator_files directory.

### Community 74 - "Community 74"
Cohesion: 0.4
Nodes (5): get_wecom_access_token(), WeCom (Enterprise WeChat) service for sending messages via Open API., Send a text message to a WeCom user.      API: https://developer.work.weixin.qq., Get WeCom access_token using corp_id and secret.      API: https://developer.wor, send_wecom_message()

### Community 75 - "Community 75"
Cohesion: 0.33
Nodes (0): 

### Community 76 - "Community 76"
Cohesion: 0.33
Nodes (0): 

### Community 77 - "Community 77"
Cohesion: 0.5
Nodes (4): format_lsp_message(), LSP4J 插件端到端测试脚本。  模拟通义灵码 IDE 插件的 WebSocket 连接行为， 验证 Clawith LSP4J 后端的完整功能链路。  使用, 格式化 LSP Base Protocol 消息, test_lsp4j()

### Community 78 - "Community 78"
Cohesion: 0.4
Nodes (3): list_agents_for_ide(), IDEA Plugin specific API endpoints., 获取用户可访问的智能体列表 (简化版,仅返回必要字段)

### Community 79 - "Community 79"
Cohesion: 0.5
Nodes (3): extract_code_diffs(), Diff handler for IDEA plugin integration., Extract code blocks with file paths from LLM response.          Matches patterns

### Community 80 - "Community 80"
Cohesion: 0.5
Nodes (0): 

### Community 81 - "Community 81"
Cohesion: 0.5
Nodes (3): find_or_create_channel_session(), Shared helper: find-or-create ChatSession by external channel conv_id.  Used by, Find an existing ChatSession by (agent_id, external_conv_id), or create one.

### Community 82 - "Community 82"
Cohesion: 0.83
Nodes (3): execute_task(), _log_error(), _restore_supervision_status()

### Community 83 - "Community 83"
Cohesion: 0.5
Nodes (0): 

### Community 84 - "Community 84"
Cohesion: 0.5
Nodes (1): Add source to tools and backfill data  Revision ID: add_tool_source Revises: add

### Community 85 - "Community 85"
Cohesion: 0.5
Nodes (1): Unified column fix for missing fields across main tables.  Revision ID: 20260313

### Community 86 - "Community 86"
Cohesion: 0.5
Nodes (1): Increase api_key_encrypted column length to support Minimax API keys.  Revision

### Community 87 - "Community 87"
Cohesion: 0.5
Nodes (1): Add chat_sessions table and update existing chat_messages conversation_ids.

### Community 88 - "Community 88"
Cohesion: 0.5
Nodes (1): add llm temperature  Revision ID: add_llm_temperature Revises:  Create Date: 202

### Community 89 - "Community 89"
Cohesion: 0.5
Nodes (1): Add agent token usage and context fields to agents table.  Revision ID: add_agen

### Community 90 - "Community 90"
Cohesion: 0.5
Nodes (1): Add api_key_hash column to users table for user-level API key support.  Revision

### Community 91 - "Community 91"
Cohesion: 0.5
Nodes (1): Add source and installed_by_agent_id to agent_tools  Revision ID: add_agent_tool

### Community 92 - "Community 92"
Cohesion: 0.5
Nodes (1): Add usage quota fields to users, agents, and tenants tables.  Idempotent — uses

### Community 93 - "Community 93"
Cohesion: 0.5
Nodes (1): Multi-tenant registration: add tenant_id to invitation_codes, delete historical

### Community 94 - "Community 94"
Cohesion: 0.5
Nodes (1): Add name_translit fields to OrgMember  Revision ID: be48e94fa052 Revises: add_da

### Community 95 - "Community 95"
Cohesion: 0.5
Nodes (1): add open_files column to chat_session  Revision ID: 25811072c8fd Revises: 45681b

### Community 96 - "Community 96"
Cohesion: 0.5
Nodes (1): merge heads  Revision ID: 45681b72317e Revises: 29f3f8de3ca0, f1a2b3c4d5e6 Creat

### Community 97 - "Community 97"
Cohesion: 0.5
Nodes (1): Add agent_triggers table for Pulse engine.  Revision ID: add_agent_triggers

### Community 98 - "Community 98"
Cohesion: 0.5
Nodes (1): Add a2a_async_enabled column to tenants table.  Revision ID: f1a2b3c4d5e6 Revise

### Community 99 - "Community 99"
Cohesion: 0.5
Nodes (1): Add tenant_id to llm_models table for per-company model pools.  Revision ID: add

### Community 100 - "Community 100"
Cohesion: 0.5
Nodes (1): User system refactor - unified migration.  Revision ID: user_refactor_v1 Revises

### Community 101 - "Community 101"
Cohesion: 0.5
Nodes (1): add_group_chat_fields_to_chat_sessions  Add is_group and group_name columns to c

### Community 102 - "Community 102"
Cohesion: 0.5
Nodes (1): merge heads  Revision ID: fd6e34661d12 Revises: 25811072c8fd, increase_api_key_l

### Community 103 - "Community 103"
Cohesion: 0.5
Nodes (1): Add tenant_id to skills table for per-company skill scoping.  Revision ID: add_s

### Community 104 - "Community 104"
Cohesion: 0.5
Nodes (1): add entrypoint missing columns  Revision ID: df3da9cf3b27 Revises: multi_tenant_

### Community 105 - "Community 105"
Cohesion: 0.5
Nodes (1): add llm request_timeout  Revision ID: d9cbd43b62e5 Revises: 440261f5594f Create

### Community 106 - "Community 106"
Cohesion: 0.5
Nodes (1): Add Microsoft Teams support to im_provider and channel_type enums.

### Community 107 - "Community 107"
Cohesion: 0.5
Nodes (1): Add participants table, extend chat_sessions and chat_messages, migrate messages

### Community 108 - "Community 108"
Cohesion: 0.5
Nodes (1): Refactor user system to global Identities - Phase 2 (Consolidated & Idempotent)

### Community 109 - "Community 109"
Cohesion: 0.5
Nodes (1): add published_pages table  Revision ID: add_published_pages Revises: df3da9cf3b2

### Community 110 - "Community 110"
Cohesion: 0.5
Nodes (1): Add invitation_codes table.  This is an idempotent migration — uses CREATE TABLE

### Community 111 - "Community 111"
Cohesion: 0.5
Nodes (1): Add IDE plugin fields to chat_sessions  Revision ID: 29f3f8de3ca0 Revises: add_u

### Community 112 - "Community 112"
Cohesion: 0.5
Nodes (1): Add sso_login_enabled to identity_providers  Revision ID: add_sso_login_enabled

### Community 113 - "Community 113"
Cohesion: 0.5
Nodes (1): Add agentbay and atlassian to channel_type_enum.  Revision ID: add_agentbay_enum

### Community 114 - "Community 114"
Cohesion: 0.5
Nodes (1): Add agent_id and sender_name to notifications table.  Revision ID: add_notificat

### Community 115 - "Community 115"
Cohesion: 0.67
Nodes (2): Run pytest tests in the specified directory, run_tests()

### Community 116 - "Community 116"
Cohesion: 0.67
Nodes (2): check_logs(), Check if the new logging is working correctly.

### Community 117 - "Community 117"
Cohesion: 0.67
Nodes (0): 

### Community 118 - "Community 118"
Cohesion: 1.0
Nodes (0): 

### Community 119 - "Community 119"
Cohesion: 1.0
Nodes (0): 

### Community 120 - "Community 120"
Cohesion: 1.0
Nodes (0): 

### Community 121 - "Community 121"
Cohesion: 1.0
Nodes (0): 

### Community 122 - "Community 122"
Cohesion: 1.0
Nodes (0): 

### Community 123 - "Community 123"
Cohesion: 1.0
Nodes (0): 

### Community 124 - "Community 124"
Cohesion: 1.0
Nodes (0): 

### Community 125 - "Community 125"
Cohesion: 1.0
Nodes (0): 

### Community 126 - "Community 126"
Cohesion: 1.0
Nodes (0): 

### Community 127 - "Community 127"
Cohesion: 1.0
Nodes (0): 

### Community 128 - "Community 128"
Cohesion: 1.0
Nodes (0): 

### Community 129 - "Community 129"
Cohesion: 1.0
Nodes (0): 

### Community 130 - "Community 130"
Cohesion: 1.0
Nodes (0): 

### Community 131 - "Community 131"
Cohesion: 1.0
Nodes (1): 向 FastAPI app 注册路由、启动钩子等。

### Community 132 - "Community 132"
Cohesion: 1.0
Nodes (1): Command name (e.g., "about").

### Community 133 - "Community 133"
Cohesion: 1.0
Nodes (1): Brief description for help output.

### Community 134 - "Community 134"
Cohesion: 1.0
Nodes (1): Alternative names for this command.

### Community 135 - "Community 135"
Cohesion: 1.0
Nodes (1): Nested subcommands (for "extensions list" style).

### Community 136 - "Community 136"
Cohesion: 1.0
Nodes (1): Execute the command with given arguments.

### Community 137 - "Community 137"
Cohesion: 1.0
Nodes (1): 将 JSON-RPC 消息格式化为 LSP Base Protocol 文本帧。          注意：Content-Length 必须是 UTF-8 编码

### Community 138 - "Community 138"
Cohesion: 1.0
Nodes (1): 从 header 块中解析 Content-Length 值。          Args:             header_block: header

### Community 139 - "Community 139"
Cohesion: 1.0
Nodes (0): 

### Community 140 - "Community 140"
Cohesion: 1.0
Nodes (0): 

### Community 141 - "Community 141"
Cohesion: 1.0
Nodes (1): Send a completion request and return the full response.

### Community 142 - "Community 142"
Cohesion: 1.0
Nodes (1): Send a streaming request and return the aggregated response.          Implementa

### Community 143 - "Community 143"
Cohesion: 1.0
Nodes (1): 从 dict 构建 SandboxConfig，支持字段级 fallback。          Args:             config: 工具配置

### Community 144 - "Community 144"
Cohesion: 1.0
Nodes (1): Backend name for identification.

### Community 145 - "Community 145"
Cohesion: 1.0
Nodes (1): Backend name for identification.

### Community 146 - "Community 146"
Cohesion: 1.0
Nodes (1): Execute code in the sandbox.

### Community 147 - "Community 147"
Cohesion: 1.0
Nodes (1): Check if the sandbox backend is healthy.

### Community 148 - "Community 148"
Cohesion: 1.0
Nodes (1): Get the capabilities of this sandbox backend.

### Community 149 - "Community 149"
Cohesion: 1.0
Nodes (1): Enqueue a new permission request for later approval.

### Community 150 - "Community 150"
Cohesion: 1.0
Nodes (1): Get all pending permission requests for a session.

### Community 151 - "Community 151"
Cohesion: 1.0
Nodes (1): Get a specific pending permission request by ID.

### Community 152 - "Community 152"
Cohesion: 1.0
Nodes (1): Process a permission decision (grant/deny).

### Community 153 - "Community 153"
Cohesion: 1.0
Nodes (1): Wait for a decision on a permission request.          Returns True if granted, F

### Community 154 - "Community 154"
Cohesion: 1.0
Nodes (1): Clear all pending requests for a session.

### Community 155 - "Community 155"
Cohesion: 1.0
Nodes (1): Count pending requests for a session.

### Community 156 - "Community 156"
Cohesion: 1.0
Nodes (1): Read content from a text file.

### Community 157 - "Community 157"
Cohesion: 1.0
Nodes (1): Write content to a text file.

### Community 158 - "Community 158"
Cohesion: 1.0
Nodes (0): 

## Knowledge Gaps
- **351 isolated node(s):** `Run pytest tests in the specified directory`, `LSP4J 插件端到端测试脚本。  模拟通义灵码 IDE 插件的 WebSocket 连接行为， 验证 Clawith LSP4J 后端的完整功能链路。  使用`, `格式化 LSP Base Protocol 消息`, `Check if the new logging is working correctly.`, `O3: Log IDE-provided MCP servers so we know what's available (not yet forwarded` (+346 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 118`** (2 nodes): `test_async.py`, `test_manager()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 119`** (2 nodes): `update_schema.py`, `main()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 120`** (2 nodes): `remove_old_tool.py`, `run()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 121`** (2 nodes): `ws-protocol.test.js`, `runTests()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 122`** (1 nodes): `check_circular_imports.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 123`** (1 nodes): `verify_superpowers_tests.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 124`** (1 nodes): `merge_graphify.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 125`** (1 nodes): `vite.config.ts`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 126`** (1 nodes): `vite-env.d.ts`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 127`** (1 nodes): `setup-win.ps1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 128`** (1 nodes): `run-win.ps1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 129`** (1 nodes): `test_import.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 130`** (1 nodes): `test_adapter_direct.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 131`** (1 nodes): `向 FastAPI app 注册路由、启动钩子等。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 132`** (1 nodes): `Command name (e.g., "about").`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 133`** (1 nodes): `Brief description for help output.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 134`** (1 nodes): `Alternative names for this command.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 135`** (1 nodes): `Nested subcommands (for "extensions list" style).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 136`** (1 nodes): `Execute the command with given arguments.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 137`** (1 nodes): `将 JSON-RPC 消息格式化为 LSP Base Protocol 文本帧。          注意：Content-Length 必须是 UTF-8 编码`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 138`** (1 nodes): `从 header 块中解析 Content-Length 值。          Args:             header_block: header`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 139`** (1 nodes): `has_api_key()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 140`** (1 nodes): `scripts____init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 141`** (1 nodes): `Send a completion request and return the full response.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 142`** (1 nodes): `Send a streaming request and return the aggregated response.          Implementa`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 143`** (1 nodes): `从 dict 构建 SandboxConfig，支持字段级 fallback。          Args:             config: 工具配置`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 144`** (1 nodes): `Backend name for identification.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 145`** (1 nodes): `Backend name for identification.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 146`** (1 nodes): `Execute code in the sandbox.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 147`** (1 nodes): `Check if the sandbox backend is healthy.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 148`** (1 nodes): `Get the capabilities of this sandbox backend.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 149`** (1 nodes): `Enqueue a new permission request for later approval.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 150`** (1 nodes): `Get all pending permission requests for a session.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 151`** (1 nodes): `Get a specific pending permission request by ID.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 152`** (1 nodes): `Process a permission decision (grant/deny).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 153`** (1 nodes): `Wait for a decision on a permission request.          Returns True if granted, F`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 154`** (1 nodes): `Clear all pending requests for a session.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 155`** (1 nodes): `Count pending requests for a session.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 156`** (1 nodes): `Read content from a text file.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 157`** (1 nodes): `Write content to a text file.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 158`** (1 nodes): `merge_semantic.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `User` connect `Community 0` to `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 10`, `Community 11`, `Community 12`, `Community 14`, `Community 15`, `Community 22`, `Community 25`, `Community 27`, `Community 29`, `Community 32`, `Community 35`, `Community 37`, `Community 41`, `Community 56`, `Community 68`, `Community 72`, `Community 78`?**
  _High betweenness centrality (0.232) - this node is a cross-community bridge._
- **Why does `Agent` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 10`, `Community 11`, `Community 15`, `Community 25`, `Community 27`, `Community 32`, `Community 35`, `Community 38`, `Community 41`, `Community 52`, `Community 56`, `Community 62`, `Community 78`?**
  _High betweenness centrality (0.135) - this node is a cross-community bridge._
- **Why does `ChannelConfig` connect `Community 1` to `Community 0`, `Community 35`, `Community 3`, `Community 4`, `Community 38`, `Community 36`, `Community 37`, `Community 41`, `Community 10`, `Community 13`, `Community 28`?**
  _High betweenness centrality (0.053) - this node is a cross-community bridge._
- **Are the 840 inferred relationships involving `User` (e.g. with `Seed data script — creates initial admin user and built-in templates.` and `Create tables and seed initial data.`) actually correct?**
  _`User` has 840 INFERRED edges - model-reasoned connections that need verification._
- **Are the 638 inferred relationships involving `Agent` (e.g. with `Seed data script — creates initial admin user and built-in templates.` and `Create tables and seed initial data.`) actually correct?**
  _`Agent` has 638 INFERRED edges - model-reasoned connections that need verification._
- **Are the 391 inferred relationships involving `IdentityProvider` (e.g. with `Base` and `Create a new SSO scan session for QR code login.`) actually correct?**
  _`IdentityProvider` has 391 INFERRED edges - model-reasoned connections that need verification._
- **Are the 354 inferred relationships involving `LLMModel` (e.g. with `Seed data script — creates initial admin user and built-in templates.` and `Create tables and seed initial data.`) actually correct?**
  _`LLMModel` has 354 INFERRED edges - model-reasoned connections that need verification._
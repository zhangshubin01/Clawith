# Graph Report - .  (2026-05-11)

## Corpus Check
- 425 files · ~755,411 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 5462 nodes · 22401 edges · 227 communities detected
- Extraction: 33% EXTRACTED · 67% INFERRED · 0% AMBIGUOUS · INFERRED: 14922 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## God Nodes (most connected - your core abstractions)
1. `User` - 1123 edges
2. `Agent` - 917 edges
3. `IdentityProvider` - 504 edges
4. `ChatMessage` - 500 edges
5. `LLMModel` - 498 edges
6. `Tenant` - 496 edges
7. `OrgMember` - 473 edges
8. `ChannelConfig` - 452 edges
9. `ChatSession` - 450 edges
10. `Participant` - 396 edges

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
Nodes (432): ensure_access_granted_platform_relationships(), Helpers that keep access permissions and relationship prerequisites aligned., Ensure private/custom platform users are in the agent's human network.      Plat, DailyTokenUsage, CompanyCreateRequest, CompanyCreateResponse, CompanyStats, create_company() (+424 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (496): get_agent_activity(), get_conversation_messages(), list_conversations(), Activity log API — view agent work history., Get messages for a specific conversation., Get recent activity logs for an agent., List all conversation partners for this agent (web users + other agents)., Agent (+488 more)

### Community 2 - "Community 2"
Cohesion: 0.02
Nodes (362): create_template(), delegate_task(), DelegateRequest, delete_template(), get_agent_metrics(), get_template(), handover_agent(), HandoverRequest (+354 more)

### Community 3 - "Community 3"
Cohesion: 0.01
Nodes (157): fetchJson(), handleCreate(), handleNotificationBarToggle(), handleSendTestEmail(), handleToggle(), handleToggleSetting(), loadCompanies(), saveEmailConfig() (+149 more)

### Community 4 - "Community 4"
Cohesion: 0.01
Nodes (292): Initial schema — create all tables for fresh deployments.  env.py already import, Rolled up token consumption per agent per day for time-series analytics., _agent_workspace(), build_agent_context(), _build_skills_index(), _load_skills_index(), _parse_skill_frontmatter(), Build rich system prompt context for agents.  Loads soul, memory, skills summary (+284 more)

### Community 5 - "Community 5"
Cohesion: 0.02
Nodes (152): AnswerSyncPlan, plan_answer_sync_before_finish(), LSP4J: decide how to sync final reply to the IDE via chat/answer before chat/fin, What to send before ``chat/finish``., Return which chat/answer (if any) to send so the IDE panel matches reply.      b, broadcast_config_refresh_models(), _build_lsp4j_ide_prompt(), ChatAskParam (+144 more)

### Community 6 - "Community 6"
Cohesion: 0.02
Nodes (169): ABC, AuditAction, Helper to write audit log entries from background services., Write audit log for role-related events.      Args:         action: Role action, Standard audit action types., Write audit log for tenant-related events.      Args:         action: Tenant act, Internal method to write audit log., Write a single audit log entry using raw SQL.      Uses raw SQL to avoid ORM for (+161 more)

### Community 7 - "Community 7"
Cohesion: 0.02
Nodes (219): _agent_has_any_channel(), _agent_has_feishu(), _agentbay_app_field(), _agentbay_browser_click(), _agentbay_browser_extract(), _agentbay_browser_login(), _agentbay_browser_navigate(), _agentbay_browser_observe() (+211 more)

### Community 8 - "Community 8"
Cohesion: 0.02
Nodes (126): AboutCommand, about command - display version information about Clawith ACP., AcpCommand, AgentsCommand, Base, AcpCommand, ClawithPlugin, CommandContext (+118 more)

### Community 9 - "Community 9"
Cohesion: 0.03
Nodes (111): AioSandboxBackend, aio-sandbox backend.      Connects to aio-sandbox (https://github.com/agent-infr, Check if aio-sandbox service is available., Execute code using aio-sandbox., BaseSandboxBackend, ExecutionResult, Result of code execution in a sandbox., Format execution result for user display. (+103 more)

### Community 10 - "Community 10"
Cohesion: 0.04
Nodes (62): AgentSideConnection, ndJson stream connection over WebSocket for ACP.  This module handles the ndJson, ACP Agent-side connection wrapping ndJson stream over WebSocket., Read one line from stdin, parse as JSON., Send one JSON message as a line., Close the connection., RPC: ask IDE to read a text file., RPC: ask IDE to write a text file. (+54 more)

### Community 11 - "Community 11"
Cohesion: 0.04
Nodes (55): extract_config_schema(), extract_skill_metadata(), Extract metadata from Superpowers SKILL.md.      Superpowers skills can have YAM, Convert Superpowers skill to Clawith Skill create/update dict., Extract JSON Schema for configuration from metadata., to_clawith_skill(), Client for interacting with Superpowers Marketplace git repository., Check if the marketplace repo is already cloned. (+47 more)

### Community 12 - "Community 12"
Cohesion: 0.03
Nodes (49): AgentBayClient, AgentBaySession, cleanup_agentbay_sessions(), get_agentbay_api_key_for_agent(), get_agentbay_client_for_agent(), _inject_credentials(), _is_plausible_agentbay_api_key(), AgentBay API client using official SDK.  This module provides a client wrapper a (+41 more)

### Community 13 - "Community 13"
Cohesion: 0.05
Nodes (59): _cdp_exec(), ClickRequest, control_click(), control_current_url(), control_drag(), control_lock(), control_press_keys(), control_screenshot() (+51 more)

### Community 14 - "Community 14"
Cohesion: 0.06
Nodes (46): AgentActivityLog, Activity log model for tracking agent actions., Records every action taken by a digital employee., log_activity(), Activity logger — simple async function to record agent actions., Record an agent activity. Fire-and-forget, never raises., _execute_heartbeat(), _heartbeat_tick() (+38 more)

### Community 15 - "Community 15"
Cohesion: 0.07
Nodes (30): _AsyncSessionCtx, _FakeAsyncSessionFactory, _FakeDb, _load_thin_server_module(), patch_acp_async_session(), Unit / integration-style tests for the clawith-acp WebSocket bridge (no real IDE, Inject a fake async_session factory; restore after test., Must not hit DB when history already present. (+22 more)

### Community 16 - "Community 16"
Cohesion: 0.09
Nodes (32): DummyResult, _make_agent(), _make_participant(), _make_tenant(), Tests for async A2A msg_type differentiation (notify/consult/task_delegate).  Va, notify msg_type should return immediately without calling LLM., task_delegate should create a focus item and an on_message trigger., consult msg_type should call LLM synchronously and return reply. (+24 more)

### Community 17 - "Community 17"
Cohesion: 0.07
Nodes (41): _apply_category_filter(), broadcast_notification(), BroadcastRequest, get_unread_count(), list_notifications(), mark_all_read(), mark_read(), Notification model — notifications for users and agents. (+33 more)

### Community 18 - "Community 18"
Cohesion: 0.08
Nodes (12): client(), FakeAsyncSessionFactory, FakeQuery, FakeScalarResult, FakeSession, FakeSkill, QueryField, RaiseOnInstanceAccess (+4 more)

### Community 19 - "Community 19"
Cohesion: 0.11
Nodes (31): _agent_visible_tool_clause(), create_tool(), _decrypt_sensitive_fields(), delete_agent_tool(), delete_category_config(), delete_tool(), _encrypt_sensitive_fields(), get_agent_tool_config() (+23 more)

### Community 20 - "Community 20"
Cohesion: 0.11
Nodes (27): CommentCreate, CommentOut, Config, create_comment(), create_post(), delete_post(), get_post(), _hidden_agent_exists_for_author() (+19 more)

### Community 21 - "Community 21"
Cohesion: 0.08
Nodes (25): DingTalkStreamManager, download_dingtalk_media(), _download_file(), _fire_and_forget(), _get_media_download_url(), _handle_media_and_dispatch(), _process_media_message(), DingTalk Stream Connection Manager.  Manages WebSocket-based Stream connections (+17 more)

### Community 22 - "Community 22"
Cohesion: 0.11
Nodes (25): AgentFocusItem, FocusItemResponse, FocusUpsertBody, Structured Focus API for Aware., A structured focus item tracked by an agent.      Focus is intentionally databas, complete_focus_item(), ensure_focus_item(), ensure_focus_sections() (+17 more)

### Community 23 - "Community 23"
Cohesion: 0.11
Nodes (20): DummyResult, _make_identity(), _make_login_data(), _make_user(), Unit tests for the authentication API (app/api/auth.py)., Login with a nonexistent user returns 401., Login with wrong password returns 401., Login with a disabled account returns 403. (+12 more)

### Community 24 - "Community 24"
Cohesion: 0.08
Nodes (8): BaseOrgSyncAdapter, _DummyAdapter, _FakeDB, _SyncAdapterWithFailure, test_sync_org_structure_skips_reconcile_after_member_failure(), test_validate_member_identifiers_allows_wecom_without_unionid(), test_validate_member_identifiers_rejects_unionid_equal_to_external_id(), test_validate_member_identifiers_requires_unionid_for_feishu()

### Community 25 - "Community 25"
Cohesion: 0.15
Nodes (29): _bucket_items(), _build_company_daily_content(), _build_company_rollup_content(), CompanyMember, _contains_risk(), _dedupe_preserve_order(), _default_report_headings(), _extract_section_lines() (+21 more)

### Community 26 - "Community 26"
Cohesion: 0.09
Nodes (22): Compatibility exports for HTML document conversion helpers., generate_html(), main(), Generate HTML report from loop output data. If auto_refresh is True, adds a meta, improve_description(), main(), Call Claude to improve the description based on eval results., find_project_root() (+14 more)

### Community 27 - "Community 27"
Cohesion: 0.09
Nodes (14): AgentExpired, check_agent_creation_quota(), check_agent_expired(), check_agent_llm_quota(), check_conversation_quota(), enforce_heartbeat_floor(), get_agent_expiry_reply(), _get_period_duration() (+6 more)

### Community 28 - "Community 28"
Cohesion: 0.09
Nodes (23): create_access_token(), decode_access_token(), decrypt_data(), encrypt_data(), get_authenticated_user(), get_current_admin(), get_current_user(), hash_password() (+15 more)

### Community 29 - "Community 29"
Cohesion: 0.11
Nodes (22): force_ipv4(), _ipv4_getaddrinfo(), Core email utilities for SMTP operations and network compatibility., Wrapper that forces AF_INET (IPv4) to avoid IPv6 failures in Docker., Context manager that forces all socket connections to use IPv4.      Docker cont, Synchronously send an email via SMTP with IPv4 enforcement.      Three connectio, send_smtp_email(), _decode_header_value() (+14 more)

### Community 30 - "Community 30"
Cohesion: 0.11
Nodes (22): _agent_available(), build_visible_agents_query(), check_agent_access(), evaluate_agent_relationship_status(), evaluate_human_relationship_status(), get_agent_access_level_for_user_id(), get_agent_accessible_user_ids(), _is_admin() (+14 more)

### Community 31 - "Community 31"
Cohesion: 0.13
Nodes (18): BaseHTTPRequestHandler, build_run(), embed_file(), find_runs(), _find_runs_recursive(), generate_html(), get_mime_type(), _kill_port() (+10 more)

### Community 32 - "Community 32"
Cohesion: 0.12
Nodes (8): DummyResult, make_agent(), make_user(), _NestedTransaction, RecordingDB, TaskCleanupDB, test_archive_agent_task_history_writes_json_snapshot(), test_delete_agent_cleans_remaining_foreign_key_rows()

### Community 33 - "Community 33"
Cohesion: 0.12
Nodes (10): AgentManager, Agent lifecycle manager — Docker container management for OpenClaw Gateway insta, Generate openclaw.json config for the agent container., Start an OpenClaw Gateway Docker container for the agent.          Returns conta, Stop the agent's Docker container., Manage OpenClaw Gateway Docker containers for digital employees., Stop and remove the agent's Docker container., Archive agent files to a backup location and return the archive directory. (+2 more)

### Community 34 - "Community 34"
Cohesion: 0.19
Nodes (9): build_wechat_headers(), _extract_wechat_text(), _process_wechat_message(), random_wechat_uin(), remember_wechat_context(), send_wechat_text_message(), split_wechat_text(), update_wechat_context_cache() (+1 more)

### Community 35 - "Community 35"
Cohesion: 0.12
Nodes (11): FeishuWSManager, _make_no_proxy_connect(), Feishu WebSocket Long Connection Manager., Handle im.message.receive_v1 events from Feishu WebSocket asynchronously., Spawns a WebSocket client fully asynchronously inside FastAPI's loop., Return a drop-in replacement for websockets.connect that forces proxy=None., Stops an actively running WebSocket client for an agent., Start WS clients for all configured Feishu agents. (+3 more)

### Community 36 - "Community 36"
Cohesion: 0.16
Nodes (13): android_module_tier(), collect_android_values_xml_hits(), contains_han(), filename_keyword_for_search_file(), is_extension_only_language_glob(), is_unusable_natural_language_file_query(), longest_latin_identifier(), LSP4J 本地检索纯函数（无 DB / 异步依赖），供 jsonrpc_router 与单测复用。 (+5 more)

### Community 37 - "Community 37"
Cohesion: 0.18
Nodes (17): _clean_cell(), _extract_docx(), _extract_pdf(), _extract_pptx(), extract_text(), _extract_xlsx(), _markdown_table(), needs_extraction() (+9 more)

### Community 38 - "Community 38"
Cohesion: 0.12
Nodes (9): Send a JSON-RPC request via Streamable HTTP transport., Send a JSON-RPC request via SSE transport.          Opens a fresh SSE connection, Auto-detect transport and send request.          Strategy: If transport is alrea, Fetch available tools from the MCP server., Execute a tool on the MCP server., Build request headers with proper MCP and auth headers., Parse response — handles both JSON and SSE (text/event-stream) formats., Extract the last JSON-RPC result from an SSE stream. (+1 more)

### Community 39 - "Community 39"
Cohesion: 0.22
Nodes (17): _check_new_agent_messages(), _cleanup_stale_invoke_cache(), _evaluate_trigger(), _extract_json_path(), _handle_okr_collection_trigger(), _handle_okr_report_trigger(), _invoke_agent_for_triggers(), _is_private_url() (+9 more)

### Community 40 - "Community 40"
Cohesion: 0.15
Nodes (17): compress_bytes_to_base64(), compress_screenshot_to_base64(), _draw_coordinate_grid(), pop_temp_screenshot(), _prune_expired_cache(), Vision injection utilities for AgentBay screenshot tools.  Architecture: "Epheme, Overlay a light desktop-coordinate grid for LLM-only screenshot analysis., Compress raw image bytes to a base64 JPEG data URL.      Resizes to _MAX_WIDTH ( (+9 more)

### Community 41 - "Community 41"
Cohesion: 0.17
Nodes (7): DummyResult, RecordingDB, test_create_session_returns_web_session_shape(), test_creator_can_list_all_sessions(), test_creator_can_view_other_users_session_messages(), test_org_admin_can_list_all_sessions(), test_org_admin_can_view_other_users_session_messages()

### Community 42 - "Community 42"
Cohesion: 0.21
Nodes (10): _async_append(), _async_return(), FakeStreamClient, _finish_response(), _finish_response_with_arguments(), _model(), _plain_response(), test_call_llm_requires_finish_tool_to_stop() (+2 more)

### Community 43 - "Community 43"
Cohesion: 0.13
Nodes (4): make_tool(), test_admin_tools_are_visible_only_to_same_tenant(), test_agent_installed_tools_require_explicit_assignment(), test_builtin_tools_are_visible_across_tenants()

### Community 44 - "Community 44"
Cohesion: 0.14
Nodes (16): generate_user_api_key(), get_user_api_key_status(), _hash_user_key(), list_users(), Update a user's quota settings (admin only)., Generate or regenerate a personal API key.      The raw key is returned only onc, Revoke the current personal API key., Return whether the user has an active API key. (+8 more)

### Community 45 - "Community 45"
Cohesion: 0.18
Nodes (16): _load_from(), _make_valid_plugin(), Calling load_plugins twice must not register routes twice., Plugin that exports a non-ClawithPlugin 'plugin' attribute must be skipped., Create a minimal valid plugin directory in tmp_path., Run load_plugins but point _PLUGINS_DIR at tmp_path., Empty plugins directory must not raise., A valid plugin with plugin.json and __init__.py registers its routes. (+8 more)

### Community 46 - "Community 46"
Cohesion: 0.15
Nodes (11): configure_discord_channel(), discord_interaction_webhook(), Discord Bot Channel API routes (slash command interactions)., Register /ask global slash command with Discord API., Verify Discord ed25519 signature., Send follow-up message(s) to Discord Interactions, chunked at 2000 chars., Handle Discord Interaction webhooks (PING + slash commands)., Configure Discord bot for an agent.      Gateway mode fields: bot_token (+ conne (+3 more)

### Community 47 - "Community 47"
Cohesion: 0.26
Nodes (7): DummyResult, make_channel(), make_user(), RecordingDB, test_delete_wecom_channel_stops_runtime_client(), test_get_wecom_channel_marks_webhook_mode_disconnected(), test_get_wecom_channel_reports_runtime_websocket_status()

### Community 48 - "Community 48"
Cohesion: 0.2
Nodes (4): _build_wecom_conv_id(), _disable_wecom_sdk_proxy(), _process_wecom_stream_message(), WeComStreamManager

### Community 49 - "Community 49"
Cohesion: 0.16
Nodes (8): EmailVerificationService, Email verification token lifecycle helpers., Email verification token lifecycle helpers., Hash a raw verification token before persistence or lookup., Create a new 6-digit email verification code and store in Redis., Build the user-facing verification URL. Note: now uses 6-digit code., Load a valid verification code from Redis and mark it used (by deleting)., Send an email verification code using the configured template.

### Community 50 - "Community 50"
Cohesion: 0.18
Nodes (9): configure_slack_channel(), Slack Bot Channel API routes., Verify Slack's HMAC-SHA256 request signature., Send text to Slack, splitting into SLACK_MSG_LIMIT chunks if needed., Handle Slack Event API callbacks., Configure Slack bot for an agent. Fields: bot_token, signing_secret., _send_slack_messages(), slack_event_webhook() (+1 more)

### Community 51 - "Community 51"
Cohesion: 0.33
Nodes (12): _build_okr_snapshot(), collect_all_focus_updates(), _compute_period(), _format_monthly_report_body(), _format_report_body(), generate_daily_report(), generate_monthly_report(), generate_weekly_report() (+4 more)

### Community 52 - "Community 52"
Cohesion: 0.31
Nodes (7): DummyResult, _make_agent(), RecordingDB, test_custom_boundary_follow_up_keeps_tools_enabled(), test_custom_follow_up_keeps_tools_enabled(), test_first_contact_is_the_only_tool_free_greeting_turn(), test_template_follow_up_keeps_tools_enabled()

### Community 53 - "Community 53"
Cohesion: 0.15
Nodes (12): TOOL_DEFINITIONS must have list_agents and call_agent with required fields., http_list_agents should return formatted agent list., http_list_agents should handle empty list gracefully., http_call_agent should return reply with session_id appended., http_call_agent should raise ValueError on 404., http_call_agent should raise ValueError when agent_id is empty., test_http_call_agent_404(), test_http_call_agent_no_agent_id() (+4 more)

### Community 54 - "Community 54"
Cohesion: 0.23
Nodes (6): _extract_message_text(), WhatsApp Cloud API channel routes., _send_whatsapp_messages(), _split_text(), _verify_signature(), whatsapp_event_webhook()

### Community 55 - "Community 55"
Cohesion: 0.23
Nodes (11): download_dingtalk_media(), get_dingtalk_access_token(), DingTalk service for sending messages via Open API., Unified message sending method.          Default behavior is sending via Robot O, Download a media file from DingTalk using a downloadCode.      Convenience wrapp, Send single chat messages via Robot using modern v1.0 API (RECOMMENDED)., Get DingTalk access_token using app_id and app_secret.      API: https://open.di, Send a work notification (工作通知).          API: https://open.dingtalk.com/documen (+3 more)

### Community 56 - "Community 56"
Cohesion: 0.27
Nodes (11): _ensure_smithery_connection(), _get_modelscope_api_token(), _get_smithery_api_key(), import_mcp_direct(), import_mcp_from_smithery(), refresh_atlassian_rovo_api_key(), _search_modelscope_api(), search_registries() (+3 more)

### Community 57 - "Community 57"
Cohesion: 0.24
Nodes (11): aggregate_results(), calculate_stats(), generate_benchmark(), generate_markdown(), load_run_results(), main(), Aggregate run results into summary statistics.      Returns run_summary with sta, Generate complete benchmark.json from run results. (+3 more)

### Community 58 - "Community 58"
Cohesion: 0.23
Nodes (4): _FakeAsyncClient, _FakeResponse, test_patch_message_raises_when_business_code_nonzero(), test_send_message_raises_when_business_code_nonzero()

### Community 59 - "Community 59"
Cohesion: 0.24
Nodes (7): make_user(), _RelationshipStatusDb, _ScalarResult, test_agent_relationship_status_active_when_original_creator_still_manages_both_agents(), test_agent_relationship_status_requires_original_creator_to_still_manage_both_agents(), test_build_visible_agents_query_platform_admin_still_uses_visibility_filters(), test_build_visible_agents_query_restricts_to_same_tenant_and_visible_permissions()

### Community 60 - "Community 60"
Cohesion: 0.17
Nodes (11): Basic smoke tests for the clawith_acp plugin. These tests verify that the main m, Test importing the connection module., Test importing the file_system_service module., Test importing the types module., Test importing the errors module., Test importing the router module., test_import_connection(), test_import_errors() (+3 more)

### Community 61 - "Community 61"
Cohesion: 0.31
Nodes (10): _can_view_all_agent_chat_sessions(), create_session(), CreateSessionIn, delete_session(), get_session_messages(), list_sessions(), PatchSessionIn, rename_session() (+2 more)

### Community 62 - "Community 62"
Cohesion: 0.18
Nodes (0): 

### Community 63 - "Community 63"
Cohesion: 0.22
Nodes (9): compress_image_if_needed(), compress_image_if_needed_async(), process_ide_image(), process_ide_image_async(), Vision handler for IDEA plugin integration., Async entry point for callers in asyncio contexts., Compress image if it exceeds the size threshold.          Args:         base64_d, Process Base64 image data from IDEA plugin.          Args:         base64_data: (+1 more)

### Community 64 - "Community 64"
Cohesion: 0.2
Nodes (9): cleanup_pending_calls(), Tool call handler for IDEA plugin integration., Send a tool call request to the connected IDE plugin., Wait for the IDEA plugin to return a tool result., Resolve a pending tool call with the result from the IDEA plugin., Clean up all pending tool calls (e.g., when WebSocket disconnects)., resolve_ide_tool_result(), send_ide_tool_request() (+1 more)

### Community 65 - "Community 65"
Cohesion: 0.29
Nodes (7): _completion_id(), list_models(), _oai_chunk_role(), _oai_response(), OAIMessage, openai_chat_completions(), _resolve_agent()

### Community 66 - "Community 66"
Cohesion: 0.22
Nodes (9): delete_trigger(), list_agent_triggers(), Triggers REST API — CRUD endpoints for the Aware page frontend., Delete a trigger entirely., List all triggers for an agent., Update a trigger (from frontend management UI)., TriggerResponse, TriggerUpdate (+1 more)

### Community 67 - "Community 67"
Cohesion: 0.29
Nodes (7): _build_qrcode_headers(), create_wechat_qrcode(), get_wechat_qrcode_image(), get_wechat_qrcode_status(), WeChat iLink Bot channel API routes., _route_tag(), _validate_qrcode_proxy_url()

### Community 68 - "Community 68"
Cohesion: 0.2
Nodes (6): Services for managing IDEA plugin session context., Manages IDEA plugin session context information., Update IDEA plugin session context., Get session context for building prompts., Get the latest IDE context for an agent's most recent session., SessionContextManager

### Community 69 - "Community 69"
Cohesion: 0.22
Nodes (6): EnterpriseSyncService, Enterprise information synchronization service.  Uses Redis Pub/Sub to notify on, Synchronize enterprise information to all online Agent containers., Update enterprise info in database and notify all agents., Pull enterprise info from DB and write to agent's enterprise_info/ directory., Sync enterprise info to all running agents. Returns count.

### Community 70 - "Community 70"
Cohesion: 0.24
Nodes (6): PlatformService, Platform-wide service for URL resolution and host type detection., Check if a host is an IP address (IPv4)., Resolve the platform's public base URL with priority lookup.                  Pr, Generate the SSO base URL for a tenant based on IP/Domain logic., Service to handle platform-wide settings and URL resolution.

### Community 71 - "Community 71"
Cohesion: 0.24
Nodes (9): _agent_workspace(), _load_skills_index(), _parse_skill_frontmatter(), Build rich system prompt context for agents.  Loads soul, memory, skills summary, Return the canonical persistent workspace path for an agent., Read a file, return empty string if missing. Truncate if too long., Parse YAML frontmatter from a skill .md file.      Returns (name, description)., Load skill index (name + description) from skills/ directory.      Supports two (+1 more)

### Community 72 - "Community 72"
Cohesion: 0.24
Nodes (3): _DummyAsyncClient, _DummyResponse, test_feishu_auth_provider_prefers_contact_user_id_over_open_id()

### Community 73 - "Community 73"
Cohesion: 0.31
Nodes (8): _get_okr_agent(), hook_new_agent(), hook_new_org_member(), Hook to automatically bind new users and company-visible agents to the OKR Agent, When a new OrgMember is created or bound, bind them to the system OKR Agent if i, Bind all existing active platform users in a tenant to its OKR Agent.      hook_, When a new company-visible agent is created, bind to OKR Agent., sync_okr_agent_platform_members()

### Community 74 - "Community 74"
Cohesion: 0.44
Nodes (8): _append_seed_marker(), _ensure_okr_tool_rows_exist(), patch_existing_okr_agent(), seed_default_agents(), seed_okr_agent(), seed_okr_agent_for_tenant(), _seed_okr_triggers(), _sync_okr_triggers_with_settings()

### Community 75 - "Community 75"
Cohesion: 0.28
Nodes (7): main(), package_skill(), Check if a path should be excluded from packaging., Package a skill folder into a .skill file.      Args:         skill_path: Path t, should_exclude(), Basic validation of a skill, validate_skill()

### Community 76 - "Community 76"
Cohesion: 0.29
Nodes (7): close_redis(), get_redis(), publish_event(), Redis Pub/Sub events for enterprise info sync., Get or create the Redis client., Publish an event to a Redis Pub/Sub channel., Close the Redis connection.

### Community 77 - "Community 77"
Cohesion: 0.39
Nodes (7): _dispatch(), _err(), _execute_tool(), mcp_handler(), mcp_sse_connect(), mcp_sse_messages(), _ok()

### Community 78 - "Community 78"
Cohesion: 0.36
Nodes (6): configure_atlassian_channel(), get_atlassian_api_key_for_agent(), get_atlassian_channel(), _serialize(), _sync_atlassian_tools_for_agent(), test_atlassian_channel()

### Community 79 - "Community 79"
Cohesion: 0.25
Nodes (7): get_agent_timezone(), get_agent_timezone_sync(), now_in_timezone(), Timezone utilities for resolving agent and tenant timezones., Resolve effective timezone for an agent.      Priority: agent.timezone → tenant., Synchronous version — when agent and tenant objects are already loaded.      Pri, Get current datetime in the given timezone.

### Community 80 - "Community 80"
Cohesion: 0.32
Nodes (1): DiscordGatewayManager

### Community 81 - "Community 81"
Cohesion: 0.25
Nodes (7): detect_agentbay_env(), get_browser_snapshot(), get_desktop_screenshot(), AgentBay live preview helpers.  Provides utility functions for fetching live pre, Get a base64-encoded screenshot of an agent's active computer session.      Uses, Get a base64-encoded screenshot of an agent's active browser session.      Retur, Detect which AgentBay environment a tool belongs to.      Returns 'desktop', 'br

### Community 82 - "Community 82"
Cohesion: 0.25
Nodes (0): 

### Community 83 - "Community 83"
Cohesion: 0.25
Nodes (1): search_input_utils 回归：不加载 jsonrpc_router，避免 DB/异步副作用。

### Community 84 - "Community 84"
Cohesion: 0.39
Nodes (6): _has_column(), _has_index(), _has_table(), _has_unique_constraint(), Add IDE plugin fields to chat_sessions  Revision ID: 29f3f8de3ca0 Revises: add_u, upgrade()

### Community 85 - "Community 85"
Cohesion: 0.48
Nodes (5): cleanup(), runTests(), sleep(), startServer(), waitForServer()

### Community 86 - "Community 86"
Cohesion: 0.33
Nodes (6): _agent_base_dir(), list_pages(), Public pages API — serves published HTML without authentication., Serve a published HTML page. No authentication required., List published pages for an agent., render_page()

### Community 87 - "Community 87"
Cohesion: 0.33
Nodes (2): get_google_provider_base_url(), get_google_redirect_uri()

### Community 88 - "Community 88"
Cohesion: 0.52
Nodes (6): _get_agent_reply(), _is_reminder_due(), _parse_schedule(), _send_supervision_reminder(), start_supervision_reminder(), _supervision_tick()

### Community 89 - "Community 89"
Cohesion: 0.43
Nodes (6): chrome_executable(), collect_browser_layout(), is_complex_css_paint(), is_translucent_css_color(), Shared Chrome rendering helpers for document conversion., Return a local Chrome/Chromium executable path if one is available.

### Community 90 - "Community 90"
Cohesion: 0.43
Nodes (5): _has_column(), _has_index(), _has_table(), add open_files column to chat_session  Revision ID: 25811072c8fd Revises: 45681b, upgrade()

### Community 91 - "Community 91"
Cohesion: 0.53
Nodes (4): combineGraphs(), extractDotBlocks(), main(), renderToSvg()

### Community 92 - "Community 92"
Cohesion: 0.33
Nodes (5): 测试 toolCall markdown 块格式。, 测试完整的 MATCHER_PATTERN 对 toolCall 的匹配。, 验证插件的正则表达式是否能匹配 toolCall 格式。, test_full_matcher_pattern(), test_toolcall_regex_match()

### Community 93 - "Community 93"
Cohesion: 0.4
Nodes (5): extract_text(), File upload API for chat — saves files to agent workspace and extracts text., Upload a file for chat context. Saves to agent workspace/uploads/ and returns ex, Extract text content from a file., upload_file()

### Community 94 - "Community 94"
Cohesion: 0.4
Nodes (5): get_skill_creator_files(), _load_file(), Content for the skill-creator builtin skill.  Based on: https://github.com/anthr, Return list of {path, content} for all skill-creator files., Load a file from the skill_creator_files directory.

### Community 95 - "Community 95"
Cohesion: 0.33
Nodes (5): add_thinking_reaction(), DingTalk emotion reaction service — "thinking" indicator on user messages., Add "🤔思考中" reaction to a user message. Fire-and-forget, never raises., Recall "🤔思考中" reaction with retry (0ms, 1500ms, 5000ms). Fire-and-forget., recall_thinking_reaction()

### Community 96 - "Community 96"
Cohesion: 0.4
Nodes (5): get_wecom_access_token(), WeCom (Enterprise WeChat) service for sending messages via Open API., Send a text message to a WeCom user.      API: https://developer.work.weixin.qq., Get WeCom access_token using corp_id and secret.      API: https://developer.wor, send_wecom_message()

### Community 97 - "Community 97"
Cohesion: 0.4
Nodes (5): ensure_primary_platform_session(), get_primary_platform_session(), Helpers for first-party chat session selection and creation., Return the current primary first-party session for a user+agent pair, if any., Return a guaranteed primary platform session for a given user+agent pair.      T

### Community 98 - "Community 98"
Cohesion: 0.33
Nodes (0): 

### Community 99 - "Community 99"
Cohesion: 0.33
Nodes (0): 

### Community 100 - "Community 100"
Cohesion: 0.33
Nodes (0): 

### Community 101 - "Community 101"
Cohesion: 0.5
Nodes (4): format_lsp_message(), LSP4J 插件端到端测试脚本。  模拟通义灵码 IDE 插件的 WebSocket 连接行为， 验证 Clawith LSP4J 后端的完整功能链路。  使用, 格式化 LSP Base Protocol 消息, test_lsp4j()

### Community 102 - "Community 102"
Cohesion: 0.4
Nodes (3): list_agents_for_ide(), IDEA Plugin specific API endpoints., 获取用户可访问的智能体列表 (简化版,仅返回必要字段)

### Community 103 - "Community 103"
Cohesion: 0.6
Nodes (3): google_workspace_callback(), _handle_google_admin_sync_callback(), _handle_google_sso_callback()

### Community 104 - "Community 104"
Cohesion: 0.7
Nodes (4): _agent_request_message(), _cleanup_legacy_daily_reply_triggers(), _human_request_message(), trigger_daily_collection_for_tenant()

### Community 105 - "Community 105"
Cohesion: 0.5
Nodes (3): extract_code_diffs(), 代码 Diff 提取器 — 从 LLM 响应中解析带文件路径的代码块。  用于 IDEA 插件集成场景，将 LLM 返回的代码块转换为结构化的 diff 数据。, 从 LLM 响应内容中提取带文件路径的代码块。      按优先级依次尝试两种匹配策略：     1. 显式路径格式: ```<lang>:<path> — 语

### Community 106 - "Community 106"
Cohesion: 0.5
Nodes (0): 

### Community 107 - "Community 107"
Cohesion: 0.5
Nodes (2): 精确验证 toolCall 正则匹配行为。, TestToolCallRegex

### Community 108 - "Community 108"
Cohesion: 0.5
Nodes (3): find_or_create_channel_session(), Shared helper: find-or-create ChatSession by external channel conv_id.  Used by, Find an existing ChatSession by (agent_id, external_conv_id), or create one.

### Community 109 - "Community 109"
Cohesion: 0.5
Nodes (3): Notification service — unified entry point for sending in-app notifications., Create and persist a notification for a user or an agent.      Args:         db:, send_notification()

### Community 110 - "Community 110"
Cohesion: 0.83
Nodes (3): execute_task(), _log_error(), _restore_supervision_status()

### Community 111 - "Community 111"
Cohesion: 0.5
Nodes (3): is_non_workday(), Business calendar helpers for scheduled OKR work.  The first layer is intentiona, Return True when a date should be skipped for business reporting.

### Community 112 - "Community 112"
Cohesion: 0.5
Nodes (0): 

### Community 113 - "Community 113"
Cohesion: 0.5
Nodes (1): Add is_system column to agents table, and agent_triggers.is_system.  Also adds i

### Community 114 - "Community 114"
Cohesion: 0.5
Nodes (1): Ensure channel_type_enum contains all channel values used by the app.  Revision

### Community 115 - "Community 115"
Cohesion: 0.5
Nodes (1): Add source to tools and backfill data  Revision ID: add_tool_source Revises: add

### Community 116 - "Community 116"
Cohesion: 0.5
Nodes (1): Unified column fix for missing fields across main tables.  Revision ID: 20260313

### Community 117 - "Community 117"
Cohesion: 0.5
Nodes (1): Merge merge_okr_api_key and add_workspace_revisions heads.  Revision ID: merge_w

### Community 118 - "Community 118"
Cohesion: 0.5
Nodes (1): Increase api_key_encrypted column length to support Minimax API keys.  Revision

### Community 119 - "Community 119"
Cohesion: 0.5
Nodes (1): Add workspace file revision and edit lock tables.  Revision ID: add_workspace_re

### Community 120 - "Community 120"
Cohesion: 0.5
Nodes (1): Merge heads after main merge  Revision ID: 5fe287d9d58b Revises: fd6e34661d12, r

### Community 121 - "Community 121"
Cohesion: 0.5
Nodes (1): Merge OKR tables branch with llm_request_timeout branch.  This merge migration r

### Community 122 - "Community 122"
Cohesion: 0.5
Nodes (1): add whatsapp channel support  Revision ID: add_whatsapp_channel_support Revises:

### Community 123 - "Community 123"
Cohesion: 0.5
Nodes (1): Add phase tracking to agent/user onboarding.  Revision ID: add_onboarding_phase

### Community 124 - "Community 124"
Cohesion: 0.5
Nodes (1): Add chat_sessions table and update existing chat_messages conversation_ids.

### Community 125 - "Community 125"
Cohesion: 0.5
Nodes (1): Add relationship access metadata.  Revision ID: add_relationship_access_metadata

### Community 126 - "Community 126"
Cohesion: 0.5
Nodes (1): add llm temperature  Revision ID: add_llm_temperature Revises:  Create Date: 202

### Community 127 - "Community 127"
Cohesion: 0.5
Nodes (1): Add agent token usage and context fields to agents table.  Revision ID: add_agen

### Community 128 - "Community 128"
Cohesion: 0.5
Nodes (1): Add api_key_hash column to users table for user-level API key support.  Revision

### Community 129 - "Community 129"
Cohesion: 0.5
Nodes (1): merge: merge main and feature heads  Revision ID: eba6ac4d8a55 Revises: 6c3ec1ce

### Community 130 - "Community 130"
Cohesion: 0.5
Nodes (1): merge add_onboarding_phase and add_token_cache_usage_fields  Revision ID: 6c3ec1

### Community 131 - "Community 131"
Cohesion: 0.5
Nodes (1): merge remaining alembic heads  Revision ID: 87ff921e8e6f Revises: 5fe287d9d58b,

### Community 132 - "Community 132"
Cohesion: 0.5
Nodes (1): Add Tenant.default_model_id + backfill per-tenant to earliest enabled model.  Re

### Community 133 - "Community 133"
Cohesion: 0.5
Nodes (1): Add source and installed_by_agent_id to agent_tools  Revision ID: add_agent_tool

### Community 134 - "Community 134"
Cohesion: 0.5
Nodes (1): Add default_mcp_servers to agent templates.  Revision ID: add_default_mcp_server

### Community 135 - "Community 135"
Cohesion: 0.5
Nodes (1): Add usage quota fields to users, agents, and tenants tables.  Idempotent — uses

### Community 136 - "Community 136"
Cohesion: 0.5
Nodes (1): Multi-tenant registration: add tenant_id to invitation_codes, delete historical

### Community 137 - "Community 137"
Cohesion: 0.5
Nodes (1): Add name_translit fields to OrgMember  Revision ID: be48e94fa052 Revises: add_da

### Community 138 - "Community 138"
Cohesion: 0.5
Nodes (1): merge heads  Revision ID: 45681b72317e Revises: 29f3f8de3ca0, f1a2b3c4d5e6 Creat

### Community 139 - "Community 139"
Cohesion: 0.5
Nodes (1): Add agent_triggers table for Pulse engine.  Revision ID: add_agent_triggers

### Community 140 - "Community 140"
Cohesion: 0.5
Nodes (1): add ide_plugin_configs  Revision ID: add_ide_plugin_configs Revises: user_refact

### Community 141 - "Community 141"
Cohesion: 0.5
Nodes (1): Add explicit agent access policy fields.  Revision ID: add_agent_access_policy R

### Community 142 - "Community 142"
Cohesion: 0.5
Nodes (1): Add wechat to channel_type_enum.  Revision ID: add_wechat_channel_support Revise

### Community 143 - "Community 143"
Cohesion: 0.5
Nodes (1): Add a2a_async_enabled column to tenants table.  Revision ID: f1a2b3c4d5e6 Revise

### Community 144 - "Community 144"
Cohesion: 0.5
Nodes (1): Add tenant_id to llm_models table for per-company model pools.  Revision ID: add

### Community 145 - "Community 145"
Cohesion: 0.5
Nodes (1): Add consolidated OKR reporting and scheduling schema updates.  Revision ID: add_

### Community 146 - "Community 146"
Cohesion: 0.5
Nodes (1): User system refactor - unified migration.  Revision ID: user_refactor_v1 Revises

### Community 147 - "Community 147"
Cohesion: 0.5
Nodes (1): Align user_tenant_onboardings varchar columns with migration server_default.  Re

### Community 148 - "Community 148"
Cohesion: 0.5
Nodes (1): add_group_chat_fields_to_chat_sessions  Add is_group and group_name columns to c

### Community 149 - "Community 149"
Cohesion: 0.5
Nodes (1): merge heads  Revision ID: fd6e34661d12 Revises: 25811072c8fd, increase_api_key_l

### Community 150 - "Community 150"
Cohesion: 0.5
Nodes (1): Per-(user, agent) onboarding junction table + drop legacy bootstrapped flag.  Re

### Community 151 - "Community 151"
Cohesion: 0.5
Nodes (1): Add tenant_id to skills table for per-company skill scoping.  Revision ID: add_s

### Community 152 - "Community 152"
Cohesion: 0.5
Nodes (1): add entrypoint missing columns  Revision ID: df3da9cf3b27 Revises: multi_tenant_

### Community 153 - "Community 153"
Cohesion: 0.5
Nodes (1): add llm request_timeout  Revision ID: d9cbd43b62e5 Revises: 440261f5594f Create

### Community 154 - "Community 154"
Cohesion: 0.5
Nodes (1): Add Microsoft Teams support to im_provider and channel_type enums.

### Community 155 - "Community 155"
Cohesion: 0.5
Nodes (1): Add structured agent focus items.  Revision ID: add_agent_focus_items Revises: w

### Community 156 - "Community 156"
Cohesion: 0.5
Nodes (1): Merge remaining release heads after PR #494.  Revision ID: merge_pr494_heads Rev

### Community 157 - "Community 157"
Cohesion: 0.5
Nodes (1): Add participants table, extend chat_sessions and chat_messages, migrate messages

### Community 158 - "Community 158"
Cohesion: 0.5
Nodes (1): Refactor user system to global Identities - Phase 2 (Consolidated & Idempotent)

### Community 159 - "Community 159"
Cohesion: 0.5
Nodes (1): add published_pages table  Revision ID: add_published_pages Revises: df3da9cf3b2

### Community 160 - "Community 160"
Cohesion: 0.5
Nodes (1): Add invitation_codes table.  This is an idempotent migration — uses CREATE TABLE

### Community 161 - "Community 161"
Cohesion: 0.5
Nodes (1): Merge okr_agent_id migration and increase_api_key_length migration heads.  Revis

### Community 162 - "Community 162"
Cohesion: 0.5
Nodes (1): Add OKR system tables.  Creates six tables for the OKR feature:   okr_objectives

### Community 163 - "Community 163"
Cohesion: 0.5
Nodes (1): Add primary first-party chat sessions and per-session read tracking.  Revision I

### Community 164 - "Community 164"
Cohesion: 0.5
Nodes (1): Rename web identity provider display name to Platform.  Revision ID: web_provide

### Community 165 - "Community 165"
Cohesion: 0.5
Nodes (1): Add okr_agent_id to okr_settings; add unique partial index on system agents.  Tw

### Community 166 - "Community 166"
Cohesion: 0.5
Nodes (1): merge add_ide_plugin_configs head  Revision ID: 482391030754 Revises: 87ff921e8e

### Community 167 - "Community 167"
Cohesion: 0.5
Nodes (1): Add sso_login_enabled to identity_providers  Revision ID: add_sso_login_enabled

### Community 168 - "Community 168"
Cohesion: 0.5
Nodes (1): Add agentbay and atlassian to channel_type_enum.  Revision ID: add_agentbay_enum

### Community 169 - "Community 169"
Cohesion: 0.5
Nodes (1): Add agent_id and sender_name to notifications table.  Revision ID: add_notificat

### Community 170 - "Community 170"
Cohesion: 0.5
Nodes (1): Add bootstrap_content + capability_bullets to agent templates.  Revision ID: add

### Community 171 - "Community 171"
Cohesion: 0.5
Nodes (1): Set default agent quotas to permanent TTL and higher daily LLM calls.  Revision

### Community 172 - "Community 172"
Cohesion: 0.5
Nodes (1): Add user/company onboarding state.  Revision ID: add_user_tenant_onboarding Revi

### Community 173 - "Community 173"
Cohesion: 0.67
Nodes (2): Run pytest tests in the specified directory, run_tests()

### Community 174 - "Community 174"
Cohesion: 0.67
Nodes (2): check_logs(), Check if the new logging is working correctly.

### Community 175 - "Community 175"
Cohesion: 0.67
Nodes (0): 

### Community 176 - "Community 176"
Cohesion: 0.67
Nodes (1): HTML to PDF conversion service.

### Community 177 - "Community 177"
Cohesion: 0.67
Nodes (1): Editable PPTX rendering implementation for HTML inputs.

### Community 178 - "Community 178"
Cohesion: 0.67
Nodes (1): HTML to PPTX conversion service.

### Community 179 - "Community 179"
Cohesion: 0.67
Nodes (0): 

### Community 180 - "Community 180"
Cohesion: 0.67
Nodes (0): 

### Community 181 - "Community 181"
Cohesion: 0.67
Nodes (1): Unit tests for LSP4J pre-finish chat/answer sync planner.

### Community 182 - "Community 182"
Cohesion: 1.0
Nodes (0): 

### Community 183 - "Community 183"
Cohesion: 1.0
Nodes (0): 

### Community 184 - "Community 184"
Cohesion: 1.0
Nodes (0): 

### Community 185 - "Community 185"
Cohesion: 1.0
Nodes (0): 

### Community 186 - "Community 186"
Cohesion: 1.0
Nodes (0): 

### Community 187 - "Community 187"
Cohesion: 1.0
Nodes (1): MCP (Model Context Protocol) Client — connects to external MCP servers.  Support

### Community 188 - "Community 188"
Cohesion: 1.0
Nodes (1): Connect to SSE endpoint (GET /sse) and extract the messages URL.          Return

### Community 189 - "Community 189"
Cohesion: 1.0
Nodes (0): 

### Community 190 - "Community 190"
Cohesion: 1.0
Nodes (0): 

### Community 191 - "Community 191"
Cohesion: 1.0
Nodes (0): 

### Community 192 - "Community 192"
Cohesion: 1.0
Nodes (0): 

### Community 193 - "Community 193"
Cohesion: 1.0
Nodes (0): 

### Community 194 - "Community 194"
Cohesion: 1.0
Nodes (0): 

### Community 195 - "Community 195"
Cohesion: 1.0
Nodes (0): 

### Community 196 - "Community 196"
Cohesion: 1.0
Nodes (0): 

### Community 197 - "Community 197"
Cohesion: 1.0
Nodes (0): 

### Community 198 - "Community 198"
Cohesion: 1.0
Nodes (0): 

### Community 199 - "Community 199"
Cohesion: 1.0
Nodes (1): 向 FastAPI app 注册路由、启动钩子等。

### Community 200 - "Community 200"
Cohesion: 1.0
Nodes (1): Command name (e.g., "about").

### Community 201 - "Community 201"
Cohesion: 1.0
Nodes (1): Brief description for help output.

### Community 202 - "Community 202"
Cohesion: 1.0
Nodes (1): Alternative names for this command.

### Community 203 - "Community 203"
Cohesion: 1.0
Nodes (1): Nested subcommands (for "extensions list" style).

### Community 204 - "Community 204"
Cohesion: 1.0
Nodes (1): Execute the command with given arguments.

### Community 205 - "Community 205"
Cohesion: 1.0
Nodes (1): 计算两段内容之间的 DiffInfo（行级 + 字符级）。          纯计算函数，不访问共享状态，可在锁内外安全调用。

### Community 206 - "Community 206"
Cohesion: 1.0
Nodes (1): 将 JSON-RPC 消息格式化为 LSP Base Protocol 文本帧。          注意：Content-Length 必须是 UTF-8 编码

### Community 207 - "Community 207"
Cohesion: 1.0
Nodes (1): 从 header 块中解析 Content-Length 值。          Args:             header_block: header

### Community 208 - "Community 208"
Cohesion: 1.0
Nodes (0): 

### Community 209 - "Community 209"
Cohesion: 1.0
Nodes (1): Send a completion request and return the full response.

### Community 210 - "Community 210"
Cohesion: 1.0
Nodes (1): Send a streaming request and return the aggregated response.          Implementa

### Community 211 - "Community 211"
Cohesion: 1.0
Nodes (1): 从 dict 构建 SandboxConfig，支持字段级 fallback。          Args:             config: 工具配置

### Community 212 - "Community 212"
Cohesion: 1.0
Nodes (1): Backend name for identification.

### Community 213 - "Community 213"
Cohesion: 1.0
Nodes (1): Backend name for identification.

### Community 214 - "Community 214"
Cohesion: 1.0
Nodes (1): Execute code in the sandbox.

### Community 215 - "Community 215"
Cohesion: 1.0
Nodes (1): Check if the sandbox backend is healthy.

### Community 216 - "Community 216"
Cohesion: 1.0
Nodes (1): Get the capabilities of this sandbox backend.

### Community 217 - "Community 217"
Cohesion: 1.0
Nodes (1): Enqueue a new permission request for later approval.

### Community 218 - "Community 218"
Cohesion: 1.0
Nodes (1): Get all pending permission requests for a session.

### Community 219 - "Community 219"
Cohesion: 1.0
Nodes (1): Get a specific pending permission request by ID.

### Community 220 - "Community 220"
Cohesion: 1.0
Nodes (1): Process a permission decision (grant/deny).

### Community 221 - "Community 221"
Cohesion: 1.0
Nodes (1): Wait for a decision on a permission request.          Returns True if granted, F

### Community 222 - "Community 222"
Cohesion: 1.0
Nodes (1): Clear all pending requests for a session.

### Community 223 - "Community 223"
Cohesion: 1.0
Nodes (1): Count pending requests for a session.

### Community 224 - "Community 224"
Cohesion: 1.0
Nodes (1): Read content from a text file.

### Community 225 - "Community 225"
Cohesion: 1.0
Nodes (1): Write content to a text file.

### Community 226 - "Community 226"
Cohesion: 1.0
Nodes (0): 

## Knowledge Gaps
- **463 isolated node(s):** `Run pytest tests in the specified directory`, `LSP4J 插件端到端测试脚本。  模拟通义灵码 IDE 插件的 WebSocket 连接行为， 验证 Clawith LSP4J 后端的完整功能链路。  使用`, `格式化 LSP Base Protocol 消息`, `Check if the new logging is working correctly.`, `O3: Log IDE-provided MCP servers so we know what's available (not yet forwarded` (+458 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 182`** (2 nodes): `test_async.py`, `test_manager()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 183`** (2 nodes): `PermissionModal.tsx`, `computeDiff()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 184`** (2 nodes): `update_schema.py`, `main()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 185`** (2 nodes): `remove_old_tool.py`, `run()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 186`** (2 nodes): `ws-protocol.test.js`, `runTests()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 187`** (2 nodes): `mcp_client.py`, `MCP (Model Context Protocol) Client — connects to external MCP servers.  Support`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 188`** (2 nodes): `._sse_connect()`, `Connect to SSE endpoint (GET /sse) and extract the messages URL.          Return`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 189`** (1 nodes): `check_circular_imports.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 190`** (1 nodes): `verify_superpowers_tests.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 191`** (1 nodes): `merge_graphify.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 192`** (1 nodes): `vite.config.ts`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 193`** (1 nodes): `vite-env.d.ts`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 194`** (1 nodes): `qrcode.d.ts`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 195`** (1 nodes): `setup-win.ps1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 196`** (1 nodes): `run-win.ps1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 197`** (1 nodes): `test_import.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 198`** (1 nodes): `test_adapter_direct.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 199`** (1 nodes): `向 FastAPI app 注册路由、启动钩子等。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 200`** (1 nodes): `Command name (e.g., "about").`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 201`** (1 nodes): `Brief description for help output.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 202`** (1 nodes): `Alternative names for this command.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 203`** (1 nodes): `Nested subcommands (for "extensions list" style).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 204`** (1 nodes): `Execute the command with given arguments.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 205`** (1 nodes): `计算两段内容之间的 DiffInfo（行级 + 字符级）。          纯计算函数，不访问共享状态，可在锁内外安全调用。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 206`** (1 nodes): `将 JSON-RPC 消息格式化为 LSP Base Protocol 文本帧。          注意：Content-Length 必须是 UTF-8 编码`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 207`** (1 nodes): `从 header 块中解析 Content-Length 值。          Args:             header_block: header`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 208`** (1 nodes): `scripts____init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 209`** (1 nodes): `Send a completion request and return the full response.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 210`** (1 nodes): `Send a streaming request and return the aggregated response.          Implementa`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 211`** (1 nodes): `从 dict 构建 SandboxConfig，支持字段级 fallback。          Args:             config: 工具配置`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 212`** (1 nodes): `Backend name for identification.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 213`** (1 nodes): `Backend name for identification.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 214`** (1 nodes): `Execute code in the sandbox.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 215`** (1 nodes): `Check if the sandbox backend is healthy.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 216`** (1 nodes): `Get the capabilities of this sandbox backend.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 217`** (1 nodes): `Enqueue a new permission request for later approval.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 218`** (1 nodes): `Get all pending permission requests for a session.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 219`** (1 nodes): `Get a specific pending permission request by ID.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 220`** (1 nodes): `Process a permission decision (grant/deny).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 221`** (1 nodes): `Wait for a decision on a permission request.          Returns True if granted, F`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 222`** (1 nodes): `Clear all pending requests for a session.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 223`** (1 nodes): `Count pending requests for a session.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 224`** (1 nodes): `Read content from a text file.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 225`** (1 nodes): `Write content to a text file.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 226`** (1 nodes): `merge_semantic.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `User` connect `Community 0` to `Community 1`, `Community 2`, `Community 4`, `Community 5`, `Community 6`, `Community 8`, `Community 11`, `Community 13`, `Community 14`, `Community 17`, `Community 20`, `Community 22`, `Community 25`, `Community 27`, `Community 28`, `Community 30`, `Community 32`, `Community 33`, `Community 44`, `Community 46`, `Community 47`, `Community 50`, `Community 54`, `Community 61`, `Community 65`, `Community 67`, `Community 86`, `Community 93`, `Community 102`?**
  _High betweenness centrality (0.217) - this node is a cross-community bridge._
- **Why does `Agent` connect `Community 1` to `Community 0`, `Community 2`, `Community 4`, `Community 5`, `Community 6`, `Community 8`, `Community 11`, `Community 14`, `Community 17`, `Community 20`, `Community 25`, `Community 27`, `Community 30`, `Community 32`, `Community 33`, `Community 34`, `Community 44`, `Community 46`, `Community 48`, `Community 50`, `Community 54`, `Community 61`, `Community 65`, `Community 69`, `Community 73`, `Community 79`, `Community 80`, `Community 102`?**
  _High betweenness centrality (0.146) - this node is a cross-community bridge._
- **Why does `ChannelConfig` connect `Community 1` to `Community 0`, `Community 2`, `Community 67`, `Community 4`, `Community 34`, `Community 35`, `Community 8`, `Community 12`, `Community 46`, `Community 47`, `Community 48`, `Community 80`, `Community 50`, `Community 21`, `Community 54`?**
  _High betweenness centrality (0.051) - this node is a cross-community bridge._
- **Are the 1120 inferred relationships involving `User` (e.g. with `Seed data script — creates initial admin user and built-in templates.` and `Create tables and seed initial data.`) actually correct?**
  _`User` has 1120 INFERRED edges - model-reasoned connections that need verification._
- **Are the 914 inferred relationships involving `Agent` (e.g. with `Seed data script — creates initial admin user and built-in templates.` and `Create tables and seed initial data.`) actually correct?**
  _`Agent` has 914 INFERRED edges - model-reasoned connections that need verification._
- **Are the 501 inferred relationships involving `IdentityProvider` (e.g. with `Base` and `Backfill department paths from the department tree and refresh member paths.  Us`) actually correct?**
  _`IdentityProvider` has 501 INFERRED edges - model-reasoned connections that need verification._
- **Are the 497 inferred relationships involving `ChatMessage` (e.g. with `LSP4J WebSocket 端点 + 认证。  提供 WebSocket 端点供通义灵码 IDE 插件连接。 URL 格式：ws://{host}/api/` and `Outbound message with schema version for forward-compatible clients.`) actually correct?**
  _`ChatMessage` has 497 INFERRED edges - model-reasoned connections that need verification._
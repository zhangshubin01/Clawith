# Graph Report - /Users/shubinzhang/.claude/rules  (2026-04-22)

## Corpus Check
- Large corpus: 499 files · ~779,971 words. Semantic extraction will be expensive (many Claude tokens). Consider running on a subfolder, or use --no-semantic to run AST-only.

## Summary
- 3917 nodes · 18686 edges · 81 communities detected
- Extraction: 27% EXTRACTED · 73% INFERRED · 0% AMBIGUOUS · INFERRED: 13610 edges (avg confidence: 0.58)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 84|Community 84]]
- [[_COMMUNITY_Community 85|Community 85]]
- [[_COMMUNITY_Community 86|Community 86]]
- [[_COMMUNITY_Community 87|Community 87]]
- [[_COMMUNITY_Community 88|Community 88]]
- [[_COMMUNITY_Community 89|Community 89]]
- [[_COMMUNITY_Community 97|Community 97]]
- [[_COMMUNITY_Community 98|Community 98]]
- [[_COMMUNITY_Community 99|Community 99]]
- [[_COMMUNITY_Community 100|Community 100]]
- [[_COMMUNITY_Community 101|Community 101]]
- [[_COMMUNITY_Community 102|Community 102]]
- [[_COMMUNITY_Community 103|Community 103]]
- [[_COMMUNITY_Community 104|Community 104]]
- [[_COMMUNITY_Community 105|Community 105]]
- [[_COMMUNITY_Community 106|Community 106]]
- [[_COMMUNITY_Community 107|Community 107]]
- [[_COMMUNITY_Community 108|Community 108]]
- [[_COMMUNITY_Community 109|Community 109]]
- [[_COMMUNITY_Community 110|Community 110]]
- [[_COMMUNITY_Community 111|Community 111]]
- [[_COMMUNITY_Community 112|Community 112]]
- [[_COMMUNITY_Community 113|Community 113]]

## God Nodes (most connected - your core abstractions)
1. `User` - 845 edges
2. `Agent` - 605 edges
3. `IdentityProvider` - 395 edges
4. `ChannelConfig` - 350 edges
5. `Tenant` - 330 edges
6. `OrgMember` - 325 edges
7. `SystemSetting` - 316 edges
8. `LLMModel` - 315 edges
9. `ChatMessage` - 298 edges
10. `Identity` - 279 edges

## Surprising Connections (you probably didn't know these)
- `Resolve relative path against session cwd.` --uses--> `AgentSideConnection`  [INFERRED]
  clawith_acp/server.py → backend/app/plugins/clawith_acp/connection.py
- `Map cloud `permission_request` to IDE `request_permission` (or env override).` --uses--> `AgentSideConnection`  [INFERRED]
  clawith_acp/server.py → backend/app/plugins/clawith_acp/connection.py
- `run_tests()` --calls--> `run()`  [INFERRED]
  run_tests.py → backend/remove_old_tool.py
- `test_manager()` --calls--> `SkillManager`  [INFERRED]
  test_async.py → backend/app/plugins/clawith_superpowers/skill_manager.py
- `fetchAuth()` --calls--> `fetch()`  [INFERRED]
  frontend/src/components/ChannelConfig.tsx → backend/app/plugins/clawith_superpowers/data/superpowers/tests/brainstorm-server/server.test.js

## Communities

### Community 0 - "Community 0"
Cohesion: 0.01
Nodes (504): Refactor user system to global Identities - Phase 2 (Consolidated & Idempotent), upgrade(), get_agent_activity(), get_conversation_messages(), list_conversations(), log_activity(), downgrade(), Add a2a_async_enabled column to tenants table.  Revision ID: f1a2b3c4d5e6 Revise (+496 more)

### Community 1 - "Community 1"
Cohesion: 0.04
Nodes (429): Rolled up token consumption per agent per day for time-series analytics., Activity log API — view agent work history., Get messages for a specific conversation., Get recent activity logs for an agent., List all conversation partners for this agent (web users + other agents)., List available agent templates., Get template details., Create a new agent template (share to template market). (+421 more)

### Community 2 - "Community 2"
Cohesion: 0.01
Nodes (308): downgrade(), add open_files column to chat_session  Revision ID: 25811072c8fd Revises: 45681b, upgrade(), _agentbay_browser_click(), _agentbay_browser_extract(), _agentbay_browser_login(), _agentbay_browser_navigate(), _agentbay_browser_observe() (+300 more)

### Community 3 - "Community 3"
Cohesion: 0.03
Nodes (312): DailyTokenUsage, CompanyCreateRequest, CompanyCreateResponse, CompanyStats, create_company(), PlatformSettingsOut, PlatformSettingsUpdate, Platform Admin company management API.  Provides endpoints for platform admins t (+304 more)

### Community 4 - "Community 4"
Cohesion: 0.02
Nodes (149): Agent, AgentSideConnection, ndJson stream connection over WebSocket for ACP.  This module handles the ndJson, ACP Agent-side connection wrapping ndJson stream over WebSocket., Read one line from stdin, parse as JSON., Send one JSON message as a line., Close the connection., RPC: ask IDE to read a text file. (+141 more)

### Community 5 - "Community 5"
Cohesion: 0.01
Nodes (122): fetchJson(), handleCreate(), handleSave(), handleSendTestEmail(), handleToggle(), handleToggleSetting(), loadCompanies(), saveEmailConfig() (+114 more)

### Community 6 - "Community 6"
Cohesion: 0.02
Nodes (127): _agent_workspace(), build_agent_context(), _build_skills_index(), _load_skills_index(), _parse_skill_frontmatter(), _read_file_cached(), _read_file_safe(), call_llm() (+119 more)

### Community 7 - "Community 7"
Cohesion: 0.02
Nodes (101): AboutCommand, about command - display version information about Clawith ACP., AcpCommand, AgentsCommand, AioSandboxBackend, aio-sandbox backend.      Connects to aio-sandbox (https://github.com/agent-infr, Check if aio-sandbox service is available., Execute code using aio-sandbox. (+93 more)

### Community 8 - "Community 8"
Cohesion: 0.03
Nodes (98): extract_config_schema(), extract_skill_metadata(), Extract metadata from Superpowers SKILL.md.      Superpowers skills can have YAM, Convert Superpowers skill to Clawith Skill create/update dict., Extract JSON Schema for configuration from metadata., to_clawith_skill(), ClawithPlugin, ClawithPlugin (+90 more)

### Community 9 - "Community 9"
Cohesion: 0.04
Nodes (122): _agent_base_dir(), agent_import_from_clawhub(), agent_import_from_url(), ClawhubImportBody, delete_enterprise_file(), delete_file(), _enterprise_info_dir(), FileContent (+114 more)

### Community 10 - "Community 10"
Cohesion: 0.14
Nodes (97): authorize(), list_providers(), Authentication API routes., Unlink an external identity from the current user., Verify email address using a token from the verification email.      On success,, Resend email verification link., Legacy registration endpoint - kept for backward compatibility.      For new imp, Step 1: Initialize registration with account credentials.      Creates/finds a g (+89 more)

### Community 11 - "Community 11"
Cohesion: 0.05
Nodes (58): ABC, BaseAuthProvider, DingTalkAuthProvider, FeishuAuthProvider, MicrosoftTeamsAuthProvider, WeComAuthProvider, AuthProviderRegistry, Authentication provider registry and factory.  This module provides a centralize (+50 more)

### Community 12 - "Community 12"
Cohesion: 0.06
Nodes (55): _append_focus_item(), _send_message_to_agent(), _wake_agent_async(), get_me(), _send_discord_followup(), DummyResult, _make_agent(), _make_participant() (+47 more)

### Community 13 - "Community 13"
Cohesion: 0.05
Nodes (59): AuditAction, Standard audit action types., BaseHTTPMiddleware, BaseSettings, Config, _default_agent_data_dir(), _default_agent_template_dir(), _default_log_dir() (+51 more)

### Community 14 - "Community 14"
Cohesion: 0.11
Nodes (49): delegate_task(), DelegateRequest, HandoverRequest, InterAgentMessage, list_collaborators(), Agent collaboration and template market API routes., TemplateCreate, TemplateOut (+41 more)

### Community 15 - "Community 15"
Cohesion: 0.07
Nodes (35): AgentActivityLog, Activity log model for tracking agent actions., Records every action taken by a digital employee., Activity logger — simple async function to record agent actions., Record an agent activity. Fire-and-forget, never raises., _heartbeat_tick(), _is_in_active_hours(), Heartbeat service — proactive agent awareness loop.  Periodically triggers agent (+27 more)

### Community 16 - "Community 16"
Cohesion: 0.09
Nodes (20): FeishuWSManager, _make_no_proxy_connect(), Feishu WebSocket Long Connection Manager., Recursively convert lark-oapi SDK objects to dictionaries for downstream process, Handle im.message.receive_v1 events from Feishu WebSocket asynchronously., Return a drop-in replacement for websockets.connect that forces proxy=None., Return status of all active WS tasks., Manages Feishu WebSocket clients for all agents. (+12 more)

### Community 17 - "Community 17"
Cohesion: 0.1
Nodes (26): FileSystemEventHandler, _agent_headers(), _fire_extract(), _get_client(), index_all_skills(), index_enterprise_info(), index_memory_file(), _invalidate_availability_cache() (+18 more)

### Community 18 - "Community 18"
Cohesion: 0.09
Nodes (12): client(), FakeAsyncSessionFactory, FakeQuery, FakeScalarResult, FakeSession, FakeSkill, QueryField, RaiseOnInstanceAccess (+4 more)

### Community 19 - "Community 19"
Cohesion: 0.1
Nodes (22): generate_html(), main(), Generate HTML report from loop output data. If auto_refresh is True, adds a meta, improve_description(), main(), Call Claude to improve the description based on eval results., find_project_root(), main() (+14 more)

### Community 20 - "Community 20"
Cohesion: 0.12
Nodes (22): Send an email verification code using the configured template., deliver_broadcast_emails(), get_email_templates(), System-owned outbound email service.  Supports both: 1. Platform-level configura, Send email with provided config., Send a password reset email using the configured template.      Args:         to, Send a company invitation email using the configured template.      Args:, Deliver broadcast emails while isolating per-recipient failures. (+14 more)

### Community 21 - "Community 21"
Cohesion: 0.13
Nodes (18): BaseHTTPRequestHandler, build_run(), embed_file(), find_runs(), _find_runs_recursive(), generate_html(), get_mime_type(), _kill_port() (+10 more)

### Community 22 - "Community 22"
Cohesion: 0.11
Nodes (8): BaseOrgSyncAdapter, _DummyAdapter, _FakeDB, _SyncAdapterWithFailure, test_sync_org_structure_skips_reconcile_after_member_failure(), test_validate_member_identifiers_allows_wecom_without_unionid(), test_validate_member_identifiers_rejects_unionid_equal_to_external_id(), test_validate_member_identifiers_requires_unionid_for_feishu()

### Community 23 - "Community 23"
Cohesion: 0.12
Nodes (9): AgentManager, Agent lifecycle manager — Docker container management for OpenClaw Gateway insta, Generate openclaw.json config for the agent container., Stop the agent's Docker container., Manage OpenClaw Gateway Docker containers for digital employees., Stop and remove the agent's Docker container., Archive agent files to a backup location and return the archive directory., Get real-time container status. (+1 more)

### Community 24 - "Community 24"
Cohesion: 0.14
Nodes (10): websocket_endpoint(), cleanup_pending_calls(), Tool call handler for IDEA plugin integration., Send a tool call request to the connected IDE plugin., Wait for the IDEA plugin to return a tool result., Resolve a pending tool call with the result from the IDEA plugin., Clean up all pending tool calls (e.g., when WebSocket disconnects)., resolve_ide_tool_result() (+2 more)

### Community 25 - "Community 25"
Cohesion: 0.26
Nodes (7): DummyResult, make_channel(), make_user(), RecordingDB, test_delete_wecom_channel_stops_runtime_client(), test_get_wecom_channel_marks_webhook_mode_disconnected(), test_get_wecom_channel_reports_runtime_websocket_status()

### Community 26 - "Community 26"
Cohesion: 0.19
Nodes (13): compress_bytes_to_base64(), compress_screenshot_to_base64(), pop_temp_screenshot(), _prune_expired_cache(), Vision injection utilities for AgentBay screenshot tools.  Architecture: "Epheme, Compress raw image bytes to a base64 JPEG data URL.      Resizes to _MAX_WIDTH (, Read a screenshot file, compress it, and return a base64 data URL.      Used onl, Try to extract a screenshot from a tool result and build a vision content array. (+5 more)

### Community 27 - "Community 27"
Cohesion: 0.17
Nodes (7): DingTalkStreamManager, DingTalk Stream Connection Manager.  Manages WebSocket-based Stream connections, Stop a running Stream client for an agent., Return status of all active Stream clients., Manages DingTalk Stream clients for all agents., Start a DingTalk Stream client for a specific agent., Run the DingTalk Stream client in a blocking thread.

### Community 28 - "Community 28"
Cohesion: 0.17
Nodes (11): Basic smoke tests for the clawith_acp plugin. These tests verify that the main m, Test importing the connection module., Test importing the file_system_service module., Test importing the types module., Test importing the errors module., Test importing the router module., test_import_connection(), test_import_errors() (+3 more)

### Community 29 - "Community 29"
Cohesion: 0.24
Nodes (9): _agent_workspace(), _load_skills_index(), _parse_skill_frontmatter(), Build rich system prompt context for agents.  Loads soul, memory, skills summary, Return the canonical persistent workspace path for an agent., Read a file, return empty string if missing. Truncate if too long., Parse YAML frontmatter from a skill .md file.      Returns (name, description)., Load skill index (name + description) from skills/ directory.      Supports two (+1 more)

### Community 30 - "Community 30"
Cohesion: 0.28
Nodes (7): main(), package_skill(), Check if a path should be excluded from packaging., Package a skill folder into a .skill file.      Args:         skill_path: Path t, should_exclude(), Basic validation of a skill, validate_skill()

### Community 32 - "Community 32"
Cohesion: 0.62
Nodes (5): combineGraphs(), extractDotBlocks(), extractGraphBody(), main(), renderToSvg()

### Community 33 - "Community 33"
Cohesion: 0.4
Nodes (5): compress_image_if_needed(), process_ide_image(), Vision handler for IDEA plugin integration., Compress image if it exceeds the size threshold.          Args:         base64_d, Process Base64 image data from IDEA plugin.          Args:         base64_data:

### Community 34 - "Community 34"
Cohesion: 0.4
Nodes (5): extract_text(), File upload API for chat — saves files to agent workspace and extracts text., Upload a file for chat context. Saves to agent workspace/uploads/ and returns ex, Extract text content from a file., upload_file()

### Community 35 - "Community 35"
Cohesion: 0.4
Nodes (5): get_skill_creator_files(), _load_file(), Content for the skill-creator builtin skill.  Based on: https://github.com/anthr, Return list of {path, content} for all skill-creator files., Load a file from the skill_creator_files directory.

### Community 36 - "Community 36"
Cohesion: 0.5
Nodes (2): calcHalfContainerWidth(), onResize()

### Community 37 - "Community 37"
Cohesion: 0.6
Nodes (3): waitForEvent(), waitForEventCount(), waitForEventMatch()

### Community 39 - "Community 39"
Cohesion: 0.5
Nodes (2): copyToClipboard(), handleCopy()

### Community 40 - "Community 40"
Cohesion: 0.83
Nodes (3): escapeHtml(), markdownToHtml(), renderInline()

### Community 42 - "Community 42"
Cohesion: 0.5
Nodes (3): get_db(), Database connection and session management., Dependency for getting async database sessions.

### Community 43 - "Community 43"
Cohesion: 0.5
Nodes (3): extract_code_diffs(), Diff handler for IDEA plugin integration., Extract code blocks with file paths from LLM response.          Matches patterns

### Community 44 - "Community 44"
Cohesion: 0.67
Nodes (2): connect(), sendEvent()

### Community 45 - "Community 45"
Cohesion: 0.5
Nodes (2): Add source to tools and backfill data  Revision ID: add_tool_source Revises: add, upgrade()

### Community 46 - "Community 46"
Cohesion: 0.5
Nodes (2): Unified column fix for missing fields across main tables.  Revision ID: 20260313, upgrade()

### Community 47 - "Community 47"
Cohesion: 0.5
Nodes (2): Add agent token usage and context fields to agents table.  Revision ID: add_agen, upgrade()

### Community 48 - "Community 48"
Cohesion: 0.5
Nodes (2): Add source and installed_by_agent_id to agent_tools  Revision ID: add_agent_tool, upgrade()

### Community 49 - "Community 49"
Cohesion: 0.5
Nodes (2): Add usage quota fields to users, agents, and tenants tables.  Idempotent — uses, upgrade()

### Community 50 - "Community 50"
Cohesion: 0.5
Nodes (1): merge heads  Revision ID: 45681b72317e Revises: 29f3f8de3ca0, f1a2b3c4d5e6 Creat

### Community 51 - "Community 51"
Cohesion: 0.5
Nodes (2): Add agent_triggers table for Pulse engine.  Revision ID: add_agent_triggers, upgrade()

### Community 52 - "Community 52"
Cohesion: 0.5
Nodes (1): add_group_chat_fields_to_chat_sessions  Add is_group and group_name columns to c

### Community 53 - "Community 53"
Cohesion: 0.5
Nodes (1): merge heads  Revision ID: fd6e34661d12 Revises: 25811072c8fd, increase_api_key_l

### Community 54 - "Community 54"
Cohesion: 0.5
Nodes (2): add entrypoint missing columns  Revision ID: df3da9cf3b27 Revises: multi_tenant_, upgrade()

### Community 55 - "Community 55"
Cohesion: 0.5
Nodes (1): add llm request_timeout  Revision ID: d9cbd43b62e5 Revises: 440261f5594f Create

### Community 56 - "Community 56"
Cohesion: 0.5
Nodes (2): Add Microsoft Teams support to im_provider and channel_type enums., upgrade()

### Community 57 - "Community 57"
Cohesion: 0.5
Nodes (2): downgrade(), Add IDE plugin fields to chat_sessions  Revision ID: 29f3f8de3ca0 Revises: add_u

### Community 58 - "Community 58"
Cohesion: 0.5
Nodes (2): Add sso_login_enabled to identity_providers  Revision ID: add_sso_login_enabled, upgrade()

### Community 59 - "Community 59"
Cohesion: 0.5
Nodes (2): Add agentbay and atlassian to channel_type_enum.  Revision ID: add_agentbay_enum, upgrade()

### Community 60 - "Community 60"
Cohesion: 0.67
Nodes (2): check_logs(), Check if the new logging is working correctly.

### Community 84 - "Community 84"
Cohesion: 1.0
Nodes (1): 向 FastAPI app 注册路由、启动钩子等。

### Community 85 - "Community 85"
Cohesion: 1.0
Nodes (1): Command name (e.g., "about").

### Community 86 - "Community 86"
Cohesion: 1.0
Nodes (1): Brief description for help output.

### Community 87 - "Community 87"
Cohesion: 1.0
Nodes (1): Alternative names for this command.

### Community 88 - "Community 88"
Cohesion: 1.0
Nodes (1): Nested subcommands (for "extensions list" style).

### Community 89 - "Community 89"
Cohesion: 1.0
Nodes (1): Execute the command with given arguments.

### Community 97 - "Community 97"
Cohesion: 1.0
Nodes (1): Send a completion request and return the full response.

### Community 98 - "Community 98"
Cohesion: 1.0
Nodes (1): Send a streaming request and return the aggregated response.          Implementa

### Community 99 - "Community 99"
Cohesion: 1.0
Nodes (1): 从 dict 构建 SandboxConfig，支持字段级 fallback。          Args:             config: 工具配置

### Community 100 - "Community 100"
Cohesion: 1.0
Nodes (1): Backend name for identification.

### Community 101 - "Community 101"
Cohesion: 1.0
Nodes (1): Backend name for identification.

### Community 102 - "Community 102"
Cohesion: 1.0
Nodes (1): Execute code in the sandbox.

### Community 103 - "Community 103"
Cohesion: 1.0
Nodes (1): Check if the sandbox backend is healthy.

### Community 104 - "Community 104"
Cohesion: 1.0
Nodes (1): Get the capabilities of this sandbox backend.

### Community 105 - "Community 105"
Cohesion: 1.0
Nodes (1): Enqueue a new permission request for later approval.

### Community 106 - "Community 106"
Cohesion: 1.0
Nodes (1): Get all pending permission requests for a session.

### Community 107 - "Community 107"
Cohesion: 1.0
Nodes (1): Get a specific pending permission request by ID.

### Community 108 - "Community 108"
Cohesion: 1.0
Nodes (1): Process a permission decision (grant/deny).

### Community 109 - "Community 109"
Cohesion: 1.0
Nodes (1): Wait for a decision on a permission request.          Returns True if granted, F

### Community 110 - "Community 110"
Cohesion: 1.0
Nodes (1): Clear all pending requests for a session.

### Community 111 - "Community 111"
Cohesion: 1.0
Nodes (1): Count pending requests for a session.

### Community 112 - "Community 112"
Cohesion: 1.0
Nodes (1): Read content from a text file.

### Community 113 - "Community 113"
Cohesion: 1.0
Nodes (1): Write content to a text file.

## Knowledge Gaps
- **339 isolated node(s):** `Run pytest tests in the specified directory`, `Check if the new logging is working correctly.`, `O3: Log IDE-provided MCP servers so we know what's available (not yet forwarded`, `Database connection and session management.`, `SQLAlchemy declarative base.` (+334 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 36`** (5 nodes): `calcHalfContainerWidth()`, `onMouseMove()`, `onMouseUp()`, `onResize()`, `AgentBayLivePanel.tsx`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 39`** (4 nodes): `copyToClipboard()`, `LinearCopyButton.tsx`, `clipboard.ts`, `handleCopy()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 44`** (4 nodes): `helper.js`, `helper.js`, `connect()`, `sendEvent()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 45`** (4 nodes): `downgrade()`, `Add source to tools and backfill data  Revision ID: add_tool_source Revises: add`, `upgrade()`, `add_tool_source.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 46`** (4 nodes): `downgrade()`, `Unified column fix for missing fields across main tables.  Revision ID: 20260313`, `upgrade()`, `20260313_column_modify.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 47`** (4 nodes): `downgrade()`, `Add agent token usage and context fields to agents table.  Revision ID: add_agen`, `upgrade()`, `add_agent_usage_fields.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 48`** (4 nodes): `downgrade()`, `Add source and installed_by_agent_id to agent_tools  Revision ID: add_agent_tool`, `upgrade()`, `add_agent_tool_source.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 49`** (4 nodes): `downgrade()`, `Add usage quota fields to users, agents, and tenants tables.  Idempotent — uses`, `upgrade()`, `add_quota_fields.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 50`** (4 nodes): `downgrade()`, `merge heads  Revision ID: 45681b72317e Revises: 29f3f8de3ca0, f1a2b3c4d5e6 Creat`, `upgrade()`, `45681b72317e_merge_heads.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 51`** (4 nodes): `downgrade()`, `Add agent_triggers table for Pulse engine.  Revision ID: add_agent_triggers`, `upgrade()`, `add_agent_triggers.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 52`** (4 nodes): `downgrade()`, `add_group_chat_fields_to_chat_sessions  Add is_group and group_name columns to c`, `upgrade()`, `a1b2c3d4e5f6_add_group_chat_fields.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 53`** (4 nodes): `fd6e34661d12_merge_heads.py`, `downgrade()`, `merge heads  Revision ID: fd6e34661d12 Revises: 25811072c8fd, increase_api_key_l`, `upgrade()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 54`** (4 nodes): `df3da9cf3b27_add_entrypoint_missing_columns.py`, `downgrade()`, `add entrypoint missing columns  Revision ID: df3da9cf3b27 Revises: multi_tenant_`, `upgrade()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 55`** (4 nodes): `d9cbd43b62e5_add_llm_request_timeout.py`, `downgrade()`, `add llm request_timeout  Revision ID: d9cbd43b62e5 Revises: 440261f5594f Create`, `upgrade()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 56`** (4 nodes): `downgrade()`, `Add Microsoft Teams support to im_provider and channel_type enums.`, `upgrade()`, `add_microsoft_teams_support.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 57`** (4 nodes): `downgrade()`, `Add IDE plugin fields to chat_sessions  Revision ID: 29f3f8de3ca0 Revises: add_u`, `upgrade()`, `29f3f8de3ca0_add_ide_plugin_fields_to_chat_sessions.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 58`** (4 nodes): `downgrade()`, `Add sso_login_enabled to identity_providers  Revision ID: add_sso_login_enabled`, `upgrade()`, `add_sso_login_enabled.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 59`** (4 nodes): `downgrade()`, `Add agentbay and atlassian to channel_type_enum.  Revision ID: add_agentbay_enum`, `upgrade()`, `add_agentbay_enum_value.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 60`** (3 nodes): `check_logs()`, `test_acp_deferred_logging.py`, `Check if the new logging is working correctly.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 84`** (1 nodes): `向 FastAPI app 注册路由、启动钩子等。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 85`** (1 nodes): `Command name (e.g., "about").`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 86`** (1 nodes): `Brief description for help output.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 87`** (1 nodes): `Alternative names for this command.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 88`** (1 nodes): `Nested subcommands (for "extensions list" style).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 89`** (1 nodes): `Execute the command with given arguments.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 97`** (1 nodes): `Send a completion request and return the full response.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 98`** (1 nodes): `Send a streaming request and return the aggregated response.          Implementa`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 99`** (1 nodes): `从 dict 构建 SandboxConfig，支持字段级 fallback。          Args:             config: 工具配置`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 100`** (1 nodes): `Backend name for identification.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 101`** (1 nodes): `Backend name for identification.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 102`** (1 nodes): `Execute code in the sandbox.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 103`** (1 nodes): `Check if the sandbox backend is healthy.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 104`** (1 nodes): `Get the capabilities of this sandbox backend.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 105`** (1 nodes): `Enqueue a new permission request for later approval.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 106`** (1 nodes): `Get all pending permission requests for a session.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 107`** (1 nodes): `Get a specific pending permission request by ID.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 108`** (1 nodes): `Process a permission decision (grant/deny).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 109`** (1 nodes): `Wait for a decision on a permission request.          Returns True if granted, F`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 110`** (1 nodes): `Clear all pending requests for a session.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 111`** (1 nodes): `Count pending requests for a session.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 112`** (1 nodes): `Read content from a text file.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 113`** (1 nodes): `Write content to a text file.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `User` connect `Community 3` to `Community 0`, `Community 1`, `Community 34`, `Community 2`, `Community 4`, `Community 6`, `Community 7`, `Community 8`, `Community 9`, `Community 10`, `Community 11`, `Community 14`, `Community 15`, `Community 23`, `Community 25`?**
  _High betweenness centrality (0.135) - this node is a cross-community bridge._
- **Why does `Agent` connect `Community 1` to `Community 0`, `Community 3`, `Community 4`, `Community 6`, `Community 7`, `Community 8`, `Community 9`, `Community 10`, `Community 11`, `Community 14`, `Community 15`, `Community 16`, `Community 23`?**
  _High betweenness centrality (0.087) - this node is a cross-community bridge._
- **Why does `ChannelConfig` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 11`, `Community 14`, `Community 16`, `Community 25`, `Community 27`?**
  _High betweenness centrality (0.037) - this node is a cross-community bridge._
- **Are the 842 inferred relationships involving `User` (e.g. with `Seed data script — creates initial admin user and built-in templates.` and `Create tables and seed initial data.`) actually correct?**
  _`User` has 842 INFERRED edges - model-reasoned connections that need verification._
- **Are the 602 inferred relationships involving `Agent` (e.g. with `Seed data script — creates initial admin user and built-in templates.` and `Create tables and seed initial data.`) actually correct?**
  _`Agent` has 602 INFERRED edges - model-reasoned connections that need verification._
- **Are the 396 inferred relationships involving `str` (e.g. with `run_tests()` and `.__init__()`) actually correct?**
  _`str` has 396 INFERRED edges - model-reasoned connections that need verification._
- **Are the 392 inferred relationships involving `IdentityProvider` (e.g. with `Base` and `Create a new SSO scan session for QR code login.`) actually correct?**
  _`IdentityProvider` has 392 INFERRED edges - model-reasoned connections that need verification._
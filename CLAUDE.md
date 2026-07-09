# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Clawith is an open-source multi-agent collaboration platform — a "digital employee" system where AI agents have persistent identity (`soul.md`), long-term memory (`memory.md`), autonomous awareness (cron/interval/webhook triggers), and can communicate with each other (A2A) and with humans via omni-channel integrations (Feishu, DingTalk, WeCom, Slack, Discord).

## Agent Instructions

Per `AGENTS.md`, canonical project rules live under `.agents/`. Read in this order at the start of work:

1. `.agents/workflows/read_architecture.md` (architecture overview)
2. `.agents/rules/design_and_dev.md` — for feature/implementation work
3. `.agents/rules/deploy.md` — for deployment/environment changes
4. `.agents/rules/github.md` — for GitHub-related work
5. `.agents/rules/release.md` — for versioning/release work

The architecture reference document is `ARCHITECTURE_SPEC_EN.md`.

## Commands

### Backend (Python / FastAPI)

```bash
cd backend

# Install dependencies
pip install -e ".[dev]"

# Run dev server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Run all tests
pytest

# Run a single test file
pytest tests/test_auth.py -v

# Run a single test
pytest tests/test_auth.py::test_login -v

# Lint
ruff check .
ruff format .

# Database migrations
alembic upgrade head
alembic revision --autogenerate -m "description"
```

### Frontend (React / TypeScript / Vite)

```bash
cd frontend

# Install dependencies
npm install

# Dev server (http://localhost:5173)
npm run dev

# Type-check + build
npm run build

# Preview production build
npm run preview
```

### Full Stack (Docker Compose)

```bash
# One-command setup (creates .env, PostgreSQL, installs deps)
bash setup.sh

# Start all services → http://localhost:3008
bash restart.sh

# Deploy to dev server (192.168.106.163, port 3009)
# See .agents/workflows/deploy-dev.md for full steps
```

## Architecture

### Monorepo Layout

- `backend/` — Python 3.11+ FastAPI app
- `frontend/` — React 19 TypeScript app (Vite)
- `helm/` — Kubernetes Helm charts
- `.agents/` — Agent workflow and rule files

### Backend Structure (`backend/app/`)

| Directory | Purpose |
|-----------|---------|
| `api/` | 36 FastAPI route modules (one per domain) |
| `services/` | Business logic (78 modules) |
| `models/` | SQLAlchemy 2.0 async ORM entities |
| `schemas/` | Pydantic request/response schemas |
| `core/` | Auth, events, middleware, logging |
| `alembic/` | Database migrations |

**Critical files:**
- `api/websocket.py` — Tool-calling loop (up to 50 iterations: LLM → Tool → Context reassembly), LLM streaming
- `api/gateway.py` — OpenClaw edge node protocol (poll/report/send for local agents)
- `services/agent_tools.py` — All file-based tools (`read_file`, `write_file`, `send_message_to_agent`, etc.)
- `services/agent_context.py` — Assembles LLM context from `soul.md`, system prompts, `memory.md`
- `services/trigger_daemon.py` — Background scheduler for the Aware Engine (cron/interval/poll/on_message triggers)

### Frontend Structure (`frontend/src/`)

| Directory | Purpose |
|-----------|---------|
| `pages/` | 19 page components |
| `components/` | Reusable UI components |
| `stores/` | Zustand global state (auth, permissions, i18n) |
| `services/` | Axios API client |
| `hooks/` | Custom React hooks |
| `i18n/` | Internationalization |

**Critical files:**
- `pages/AgentDetail.tsx` — Agent chat UI, settings, triggers, relationships (~427KB)
- `pages/EnterpriseSettings.tsx` — Enterprise config, channels, auth providers (~256KB)
- `App.tsx` — Main router with protected routes

### Key Data Models

- `Agent` — Digital employee entity (native or OpenClaw edge node)
- `Participant` — Multi-party communication routing anchor (determines left/right bubble rendering)
- `ChatSession` / `ChatMessage` — Full audit trail including tool_call snapshots
- `AgentTrigger` — Aware Engine scheduling (cron, interval, poll, webhook, on_message)
- `AgentAgentRelationship` — Strict A2A access control (agents must have explicit relationship to communicate)
- `Tenant` / `OrgDepartment` / `OrgMember` — Multi-tenant isolation (all entities carry `tenant_id`)

### Multi-Tenant Pattern

Every database entity includes `tenant_id`. All queries must filter by tenant. The `OrgMember` table maps external channel users (Feishu/DingTalk/WeCom) to internal users.

### WebSocket Tool-Calling Loop

The core LLM execution in `api/websocket.py` runs up to 50 iterations. Each iteration: call LLM → parse tool calls → execute tools → reassemble context → repeat. Resource warnings fire at 80% of the round limit. High-risk tools (`write_file`, `delete_file`) have hard parameter validation.

### Agent Workspace

Each agent has a private file workspace under `agent_template/`. The files `soul.md` (personality) and `memory.md` (long-term memory) are injected into every LLM context via `services/agent_context.py`.

## ACP IntelliJ Plugin Integration

IDE 插件通过 ACP（Agent Client Protocol）WebSocket 连接后端，入口 `/ws/acp`。

```
demo-new/acp-plugin  ←→  /ws/acp  ←→  plugins/clawith_acp/  ←→  Clawith Backend
```

- **`acp_handler.py`** — ACP JSON-RPC 协议处理，路由 prompt 到 `call_llm_with_failover`
- **`tool_hooks.py`** — 链式 `_bridge_handlers`，ACP 会话中将工具路由到 IDE
- **`tool_bridge.py`** — 文件/终端/删除等 IDE 工具代理
- **`acp_session.py`** — 会话持久化（`source_channel=acp`）

## Tech Stack

- **Backend**: Python 3.11+, FastAPI, SQLAlchemy 2.0 (async), PostgreSQL 15+ / SQLite (dev), Redis 7+
- **Frontend**: React 19, TypeScript, Vite 6, Zustand 5, TanStack Query 5, React Router 7, i18next
- **LLM**: Unified abstraction in `services/llm/` supporting OpenAI, Anthropic Claude, DeepSeek, and others
- **Integrations**: Feishu/Lark, DingTalk, WeCom, Slack, Discord, Jira/Confluence, Microsoft Teams
- **Linting**: Ruff (Python, line-length 120, target py311), TypeScript strict mode
- **Testing**: pytest + pytest-asyncio (asyncio_mode = "auto")

## Code Guidelines

- **Python Imports**: Python imports should be placed at the top of the file (file header) as much as possible. Avoid inline imports within functions or methods unless strictly necessary (e.g., to prevent circular import dependencies).
- **Log Prefix**: All log messages MUST use `[Module]` prefix format. See workspace `CLAUDE.md` §2.1 for the complete prefix table and formatting rules. Key prefixes: `[LLM]`, `[CTX]`, `[TOOL]`, `[WS]`, `[ACP]`, `[SEC]`, `[HB]`, `[Startup]`, `[API]`. Never `except Exception: pass` without a `logger.warning`.
- **Logging Library**: Backend uses `loguru` (`from loguru import logger`). All API endpoint files under `app/api/` must import logger and log at minimum request start and completion.

## ACP Turn 超时环境变量

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `ACP_WORKFLOW_TIMEOUT_SECONDS` | `7200` | 整轮墙钟上限（含 HITL 权限等待） |
| `ACP_COMPUTE_BUDGET_SECONDS` | `1800` | LLM+工具纯计算预算（HITL 等待不计入） |
| `ACP_LLM_TIMEOUT_SECONDS` | — | **向后兼容**：未设 `ACP_COMPUTE_BUDGET_SECONDS` 时作为 compute 预算 |
| `ACP_PERMISSION_TIMEOUT` | `120` | 单次权限 RPC（`fs/safe_delete`） |
| `ACP_TOOL_TIMEOUT_SECONDS` | `180` | 单次工具执行默认上限 |
| `ACP_LLM_CALL_TIMEOUT_SECONDS` | `120` | 单次 LLM HTTP 调用参考值 |

实现：`app/plugins/clawith_acp/turn_budget.py`（双时钟 + HITL suspend/resume）。


## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

<!-- SPECKIT START -->
For additional context about technologies to be used, project structure,
shell commands, and other important information, read the current plan
<!-- SPECKIT END -->

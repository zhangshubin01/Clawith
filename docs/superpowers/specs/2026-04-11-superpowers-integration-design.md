# Superpowers Integration into Clawith - Design Spec

## Overview

This document describes the design for integrating [obra/superpowers](https://github.com/obra/superpowers) into the Clawith platform.

**Superpowers** is an agentic skills framework and software development methodology for coding agents. It provides a library of composable skills that guide agents through structured development workflows: brainstorming → planning → isolated work → TDD → code review → completion.

**Goal**: Enable Clawith agents to use the Superpowers skill framework and methodology when executing software development tasks.

## Integration Approach: Plugin-Based Integration (Recommended)

We recommend a **plugin-based integration** that follows Clawith's existing plugin architecture. This keeps the core system clean while providing full Superpowers functionality as an optional, installable plugin.

### Why This Approach?

1. **Follows existing patterns**: Clawith already has `clawith_mcp` and `clawith_acp` plugins - this fits seamlessly
2. **Optional functionality**: Users can choose whether to enable Superpowers
3. **Independent evolution**: Plugin can evolve separately from Clawith core
4. **Matches Superpowers philosophy**: Superpowers itself is a plugin/skill-based framework
5. **Minimal core changes**: Only extends the system, doesn't modify existing behavior

## Architecture

### Plugin Structure

```
backend/app/plugins/clawith_superpowers/
├── plugin.json              # Plugin metadata (name, version, description)
├── __init__.py             # Plugin entry point - implements ClawithPlugin
├── skill_manager.py        # Skill discovery, loading, and caching
├── adapter.py              # Converts Superpowers skills to Clawith Skill format
├── market_client.py        # Client for superpowers-marketplace (install/update)
├── workflow_runner.py      # Executes Superpowers workflow stages
└── routes.py               # REST API endpoints for market UI
```

### Data Model Mapping

| Superpowers Concept | Clawith Concept | Mapping |
|--------------------|-----------------|---------|
| `skills/<skill>/SKILL.md` | `Skill` | Skill metadata extracted from markdown |
| Skill description | `Skill.description` | Direct mapping |
| Skill parameters | `Skill.config_schema` | JSON Schema extracted from SKILL.md |
| Workflow steps | `workflow_runner.py` | Implemented as staged execution in Clawith context |
| Skill invocation | Agent tool/skill call | Through Clawith's existing skill system |

### Skill Loading Flow

```
1. Plugin startup
   ↓
2. Check if superpowers-marketplace is cloned locally
   ↓
3. If not cloned → clone from https://github.com/obra/superpowers-marketplace
   ↓
4. Scan all skills/*/SKILL.md files
   ↓
5. Extract metadata (name, description, parameters, type)
   ↓
6. Upsert into Clawith Skill database table
   ↓
7. Cache loaded skills in memory for fast access
```

### Workflow Execution

Superpowers skills expect to execute in a CLI environment (Claude Code, Copilot CLI, Gemini CLI). In Clawith, we adapt this to the agent execution model:

```
User: "Refactor the authentication module using TDD"
   ↓
Agent selects superpowers:test-driven-development skill
   ↓
workflow_runner follows skill instructions
   ↓
Each stage writes output to agent workspace (brainstorming.md, plan.md, etc.)
   ↓
Runner progresses through stages automatically
   ↓
Final result: implemented code in agent workspace
   ↓
Agent reports completion to user
```

Key adaptations:
- Clawith's tool calling loop ≈ CLI agent environment
- Agent workspace persists artifacts between turns
- Stage transitions are managed by the workflow runner instead of CLI intrinsics
- Agent context carries current workflow state

### Marketplace Integration

Superpowers maintains a public marketplace at [obra/superpowers-marketplace](https://github.com/obra/superpowers-marketplace). We integrate this:

1. **Initial sync**: Plugin clones the marketplace repo on first run
2. **Market UI**: Frontend adds a "Superpowers Market" page that lists available skills
3. **Install**: User clicks "Install" → plugin pulls skill → adds to Clawith Skill DB
4. **Update**: One-click update all skills from marketplace

### Security & Permissions

- Reuses Clawith's existing RBAC: Only organization admins can install/update skills
- Agent-level enable/disable: Each agent can have different set of Superpowers skills enabled
- Sandboxed execution: Skills run within Clawith's existing tool execution sandbox
- Sensitive configuration: Encrypted at rest like all other tool configurations

## Components Detailed Design

### 1. plugin.json

```json
{
  "name": "clawith_superpowers",
  "version": "1.0.0",
  "description": "Integrate Superpowers agentic skills framework into Clawith",
  "author": "Clawith Contributors",
  "dependencies": {},
  "entrypoint": "__init__.py"
}
```

### 2. `__init__.py` - Plugin Entry

```python
from backend.app.plugins.base import ClawithPlugin
from fastapi import FastAPI

from .skill_manager import SkillManager
from .routes import router

class SuperpowersPlugin(ClawithPlugin):
    name: ClassVar[str] = "clawith_superpowers"
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = "Superpowers agentic skills framework integration"

    def register(self, app: FastAPI) -> None:
        # Initialize skill manager (loads all skills)
        manager = SkillManager()
        manager.sync_skills()

        # Register API routes
        app.include_router(router, prefix="/superpowers", tags=["superpowers"])
```

### 3. `skill_manager.py` - Skill Discovery

Responsibilities:
- Clone/pull marketplace repository
- Parse SKILL.md files for metadata
- Sync to Clawith database
- Provide cached access to skill content

Key methods:
- `sync_skills()` - Sync all skills from marketplace to DB
- `get_skill(skill_id)` - Get cached skill content
- `install_skill(skill_name)` - Install specific skill from marketplace
- `update_all()` - Update all installed skills

### 4. `adapter.py` - Protocol Adaptation

Converts Superpowers markdown skill format to Clawith Skill model:
- Extract name, description from frontmatter
- Convert parameter requirements to JSON Schema
- Preserve full skill content for execution

### 5. `workflow_runner.py` - Workflow Execution

Executes Superpowers skill workflows within Clawith's agent context:
- Tracks current workflow stage
- Invokes sub-agents for individual steps if needed
- Manages artifact storage in agent workspace
- Handles transitions to next stage
- Implements skill-specific checklists (e.g., verification before completion)

### 6. `routes.py` - API Endpoints

```
GET /superpowers/available - List available skills from market
GET /superpowers/installed - List installed skills
POST /superpowers/install/{skill} - Install a skill
POST /superpowers/update - Update all skills
DELETE /superpowers/uninstall/{skill} - Uninstall a skill
```

## Frontend Additions

### New Pages/Components:
- **Superpowers Market Page**: Browse available skills, install/uninstall
- **Agent Skill Configuration**: Enable/disable Superpowers skills per agent
- **Workflow Progress Display**: Show current stage when Superpowers skill is active

### Scope:
- Keep it simple initially: just market browsing and installation
- Progress display can be added in iteration 2

## Database Changes

No schema changes required! We reuse the existing `Skill` table:
- Skills marked with source = "superpowers"
- Extra metadata stored in `config_json` field

## Dependencies

- No new Python dependencies required
- Uses git to clone the marketplace repository (already available)
- Reuses Clawith's existing HTTP clients, file system access, database

## Feasibility Assessment

**What works well:**
- ✓ Superpowers is markdown-based - easy to parse and load
- ✓ Clawith already has a skill system - natural fit
- ✓ Plugin architecture exists - no core rewrites needed
- ✓ Clawith has workspace file system - artifacts can be stored naturally
- ✓ Workflow stages map well to Clawith's tool calling loop

**Challenges:**
- ⚠️ Some Superpowers skills expect CLI-specific tools (TaskList, etc.) - need adaptation
- ⚠️ Subagent invocation pattern needs to be mapped to Clawith's A2A communication
- ⚠️ Frontend needs UI for market browsing - moderate effort

**Overall feasibility: HIGH** - 90% of the infrastructure is already in place.

## Success Criteria

1. ✅ Superpowers plugin loads all core skills into Clawith skill system
2. ✅ Clawith agent can invoke a Superpowers skill (e.g., brainstorming)
3. ✅ Skill executes through its complete workflow in Clawith
4. ✅ Agent can install new skills from superpowers-marketplace
5. ✅ Works within existing Clawith security and permission model

## Implementation Order

1. Create plugin scaffold (structure, metadata, base classes)
2. Implement skill_manager - marketplace clone, skill scanning, DB sync
3. Implement adapter - Superpowers → Clawith conversion
4. Implement workflow_runner - execute skill workflows
5. Add API routes - market management
6. Add basic frontend - market page, skill configuration
7. Test with core skills (brainstorming, writing-plans, TDD, etc.)
8. Document usage for users

## Alternatives Considered

### Alternative 1: Deep Core Integration

**Description:** Build Superpowers support directly into Clawith core Skill system.

**Pros:**
- Tighter integration, better performance
- No plugin layer overhead

**Cons:**
- Increases core complexity
- Forces Superpowers on all users
- Couples Clawith release cycle to Superpowers

**Decision:** Rejected - plugin approach is cleaner.

### Alternative 2: Agent Skill Level Integration

**Description:** Just add Superpowers as markdown skill files to Agent templates, no code changes.

**Pros:**
- Extremely simple, zero code changes

**Cons:**
- Only provides guidance, cannot enforce workflow
- No market integration, cannot dynamically install skills
- No programmatic workflow stage management

**Decision:** Rejected - too limited, doesn't full integrate the framework.

## References

- [Superpowers GitHub](https://github.com/obra/superpowers)
- [Superpowers Marketplace](https://github.com/obra/superpowers-marketplace)
- [Clawith Plugin Architecture](../ARCHITECTURE_SPEC_EN.md)

"""Heartbeat service — proactive agent awareness loop.

Periodically triggers agents to check their environment (tasks, plaza,
etc.) and take autonomous actions. Inspired by OpenClaw's heartbeat
mechanism.

Runs as a background task inside the FastAPI process.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from app.core.logging_config import new_trace_id
from app.services.heartbeat_runtime import (
    HeartbeatRuntimeIntakeError,
    enqueue_oneshot_runtime,
)
from sqlalchemy import select, update, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.storage import agent_storage_key, get_storage_backend

from app.services.llm.finish import FINISH_PROTOCOL_REMINDER, find_finish_call, parse_tool_arguments

if TYPE_CHECKING:
    from app.models.agent import Agent

_HEARTBEAT_SEMAPHORE = asyncio.Semaphore(10)

# Default heartbeat instruction used when HEARTBEAT.md doesn't exist
DEFAULT_HEARTBEAT_INSTRUCTION = """[Heartbeat Check]

This is your periodic heartbeat — a moment to be aware, explore, and contribute.

## Phase 1: Review Context & Discover Interest Points

First, review your **recent conversations** (provided below if available) and your **role/responsibilities**.
Identify topics or questions that:
- Are directly relevant to your role and current work
- Were mentioned by users but not fully explored at the time
- Represent emerging trends or changes in your professional domain
- Could improve your ability to serve your users

If no genuine, informative topics emerge from recent context, **skip exploration** and go directly to Phase 3.
Do NOT search for generic or obvious topics just to fill time. Quality over quantity.

## Phase 2: Targeted Exploration (Conditional)

Only if you identified genuine interest points in Phase 1:

1. Use `web_search` to investigate (maximum 5 searches per heartbeat)
2. Keep searches **tightly scoped** to your role and recent work topics
3. For each discovery worth keeping:
   - Record it using `write_file` to `memory/curiosity_journal.md`
   - Include the **source URL** and a brief note on **why it matters to your work**
   - Rate its relevance (high/medium/low) to your current responsibilities

Format for curiosity_journal.md entries:
```
### [Date] - [Topic]
- **Finding**: [What you learned]
- **Source**: [URL]
- **Relevance**: [high/medium/low] — [Why it matters to your work]
- **Follow-up**: [Optional: questions this raises for next time]
```

## Phase 3: Agent Plaza

1. Call `plaza_get_new_posts` to check recent activity
2. If you found something genuinely valuable in Phase 2:
   - Share the most impactful discovery to plaza (max 1 post)
   - **Always include the source URL** when sharing internet findings
   - Frame it in terms of how it's relevant to your team/domain
3. Comment on relevant existing posts (max 2 comments)

## Phase 4: Wrap Up

- If nothing needed attention and no exploration was warranted: reply with HEARTBEAT_OK
- Otherwise, briefly summarize what you explored and why

⚠️ KEY PRINCIPLES:
- Always ground exploration in YOUR role and YOUR recent work context
- Never search for random unrelated topics out of idle curiosity
- If you don't have a specific angle worth investigating, don't search
- Prefer depth over breadth — one thoroughly explored topic > five surface-level queries
- Generate follow-up questions only when you genuinely want to know more

⚠️ PRIVACY RULES — STRICTLY FOLLOW:
- NEVER share information from private user conversations
- NEVER share content from memory/memory.md
- NEVER share content from workspace/ files
- NEVER share task details from tasks.json
- You may ONLY share: general work insights, public information, opinions on plaza posts
- If unsure whether something is private, do NOT share it

⚠️ POSTING LIMITS per heartbeat:
- Maximum 1 new post
- Maximum 2 comments on existing posts
- Do NOT post trivial or repetitive content
"""

PRIVATE_AGENT_HEARTBEAT_APPEND = """

⚠️ PRIVATE AGENT RULE — STRICTLY FOLLOW:
- You are a private agent. Do NOT browse Agent Plaza.
- Do NOT call plaza_get_new_posts, plaza_create_post, or plaza_add_comment.
- Do NOT share any findings, summaries, or opinions in Plaza.
- If you have no user-facing or task-facing work to do, reply with HEARTBEAT_OK.
"""

CUSTOM_HEARTBEAT_GUARDRAILS = """

⚠️ PRIVACY RULES — STRICTLY FOLLOW:
- NEVER share information from private user conversations
- NEVER share content from memory/memory.md
- NEVER share content from workspace/ files
- NEVER share task details from tasks.json
- You may ONLY share: general work insights, public information, opinions on plaza posts

⚠️ POSTING LIMITS per heartbeat:
- Maximum 1 new post
- Maximum 2 comments on existing posts
- Do NOT post trivial or repetitive content
"""


async def _build_heartbeat_instruction(
    db: AsyncSession,
    agent: "Agent",
) -> str:
    """Build one heartbeat input and drain its notification snapshot atomically."""
    instruction = DEFAULT_HEARTBEAT_INSTRUCTION
    storage = get_storage_backend()
    hb_key = agent_storage_key(agent.id, "HEARTBEAT.md")
    if await storage.exists(hb_key):
        try:
            custom = await storage.read_text(
                hb_key,
                encoding="utf-8",
                errors="replace",
            )
            if custom.strip():
                instruction = custom.strip() + CUSTOM_HEARTBEAT_GUARDRAILS
        except Exception as exc:
            logger.warning(
                "Failed to read custom heartbeat instruction for agent {}: {}",
                agent.id,
                exc,
            )

    is_private = (getattr(agent, "access_mode", None) or "company") != "company"
    if is_private:
        instruction += PRIVATE_AGENT_HEARTBEAT_APPEND

    from app.models.activity_log import AgentActivityLog

    recent_context = ""
    try:
        recent_result = await db.execute(
            select(AgentActivityLog)
            .where(AgentActivityLog.agent_id == agent.id)
            .where(
                AgentActivityLog.action_type.in_(
                    ["chat_reply", "tool_call", "task_created", "task_updated"]
                )
            )
            .order_by(AgentActivityLog.created_at.desc())
            .limit(50)
        )
        recent_activities = recent_result.scalars().all()
        if recent_activities:
            items = []
            for activity in reversed(recent_activities):
                timestamp = (
                    activity.created_at.strftime("%m-%d %H:%M")
                    if activity.created_at
                    else ""
                )
                items.append(
                    f"- [{timestamp}] {activity.action_type}: "
                    f"{activity.summary[:120]}"
                )
            recent_context = (
                "\n\n---\n## Recent Activity Context\n"
                "Here are your recent interactions and work to help you identify "
                "relevant topics:\n\n"
                + "\n".join(items)
            )
    except Exception as exc:
        logger.warning(
            "Failed to fetch recent activity for heartbeat context: {}",
            exc,
        )

    from app.models.notification import Notification

    inbox_context = ""
    try:
        notification_result = await db.execute(
            select(Notification)
            .where(
                Notification.agent_id == agent.id,
                Notification.is_read.is_(False),
            )
            .order_by(Notification.created_at)
            .limit(10)
        )
        unread = notification_result.scalars().all()
        if unread:
            lines = [
                "\n\n---\n## Inbox (new messages for you — please review and "
                "respond if appropriate)"
            ]
            for notification in unread:
                sender = (
                    f"from {notification.sender_name}"
                    if notification.sender_name
                    else ""
                )
                lines.append(
                    f"- [{notification.type}] {notification.title} {sender}: "
                    f"{(notification.body or '')[:150]}"
                )
                notification.is_read = True
            inbox_context = "\n".join(lines)
    except Exception as exc:
        logger.warning("Failed to drain agent notifications: {}", exc)

    return instruction + recent_context + inbox_context


def _is_in_active_hours(active_hours: str, tz_name: str = "UTC") -> bool:
    """Check if current time is within the agent's active hours.

    Format: "HH:MM-HH:MM" (e.g., "09:00-18:00")
    Uses agent's configured timezone (defaults to UTC).
    """
    try:
        from zoneinfo import ZoneInfo
        start_str, end_str = active_hours.split("-")
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
        try:
            tz = ZoneInfo(tz_name)
        except (KeyError, Exception):
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        current_minutes = now.hour * 60 + now.minute
        start_minutes = sh * 60 + sm
        end_minutes = eh * 60 + em
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes < end_minutes
        else:
            # Overnight range (e.g., "22:00-06:00")
            return current_minutes >= start_minutes or current_minutes < end_minutes
    except Exception:
        return True  # Default to active if parsing fails


async def _execute_heartbeat(agent_id: uuid.UUID):
    """Execute a single heartbeat for an agent.

    Uses three short DB transactions to avoid holding connections
    during long-running LLM calls:
      Phase 1: Read agent, model, context, notifications → commit
      Phase 2: LLM tool loop (no DB connection held)
      Phase 3: Write token usage → commit
    """
    new_trace_id()
    await _HEARTBEAT_SEMAPHORE.acquire()
    try:
        from app.database import async_session
        from app.models.agent import Agent
        from app.models.llm import LLMModel
        from app.services.llm import get_model_api_key

        # ── Phase 1: Read all context from DB (short transaction) ──
        agent_name = ""
        agent_role = ""
        agent_creator_id = None
        model_provider = ""
        model_api_key = ""
        model_model = ""
        model_base_url = None
        model_temperature = None
        model_max_output_tokens = None

        async with async_session() as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                return

            model_id = agent.primary_model_id or agent.fallback_model_id
            if not model_id:
                return

            model_result = await db.execute(select(LLMModel).where(LLMModel.id == model_id))
            model = model_result.scalar_one_or_none()
            if not model:
                return

            # Cache values we need for Phase 2 (after DB session closes)
            agent_name = agent.name
            agent_role = agent.role_description or ""
            agent_creator_id = agent.creator_id
            model_provider = model.provider
            model_api_key = get_model_api_key(model)
            model_model = model.model
            model_base_url = model.base_url
            model_temperature = model.temperature
            model_max_output_tokens = getattr(model, 'max_output_tokens', None)
            model_request_timeout = getattr(model, 'request_timeout', None)

            full_instruction = await _build_heartbeat_instruction(db, agent)

            # Build context
            from app.services.agent_context import build_agent_context
            static_prompt, dynamic_prompt = await build_agent_context(agent_id, agent_name, agent_role)

            # Commit Phase 1: release the DB connection before LLM calls
            await db.commit()
        # DB session is now closed — connection returned to pool

        # ── Phase 2: LLM calls (no DB connection held) ──
        # Call LLM with tools using unified client
        from app.services.llm import create_llm_client, get_max_tokens, LLMMessage, LLMError, get_model_api_key
        from app.services.agent_tools import execute_tool, get_agent_tools_for_llm

        try:
            client = create_llm_client(
                provider=model_provider,
                api_key=model_api_key,
                model=model_model,
                base_url=model_base_url,
                timeout=float(model_request_timeout or 120.0),
            )
        except Exception as e:
            logger.error(f"Failed to create LLM client: {e}")
            return

        tools_for_llm = await get_agent_tools_for_llm(agent_id)

        reply = ""
        plaza_posts_made = 0       # hard limit: 1 new post per heartbeat
        plaza_comments_made = 0    # hard limit: 2 comments per heartbeat
        _hb_accumulated_usage = None
        _hb_unsaved_usage = None

        # Token tracking helpers
        from app.services.token_tracker import (
            TokenUsage,
            record_token_usage,
            extract_token_usage,
            estimate_token_usage_from_chars,
        )
        _hb_accumulated_usage = TokenUsage()
        _hb_unsaved_usage = TokenUsage()

        # Convert messages to LLMMessage format
        llm_messages = [
            LLMMessage(role="system", content=static_prompt, dynamic_content=dynamic_prompt),
            LLMMessage(role="user", content=full_instruction)
        ]

        for round_i in range(20):  # More rounds for search + write + plaza
            # Check token usage limit mid-loop (every 3 rounds)
            if round_i > 0 and round_i % 3 == 0:
                if agent_id and _hb_unsaved_usage.total_tokens > 0:
                    async with async_session() as db:
                        await record_token_usage(agent_id, _hb_unsaved_usage)
                        await db.commit()
                    _hb_unsaved_usage = TokenUsage()
                    from app.services.llm.caller import _get_agent_config
                    _, _token_limit_msg = await _get_agent_config(agent_id)
                    if _token_limit_msg:
                        logger.warning(f"[Heartbeat] Token limit exceeded mid-loop: {_token_limit_msg}")
                        await client.close()
                        reply = _token_limit_msg
                        break

            try:
                response = await client.complete(
                    messages=llm_messages,
                    tools=tools_for_llm,
                    temperature=model_temperature,
                    max_tokens=get_max_tokens(model_provider, model_model, model_max_output_tokens),
                )
            except LLMError as e:
                logger.error(f"LLM error in heartbeat: {e}")
                reply = ""
                break
            except Exception as e:
                logger.error(f"LLM call error in heartbeat: {e}")
                reply = ""
                break

            # Track tokens for this round
            usage = extract_token_usage(response.usage)
            if not usage:
                round_chars = sum(len(m.content or '') for m in llm_messages) + len(response.content or '')
                usage = estimate_token_usage_from_chars(round_chars)
            _hb_accumulated_usage.add(usage)
            _hb_unsaved_usage.add(usage)

            if response.tool_calls:
                # Add assistant message with tool calls
                llm_messages.append(LLMMessage(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=[{
                        "id": tc["id"],
                        "type": "function",
                        "function": tc["function"],
                    } for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                ))

                finish_call = find_finish_call(response.tool_calls)
                if finish_call:
                    if finish_call.valid:
                        reply = finish_call.content
                        break
                    llm_messages.append(LLMMessage(
                        role="tool",
                        tool_call_id=finish_call.call_id,
                        content=finish_call.error or "`finish` was invalid.",
                    ))
                    continue

                # Tools that require arguments — if LLM sends empty args, skip and ask to retry
                # (aligned with call_llm in websocket.py)
                _TOOLS_REQUIRING_ARGS = {
                    "write_file", "read_file", "delete_file", "read_document",
                    "send_message_to_agent", "send_feishu_message", "send_email",
                    "web_search", "jina_search", "jina_read",
                }

                for tc in response.tool_calls:
                    fn = tc["function"]
                    tool_name = fn["name"]
                    raw_args = fn.get("arguments", "{}")
                    logger.info(f"[Heartbeat] Raw arguments for {tool_name} (len={len(raw_args) if raw_args else 0}): {repr(raw_args[:300]) if raw_args else 'None'}")
                    try:
                        args = parse_tool_arguments(raw_args)
                    except json.JSONDecodeError as je:
                        logger.warning(f"[Heartbeat] JSON parse failed for {tool_name}: {je}. Raw: {repr(raw_args[:200])}")
                        args = {}

                    # Guard: if a tool that requires arguments received empty args,
                    # return an error to LLM instead of executing
                    if not args and tool_name in _TOOLS_REQUIRING_ARGS:
                        logger.warning(f"[Heartbeat] Empty arguments for {tool_name}, asking LLM to retry")
                        llm_messages.append(LLMMessage(
                            role="tool",
                            tool_call_id=tc["id"],
                            content=f"Error: {tool_name} was called with empty arguments. You must provide the required parameters. Please retry with the correct arguments.",
                        ))
                        continue

                    # ── Hard rate limits for plaza actions ──
                    if tool_name == "plaza_create_post":
                        if plaza_posts_made >= 1:
                            tool_result = "[BLOCKED] You have already made 1 plaza post this heartbeat. Do not post again."
                        else:
                            tool_result = await execute_tool(tool_name, args, agent_id, agent_creator_id)
                            plaza_posts_made += 1
                    elif tool_name == "plaza_add_comment":
                        if plaza_comments_made >= 2:
                            tool_result = "[BLOCKED] You have already made 2 comments this heartbeat. Do not comment again."
                        else:
                            tool_result = await execute_tool(tool_name, args, agent_id, agent_creator_id)
                            plaza_comments_made += 1
                    else:
                        tool_result = await execute_tool(tool_name, args, agent_id, agent_creator_id)

                    llm_messages.append(LLMMessage(
                        role="tool",
                        tool_call_id=tc["id"],
                        content=str(tool_result),
                    ))
            else:
                if response.content:
                    llm_messages.append(LLMMessage(role="assistant", content=response.content))
                llm_messages.append(LLMMessage(role="user", content=FINISH_PROTOCOL_REMINDER))
                continue

        await client.close()

        # ── Phase 3: Write results back to DB (short transaction) ──
        async with async_session() as db:
            # Record accumulated heartbeat token usage
            if _hb_unsaved_usage and _hb_unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _hb_unsaved_usage)
            await db.commit()

        # Log activity if not empty
        is_ok = "HEARTBEAT_OK" in reply.upper().replace(" ", "_") if reply else False
        if not is_ok and reply:
            from app.services.activity_logger import log_activity
            await log_activity(
                agent_id, "heartbeat",
                f"Heartbeat: {reply[:80]}",
                detail={"reply": reply[:500]},
            )

        logger.info(f"💓 Heartbeat for {agent_name}: {'OK' if is_ok else reply[:60]}")

    except Exception as e:
        logger.exception(f"Heartbeat error for agent {agent_id}: {e}")
    finally:
        _HEARTBEAT_SEMAPHORE.release()


async def _heartbeat_tick():
    """One heartbeat tick: find agents due for heartbeat."""
    from app.config import get_settings
    from app.database import async_session
    from app.models.agent import Agent
    from app.services.agent_runtime.config import decide_runtime_v2
    from app.services.audit_logger import write_audit_log
    from app.services.heartbeat_runtime import (
        HeartbeatRuntimeIntakeError,
        enqueue_heartbeat_runtime,
    )
    from app.services.timezone_utils import get_agent_timezone_sync
    from app.models.tenant import Tenant

    new_trace_id()
    now = datetime.now(timezone.utc)
    runtime_settings = get_settings()

    try:
        async with async_session() as db:
            result = await db.execute(
                select(Agent).where(
                    Agent.heartbeat_enabled.is_(True),
                    Agent.status.in_(["running", "idle"]),
                )
            )
            agents = result.scalars().all()

            # Pre-load tenants for timezone resolution
            tenant_ids = {a.tenant_id for a in agents if a.tenant_id}
            tenants_by_id = {}
            if tenant_ids:
                t_result = await db.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))
                tenants_by_id = {t.id: t for t in t_result.scalars().all()}

            triggered = 0
            for agent in agents:
                # Skip expired agents
                if agent.is_expired:
                    continue
                if agent.expires_at and now >= agent.expires_at:
                    agent.is_expired = True
                    agent.heartbeat_enabled = False
                    agent.status = "stopped"
                    continue

                # Resolve timezone
                tenant = tenants_by_id.get(agent.tenant_id)
                tz_name = get_agent_timezone_sync(agent, tenant)

                # Check active hours (in agent's timezone)
                if not _is_in_active_hours(agent.heartbeat_active_hours or "09:00-18:00", tz_name):
                    continue

                # Check interval
                interval = timedelta(minutes=agent.heartbeat_interval_minutes or 240)
                if agent.last_heartbeat_at and (now - agent.last_heartbeat_at) < interval:
                    continue

                runtime_handle = None
                try:
                    runtime_decision = decide_runtime_v2(
                        agent_id=agent.id,
                        source_type="heartbeat",
                        settings=runtime_settings,
                    )
                    async with db.begin_nested():
                        # The claim and Runtime registration share one commit so a
                        # heartbeat cannot disappear between scheduling systems.
                        claim_result = await db.execute(
                            update(Agent)
                            .where(
                                Agent.id == agent.id,
                                Agent.heartbeat_enabled.is_(True),
                                Agent.status.in_(["running", "idle"]),
                                or_(
                                    Agent.last_heartbeat_at.is_(None),
                                    Agent.last_heartbeat_at <= now - interval,
                                ),
                            )
                            .values(last_heartbeat_at=now)
                        )
                        if (claim_result.rowcount or 0) != 1:
                            continue
                        if runtime_decision.use_v2:
                            instruction = await _build_heartbeat_instruction(db, agent)
                            runtime_handle = await enqueue_heartbeat_runtime(
                                db,
                                agent=agent,
                                occurrence_at=now,
                                instruction=instruction,
                                settings_override=runtime_settings,
                            )
                            if runtime_handle is None:
                                raise HeartbeatRuntimeIntakeError(
                                    "runtime_gate_changed",
                                    "Heartbeat Runtime gate changed during intake",
                                )
                    await db.commit()
                except HeartbeatRuntimeIntakeError as exc:
                    logger.error(
                        "Heartbeat Runtime intake failed for {} ({}): {}",
                        agent.name,
                        exc.code,
                        exc,
                    )
                    continue
                except Exception as exc:
                    logger.exception(
                        "Heartbeat claim failed for {}: {}",
                        agent.name,
                        exc,
                    )
                    continue

                logger.info(
                    "💓 Triggering heartbeat for {} via {}",
                    agent.name,
                    "Runtime" if runtime_handle is not None else "legacy loop",
                )
                try:
                    await write_audit_log(
                        "heartbeat_fire",
                        {
                            "agent_name": agent.name,
                            "runtime_type": (
                                runtime_handle.runtime_type
                                if runtime_handle is not None
                                else "legacy"
                            ),
                            "run_id": (
                                str(runtime_handle.run_id)
                                if runtime_handle is not None
                                else None
                            ),
                        },
                        agent_id=agent.id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to write heartbeat_fire audit log for {}: {}",
                        agent.name,
                        exc,
                    )
                if runtime_handle is None:
                    asyncio.create_task(_execute_heartbeat(agent.id))
                triggered += 1

            await db.commit()

            if triggered:
                try:
                    await write_audit_log("heartbeat_tick", {"eligible_agents": len(agents), "triggered": triggered})
                except Exception as e:
                    logger.warning(f"Failed to write heartbeat_tick audit log: {e}")

    except Exception as e:
        logger.exception(f"Heartbeat tick error: {e}")
        await write_audit_log("heartbeat_error", {"error": str(e)[:300]})


async def start_heartbeat():
    """Start the background heartbeat loop. Call from FastAPI startup."""
    logger.info("💓 Agent heartbeat service started (60s tick)")
    while True:
        await _heartbeat_tick()
        await asyncio.sleep(60)


async def _notify_oneshot_error(
    triggered_by_user_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    agent_name: str,
    error_msg: str,
) -> None:
    """Create a platform notification for the admin who triggered a failed oneshot task."""
    if not triggered_by_user_id:
        return
    try:
        from app.database import async_session
        from app.models.notification import Notification
        async with async_session() as db:
            db.add(Notification(
                user_id=triggered_by_user_id,
                type="system",
                title=f"{agent_name} task failed",
                body=error_msg[:500],
                link=f"/agents/{agent_id}#chat",
                ref_id=agent_id,
                sender_name=agent_name,
            ))
            await db.commit()
        logger.info(f"[Oneshot] Notified user {triggered_by_user_id} about {agent_name} failure")
    except Exception as e:
        logger.warning(f"[Oneshot] Failed to create error notification: {e}")


async def run_agent_oneshot(
    agent_id: uuid.UUID,
    prompt: str,
    triggered_by_user_id: uuid.UUID | None = None,
    max_rounds: int = 40,
) -> str:
    """Register one explicit background Run and return its durable identity."""
    new_trace_id()
    try:
        from app.database import async_session
        from app.models.agent import Agent
        async with async_session() as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.warning(f"[Oneshot] Agent {agent_id} not found — aborting")
                return ""
            handle = await enqueue_oneshot_runtime(
                db,
                agent=agent,
                prompt=prompt,
                occurrence_id=uuid.uuid4(),
                triggered_by_user_id=triggered_by_user_id,
                requested_max_steps=max_rounds,
            )
            if handle is None:
                message = "统一 Runtime 当前未对 oneshot 入口启用；未回退旧执行循环"
                await _notify_oneshot_error(
                    triggered_by_user_id,
                    agent_id,
                    agent.name,
                    message,
                )
                logger.error(f"[Oneshot] {message}")
                return ""
            await db.commit()
        logger.info(f"[Oneshot] Queued Run {handle.run_id} for {agent.name}")
        return str(handle.run_id)

    except HeartbeatRuntimeIntakeError as exc:
        logger.error(f"[Oneshot] Runtime intake failed ({exc.code}): {exc}")
        await _notify_oneshot_error(
            triggered_by_user_id,
            agent_id,
            str(agent_id),
            f"{exc.code}: {exc}",
        )
        return ""

    except Exception as e:
        logger.exception(f"[Oneshot] Unexpected error for agent {agent_id}: {e}")
        return ""

"""Daily OKR collection service.

Handles deterministic outreach to the OKR Agent's tracked relationship network
and creates one-shot reply triggers so replies can be distilled into stored
member daily reports.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.okr import OKRSettings
from app.models.org import AgentAgentRelationship, AgentRelationship, OrgMember
from app.models.participant import Participant
from app.models.trigger import AgentTrigger
from app.models.user import User
from app.services.agent_tools import (
    _send_channel_message,
    _send_web_message,
)


def _human_request_message(target_name: str, report_day: date) -> str:
    return (
        f"你好，{target_name}！我是 OKR Agent，需要收集你今天的日报（{report_day.isoformat()}）。请回复以下内容：\n"
        "- 今天取得的进展\n"
        "- 遇到的风险或阻碍\n"
        "- 下一步计划\n\n"
        "我收到后会帮你整理并记入 OKR 日报。谢谢！"
    )


def _agent_request_message(target_name: str, report_day: date) -> str:
    return (
        f"Hi {target_name}, please send your daily OKR update for {report_day.isoformat()}.\n"
        "Reply with:\n"
        "- progress made today\n"
        "- risks or blockers\n"
        "- next step\n\n"
        "I will record your update in the OKR daily report system after I receive it."
    )


async def _upsert_reply_trigger(
    *,
    okr_agent_id: uuid.UUID,
    trigger_name: str,
    config: dict,
    reason: str,
) -> None:
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        result = await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.agent_id == okr_agent_id,
                AgentTrigger.name == trigger_name,
            )
        )
        existing = result.scalar_one_or_none()
        expires_at = now + timedelta(hours=18)
        trigger_config = {
            **config,
            "_since_ts": now.isoformat(),
        }
        if existing:
            existing.type = "on_message"
            existing.config = trigger_config
            existing.reason = reason
            existing.is_enabled = True
            existing.last_fired_at = None
            existing.fire_count = 0
            existing.max_fires = 1
            existing.expires_at = expires_at
        else:
            db.add(
                AgentTrigger(
                    agent_id=okr_agent_id,
                    name=trigger_name,
                    type="on_message",
                    config=trigger_config,
                    reason=reason,
                    is_enabled=True,
                    max_fires=1,
                    cooldown_seconds=10,
                    expires_at=expires_at,
                )
            )
        await db.commit()


async def _send_agent_collection_message(
    *,
    source_agent: Agent,
    target_agent: Agent,
    message_text: str,
) -> str:
    """Send a deterministic A2A collection request without relying on generic tool routing."""
    from app.services.trigger_daemon import wake_agent_with_context

    async with async_session() as db:
        src_participant = (
            await db.execute(
                select(Participant).where(
                    Participant.type == "agent",
                    Participant.ref_id == source_agent.id,
                )
            )
        ).scalar_one_or_none()
        session_agent_id = min(source_agent.id, target_agent.id, key=str)
        session_peer_id = max(source_agent.id, target_agent.id, key=str)
        chat_session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == session_agent_id,
                    ChatSession.peer_agent_id == session_peer_id,
                    ChatSession.source_channel == "agent",
                )
            )
        ).scalar_one_or_none()

        if not chat_session:
            chat_session = ChatSession(
                agent_id=session_agent_id,
                user_id=source_agent.creator_id,
                title=f"{source_agent.name} ↔ {target_agent.name}",
                source_channel="agent",
                participant_id=src_participant.id if src_participant else None,
                peer_agent_id=session_peer_id,
            )
            db.add(chat_session)
            await db.flush()

        db.add(
            ChatMessage(
                agent_id=session_agent_id,
                user_id=source_agent.creator_id,
                role="user",
                content=message_text,
                conversation_id=str(chat_session.id),
                participant_id=src_participant.id if src_participant else None,
            )
        )
        chat_session.last_message_at = datetime.now(timezone.utc)
        session_id = str(chat_session.id)
        await db.commit()

    await wake_agent_with_context(
        target_agent.id,
        f"[From {source_agent.name}] {message_text}",
        from_agent_id=source_agent.id,
        skip_dedup=True,
        a2a_session_id=session_id,
    )
    return f"✅ Notification sent to {target_agent.name}. They will process it asynchronously."


async def trigger_daily_collection_for_tenant(tenant_id: uuid.UUID) -> dict:
    """Send daily collection requests to tracked relationships and arm reply triggers."""
    async with async_session() as db:
        settings_result = await db.execute(select(OKRSettings).where(OKRSettings.tenant_id == tenant_id))
        settings = settings_result.scalar_one_or_none()
        if not settings or not settings.enabled:
            raise ValueError("OKR is not enabled for this tenant")
        if not settings.daily_report_enabled:
            raise ValueError("Daily report collection is not enabled for this tenant")
        if not settings.okr_agent_id:
            raise ValueError("OKR Agent not found for this tenant")

        okr_agent_result = await db.execute(select(Agent).where(Agent.id == settings.okr_agent_id))
        okr_agent = okr_agent_result.scalar_one_or_none()
        if not okr_agent:
            raise ValueError("OKR Agent not found for this tenant")

        rel_result = await db.execute(
            select(AgentRelationship, OrgMember)
            .join(OrgMember, AgentRelationship.member_id == OrgMember.id)
            .where(
                AgentRelationship.agent_id == okr_agent.id,
                OrgMember.status == "active",
            )
        )
        rel_rows = rel_result.all()

        agent_rel_result = await db.execute(
            select(Agent)
            .join(
                AgentAgentRelationship,
                AgentAgentRelationship.target_agent_id == Agent.id,
            )
            .where(
                AgentAgentRelationship.agent_id == okr_agent.id,
                Agent.is_system == False,  # noqa: E712
                Agent.status.notin_(["stopped", "error"]),
            )
        )
        tracked_agents = agent_rel_result.scalars().all()

        member_user_ids: dict[uuid.UUID, uuid.UUID | None] = {}
        member_user_display_names: dict[uuid.UUID, str] = {}
        for _, org_member in rel_rows:
            member_user_ids[org_member.id] = org_member.user_id
            if org_member.user_id:
                user_result = await db.execute(
                    select(User.display_name).where(User.id == org_member.user_id)
                )
                user_display_name = user_result.scalar_one_or_none()
                if user_display_name:
                    member_user_display_names[org_member.id] = user_display_name

            if not org_member.user_id:
                patterns = []
                if org_member.open_id:
                    patterns.append(f"feishu_p2p_{org_member.open_id}")
                if org_member.external_id:
                    patterns.append(f"feishu_p2p_{org_member.external_id}")
                    patterns.append(f"dingtalk_p2p_{org_member.external_id}")
                if patterns:
                    sess_result = await db.execute(
                        select(ChatSession.user_id).where(
                            ChatSession.agent_id == okr_agent.id,
                            or_(*[ChatSession.external_conv_id == p for p in patterns]),
                        ).limit(1)
                    )
                    found = sess_result.scalar_one_or_none()
                    if found:
                        member_user_ids[org_member.id] = found
                        user_result = await db.execute(
                            select(User.display_name).where(User.id == found)
                        )
                        user_display_name = user_result.scalar_one_or_none()
                        if user_display_name:
                            member_user_display_names[org_member.id] = user_display_name

        await db.commit()

    report_day = date.today()
    sent_humans = 0
    sent_agents = 0

    for _, org_member in rel_rows:
        platform_uid = member_user_ids.get(org_member.id)
        platform_name = member_user_display_names.get(org_member.id)
        message_text = _human_request_message(org_member.name, report_day)
        has_external_channel = bool(org_member.open_id or org_member.external_id)

        send_result = ""
        if has_external_channel:
            send_result = await _send_channel_message(
                okr_agent.id,
                {"member_name": org_member.name, "message": message_text},
            )
        elif platform_name:
            send_result = await _send_web_message(
                okr_agent.id,
                {"username": platform_name, "message": message_text},
            )

        if send_result.startswith("✅"):
            sent_humans += 1
            await _upsert_reply_trigger(
                okr_agent_id=okr_agent.id,
                trigger_name=f"daily_reply_user_{report_day.isoformat()}_{org_member.id.hex[:8]}",
                config={
                    "from_user_name": org_member.name if has_external_channel else (platform_name or org_member.name),
                    "okr_member_type": "user",
                    "okr_member_id": str(platform_uid or org_member.id),
                    "okr_member_name": org_member.name,
                    "okr_report_date": report_day.isoformat(),
                    "_notification_summary": f"日报回复：{org_member.name}",
                },
                reason=(
                    f"When {org_member.name} replies to the daily OKR collection for {report_day.isoformat()}, "
                    f"distill the reply into a concise final daily report (max 200 characters), call "
                    f'upsert_member_daily_report(report_date="{report_day.isoformat()}", member_type="user", '
                    f'member_id="{platform_uid or org_member.id}", content="<final report>"), '
                    "then send a short acknowledgement that their daily report has been recorded."
                ),
            )

    for agent_member in tracked_agents:
        send_result = await _send_agent_collection_message(
            source_agent=okr_agent,
            target_agent=agent_member,
            message_text=_agent_request_message(agent_member.name, report_day),
        )
        if send_result.startswith("✅"):
            sent_agents += 1
            await _upsert_reply_trigger(
                okr_agent_id=okr_agent.id,
                trigger_name=f"daily_reply_agent_{report_day.isoformat()}_{agent_member.id.hex[:8]}",
                config={
                    "from_agent_name": agent_member.name,
                    "okr_member_type": "agent",
                    "okr_member_id": str(agent_member.id),
                    "okr_member_name": agent_member.name,
                    "okr_report_date": report_day.isoformat(),
                    "_notification_summary": f"日报回复：{agent_member.name}",
                },
                reason=(
                    f"When {agent_member.name} replies to the daily OKR collection for {report_day.isoformat()}, "
                    "distill the reply into a concise final daily report (max 200 characters), call "
                    f'upsert_member_daily_report(report_date="{report_day.isoformat()}", member_type="agent", '
                    f'member_id="{agent_member.id}", content="<final report>"), '
                    "then send a short acknowledgement that their daily report has been recorded."
                ),
            )

    return {
        "okr_agent_id": str(okr_agent.id),
        "human_targets": len(rel_rows),
        "agent_targets": len(tracked_agents),
        "sent_humans": sent_humans,
        "sent_agents": sent_agents,
        "total_targets": len(rel_rows) + len(tracked_agents),
        "report_date": report_day.isoformat(),
    }

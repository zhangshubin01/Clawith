"""Experience library — AI consumption side (PRD v2 P0-4, hybrid pull).

Nothing heavy sits in the agent's context: only a one-line hint. The agent then
pulls on demand:
    search_experience(keyword) → lightweight candidates (title + applicability)
    read_experience(entry_id)  → full text, records a `read`

Adoption is recorded separately: when the agent's final output cites an entry
with a [[exp:<uuid>]] marker, `record_experience_citations` logs a `cited` row.
Read != used — the kill-switch metric counts `cited` only.

All reads honor P0-6 visibility and never surface legacy_plaza imports.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import and_, exists, or_, select

from app.database import async_session
from app.models.agent import Agent
from app.models.experience import ExperienceEntry
from app.models.experience_reference import ExperienceReference
from app.models.org import OrgMember

# Agents echo this marker in their final answer to cite an entry they actually used.
CITATION_RE = re.compile(r"\[\[exp:([0-9a-fA-F-]{36})\]\]")

_HINT = (
    "\n## Team Experience Library\n"
    "Your team keeps a private, human-curated library of hard-won internal experience "
    "(internal-system gotchas, private-deployment config, hidden process rules) that public web "
    "search cannot surface. When your current work touches internal systems, internal processes, "
    "or a private/self-hosted environment, FIRST call `search_experience` with a few keywords. "
    "If a candidate's applicability matches your situation, call `read_experience` to read it in full "
    "and follow it. When an entry actually informs your answer, cite it by appending its marker "
    "`[[exp:<entry_id>]]` (the id is given in the search/read results) — this is how reuse is tracked. "
    "If nothing matches, ignore this and do not invent experiences."
)

_MAX_CANDIDATES = 8


async def _resolve_agent(db, agent_id: uuid.UUID) -> Agent | None:
    return (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()


async def _agent_department_ids(db, agent: Agent) -> set[uuid.UUID]:
    """Departments an agent belongs to = the department(s) of its creator.

    Agents have no first-class department; they inherit it from the human who
    created them (agent.creator_id → OrgMember.user_id → OrgMember.department_id).
    """
    if not agent.creator_id:
        return set()
    rows = await db.execute(
        select(OrgMember.department_id).where(
            OrgMember.user_id == agent.creator_id,
            OrgMember.tenant_id == agent.tenant_id,
            OrgMember.department_id.isnot(None),
        )
    )
    return {r[0] for r in rows.all() if r[0]}


def _visibility_condition(dept_ids: set[uuid.UUID]):
    """P0-6 filter for an agent consumer: company always; department if matched.

    user-scoped entries are for human viewing in the UI and are never surfaced to
    agents. If the org hierarchy is empty, dept_ids is empty and only company shows
    (the degrade rule holds naturally).
    """
    conds = [ExperienceEntry.visibility_scope == "company"]
    if dept_ids:
        conds.append(
            and_(
                ExperienceEntry.visibility_scope == "department",
                ExperienceEntry.visibility_scope_id.in_(dept_ids),
            )
        )
    return or_(*conds)


def _freshness_marker(entry: ExperienceEntry) -> str:
    # P1-2: stale entries are downweighted; flag them so the agent trusts them less.
    if not entry.last_reviewed_at:
        return "⚠️ 未复核"
    age = datetime.now(timezone.utc) - entry.last_reviewed_at
    return "⚠️ 复核超期" if age > timedelta(days=90) else "✅"


async def build_experience_hint(agent_id: uuid.UUID) -> str:
    """Return the always-on hint, or "" when the tenant has no published entries.

    Cold-start / empty library → no hint, so we never nudge the agent to search
    a library that has nothing in it.
    """
    try:
        async with async_session() as db:
            agent = await _resolve_agent(db, agent_id)
            if not agent or agent.is_system:
                return ""
            has_any = (
                await db.execute(
                    select(
                        exists().where(
                            and_(
                                ExperienceEntry.tenant_id == agent.tenant_id,
                                ExperienceEntry.status == "published",
                                ExperienceEntry.origin != "legacy_plaza",
                            )
                        )
                    )
                )
            ).scalar()
            return _HINT if has_any else ""
    except Exception as e:
        logger.warning(f"build_experience_hint failed for {agent_id}: {e}")
        return ""


async def search_experience(agent_id: uuid.UUID, arguments: dict) -> str:
    """Keyword search over the visible, published library. Returns lightweight candidates."""
    keyword = (arguments.get("keyword") or arguments.get("query") or "").strip()
    if not keyword:
        return "Provide a `keyword` to search the experience library."
    try:
        async with async_session() as db:
            agent = await _resolve_agent(db, agent_id)
            if not agent or agent.is_system:
                return "This agent cannot access the experience library."
            dept_ids = await _agent_department_ids(db, agent)

            like = f"%{keyword}%"
            q = (
                select(ExperienceEntry)
                .where(
                    ExperienceEntry.tenant_id == agent.tenant_id,
                    ExperienceEntry.status == "published",
                    ExperienceEntry.origin != "legacy_plaza",
                    _visibility_condition(dept_ids),
                    or_(
                        ExperienceEntry.title.ilike(like),
                        ExperienceEntry.applicability.ilike(like),
                        ExperienceEntry.scenario.ilike(like),
                    ),
                )
                .order_by(ExperienceEntry.last_reviewed_at.desc())
                .limit(_MAX_CANDIDATES)
            )
            entries = (await db.execute(q)).scalars().all()

            # Tag matches aren't expressible in SQL portably; fold them in as a fallback.
            if len(entries) < _MAX_CANDIDATES:
                seen = {e.id for e in entries}
                tag_q = (
                    select(ExperienceEntry)
                    .where(
                        ExperienceEntry.tenant_id == agent.tenant_id,
                        ExperienceEntry.status == "published",
                        ExperienceEntry.origin != "legacy_plaza",
                        _visibility_condition(dept_ids),
                    )
                    .order_by(ExperienceEntry.last_reviewed_at.desc())
                    .limit(50)
                )
                for e in (await db.execute(tag_q)).scalars().all():
                    if e.id in seen:
                        continue
                    if any(keyword.lower() in str(t).lower() for t in (e.tags or [])):
                        entries.append(e)
                        if len(entries) >= _MAX_CANDIDATES:
                            break

            if not entries:
                return f"No experience entries match “{keyword}”. Proceed without internal experience."

            lines = [
                f"Found {len(entries)} candidate experience entr(y/ies) for “{keyword}”. "
                "Read the full entry only if its applicability matches your situation:\n"
            ]
            for e in entries:
                applic = (e.applicability or "").strip().replace("\n", " ")
                if len(applic) > 160:
                    applic = applic[:160] + "…"
                lines.append(
                    f"- {_freshness_marker(e)} **{e.title or '(untitled)'}** "
                    f"[[exp:{e.id}]]\n  适用条件/失效信号: {applic}"
                )
            lines.append("\nTo read one: call `read_experience` with its entry id.")
            return "\n".join(lines)
    except Exception as e:
        logger.warning(f"search_experience failed for {agent_id}: {e}")
        return f"Experience search failed: {str(e)[:160]}"


async def read_experience(agent_id: uuid.UUID, arguments: dict) -> str:
    """Return the full four-part entry and record a `read` reference."""
    raw_id = str(arguments.get("entry_id") or "").strip()
    # Tolerate the agent pasting the full "[[exp:<uuid>]]" citation marker.
    m = re.search(r"[0-9a-fA-F-]{36}", raw_id)
    try:
        entry_id = uuid.UUID(m.group(0) if m else raw_id)
    except (ValueError, AttributeError):
        return "Provide a valid `entry_id` (from search_experience results)."
    try:
        async with async_session() as db:
            agent = await _resolve_agent(db, agent_id)
            if not agent or agent.is_system:
                return "This agent cannot access the experience library."
            dept_ids = await _agent_department_ids(db, agent)
            entry = (
                await db.execute(
                    select(ExperienceEntry).where(
                        ExperienceEntry.id == entry_id,
                        ExperienceEntry.tenant_id == agent.tenant_id,
                        ExperienceEntry.status == "published",
                        ExperienceEntry.origin != "legacy_plaza",
                        _visibility_condition(dept_ids),
                    )
                )
            ).scalar_one_or_none()
            if not entry:
                return "Experience entry not found or not visible to you."

            db.add(
                ExperienceReference(
                    entry_id=entry.id,
                    kind="read",
                    tenant_id=agent.tenant_id,
                    agent_id=agent.id,
                )
            )
            await db.commit()

            tags = ", ".join(entry.tags or []) or "—"
            return (
                f"📚 Experience [[exp:{entry.id}]] — {entry.title}\n"
                f"标签: {tags} · 复核: {_freshness_marker(entry)}\n\n"
                f"## 场景\n{entry.scenario}\n\n"
                f"## 遇到的问题\n{entry.problem}\n\n"
                f"## 解决方式\n{entry.solution}\n\n"
                f"## 适用条件与失效信号\n{entry.applicability}\n\n"
                f"If this informs your answer, cite it with [[exp:{entry.id}]]. "
                "If your situation no longer matches the applicability above, do not apply it."
            )
    except Exception as e:
        logger.warning(f"read_experience failed for {agent_id}/{raw_id}: {e}")
        return f"Failed to read experience: {str(e)[:160]}"


async def record_experience_citations(
    text: str,
    agent_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
) -> int:
    """Scan a final agent output for [[exp:<uuid>]] markers and log `cited` references.

    Best-effort: only records citations for entries that are published and visible
    to the agent (guards against hallucinated / stale ids). Deduplicated per entry.
    Returns the number of citations recorded.
    """
    if not text:
        return 0
    ids: set[uuid.UUID] = set()
    for m in CITATION_RE.findall(text):
        try:
            ids.add(uuid.UUID(m))
        except ValueError:
            continue
    if not ids:
        return 0
    try:
        async with async_session() as db:
            agent = await _resolve_agent(db, agent_id)
            if not agent:
                return 0
            dept_ids = await _agent_department_ids(db, agent)
            valid = (
                await db.execute(
                    select(ExperienceEntry.id).where(
                        ExperienceEntry.id.in_(ids),
                        ExperienceEntry.tenant_id == agent.tenant_id,
                        ExperienceEntry.status == "published",
                        ExperienceEntry.origin != "legacy_plaza",
                        _visibility_condition(dept_ids),
                    )
                )
            ).scalars().all()
            for eid in valid:
                db.add(
                    ExperienceReference(
                        entry_id=eid,
                        kind="cited",
                        tenant_id=agent.tenant_id,
                        agent_id=agent.id,
                        session_id=session_id,
                        message_id=message_id,
                    )
                )
            if valid:
                await db.commit()
            return len(valid)
    except Exception as e:
        logger.warning(f"record_experience_citations failed for {agent_id}: {e}")
        return 0

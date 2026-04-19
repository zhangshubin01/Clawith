"""OKR reporting services built on top of member daily reports.

This module implements the simplified reporting chain:

  member daily report -> company daily report -> company weekly report
  -> company monthly report

The implementation intentionally keeps summarization lightweight:
  - member reports are capped at 200 chars at write time
  - company reports use deterministic section-building
  - bucketed aggregation is used when source volume is large
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from app.database import async_session
from app.models.agent import Agent
from app.models.okr import CompanyReport, MemberDailyReport
from app.models.user import User


MEMBER_DAILY_CHAR_LIMIT = 200
BUCKET_SIZE = 20

RISK_KEYWORDS = (
    "risk", "block", "blocked", "issue", "delay", "delayed",
    "problem", "pending", "stuck", "dependency",
    "风险", "阻塞", "问题", "延期", "卡住", "依赖",
)


@dataclass
class CompanyMember:
    """Resolved member metadata used by reporting and the Reports UI."""

    member_type: str
    member_id: uuid.UUID
    display_name: str
    avatar_url: str | None
    group_label: str


def _truncate_report_content(content: str) -> str:
    """Normalize member report content and enforce the character cap."""
    compact = " ".join((content or "").strip().split())
    if len(compact) <= MEMBER_DAILY_CHAR_LIMIT:
        return compact
    return compact[: MEMBER_DAILY_CHAR_LIMIT - 1].rstrip() + "…"


def _contains_risk(text: str) -> bool:
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in RISK_KEYWORDS)


def _period_label(report_type: str, period_start: date, period_end: date) -> str:
    """Build a compact display label for the report period."""
    if report_type == "daily":
        return period_start.isoformat()
    if report_type == "weekly":
        iso_year, iso_week, _ = period_start.isocalendar()
        return f"{iso_year} W{iso_week:02d}"
    return period_start.strftime("%Y-%m")


def _monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _month_end(day: date) -> date:
    if day.month == 12:
        return day.replace(month=12, day=31)
    return day.replace(month=day.month + 1, day=1) - timedelta(days=1)


async def list_company_members(tenant_id: uuid.UUID) -> list[CompanyMember]:
    """Return active human members plus active non-system agents in the tenant."""
    async with async_session() as db:
        users_result = await db.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.is_active == True,  # noqa: E712
            )
        )
        agents_result = await db.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.is_system == False,  # noqa: E712
                Agent.status.notin_(["stopped", "error"]),
            )
        )

        members: list[CompanyMember] = []
        for user in users_result.scalars().all():
            members.append(
                CompanyMember(
                    member_type="user",
                    member_id=user.id,
                    display_name=user.display_name,
                    avatar_url=user.avatar_url,
                    group_label=user.title or "Members",
                )
            )
        for agent in agents_result.scalars().all():
            members.append(
                CompanyMember(
                    member_type="agent",
                    member_id=agent.id,
                    display_name=agent.name,
                    avatar_url=agent.avatar_url,
                    group_label="Digital Employees",
                )
            )
        members.sort(key=lambda item: (item.group_label, item.display_name.lower()))
        return members


async def upsert_member_daily_report(
    tenant_id: uuid.UUID,
    member_type: str,
    member_id: uuid.UUID,
    report_date: date,
    content: str,
    *,
    source: str = "okr_agent_assisted",
    mark_late_if_past: bool = True,
) -> MemberDailyReport:
    """Create or update a member daily report and mark related company reports dirty."""
    normalized = _truncate_report_content(content)
    today = date.today()
    status = "late" if mark_late_if_past and report_date < today else "submitted"

    async with async_session() as db:
        result = await db.execute(
            select(MemberDailyReport).where(
                MemberDailyReport.tenant_id == tenant_id,
                MemberDailyReport.member_type == member_type,
                MemberDailyReport.member_id == member_id,
                MemberDailyReport.report_date == report_date,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.content = normalized
            existing.status = "revised" if existing.content != normalized else existing.status
            existing.source = source
            existing.updated_at = datetime.now(timezone.utc)
            report = existing
        else:
            report = MemberDailyReport(
                tenant_id=tenant_id,
                member_type=member_type,
                member_id=member_id,
                report_date=report_date,
                content=normalized,
                status=status,
                source=source,
            )
            db.add(report)

        await _mark_dependent_company_reports_for_refresh(db, tenant_id, report_date)
        await db.commit()
        await db.refresh(report)
        return report


async def list_member_daily_reports_for_date(
    tenant_id: uuid.UUID,
    report_date: date,
) -> list[dict]:
    """Return all tenant members with report status for a specific date."""
    members = await list_company_members(tenant_id)
    async with async_session() as db:
        result = await db.execute(
            select(MemberDailyReport).where(
                MemberDailyReport.tenant_id == tenant_id,
                MemberDailyReport.report_date == report_date,
            )
        )
        reports = {
            (row.member_type, row.member_id): row
            for row in result.scalars().all()
        }

    items: list[dict] = []
    for member in members:
        report = reports.get((member.member_type, member.member_id))
        items.append({
            "member_type": member.member_type,
            "member_id": str(member.member_id),
            "display_name": member.display_name,
            "avatar_url": member.avatar_url,
            "group_label": member.group_label,
            "status": report.status if report else "missing",
            "content": report.content if report else "",
            "submitted_at": report.submitted_at.isoformat() if report and report.submitted_at else None,
            "updated_at": report.updated_at.isoformat() if report and report.updated_at else None,
        })
    return items


def _bucket_items(items: list[dict], bucket_size: int = BUCKET_SIZE) -> list[list[dict]]:
    """Split items into deterministic fixed-size buckets."""
    return [items[idx: idx + bucket_size] for idx in range(0, len(items), bucket_size)]


def _summarize_member_bucket(bucket: list[dict], label: str) -> tuple[list[str], list[str]]:
    """Produce lightweight bucket-level progress and risk bullets."""
    updates: list[str] = []
    risks: list[str] = []

    for item in bucket:
        text = item["content"].strip()
        if not text:
            continue
        display_name = item["display_name"]
        sentence = text.replace("\n", " ").strip()
        if _contains_risk(sentence):
            risks.append(f"{display_name}: {sentence}")
        else:
            updates.append(f"{display_name}: {sentence}")

    update_lines = updates[:3]
    risk_lines = risks[:2]
    if update_lines:
        update_lines = [f"{label}: " + " | ".join(update_lines)]
    if risk_lines:
        risk_lines = [f"{label}: " + " | ".join(risk_lines)]
    return update_lines, risk_lines


def _build_company_daily_content(
    period_day: date,
    submitted_count: int,
    missing_members: list[dict],
    submitted_items: list[dict],
) -> str:
    """Build a concise company daily report from member daily reports."""
    lines = [
        f"# Company Daily Report",
        f"Date: {period_day.isoformat()}",
        "",
        "## Submission Summary",
        f"- Submitted: {submitted_count}",
        f"- Missing: {len(missing_members)}",
        "",
    ]

    updates: list[str] = []
    risks: list[str] = []
    buckets = _bucket_items(submitted_items)
    for idx, bucket in enumerate(buckets, start=1):
        bucket_updates, bucket_risks = _summarize_member_bucket(bucket, f"Bucket {idx}")
        updates.extend(bucket_updates)
        risks.extend(bucket_risks)

    lines.append("## Key Updates")
    if updates:
        lines.extend(f"- {line}" for line in updates[:8])
    else:
        lines.append("- No major progress updates were submitted.")
    lines.append("")

    lines.append("## Key Risks")
    if risks:
        lines.extend(f"- {line}" for line in risks[:6])
    else:
        lines.append("- No major risks were highlighted.")
    lines.append("")

    lines.append("## Follow-up")
    if missing_members:
        preview = ", ".join(item["display_name"] for item in missing_members[:10])
        suffix = " ..." if len(missing_members) > 10 else ""
        lines.append(f"- Missing reports: {preview}{suffix}")
    else:
        lines.append("- All members submitted their reports.")

    return "\n".join(lines)


def _extract_section_lines(content: str, section: str) -> list[str]:
    """Extract bullet lines from a markdown section title."""
    lines = content.splitlines()
    in_section = False
    collected: list[str] = []
    for line in lines:
        if line.startswith("## "):
            in_section = line.strip() == f"## {section}"
            continue
        if in_section and line.startswith("- "):
            collected.append(line[2:].strip())
    return collected


def _build_company_rollup_content(
    title: str,
    period_start: date,
    period_end: date,
    source_reports: list[CompanyReport],
    *,
    missing_count: int,
    submitted_count: int,
) -> str:
    """Build a weekly or monthly report from lower-level company reports."""
    lines = [
        f"# {title}",
        f"Period: {period_start.isoformat()} to {period_end.isoformat()}",
        "",
        "## Coverage",
        f"- Submitted: {submitted_count}",
        f"- Missing: {missing_count}",
        "",
    ]

    aggregated_updates: list[str] = []
    aggregated_risks: list[str] = []
    aggregated_followups: list[str] = []

    for report in source_reports:
        aggregated_updates.extend(_extract_section_lines(report.content, "Key Updates"))
        aggregated_risks.extend(_extract_section_lines(report.content, "Key Risks"))
        aggregated_followups.extend(_extract_section_lines(report.content, "Follow-up"))

    lines.append("## Key Updates")
    if aggregated_updates:
        lines.extend(f"- {item}" for item in aggregated_updates[:10])
    else:
        lines.append("- No major updates were recorded in this period.")
    lines.append("")

    lines.append("## Key Risks")
    if aggregated_risks:
        lines.extend(f"- {item}" for item in aggregated_risks[:8])
    else:
        lines.append("- No sustained risks were identified.")
    lines.append("")

    lines.append("## Follow-up")
    if aggregated_followups:
        lines.extend(f"- {item}" for item in aggregated_followups[:6])
    else:
        lines.append("- No follow-up items were carried over.")

    return "\n".join(lines)


async def _upsert_company_report(
    tenant_id: uuid.UUID,
    report_type: str,
    period_start: date,
    period_end: date,
    *,
    content: str,
    submitted_count: int,
    missing_count: int,
    needs_refresh: bool = False,
) -> CompanyReport:
    """Insert or update a company report for the same period."""
    async with async_session() as db:
        result = await db.execute(
            select(CompanyReport).where(
                CompanyReport.tenant_id == tenant_id,
                CompanyReport.report_type == report_type,
                CompanyReport.period_start == period_start,
                CompanyReport.period_end == period_end,
            )
        )
        existing = result.scalar_one_or_none()
        label = _period_label(report_type, period_start, period_end)
        if existing:
            existing.content = content
            existing.period_label = label
            existing.submitted_count = submitted_count
            existing.missing_count = missing_count
            existing.needs_refresh = needs_refresh
            existing.updated_at = datetime.now(timezone.utc)
            report = existing
        else:
            report = CompanyReport(
                tenant_id=tenant_id,
                report_type=report_type,
                period_start=period_start,
                period_end=period_end,
                period_label=label,
                content=content,
                submitted_count=submitted_count,
                missing_count=missing_count,
                needs_refresh=needs_refresh,
            )
            db.add(report)
        await db.commit()
        await db.refresh(report)
        return report


async def generate_company_daily_report(tenant_id: uuid.UUID, period_day: date) -> CompanyReport:
    """Generate the company daily report for a specific day."""
    members = await list_company_members(tenant_id)
    async with async_session() as db:
        result = await db.execute(
            select(MemberDailyReport).where(
                MemberDailyReport.tenant_id == tenant_id,
                MemberDailyReport.report_date == period_day,
            )
        )
        rows = result.scalars().all()

    submitted_lookup = {(row.member_type, row.member_id): row for row in rows}
    submitted_items: list[dict] = []
    missing_items: list[dict] = []
    for member in members:
        row = submitted_lookup.get((member.member_type, member.member_id))
        member_payload = {
            "display_name": member.display_name,
            "content": row.content if row else "",
        }
        if row:
            submitted_items.append(member_payload)
        else:
            missing_items.append({"display_name": member.display_name})

    content = _build_company_daily_content(
        period_day,
        len(submitted_items),
        missing_items,
        submitted_items,
    )
    return await _upsert_company_report(
        tenant_id,
        "daily",
        period_day,
        period_day,
        content=content,
        submitted_count=len(submitted_items),
        missing_count=len(missing_items),
        needs_refresh=False,
    )


async def generate_company_weekly_report(tenant_id: uuid.UUID, week_start: date) -> CompanyReport:
    """Generate the company weekly report for the ISO week starting at week_start."""
    week_end = week_start + timedelta(days=6)
    async with async_session() as db:
        result = await db.execute(
            select(CompanyReport).where(
                CompanyReport.tenant_id == tenant_id,
                CompanyReport.report_type == "daily",
                CompanyReport.period_start >= week_start,
                CompanyReport.period_start <= week_end,
            ).order_by(CompanyReport.period_start.asc())
        )
        source_reports = result.scalars().all()

    submitted_count = max((report.submitted_count for report in source_reports), default=0)
    missing_count = max((report.missing_count for report in source_reports), default=0)
    content = _build_company_rollup_content(
        "Company Weekly Report",
        week_start,
        week_end,
        source_reports,
        missing_count=missing_count,
        submitted_count=submitted_count,
    )
    return await _upsert_company_report(
        tenant_id,
        "weekly",
        week_start,
        week_end,
        content=content,
        submitted_count=submitted_count,
        missing_count=missing_count,
        needs_refresh=False,
    )


async def generate_company_monthly_report(tenant_id: uuid.UUID, month_anchor: date) -> CompanyReport:
    """Generate the company monthly report for the month containing month_anchor."""
    period_start = _month_start(month_anchor)
    period_end = _month_end(month_anchor)
    async with async_session() as db:
        result = await db.execute(
            select(CompanyReport).where(
                CompanyReport.tenant_id == tenant_id,
                CompanyReport.report_type == "weekly",
                CompanyReport.period_start >= period_start,
                CompanyReport.period_start <= period_end,
            ).order_by(CompanyReport.period_start.asc())
        )
        source_reports = result.scalars().all()

    submitted_count = max((report.submitted_count for report in source_reports), default=0)
    missing_count = max((report.missing_count for report in source_reports), default=0)
    content = _build_company_rollup_content(
        "Company Monthly Report",
        period_start,
        period_end,
        source_reports,
        missing_count=missing_count,
        submitted_count=submitted_count,
    )
    return await _upsert_company_report(
        tenant_id,
        "monthly",
        period_start,
        period_end,
        content=content,
        submitted_count=submitted_count,
        missing_count=missing_count,
        needs_refresh=False,
    )


async def list_company_reports(
    tenant_id: uuid.UUID,
    report_type: str | None = None,
    limit: int = 50,
) -> list[CompanyReport]:
    """List company reports newest first."""
    async with async_session() as db:
        query = (
            select(CompanyReport)
            .where(CompanyReport.tenant_id == tenant_id)
            .order_by(CompanyReport.period_start.desc(), CompanyReport.updated_at.desc())
            .limit(limit)
        )
        if report_type:
            query = query.where(CompanyReport.report_type == report_type)
        result = await db.execute(query)
        return list(result.scalars().all())


async def _mark_dependent_company_reports_for_refresh(db, tenant_id: uuid.UUID, report_day: date) -> None:
    """Mark the affected company reports as stale after a member report change."""
    week_start = _monday_of(report_day)
    week_end = week_start + timedelta(days=6)
    month_start = _month_start(report_day)
    month_end = _month_end(report_day)

    result = await db.execute(
        select(CompanyReport).where(
            CompanyReport.tenant_id == tenant_id,
            or_(
                and_(
                    CompanyReport.report_type == "daily",
                    CompanyReport.period_start == report_day,
                ),
                and_(
                    CompanyReport.report_type == "weekly",
                    CompanyReport.period_start == week_start,
                    CompanyReport.period_end == week_end,
                ),
                and_(
                    CompanyReport.report_type == "monthly",
                    CompanyReport.period_start == month_start,
                    CompanyReport.period_end == month_end,
                ),
            ),
        )
    )
    for report in result.scalars().all():
        report.needs_refresh = True
        report.updated_at = datetime.now(timezone.utc)

